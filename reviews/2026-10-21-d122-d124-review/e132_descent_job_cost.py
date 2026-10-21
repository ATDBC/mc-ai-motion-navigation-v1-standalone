"""E132：`test_continuous_descent` 里被判 EXPIRED 的后台求解任务，在两个提交上各花多少时间、花在哪里。

项目测试 `test_next_stair_drop_is_solved_from_the_confirmed_moving_exit` 在进程内直接调用
`motion_worker._execute_job(jobs[0])`，再交给 `_accept_result`；后者用真实 monotonic 时钟检查
1 s 的工作窗口（`_MOTION_SOLVE_LIMIT_NS`），超时结果判 EXPIRED 并丢弃，测试断言 `accepted` 失败。
本脚本原样运行这条项目测试，只包装 `_execute_job`：
  time     —— 记录该次调用的墙钟时间（不开 profile），并报告测试是否通过；
  profile  —— 用 cProfile 记录同一次调用，报告几个物理查询函数的调用次数、累计时间和调用者。
profile 模式下 cProfile 开销会让该测试自身超过 1 s 窗口而失败，这是预期，只看函数分布。
用法：python e132_descent_job_cost.py time|profile   （在检出根目录运行）
"""
import cProfile
import io
import json
import pstats
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

TEST = ("tests.motion_nav.test_continuous_descent.ContinuousDescentTests."
        "test_next_stair_drop_is_solved_from_the_confirmed_moving_exit")
FUNCTIONS = ("step", "_contacts_unmodeled_material", "occupied_cells_for_sweep", "required_cells_for_sweep",
             "support", "query_support", "shapes_for_sweeps")


def main():
    mode = sys.argv[1]
    from mc2p.motion_nav import motion_worker
    original = motion_worker._execute_job
    seconds, profiles = [], []

    def wrapped(job):
        if mode == "profile":
            profile = cProfile.Profile()
            result = profile.runcall(original, job)
            profiles.append(profile)
            return result
        started = time.perf_counter()
        result = original(job)
        seconds.append(round(time.perf_counter() - started, 3))
        return result

    motion_worker._execute_job = wrapped
    outcome = unittest.TextTestRunner(verbosity=0, stream=io.StringIO()).run(
        unittest.defaultTestLoader.loadTestsFromName(TEST))
    report = {"test_passed": outcome.wasSuccessful()}
    if mode == "time":
        report["execute_job_seconds"] = seconds
    else:
        stats = pstats.Stats(profiles[0])
        report["profiled_total_seconds"] = round(stats.total_tt, 3)
        rows = []
        for (_file, _line, name), (_cc, calls, _tt, cumulative, callers) in stats.stats.items():
            if name in FUNCTIONS and "motion_nav" in _file:
                rows.append({"function": f"{Path(_file).name}:{name}", "calls": calls,
                             "cumulative_seconds": round(cumulative, 3),
                             "callers": {caller[2]: value[1] for caller, value in callers.items()}})
        report["functions"] = sorted(rows, key=lambda row: -row["cumulative_seconds"])
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
