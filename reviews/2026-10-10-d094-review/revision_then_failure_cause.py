"""D094 审查探针：普通任务（没有输入失联）改目标后失败，报告原因是什么。

运行：在 19231c6 和 34d8ba1 源树根目录分别执行 PYTHONPATH=. python <本文件>
flat_walk 走到第 10 tick 时把目标改到平台外的空中（不可达），任务应有界失败。
"""
from dataclasses import replace

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.runner import _goal, run
from tests.sim.scenarios import SCENARIOS

BASE = next(s for s in SCENARIOS if s.name == "flat_walk")
reports = []
revised = []


def step(context):
    if not revised and context.tick >= 10:
        goal = _goal((5.5, 64.0, 4.5), context.risk_policy_id)   # 平台外，没有支撑
        revised.append(context.driver.replace_goal(
            "goal", 2, goal, context.clock[0],
            damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points)))
        context.goal_state = goal
    context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
    reports.append(context.session.report)
    return ()


result = run(replace(BASE, max_ticks=300, expect="failed"), control_step=step)
cause = getattr(reports[-1], "failure_cause", None)
print(f"revision accepted={revised} outcome={result.outcome} reason={result.reason} "
      f"report.failure_cause={None if cause is None else cause.value} damage={result.damage}")
