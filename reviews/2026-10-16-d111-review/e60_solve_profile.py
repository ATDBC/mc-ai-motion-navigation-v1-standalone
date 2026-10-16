"""E60：一次跨隙求解的时间花在哪里（Linux，cProfile）。

用项目测试夹具构造 R4 的 12 种带速入口（4 方向 × 0.75/1.5/2.5 格／秒）和 4 个静止对正入口，
每种求解 10 次，cProfile 按“累计耗时”列出 solve_one_cell_gap 以下的主要函数，并给出每次求解的
候选数、证明里各部分的条目数。绝对毫秒只代表本机；看的是各部分的占比。
运行：PYTHONPATH=<检出> python e60_solve_profile.py
"""
import cProfile, collections, io, pstats, statistics, subprocess, time

from mc2p.motion_nav.motion_solver import solve_one_cell_gap
from tests.motion_nav.test_b10_gap_solver import B10GapSolverTests, fixture

DIRECTIONS = ((0, 1), (1, 0), (0, -1), (-1, 0))


def cases():
    helper = B10GapSolverTests()
    built = []
    for direction in DIRECTIONS:
        anchor, world, target, _ = fixture(direction)
        built.append(("静止对正", anchor, world, helper.request(target, direction)))
        for speed in (0.75, 1.5, 2.5):
            built.append((f"带速 {speed}", helper.moving_anchor(anchor, direction, speed), world,
                          helper.request(target, direction)))
    return built


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    built = cases()
    for _, anchor, world, request in built:
        solve_one_cell_gap(anchor, world, request)  # warm-up
    walls = collections.defaultdict(list)
    shapes = {}
    for _ in range(10):
        for label, anchor, world, request in built:
            started = time.perf_counter_ns()
            solved = solve_one_cell_gap(anchor, world, request)
            walls[label].append((time.perf_counter_ns() - started) / 1e6)
            proof = solved.proof
            shapes[label] = (solved.candidates_evaluated, len(proof.commands), len(proof.trajectory),
                             len(proof.delayed_start_variants))
    print(f"检出 {head}：每种入口 10 次 × 4 方向（Linux）")
    for label in walls:
        c, commands, trajectory, variants = shapes[label]
        print(f"  {label:<10} 中位 {statistics.median(walls[label]):6.2f} ms；候选 {c}，命令 {commands}，"
              f"轨迹状态 {trajectory}，延迟起跳变体 {variants}")
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(5):
        for _, anchor, world, request in built:
            solve_one_cell_gap(anchor, world, request)
    profiler.disable()
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).strip_dirs()
    stats.sort_stats("cumulative").print_stats(28)
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    start = next(i for i, line in enumerate(lines) if "ncalls" in line)
    print("  cProfile（80 次求解，按累计耗时，前 28 个函数）：")
    for line in lines[start:start + 29]:
        print("   ", line.strip()[:150])


if __name__ == "__main__":
    main()
