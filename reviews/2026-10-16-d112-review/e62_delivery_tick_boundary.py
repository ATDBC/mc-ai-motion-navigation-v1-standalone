"""E62：D112 组件矩阵的“5 tick 延迟”与 Fabric 的“5 tick 延迟”是不是同一个量。

只调用项目的组件夹具：`_DeliveryWorker`（按 movement tick 扣留真实 worker 结果）、`_gap_case`、
`height_action_scenarios` 和 `tests.sim.runner.run`。组件 runner 在每次 drain 前有一道
`wait_for_receiver` 屏障，使到期结果在同一 tick 被控制线程取用；Fabric 没有这道屏障，
D112 正式证据里 receiver 在提交后第 N tick 看见结果，控制线程在第 N+1 tick 才取用。

对每个动作和每个延迟，报告：
  - 结果、终态原因、提交的 worker 作业数；
  - 每个作业从提交 tick 到控制线程可见 tick 的实际间隔（协调器 `_delivery_ticks` 采样的量）；
  - 协调器最终的 `_delivery_ticks`。
若组件 6 tick 与 Fabric 5 tick 得到同一终态 `motion_retry_exhausted:new_motion_grant_has_no_start_slack`，
说明 D112 的 5 tick 上限只覆盖“提交→取用 ≤5 tick”，即 Fabric worker 延迟 ≤4 tick。
运行：PYTHONPATH=<检出> python e62_delivery_tick_boundary.py [延迟...]
"""
from dataclasses import replace
import subprocess
import sys

from tests.motion_nav.test_action_continuity_formal import _DeliveryWorker, _gap_case
from tests.sim.continuous_height_matrix import height_action_scenarios
from tests.sim.runner import run


def cases():
    height = {case.name: case for case in height_action_scenarios()}
    return {"gap": _gap_case(), "jump_up": height["jump_up_1"], "drop": height["direct_drop_2"]}


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    delays = [int(value) for value in sys.argv[1:]] or [4, 5, 6, 7]
    print(f"检出 {head}；组件延迟 = 结果在提交后第几个 movement tick 对控制线程可见")
    for name, case in cases().items():
        for delay in delays:
            worker = _DeliveryWorker(delivery_polls=delay)
            result = run(replace(case, max_ticks=max(case.max_ticks, 500)), motion_factory=lambda: worker)
            gaps = [observed - job.anchor.movement_tick_id for observed, job in worker.observed_deliveries]
            prefixes = [len(job.entry_prefix) for job in worker.jobs]
            print(f"  {name:<7} 延迟 {delay:>2}：{result.outcome:<7} 作业 {len(worker.jobs)}  "
                  f"提交→可见 {gaps}  前缀长度 {prefixes}  原因 {result.reason or '-'}")


if __name__ == "__main__":
    main()
