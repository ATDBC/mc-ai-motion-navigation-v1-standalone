"""E69：生产驱动“撤销旧移动租约”这一步是否就是 10-tick 等待不超时的原因。

组件 runner 用生产 `RuntimeNavigationDriver`，它在会话帧不声明移动时先 `cancel_source`
（`mc2p/skills/navigation_session_driver.py`）。B10 Fabric 夹具的等待分支没有这一步。
本脚本在给定检出上用项目组件夹具跑 gap／jump_up／drop × 延迟 5、6、10 与 worker 永不返回，报告结果、终态原因和
trace 中第一次 Runtime 失败。对照检出只去掉那一行 `cancel_source`（e69_no_lease_revoke_control.diff）。
组件 runner 以 50 ms 模拟时钟推进，Runtime 照常用动作期限检查每一步。
运行：PYTHONPATH=<检出> python e69_driver_lease_guard.py
"""
from dataclasses import replace
import subprocess

from tests.motion_nav.test_action_continuity_formal import _DeliveryWorker, _gap_case
from tests.motion_nav.test_d112_worker_degradation import _AliveNoResultWorker
from tests.sim.continuous_height_matrix import height_action_scenarios
from tests.sim.runner import run


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "diff", "--stat"], capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}{'（含对照改动：' + dirty.splitlines()[-1].strip() + '）' if dirty else ''}")
    height = {case.name: case for case in height_action_scenarios()}
    cases = {"gap": _gap_case(), "jump_up": height["jump_up_1"], "drop": height["direct_drop_2"]}
    for name, case in cases.items():
        for delay in (5, 6, 10, "永不返回"):
            worker = (_AliveNoResultWorker(delivery_polls=0) if delay == "永不返回"
                      else _DeliveryWorker(delivery_polls=delay))
            result = run(replace(case, max_ticks=max(case.max_ticks, 500)), motion_factory=lambda: worker)
            failures = [row["runtime_failure"] for row in result.trace if row["runtime_failure"] is not None]
            first = failures[0] if failures else "-"
            print(f"  {name:<7} 延迟 {delay!s:>4}：{result.outcome:<7} 原因 {result.reason or '-'}；"
                  f"Runtime 失败 {len(failures)} 次，首次 {first}")


if __name__ == "__main__":
    main()
