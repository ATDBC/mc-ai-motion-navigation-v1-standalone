"""E46：B10 求解最大值超 30 ms 的一个可检验来源——同进程的后台负载。

B10 在控制线程上同步调用 solve_one_cell_gap 并计墙钟（scripts/b10_gap_solver_runtime.py:1018–1023）。
D108 起同一 Python 进程里多了两类与求解争用的工作：
  - Runtime trace 改由 BoundedAsyncTraceWriter 的 worker 线程投影并写 JSONL（持有 GIL）；
  - 六类 B10 JSONL 证据改为在内存里缓冲到运行结束（上限 43 MiB），堆里常驻大量小对象，
    第 2 代 GC 每次要扫描的对象随之增多。
本实验在 Linux 上用项目的求解器、BoundedAsyncTraceWriter 和 JsonlTraceWriterV0，比较四种条件下
同一组求解（4 个方向 × 3 个速度带代表值，R4 带速入口）的耗时分布：
  A 只求解；
  B 后台 trace：worker 以约 80 条／秒接收约 6 KB 的记录（D8 最终 B10：22409 条、134.9 MB、约 280 s）；
  C 常驻堆：先在内存中保留约 9 MB 的诊断行（与 D8 diagnostics.jsonl 字节数同量级）的 Python 对象，GC 开启；
  D B + C。
每次求解同时记录墙钟和本线程 CPU 时间（time.thread_time_ns）：两者之差是线程没在跑的时间
（等 GIL、被抢占），CPU 时间本身反映求解工作量（含本线程触发的 GC）。另用 gc.callbacks 记录与求解
重叠的第 2 代 GC 停顿。绝对毫秒只代表本机，比较的是同机相对变化。
只调用项目函数，不改项目文件。运行：PYTHONPATH=<检出> python e46_solver_tail_inprocess_load.py [每组求解次数]
"""
import gc, json, math, pathlib, statistics, subprocess, sys, tempfile, threading, time

from mc2p.motion_nav.motion_solver import SolveStatus, solve_one_cell_gap
from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
from mc2p.runtime.trace import JsonlTraceWriterV0
from tests.motion_nav.test_b10_gap_solver import B10GapSolverTests, fixture

DIRECTIONS = ((0, 1), (1, 0), (0, -1), (-1, 0))
SPEEDS = (0.75, 1.5, 2.5)
gen2 = {"start": None, "pauses": []}


def on_gc(phase, info):
    if info.get("generation") != 2:
        return
    if phase == "start":
        gen2["start"] = time.perf_counter_ns()
    elif gen2["start"] is not None:
        gen2["pauses"].append((gen2["start"], time.perf_counter_ns()))
        gen2["start"] = None


def cases():
    helper = B10GapSolverTests()
    built = []
    for direction in DIRECTIONS:
        for speed in SPEEDS:
            anchor, world, target, _ = fixture(direction)
            built.append((helper.moving_anchor(anchor, direction, speed), world,
                          helper.request(target, direction)))
    return built


def fake_observation_payload(index):
    # About 6 KB once projected and serialized, like the D8 trace average.
    return {"observation": {"sequence_id": index, "blocks": [
        {"position": [index % 97, 63, k], "block_id": "minecraft:grass_block",
         "collision": {"kind": "full_cube", "boxes": [[0, 0, 0, 1, 1, 1]], "reason": None},
         "sources": ["surface_depth"]} for k in range(40)]}}


def background_trace(stop, directory):
    writer = BoundedAsyncTraceWriter(JsonlTraceWriterV0(directory / "trace.jsonl"), capacity=16_384)
    index = 0
    while not stop.is_set():
        writer.write("step", fake_observation_payload(index))
        index += 1
        time.sleep(1 / 80)
    return writer.close(timeout_seconds=10.0)


def retained_heap():
    rows = []
    size = 0
    index = 0
    while size < 9_000_000:
        row = {"episode_id": "fabric-21001-0", "observation_sequence_id": index,
               "diagnostics": {"client_tick": index, "movement_tick": index,
                               "timings": {f"stage_{k}": k * 1.5 for k in range(12)},
                               "flags": [index % 2 == 0, index % 3 == 0]}}
        size += len(json.dumps(row))
        rows.append(row)
        index += 1
    return rows


def measure(solves, *, trace, heap):
    built = cases()
    keep = retained_heap() if heap else None
    stop = threading.Event()
    result = {}
    folder = tempfile.TemporaryDirectory(prefix="e46-")
    thread = None
    if trace:
        thread = threading.Thread(target=lambda: result.setdefault("close", background_trace(stop, pathlib.Path(folder.name))))
        thread.start()
        time.sleep(0.5)
    for anchor, world, request in built[:3]:
        solve_one_cell_gap(anchor, world, request)  # warm-up
    gen2["pauses"].clear()
    durations, cpu, overlapping = [], [], []
    for index in range(solves):
        anchor, world, request = built[index % len(built)]
        started = time.perf_counter_ns()
        cpu_started = time.thread_time_ns()
        solved = solve_one_cell_gap(anchor, world, request)
        cpu_finished = time.thread_time_ns()
        finished = time.perf_counter_ns()
        assert solved.status is SolveStatus.SOLVED
        durations.append((finished - started) / 1e6)
        cpu.append((cpu_finished - cpu_started) / 1e6)
        overlapping.extend((end - begin) / 1e6 for begin, end in gen2["pauses"]
                           if begin < finished and end > started)
        gen2["pauses"].clear()
        time.sleep(0.02)  # B10 solves between movement ticks, not back to back
    stop.set()
    if thread is not None:
        thread.join()
    folder.cleanup()
    del keep
    return durations, cpu, overlapping, result.get("close")


def nearest_rank(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    solves = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    gc.callbacks.append(on_gc)
    print(f"检出 {head}：R4 带速入口 {len(DIRECTIONS) * len(SPEEDS)} 种情形轮换，每组 {solves} 次求解（Linux）")
    for label, trace, heap in (("A 只求解", False, False), ("B 后台异步 trace", True, False),
                               ("C 常驻约 9 MB 诊断对象", False, True), ("D B + C", True, True)):
        durations, cpu, pauses, close = measure(solves, trace=trace, heap=heap)
        waiting = [wall - work for wall, work in zip(durations, cpu)]
        worst = max(range(len(durations)), key=durations.__getitem__)
        print(f"  {label:<16} 墙钟 P50 {statistics.median(durations):6.2f}  P95 {nearest_rank(durations, .95):6.2f}  "
              f"P99 {nearest_rank(durations, .99):6.2f}  最大 {max(durations):6.2f} ms")
        print(f"  {'':<16} CPU  P50 {statistics.median(cpu):6.2f}  P95 {nearest_rank(cpu, .95):6.2f}  "
              f"最大 {max(cpu):6.2f} ms；墙钟−CPU P50 {statistics.median(waiting):5.2f}  P95 {nearest_rank(waiting, .95):5.2f}  "
              f"最大 {max(waiting):5.2f} ms；最慢一次的 墙钟/CPU {durations[worst]:.2f}/{cpu[worst]:.2f} ms；"
              f"与求解重叠的第 2 代 GC {len(pauses)} 次"
              f"{'（最长 %.2f ms）' % max(pauses) if pauses else ''}"
              f"{'；trace close ' + close.status.value if close is not None else ''}")


if __name__ == "__main__":
    main()
