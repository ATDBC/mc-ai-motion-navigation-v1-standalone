"""E45：D109 最终 B10 的 time attribution `'NoneType' object is not subscriptable` 从何而来。

B10 的 Runtime trace 是 BoundedAsyncTraceWriter(CloseDiagnosticsTrace(directory, backend))。
CloseDiagnosticsTrace.write_projected() 在异步 worker 线程上处理 close_release 记录时，才去读
`backend.last_diagnostics`。而 PlayerRuntimeV1.close() 的顺序是：
  写 close_release（只入队）→ finally 里 _close_backend() → trace.close()。
FabricBehaviorBackendV1.close() 会把 `_diagnostics` 置为 None。若 worker 晚于 backend.close()
读到它，diagnostics.jsonl 的最后一行就是 `"diagnostics": null`，attribution 读
`row["diagnostics"]["client_tick"]` 时抛 TypeError。

本实验用真实的 PlayerRuntimeV1、BoundedAsyncTraceWriter、CloseDiagnosticsTrace 和
client_time_evidence.export_runtime_time_evidence。后端沿用项目测试里的 V1 Backend，只加上与
FabricBehaviorBackendV1 相同的 `last_diagnostics`（返回副本）和 `close()` 时清空诊断两条语义。
每个 reset／step 的诊断行按 B10 的做法由调用方同步追加；close_release 的诊断行由
CloseDiagnosticsTrace 写。
两者谁先发生取决于线程调度。为贴近实机，另设一组“按实测投影成本”：把 worker 上每条记录的
trace_projection 补足到 D8 最终 B10 trace_delivery_summary 实测的 P95 0.2201 ms（忙等，持有 GIL，
与真实投影同类）；不加时的一组照原样运行。
对照：在调用方线程上、入队前抓取 close_release 的诊断（只用于说明修法，不改项目文件）。
在 a9d51a2（D108，即 D1 所用的生产代码）上运行同一脚本，可复现 D1 的另一种表现：异步 worker 把已投影的
dict 当 typed 对象读，worker 失败，close_release 诊断行缺失，attribution 报 “do not cover”。
运行：PYTHONPATH=<检出> python e45_close_diagnostics_race.py [重复次数]
"""
import collections, dataclasses, json, pathlib, subprocess, sys, tempfile, time

from mc2p.runtime import async_trace
from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from scripts.client_time_evidence import export_runtime_time_evidence
from scripts.control_probe_core import append_jsonl
from scripts.fabric_visibility_scenario import CloseDiagnosticsTrace
from tests import test_player_runtime as legacy
from tests.test_player_runtime_v1 import Backend


class FabricLikeBackend(Backend):
    """Project test backend plus FabricBehaviorBackendV1's diagnostics lifetime."""

    def __init__(self):
        super().__init__()
        self._diagnostics = None
        self.observation_overrides = {"source_backend": "fabric"}

    @property
    def last_diagnostics(self):
        return None if self._diagnostics is None else dict(self._diagnostics)

    def reset(self, request):
        result = super().reset(request)
        self._diagnostics = {"client_tick": 1000}
        return dataclasses.replace(result, observation=dataclasses.replace(
            result.observation, source_backend="fabric"))

    def step(self, action, deadline):
        result = super().step(action, deadline)
        self._diagnostics = {"client_tick": 1000 + self.sequence}
        return result

    def close(self):
        super().close()
        self._diagnostics = None  # same as FabricBehaviorBackendV1.close()


class ProducerSideCloseDiagnostics:
    """Control: capture close_release diagnostics on the caller thread before enqueue."""

    def __init__(self, directory, backend, delegate):
        self.directory, self.backend, self.delegate = directory, backend, delegate

    def write(self, kind, payload):
        self.delegate.write(kind, payload)
        if kind == "close_release":
            observation = payload["backend_result"].observation
            append_jsonl(self.directory / "diagnostics.jsonl", dict(
                episode_id=observation.episode_id,
                observation_sequence_id=observation.sequence_id,
                diagnostics=self.backend.last_diagnostics))

    def close(self):
        return self.delegate.close()


class PlainCloseDiagnostics(CloseDiagnosticsTrace):
    """Control sink: the same file layout without reading the backend on the worker."""

    def write_projected(self, kind, payload):
        self.trace.write_projected(kind, payload)


REAL_PROJECTION_P95_NS = 220_100  # d8-final-b10-post-repair/summary.json projection_timing_ns.p95
_ORIGINAL_PROJECTION = async_trace.trace_projection


def emulate_projection_cost(enabled: bool) -> None:
    if not enabled:
        async_trace.trace_projection = _ORIGINAL_PROJECTION
        return

    def projection(payload):
        started = time.perf_counter_ns()
        projected = _ORIGINAL_PROJECTION(payload)
        while time.perf_counter_ns() - started < REAL_PROJECTION_P95_NS:
            pass
        return projected
    async_trace.trace_projection = projection


def run_once(control: bool):
    with tempfile.TemporaryDirectory(prefix="e45-") as folder:
        directory = pathlib.Path(folder)
        backend = FabricLikeBackend()
        if control:
            trace = ProducerSideCloseDiagnostics(
                directory, backend,
                BoundedAsyncTraceWriter(PlainCloseDiagnostics(directory, backend), capacity=16_384))
        else:
            trace = BoundedAsyncTraceWriter(CloseDiagnosticsTrace(directory, backend), capacity=16_384)
        runtime = PlayerRuntimeV1(backend, trace, clock_ns=lambda: 10)  # same fixed clock as the project tests
        reset = runtime.reset(legacy._reset_request())
        assert reset.succeeded
        observation = runtime.observation
        append_jsonl(directory / "diagnostics.jsonl", dict(
            episode_id=observation.episode_id, observation_sequence_id=observation.sequence_id,
            diagnostics=backend.last_diagnostics))
        runtime.close()
        failures = [failure.code.value for failure in runtime.cleanup_failures]
        rows = [json.loads(line) for line in (directory / "diagnostics.jsonl").read_text("utf-8").splitlines()]
        (directory / "mc2p-client-time.jsonl").write_text("", encoding="utf-8")
        report = export_runtime_time_evidence(directory)
        return rows[-1]["diagnostics"], report.get("errors", []), failures


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    print(f"检出 {head}：PlayerRuntimeV1 reset 后 close（一次 close neutral），每组 {repeats} 次")
    from mc2p.runtime.trace import JsonlTraceWriterV0
    variants = [("项目现状，不加时", False, False), ("项目现状，按实测投影 P95 补足", False, True)]
    if hasattr(JsonlTraceWriterV0, "write_projected"):
        variants.append(("对照（调用方线程入队前抓取），按实测投影 P95 补足", True, True))
    else:
        print("  （本检出没有 D109 的单次投影契约 write_projected，对照组不适用，略过）")
    for label, control, emulate in variants:
        emulate_projection_cost(emulate)
        last_rows, errors, cleanup = collections.Counter(), collections.Counter(), collections.Counter()
        for _ in range(repeats):
            diagnostics, report_errors, failures = run_once(control)
            last_rows[json.dumps(diagnostics)] += 1
            errors[" | ".join(report_errors) or "无"] += 1
            cleanup[",".join(failures) or "无"] += 1
        emulate_projection_cost(False)
        print(f"  {label}")
        print(f"    close_release 诊断行的 diagnostics：{dict(last_rows)}")
        print(f"    export_runtime_time_evidence 错误：{dict(errors)}")
        print(f"    Runtime cleanup failures：{dict(cleanup)}")


if __name__ == "__main__":
    main()
