"""E44：test_d108_snapshot_materialization 中绝对时间门槛在 Linux 上的稳定性。

项目测试 test_windows_semantic_observation_and_local_borrow_deadlines 调用
scripts.benchmark_d108_snapshot_materialization.run_probe(samples=3)，要求每行 P95 ≤ 8 ms、
P99 ≤ 15 ms、最大 < 30 ms 等绝对门槛；3 个样本时 P95 就是最大值。这里重复调用 R 次，
记录通过率、不通过的行，以及 D108 真正要证明的相对性质：相关范围借用中位不随无关脏 section 数增长
（N=32 与 N=4 的中位比）。只调用项目函数，不改项目文件。
运行：PYTHONPATH=<检出> python e44_snapshot_gate_flakiness.py [重复次数]
"""
import statistics, subprocess, sys

from scripts.benchmark_d108_snapshot_materialization import run_probe


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    passed, failing_rows, ratios, borrow_p50 = 0, {}, [], []
    for _ in range(repeats):
        result = run_probe(samples=3)
        passed += bool(result["passed"])
        for row in result["unrelated_dirty"]:
            ok = (row["deadline_passed"] and row["range_lifecycle_passed"]
                  and row["owner_lock_wait_ms"] < 10.0 and row["python_control_unavailable_ms"] < 30.0)
            if not ok:
                key = f"无关脏 {row['unrelated_dirty_sections']}"
                failing_rows[key] = failing_rows.get(key, 0) + 1
        for row in result["semantic_changes"]:
            if not row["deadline_passed"]:
                key = f"语义变化 {row['sections']}"
                failing_rows[key] = failing_rows.get(key, 0) + 1
        if not result["b10_bounds"]["deadline_passed"]:
            failing_rows["B10 bounds"] = failing_rows.get("B10 bounds", 0) + 1
        dirty = {row["unrelated_dirty_sections"]: row["p50_ms"] for row in result["unrelated_dirty"]}
        ratios.append(dirty[32] / dirty[4])
        borrow_p50.extend(dirty.values())
    print(f"检出 {head}：run_probe(samples=3) 重复 {repeats} 次（Linux）")
    print(f"  绝对门槛通过 {passed}/{repeats}；不通过的行 {failing_rows or '无'}")
    print(f"  相关范围借用中位 {statistics.median(borrow_p50):.2f} ms（最小 {min(borrow_p50):.2f}，最大 {max(borrow_p50):.2f}）；"
          f"Windows 正式结果 P95 约 3.5 ms，门槛 8 ms")
    print(f"  N=32 / N=4 中位比：中位 {statistics.median(ratios):.2f}，范围 {min(ratios):.2f}–{max(ratios):.2f}"
          f"（与无关脏 section 数无关时应接近 1）")


if __name__ == "__main__":
    main()
