"""E70：D113 的交付估计在同一条路线内只增不减，一次慢交付会不会被后面每个严格动作继承。

D113 把 `_sample_solve_delivery` 改成 `max(旧估计, 本次)`，对 SOLVE 也生效；协调器按路线创建
（`navigation_session.py` 中 `MotionRouteCoordinator(...)`），估计在整条路线内保留。
本脚本用项目组件夹具 `_DeliveryWorker(delivery_polls=1, first_delivery_polls=5)`：只有第一个作业
晚 5 tick 可见，之后全部 1 tick。场景取 tests/sim/scenarios.py 里一条路线含多个 jump-up 的
terrace_two_ledges、terrace_three_ledges。报告每个作业的动作、操作、准备前缀长度，以及完成所用 tick。
在 D112（54a904e）和 D113（81367da）检出上各跑一次比较。
运行：PYTHONPATH=<检出> python e70_sticky_delivery_estimate.py
"""
from dataclasses import replace
import subprocess

from tests.motion_nav.test_action_continuity_formal import _DeliveryWorker
from tests.sim import scenarios
from tests.sim.runner import run


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}；第一个作业 5 tick 后可见，其余 1 tick")
    wanted = {"terrace_two_ledges", "terrace_three_ledges"}
    for case in scenarios.SCENARIOS:
        if getattr(case, "name", None) not in wanted:
            continue
        for first in (1, 5):
            worker = _DeliveryWorker(delivery_polls=1, first_delivery_polls=first)
            result = run(replace(case, max_ticks=max(case.max_ticks, 300)), motion_factory=lambda: worker)
            jobs = [f"{job.connection_id.rsplit('/', 1)[-1]}:{job.operation.value}/前缀{len(job.entry_prefix)}"
                    for job in worker.jobs]
            print(f"  {case.name:<22} 首个作业 {first} tick：{result.outcome:<7} 用 {len(result.trace)} tick；作业 {jobs}")


if __name__ == "__main__":
    main()
