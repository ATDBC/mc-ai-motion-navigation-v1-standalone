"""E58：D111 的求解变慢是“求解本身”还是“整台机器／整个进程”——只读项目公开证据。

求解代码与 D110 相同（计时区间内只多记了起止时间戳，见 scripts/b10_gap_solver_runtime.py 的
`_measure_gap_solve`），B10 世界是超平坦世界（level-type minecraft:flat），seed 不改变求解面对的地形。
比较 D109 D8、D110、D111 三次正式 B10 的公开摘要：
  - 求解墙钟分位数；
  - 线程 CPU 时间的平均值：Windows 以 15.625 ms 记账，单个样本只能是 15.625 的整数倍，但起点相位随机时
    量化误差均值为 0，142 个样本的平均值标准误约 15.625/√12/√142 ≈ 0.38 ms（上界取 ±1 个步进时约 0.76 ms）；
  - 同一进程里与求解无关的 trace 投影耗时（worker 线程上的纯 Python 工作）；
  - 运行的控制步数。
运行：python e58_run_to_run_shift.py <项目检出>
"""
import json, math, pathlib, statistics, sys

ROOT = pathlib.Path(sys.argv[1]) / "evidence/motion_navigation"
RUNS = (
    ("D109 D8", "d109-b10-lifecycle-v1/d8-final-b10-post-repair/summary.json"),
    ("D110", "d110-evidence-lifecycle-v1/fabric-b10/summary.json"),
    ("D111", "d111-b10-gc-attribution-v1/fabric-b10/summary.json"),
)
TICK = 15.625


def cpu_mean(solver):
    distribution = solver.get("solve_thread_cpu_ns_distribution")
    if distribution:
        values = [int(k) / 1e6 for k, n in distribution.items() for _ in range(n)]
    elif solver.get("samples"):
        values = [s["solve_thread_cpu_ns"] / 1e6 for s in solver["samples"]]
    else:
        return None
    return statistics.mean(values), len(values)


def main():
    print("求解墙钟（ms）、线程 CPU 平均值、trace 投影耗时（µs）、控制步数")
    for label, path in RUNS:
        data = json.loads((ROOT / path).read_text("utf-8"))
        solver = data.get("solver") or {}
        wall = solver.get("solve_time_ns") or data.get("solve_time")
        if "p95_ns" in (wall or {}):
            p95, p99, top = wall["p95_ns"], wall["p99_ns"], wall["maximum_ns"]
        else:
            p95, p99, top = wall["p95"], wall["p99"], wall["maximum"]
        samples = solver.get("samples") or []
        mean_wall = statistics.mean(s["solve_time_ns"] for s in samples) / 1e6 if samples else None
        cpu = cpu_mean(solver)
        trace = data.get("trace", {}).get("delivery_summary") or data.get("trace_delivery_summary") or {}
        projection = trace.get("projection_timing_ns", {})
        rows = data.get("trace", {}).get("diagnostics_rows")
        line = f"  {label:<8} 墙钟 P95/P99/最大 {p95/1e6:6.2f}/{p99/1e6:6.2f}/{top/1e6:6.2f}"
        if mean_wall is not None:
            line += f"，平均 {mean_wall:5.2f}"
        if cpu:
            mean, count = cpu
            line += f"；CPU 平均 {mean:5.2f}（{count} 个样本，量化标准误约 {TICK / math.sqrt(12) / math.sqrt(count):.2f}）"
        if projection:
            line += (f"；投影 平均 {projection['total'] / projection['count'] / 1e3:5.1f}，"
                     f"P95 {projection['p95'] / 1e3:5.1f}，P99 {projection['p99'] / 1e3:5.1f}")
        if rows:
            line += f"；诊断行 {rows}"
        print(line)


if __name__ == "__main__":
    main()
