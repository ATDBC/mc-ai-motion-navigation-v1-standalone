"""E51：D110 的 close 诊断修复在真实关闭顺序下是否成立，trace 字节是否不变。

沿用 D109 审查 E45 的夹具：真实 PlayerRuntimeV1 + 项目测试 V1 Backend，后端补上与
FabricBehaviorBackendV1 相同的两条语义（last_diagnostics 返回副本；close() 清空诊断）。
trace 由项目的 `_runtime_trace(..., capture_close_diagnostics=True, async_delivery=...)`
构造，也就是 B10 实际使用的入口。

组别：
  1. 异步（B10 入口），不加时；
  2. 异步，worker 前面先排 200 条记录，每条投影补足到 D110 实测 P95（0.2038 ms，忙等持 GIL），
     让 worker 必然晚于 backend.close() 处理 close_release；
  3. 同步（async_delivery=False）。
每组检查：最后一行诊断的值、trace 与诊断中 close 行的数量、归因错误、Runtime cleanup，
以及 trace.jsonl／diagnostics.jsonl 的 SHA-256（trace 去掉异步 writer 每次不同的
trace_delivery_summary 行；不加填充记录时，与 D109 同步路径逐字节比较）。

另有两个失败注入：
  A. close neutral 之后后端诊断本身为 None：归因应给出带上下文的 ValueError；
  B. 队列满导致 close_release 被拒收：trace 和诊断都不应出现 close 行（同生同灭）。

运行：PYTHONPATH=<检出> python e51_close_diagnostics_fix.py [重复次数]
"""
import collections, dataclasses, hashlib, json, pathlib, subprocess, sys, tempfile, threading, time

from mc2p.runtime import async_trace
from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from scripts.client_time_evidence import export_runtime_time_evidence
from scripts.control_probe_core import append_jsonl
from scripts.probe_fabric_deployment_observation import _runtime_trace
from tests import test_player_runtime as legacy
from tests.test_player_runtime_v1 import Backend


class FabricLikeBackend(Backend):
    def __init__(self, close_step_clears=False):
        super().__init__()
        self._diagnostics = None
        self.close_step_clears = close_step_clears
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
        self._diagnostics = None if self.close_step_clears else {"client_tick": 1000 + self.sequence}
        return result

    def close(self):
        super().close()
        self._diagnostics = None


PROJECTION_P95_NS = 203_800  # d110 fabric-b10/summary.json trace.delivery_summary.projection_timing_ns.p95
_ORIGINAL = async_trace.trace_projection


def emulate_projection(enabled):
    if not enabled:
        async_trace.trace_projection = _ORIGINAL
        return

    def projection(payload):
        started = time.perf_counter_ns()
        projected = _ORIGINAL(payload)
        while time.perf_counter_ns() - started < PROJECTION_P95_NS:
            pass
        return projected
    async_trace.trace_projection = projection


def sha(path):
    """Hash the file, leaving out the async writer's run-dependent delivery summary."""
    if not path.exists():
        return "无文件"
    lines = [line for line in path.read_bytes().splitlines(keepends=True)
             if b'"record_type": "trace_delivery_summary"' not in line]
    return hashlib.sha256(b"".join(lines)).hexdigest()[:12]


def run_once(directory, *, async_delivery, filler=0, backend=None, trace=None):
    backend = backend or FabricLikeBackend()
    trace = trace or _runtime_trace(directory, backend, capture_close_diagnostics=True,
                                    async_delivery=async_delivery)
    runtime = PlayerRuntimeV1(backend, trace, clock_ns=lambda: 10)
    assert runtime.reset(legacy._reset_request()).succeeded
    observation = runtime.observation
    append_jsonl(directory / "diagnostics.jsonl", dict(
        episode_id=observation.episode_id, observation_sequence_id=observation.sequence_id,
        diagnostics=backend.last_diagnostics))
    for index in range(filler):
        trace.write("filler", {"index": index})
    runtime.close()
    rows = [json.loads(line) for line in (directory / "diagnostics.jsonl").read_text("utf-8").splitlines()]
    records = [json.loads(line) for line in (directory / "trace.jsonl").read_text("utf-8").splitlines()]
    (directory / "mc2p-client-time.jsonl").write_text("", encoding="utf-8")
    report = export_runtime_time_evidence(directory)
    return dict(
        last=json.dumps(rows[-1]["diagnostics"]),
        close_counts=(sum(r["record_type"] == "close_release" for r in records),
                      sum(r["observation_sequence_id"] == 1 for r in rows)),
        errors=" | ".join(report.get("errors", [])) or "无",
        cleanup=",".join(f.code.value for f in runtime.cleanup_failures) or "无",
        hashes=(sha(directory / "trace.jsonl"), sha(directory / "diagnostics.jsonl")),
        stats=getattr(trace, "stats", None),
    )


