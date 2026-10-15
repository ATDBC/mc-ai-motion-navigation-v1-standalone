"""E52：D110 的逐样本求解字段能否回答“超 30 ms 的那一次是不是 GC”，以及怎样才能回答。

D110 给每次求解记录了墙钟、`time.thread_time_ns()` 差值和三代 GC 完成次数的变化
（scripts/b10_gap_solver_runtime.py `_measure_gap_solve`）。Windows 上线程 CPU 时间按约
15.625 ms 的时钟中断记账，所以 D110 的 142 个样本只出现 0／15.625／31.25 ms 三种值。

本实验在 Linux 上直接调用项目的 `_measure_gap_solve`，求解 R4 的 12 种带速入口，同时：
  - 常驻约 30 MB 的诊断对象，让一次完整 GC 的停顿达到几十毫秒；
  - 后台线程在随机时刻调用 gc.collect()（模拟进程里其他线程触发的第 2 代 GC，求解线程只能等）；
  - 本脚本另用 gc.callbacks 记录整个运行中每次 GC 的开始和结束（perf_counter_ns），运行结束后再
    统一计算与每次求解重叠的 GC 停顿时长。不能在每次求解结束时立即计算：别的线程触发的 GC 结束后，
    它的 stop 回调要等那个线程重新拿到 GIL 才执行，可能晚于求解线程读取结束时间。
每次求解比较四种归因手段：
  1. 项目字段：墙钟、线程 CPU、GC 次数变化；
  2. 把线程 CPU 按 Windows 的 15.625 ms 记账粒度量化（用两端的绝对线程时间各自取整）；
  3. gc.callbacks 测得的重叠停顿时长；
  4. 墙钟减去重叠停顿。
最后单独测一次完整 GC 在 gc.freeze() 前后的停顿，作为可选缓解手段的量级参考。
绝对毫秒只代表本机。运行：PYTHONPATH=<检出> python e52_solver_gc_attribution.py [求解次数]
"""
import gc, json, math, random, statistics, subprocess, sys, threading, time

from scripts.b10_gap_solver_runtime import _measure_gap_solve
from mc2p.motion_nav.motion_solver import SolveStatus
from tests.motion_nav.test_b10_gap_solver import B10GapSolverTests, fixture

DIRECTIONS = ((0, 1), (1, 0), (0, -1), (-1, 0))
SPEEDS = (0.75, 1.5, 2.5)
WINDOWS_TICK_NS = 15_625_000
pauses, pause_start = [], {}


def on_gc(phase, info):
    key = threading.get_ident()
    if phase == "start":
        pause_start[key] = time.perf_counter_ns()
    elif key in pause_start:
        pauses.append((pause_start.pop(key), time.perf_counter_ns(), info.get("generation")))


def cases():
    helper = B10GapSolverTests()
    built = []
    for direction in DIRECTIONS:
        for speed in SPEEDS:
            anchor, world, target, _ = fixture(direction)
            built.append((helper.moving_anchor(anchor, direction, speed), world,
                          helper.request(target, direction)))
    return built


def retained_heap(target_bytes):
    rows, size, index = [], 0, 0
    while size < target_bytes:
        row = {"episode_id": "fabric-21001-0", "observation_sequence_id": index,
               "diagnostics": {"client_tick": index, "movement_tick": index,
                               "timings": {f"stage_{k}": k * 1.5 for k in range(12)},
                               "flags": [index % 2 == 0, index % 3 == 0]}}
        size += len(json.dumps(row))
        rows.append(row)
        index += 1
    return rows


def background_collector(stop, rng):
    while not stop.wait(rng.uniform(.15, .45)):
        gc.collect()


def overlap_ns(begin, end):
    return sum(max(0, min(end, b) - max(begin, a)) for a, b, _ in pauses)


