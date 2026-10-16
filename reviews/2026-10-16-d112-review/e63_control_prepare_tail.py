"""E63：D112 的“控制准备 P99 超过 15 ms”量到的是不是同步探针求解。

项目的 `analyze_d112_delivery_baseline._control_prepare` 取 trace 里每一条 step 记录的
`dispatched_monotonic_ns - decision_observation_received_ns`，不区分阶段。B10 探针的
`_measure_gap_solve` 在两次 `runtime.step()` 之间同步运行（收到观察之后、下一次派发之前），
所以每个探针试次正好给这个量贡献一个约等于“快照 + 求解 + 接纳”的样本。

本脚本只读公开证据：
  1. D111：D112-0 公布的 D111 控制准备（5,399 样本；P95/P99/最大）与 D111 公布的 142 个探针求解
     逐样本墙钟。若“尾部 = 探针求解 + 固定开销”，则控制准备 P99 应等于探针求解中第
     (P99 名次 - 非探针样本数) 小的值加开销，最大值应等于探针求解最大值加同样开销；
  2. D112：2,000 个样本中有 82 个探针试次。用同样的名次算 P95、P99 落在哪一段；
  3. 同一次运行里，只量生产协调器帧的 `proposal + runtime outside backend`（574 帧）作对照。
运行：python e63_control_prepare_tail.py <项目检出>
"""
import json, math, pathlib, sys

ROOT = pathlib.Path(sys.argv[1]) / "evidence/motion_navigation"


def rank(n, q):
    return max(1, math.ceil(n * q))


def main():
    d111 = json.loads((ROOT / "d111-b10-gc-attribution-v1/fabric-b10/summary.json").read_text("utf-8"))
    probe = sorted(sample["solve_time_ns"] / 1e6 for sample in d111["solver"]["samples"])
    # D112-0 published these three D111 numbers in the D112 acceptance document, §3.1.
    n, p95, p99, top = 5399, 3.5100, 29.6744, 42.3297
    k = len(probe)
    print(f"D111：控制准备 {n} 样本，其中探针求解 {k} 个（只占 {k / n:.1%}）")
    for label, q, observed in (("P95", .95, p95), ("P99", .99, p99)):
        r = rank(n, q)
        if r <= n - k:
            print(f"  {label} 名次 {r} ≤ 非探针样本数 {n - k}：落在普通控制帧，公布值 {observed:.4f} ms")
        else:
            value = probe[r - (n - k) - 1]
            print(f"  {label} 名次 {r} 落在探针段第 {r - (n - k)} 个：探针求解 {value:.4f} ms，"
                  f"公布控制准备 {observed:.4f} ms，差 {observed - value:.4f} ms")
    print(f"  最大值：探针求解 {probe[-1]:.4f} ms，公布控制准备 {top:.4f} ms，差 {top - probe[-1]:.4f} ms")
    valid = ROOT / "d112-worker-delivery-v1/fabric-b10/valid-run"
    summary = json.loads((valid / "summary.json").read_text("utf-8"))
    trace = summary["performance"]["trace_control_prepare_ms"]
    coordinator = summary["performance"]["coordinator_proposal_plus_runtime_outside_ms"]
    n, k = trace["samples"], summary["legacy_solver_trial_rows"]
    print(f"D112 正式批次：控制准备 {n} 样本，其中探针试次 {k} 个（{k / n:.1%}）")
    for label, q in (("P95", .95), ("P99", .99)):
        r = rank(n, q)
        where = "普通控制帧" if r <= n - k else f"探针段第 {r - (n - k)}/{k} 个"
        print(f"  {label} 名次 {r}：{where}；公布值 {trace[label.lower()]:.4f} ms")
    print(f"  最大值 {trace['maximum']:.4f} ms；校准用固定 R4 单候选求解 P50 约 24.9 ms")
    print(f"  同一次运行的生产协调器帧（{coordinator['samples']} 帧）proposal + runtime outside backend："
          f"P95/P99/最大 {coordinator['p95']:.4f}/{coordinator['p99']:.4f}/{coordinator['maximum']:.4f} ms")


if __name__ == "__main__":
    main()