def group(label, repeats, **kwargs):
    counters = collections.defaultdict(collections.Counter)
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="e51-") as folder:
            result = run_once(pathlib.Path(folder), **kwargs)
        for key in ("last", "close_counts", "errors", "cleanup", "hashes"):
            counters[key][str(result[key])] += 1
    print(f"  {label}（{repeats} 次）")
    print(f"    最后一行 diagnostics：{dict(counters['last'])}")
    print(f"    close 行数（trace, 诊断）：{dict(counters['close_counts'])}")
    print(f"    归因错误：{dict(counters['errors'])}")
    print(f"    Runtime cleanup failures：{dict(counters['cleanup'])}")
    print(f"    SHA-256 前 12 位（trace, 诊断）：{dict(counters['hashes'])}")


def injection_null(directory):
    result = run_once(directory, async_delivery=True, backend=FabricLikeBackend(close_step_clears=True))
    print(f"  注入 A：close neutral 后诊断为 None → 最后一行 {result['last']}；归因错误：{result['errors']}")


def injection_drop(directory):
    from scripts.fabric_visibility_scenario import CloseDiagnosticsTrace
    backend = FabricLikeBackend()
    trace = CloseDiagnosticsTrace(directory, backend, async_delivery=False)
    inner = trace.trace
    gate, entered = threading.Event(), threading.Event()
    original = inner.write_projected

    def blocked(kind, payload):
        entered.set()
        gate.wait(10)
        original(kind, payload)
    inner.write_projected = blocked
    trace.trace = BoundedAsyncTraceWriter(inner, capacity=1)
    runtime = PlayerRuntimeV1(backend, trace, clock_ns=lambda: 10)
    assert runtime.reset(legacy._reset_request()).succeeded
    entered.wait(5)                      # worker is now stuck on the reset record
    trace.write("filler", {"index": 0})  # fills the only queue slot
    threading.Timer(.3, gate.set).start()
    runtime.close()                      # close_release is rejected: queue full
    records = [json.loads(line) for line in (directory / "trace.jsonl").read_text("utf-8").splitlines()]
    path = directory / "diagnostics.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()] if path.exists() else []
    stats = trace.stats
    print(f"  注入 B：队列满 → trace 记录类型 {[r['record_type'] for r in records]}；诊断行 {len(rows)}；"
          f"dropped_records={stats.dropped_records}；"
          f"cleanup={[f.code.value for f in runtime.cleanup_failures] or '无'}")


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    print(f"检出 {head}：reset 后 close，经 _runtime_trace(capture_close_diagnostics=True)")
    group("异步（B10 入口），不加时", repeats, async_delivery=True)
    emulate_projection(True)
    group("异步，先排 200 条填充记录，投影补足到实测 P95", max(10, repeats // 5), async_delivery=True, filler=200)
    emulate_projection(False)
    group("同步（async_delivery=False）", repeats, async_delivery=False)
    if "async_delivery" in __import__("inspect").signature(
            __import__("scripts.fabric_visibility_scenario", fromlist=["x"]).CloseDiagnosticsTrace).parameters:
        with tempfile.TemporaryDirectory(prefix="e51-") as folder:
            injection_null(pathlib.Path(folder))
        with tempfile.TemporaryDirectory(prefix="e51-") as folder:
            injection_drop(pathlib.Path(folder))
    else:
        print("  （本检出的 CloseDiagnosticsTrace 没有 async_delivery 参数，失败注入略过）")


if __name__ == "__main__":
    main()
