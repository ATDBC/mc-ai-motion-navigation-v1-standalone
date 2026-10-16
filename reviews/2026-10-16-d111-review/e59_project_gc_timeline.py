"""E59：D111 的 GC 时间线实现（`_GCPauseTimeline` + `_attribute_solve_gc_pauses`）在真实 GC 下是否成立。

D111 在 Windows 上用故障注入和 5 次“真实后台 GC”机制检查验证了实现。这里在 Linux 上用项目的类和函数
直接做一次真实负载复核，不复制它们的逻辑：
  - 常驻约 30 MB 对象，后台线程在随机时刻 gc.collect()（其他线程触发的 GC，结束回调可能晚于求解返回）；
  - 进程里先登记一个第三方 gc 回调，检查项目只移除自己的回调；
  - 用项目 `_measure_gap_solve` 求解 R4 的 12 种入口 200 次，组装与 B10 相同字段的 trial；
  - 全部结束后调用项目 `_attribute_solve_gc_pauses`。
检查：时间线完整（无丢弃、未配对、测量错误）；旧的 GC 次数判定与区间判定是否一致；与第 2 代 GC
重叠的样本扣除停顿后，是否回到无 GC 样本的分布；第三方回调是否保留。
另外用项目 `_B10EvidenceBuffer` 比较 D110／D111 两种存储方式各自新增多少受 GC 跟踪的对象。
运行：PYTHONPATH=<检出> python e59_project_gc_timeline.py [求解次数]
"""
import gc, json, math, pathlib, random, statistics, subprocess, sys, tempfile, threading, time

from scripts import b10_gap_solver_runtime as probe
from mc2p.motion_nav.motion_solver import SolveStatus
from tests.motion_nav.test_b10_gap_solver import B10GapSolverTests, fixture

DIRECTIONS = ((0, 1), (1, 0), (0, -1), (-1, 0))


def cases():
    helper = B10GapSolverTests()
    built = []
    for direction in DIRECTIONS:
        for speed in (0.75, 1.5, 2.5):
            anchor, world, target, _ = fixture(direction)
            built.append((helper.moving_anchor(anchor, direction, speed), world, helper.request(target, direction)))
    return built


def nearest_rank(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def retained_heap(target_bytes=30_000_000):
    rows, size, index = [], 0, 0
    while size < target_bytes:
        row = {"i": index, "d": {"client_tick": index, "t": {f"s{k}": k * 1.5 for k in range(12)}}}
        size += len(json.dumps(row))
        rows.append(row)
        index += 1
    return rows


def tracked_objects_for_buffer(rows):
    with tempfile.TemporaryDirectory(prefix="e59-") as folder:
        gc.collect()
        before = len(gc.get_objects())
        buffer = probe._B10EvidenceBuffer(pathlib.Path(folder))
        for row in rows:
            assert buffer.append("diagnostics.jsonl", row)
        gc.collect()
        added = len(gc.get_objects()) - before
        stored = type(buffer._records["diagnostics.jsonl"][0]).__name__
        del buffer
    return added, stored


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    solves = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    print(f"检出 {head}")
    sample = [{"episode_id": "fabric-21002-0", "observation_sequence_id": i,
               "diagnostics": {"client_tick": i, "observation_pipeline": {"block_read_ns": i, "json_build_ns": 2 * i}}}
              for i in range(5401)]
    added, stored = tracked_objects_for_buffer(sample)
    print(f"  _B10EvidenceBuffer 追加 5401 行诊断：内部存储类型 {stored}，新增受跟踪对象 {added}")
    if not hasattr(probe, "_GCPauseTimeline"):
        print("  （本检出没有 _GCPauseTimeline，GC 时间线部分略过）")
        return
    rng = random.Random(111)
    built = cases()
    keep = retained_heap()
    third_party_calls = []
    third_party = lambda phase, info: third_party_calls.append(phase)
    gc.callbacks.append(third_party)
    for anchor, world, request in built[:3]:
        probe._measure_gap_solve(anchor, world, request)
    stop = threading.Event()

    def collector():
        while not stop.wait(rng.uniform(.15, .45)):
            gc.collect()

    trials = []
    with probe._GCPauseTimeline() as timeline:
        thread = threading.Thread(target=collector, daemon=True)
        thread.start()
        for index in range(solves):
            anchor, world, request = built[index % len(built)]
            solved, measured = probe._measure_gap_solve(anchor, world, request)
            assert solved.status is SolveStatus.SOLVED
            trials.append(dict(name=f"t{index}", **measured))
            time.sleep(rng.uniform(.01, .04))
        stop.set()
        thread.join()
        time.sleep(.1)
    still_registered = third_party in gc.callbacks
    gc.callbacks.remove(third_party)
    result = probe._attribute_solve_gc_pauses(trials, timeline)
    line = result["gc_pause_timeline"]
    print(f"  时间线：事件 {line['event_count']}，区间 {len(line['intervals'])}，complete={line['complete']}，"
          f"丢弃 {line['dropped_events']}，未配对 {line['unpaired_events']}，测量错误 {line['measurement_error_count']}")
    print(f"  第三方回调在项目回调移除后仍登记：{still_registered}；期间被调用 {len(third_party_calls)} 次")
    old = [t["solve_gc_overlap"] for t in trials]
    new = [t["solve_gc_pause_overlap_ns"] > 0 for t in trials]
    print(f"  旧判定（完成次数变化）与区间判定不一致：{sum(a != b for a, b in zip(old, new))} 次（共 {len(trials)}）")
    clean = [t["solve_time_ns"] / 1e6 for t in trials if t["solve_gc_pause_overlap_ns"] == 0]
    gen2 = [t for t in trials if t["solve_gc_pause_overlap_counts"]["gen2"]]
    print(f"  无 GC 重叠样本 {len(clean)}：墙钟 P50 {statistics.median(clean):.2f}  P95 {nearest_rank(clean, .95):.2f}  "
          f"最大 {max(clean):.2f} ms")
    if gen2:
        walls = [t["solve_time_ns"] / 1e6 for t in gen2]
        corrected = [t["solve_wall_minus_gc_pause_ns"] / 1e6 for t in gen2]
        print(f"  与第 2 代 GC 重叠样本 {len(gen2)}：墙钟 P50 {statistics.median(walls):.2f}  最大 {max(walls):.2f} ms；"
              f"项目扣除停顿后 P50 {statistics.median(corrected):.2f}  最大 {max(corrected):.2f} ms")
    del keep


if __name__ == "__main__":
    main()