def quantized(before, after):
    return (after // WINDOWS_TICK_NS - before // WINDOWS_TICK_NS) * WINDOWS_TICK_NS


def nearest_rank(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def full_collection_ms(repeats=5):
    samples = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        gc.collect()
        samples.append((time.perf_counter_ns() - started) / 1e6)
    return statistics.median(samples)


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    solves = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    rng = random.Random(110)
    built = cases()
    keep = retained_heap(30_000_000)
    for anchor, world, request in built[:3]:
        _measure_gap_solve(anchor, world, request)  # warm-up
    gc.callbacks.append(on_gc)
    stop = threading.Event()
    collector = threading.Thread(target=background_collector, args=(stop, rng), daemon=True)
    collector.start()
    rows = []
    for index in range(solves):
        anchor, world, request = built[index % len(built)]
        cpu_before = time.thread_time_ns()
        begin = time.perf_counter_ns()
        solved, measured = _measure_gap_solve(anchor, world, request)
        end = time.perf_counter_ns()
        cpu_after = time.thread_time_ns()
        assert solved.status is SolveStatus.SOLVED and not measured["solve_measurement_errors"]
        rows.append(dict(
            begin=begin, end=end,
            wall=measured["solve_time_ns"] / 1e6,
            cpu=measured["solve_thread_cpu_ns"] / 1e6,
            cpu_windows=quantized(cpu_before, cpu_after) / 1e6,
            counts=measured["solve_gc_collections_delta"],
            overlap=measured["solve_gc_overlap"],
        ))
        time.sleep(rng.uniform(.01, .04))
    stop.set()
    collector.join()
    time.sleep(.1)
    gc.callbacks.remove(on_gc)
    for row in rows:  # attribute after the run, when every stop callback has run
        row["gc"] = overlap_ns(row["begin"], row["end"]) / 1e6

    clean = [r for r in rows if not r["overlap"]]
    hit = [r for r in rows if r["overlap"]]
    gen2_hit = [r for r in hit if r["counts"]["gen2"]]
    print(f"检出 {head}：项目 _measure_gap_solve，R4 12 种入口轮换 {solves} 次；常驻约 30 MB 对象，"
          f"后台线程随机 gc.collect()")
    print(f"  项目字段 solve_gc_overlap=True：{len(hit)} 次，其中 gen2 计数增加 {len(gen2_hit)} 次；"
          f"gc.callbacks 测得有重叠停顿 {sum(r['gc'] > 0 for r in rows)} 次；"
          f"两种判定不一致 {sum((r['gc'] > 0) != r['overlap'] for r in rows)} 次")
    walls = [r["wall"] for r in clean]
    print(f"  无 GC 重叠的样本（{len(clean)}）：墙钟 P50 {statistics.median(walls):.2f}  "
          f"P95 {nearest_rank(walls, .95):.2f}  最大 {max(walls):.2f} ms")
    if gen2_hit:
        print("  与第 2 代 GC 重叠的样本（按墙钟降序，最多 8 个）：")
        print("    墙钟    线程CPU  Windows粒度CPU  GC次数(0/1/2)  重叠停顿  墙钟−停顿   (ms)")
        for r in sorted(gen2_hit, key=lambda r: -r["wall"])[:8]:
            c = r["counts"]
            print(f"    {r['wall']:6.2f}  {r['cpu']:6.2f}   {r['cpu_windows']:6.2f}         "
                  f"{c['gen0']}/{c['gen1']}/{c['gen2']}          {r['gc']:6.2f}   {r['wall'] - r['gc']:6.2f}")
        residual = [r["wall"] - r["gc"] for r in gen2_hit]
        print(f"  这些样本扣除重叠停顿后：P50 {statistics.median(residual):.2f}  最大 {max(residual):.2f} ms"
              f"（对照无重叠样本最大 {max(walls):.2f} ms）")
    windows_values = sorted({round(r["cpu_windows"], 3) for r in rows})
    print(f"  Windows 粒度量化后的线程 CPU 只出现这些值（ms）：{windows_values}")
    before = full_collection_ms()
    gc.freeze()
    after = full_collection_ms()
    gc.unfreeze()
    print(f"  一次完整 gc.collect() 的停顿中位数：常驻对象在 gen2 时 {before:.2f} ms；gc.freeze() 后 {after:.2f} ms")
    del keep


if __name__ == "__main__":
    main()
