"""E139：后继 PreparedMotionJob 在 worker 里要算多久，与父动作执行期（第一拍到 L 的 26 拍）相比余量多少。

原样运行项目组件测试
`test_component_runs_the_exact_fabric_pretrial_assembly_after_fixture_steps`（四方向），
只包装 `RecordingProcessWorker.submit` 收集正式提交的 `PreparedMotionJob`（不改返回值）。
测试结束后，对每个方向收集到的 job 在本进程里各调用一次正式 `motion_worker._execute_job`，
记录单核墙钟时间和结果类型。父动作窗口在 anchor+1，后继窗口在 L+1：
父动作 26 条命令，执行期约 26 × 50 ms = 1.3 s；job 在第一拍 dispatch 返回后才提交（run-11 控制流）。
用法：python e139_successor_job_budget.py   （在检出根目录运行）
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

TEST = ("tests.motion_nav.test_d122_fabric_timing.D122FabricTimingTests."
        "test_component_runs_the_exact_fabric_pretrial_assembly_after_fixture_steps")


def main():
    from mc2p.motion_nav import motion_worker
    from tests.motion_nav import test_d121_successor_binding as binding
    jobs = []
    original_submit = binding.RecordingProcessWorker.submit

    def submit(self, job):
        accepted = original_submit(self, job)
        if accepted and type(job) is motion_worker.PreparedMotionJob:
            jobs.append(job)
        return accepted
    binding.RecordingProcessWorker.submit = submit
    result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(
        unittest.defaultTestLoader.loadTestsFromName(TEST))
    rows = []
    for job in jobs:
        started = time.perf_counter_ns()
        outcome = motion_worker._execute_job(job)
        elapsed = (time.perf_counter_ns() - started) / 1e6
        rows.append({"execute_job_ms": round(elapsed, 1), "result_type": type(outcome).__name__,
                     "parent_ticks_available_ms": 26 * 50})
    print(json.dumps({"test_passed": result.wasSuccessful(), "prepared_jobs": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
