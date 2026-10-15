"""E55：B10 进程里自己常驻的证据对象让一次完整 GC 多扫描多少对象、多停顿多久。

B10 探针把六类 JSONL 证据缓冲在内存里，运行结束才落盘（scripts/b10_gap_solver_runtime.py
`_B10EvidenceBuffer`，总上限 43 MiB）。`append()` 先 `strict_json_dumps(row)`，再把
`json.loads(encoded)` 得到的 dict／list 树留在内存；`diagnostic()` 另外把原始行留在 `rows` 列表里。
这些容器对象都受循环 GC 跟踪，随运行增长，每次第 2 代 GC 都要全部扫描一遍。字符串和整数不受跟踪。

本实验按 D110 正式 B10 的实际规模填充项目的 `_B10EvidenceBuffer`：
  - diagnostics.jsonl 5253 行，行结构取自 D110 fabric-b10/summary.json 的 last_close_diagnostics；
    探针同时在 `rows` 里保留原始行；
  - b10-gap-solver-trials.jsonl 142 行，每行是 R4 求解结果经 trace_projection 后的结构。
比较三种状态下一次完整 gc.collect() 的停顿和受跟踪对象数：
  0. 只有导入和求解夹具；
  1. 加上按项目方式保留的证据（缓冲里的 dict 树 + rows 原始行）；
  2. 同样的证据，但缓冲只保留已经算好的 JSON 字符串（rows 仍保留原始行）；
  3. 缓冲和 rows 都只保留字符串。
只说明这部分证据本身的量级，不代表 B10 进程的全部堆（世界模型、观察、Fabric 编解码等不在内）。
运行：PYTHONPATH=<检出> python e55_evidence_buffer_gc_cost.py
"""
import copy, gc, json, pathlib, statistics, subprocess, tempfile, time

from scripts.b10_gap_solver_runtime import _B10EvidenceBuffer
from scripts.control_probe_core import strict_json_dumps
from mc2p.motion_nav.motion_solver import solve_one_cell_gap
from mc2p.runtime.trace import trace_projection
from tests.motion_nav.test_b10_gap_solver import B10GapSolverTests, fixture

DIAGNOSTIC_ROWS, TRIAL_ROWS = 5253, 142
SUMMARY = pathlib.Path("evidence/motion_navigation/d110-evidence-lifecycle-v1/fabric-b10/summary.json")


def diagnostic_rows():
    template = json.loads(SUMMARY.read_text("utf-8"))["trace"]["last_close_diagnostics"]
    rows = []
    for index in range(DIAGNOSTIC_ROWS):
        row = copy.deepcopy(template)
        row["observation_sequence_id"] = index
        row["diagnostics"]["client_tick"] = 700 + index
        row["diagnostics"]["world_render_attempts"] = 3 * index
        for key, value in row["diagnostics"]["observation_pipeline"].items():
            if type(value) is int:
                row["diagnostics"]["observation_pipeline"][key] = value + index % 97
        rows.append(row)
    return rows


def trial_rows():
    helper = B10GapSolverTests()
    rows = []
    for index in range(TRIAL_ROWS):
        direction = ((0, 1), (1, 0), (0, -1), (-1, 0))[index % 4]
        anchor, world, target, _ = fixture(direction)
        anchor = helper.moving_anchor(anchor, direction, (0.75, 1.5, 2.5)[index % 3])
        solved = solve_one_cell_gap(anchor, world, helper.request(target, direction))
        proof = trace_projection(solved.proof)
        # About 6 KB per row, close to D110's 977858 bytes / 142 trials.
        rows.append({"name": f"trial-{index}", "solve_time_ns": 1,
                     "candidates_evaluated": solved.candidates_evaluated,
                     **{key: proof[key] for key in (
                         "entry_state", "commands", "tick_inputs", "step_events",
                         "exit_state", "landing", "world_dependencies")}})
    return rows


def pause_ms(repeats=15):
    """Median and minimum of repeated full collections (the minimum is least disturbed)."""
    samples = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        gc.collect()
        samples.append((time.perf_counter_ns() - started) / 1e6)
    return statistics.median(samples), min(samples)


def tracked():
    gc.collect()
    return len(gc.get_objects())


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    diagnostics, trials = diagnostic_rows(), trial_rows()
    print(f"检出 {head}：diagnostics {DIAGNOSTIC_ROWS} 行，trials {TRIAL_ROWS} 行"
          f"（序列化后约 {sum(len(strict_json_dumps(r)) for r in diagnostics) / 1e6:.2f} MB 与 "
          f"{sum(len(strict_json_dumps(r)) for r in trials) / 1e6:.2f} MB）")
    # Keep the generated sources as strings only, so they do not count as retained state.
    diagnostic_text = [strict_json_dumps(r) for r in diagnostics]
    trial_text = [strict_json_dumps(r) for r in trials]
    del diagnostics, trials
    base_objects, (base_pause, base_min) = tracked(), pause_ms()
    print(f"  0 只有导入和求解夹具：受跟踪对象 {base_objects}，完整 GC 中位 {base_pause:.2f}／最小 {base_min:.2f} ms")
    for label, buffer_as_text, rows_as_text in (
            ("1 项目现状（缓冲 dict 树 + rows 原始行）", False, False),
            ("2 缓冲只存字符串，rows 仍存原始行", True, False),
            ("3 缓冲和 rows 都只存字符串", True, True)):
        with tempfile.TemporaryDirectory(prefix="e55-") as folder:
            buffer = _B10EvidenceBuffer(pathlib.Path(folder))
            rows = []
            for text in diagnostic_text:
                row = json.loads(text)
                rows.append(text if rows_as_text else row)
                assert buffer.append("diagnostics.jsonl", row)
            for text in trial_text:
                assert buffer.append("b10-gap-solver-trials.jsonl", json.loads(text))
            if buffer_as_text:  # same bytes, kept as the already-encoded JSON line
                for name, records in buffer._records.items():
                    buffer._records[name] = [strict_json_dumps(record) for record in records]
            objects, (pause, low) = tracked(), pause_ms()
            print(f"  {label}：受跟踪对象 +{objects - base_objects}，完整 GC 中位 {pause:.2f}／最小 {low:.2f} ms"
                  f"（最小值相对 0 增加 {low - base_min:.2f} ms）")
            del buffer, rows


if __name__ == "__main__":
    main()
