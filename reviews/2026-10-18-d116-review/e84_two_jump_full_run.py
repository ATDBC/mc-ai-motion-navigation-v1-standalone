"""E84：D116 代表正式链跑完整条路线，而不是只看前 25 tick。

复用项目脚本 scripts/d116_handoff_timeline.py 的 run_representative_timeline()：
它在进程内恢复 D115 撤回的 prepare_upcoming_action() 调用，其余全部是正式链
（PlayerRuntimeV1 → RuntimeNavigationDriver → NavigationSession → ExecutionSupervisor
→ ActionRouteExecutor → MotionRouteCoordinator → CalculatorBackend）。
本脚本只把代表场景的 max_ticks 从 25 改成参数值，并在结束后汇总：
  - 终态、原因、终点、伤害、监视器违规；
  - 每个 tick 的 action index、着地、实际输入、路线复核结论；
  - 第一跳落地 L、第二跳首个跳跃输入 tick、第二跳落地 tick、两跳间“着地且无输入”的帧数。
在哪棵树里运行，就验证哪棵树（原样或临时对照树）。
"""
import json
import sys
from dataclasses import replace

from scripts import d116_handoff_timeline as timeline_module


def main() -> int:
    max_ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    original = timeline_module._representative

    def longer():
        return replace(original(), max_ticks=max_ticks)

    timeline_module._representative = longer
    try:
        result = timeline_module.run_representative_timeline()
    finally:
        timeline_module._representative = original
    rows = result["timeline"]
    print(f"max_ticks={max_ticks} 终态={result['outcome']} 原因={result['reason']} "
          f"终点={[round(v, 3) for v in result['final_position']]} 伤害={result['damage']}")
    print("规划动作:", " -> ".join(result["planned_actions"]))
    print("监视器违规:", result["violations"])
    print("里程碑:", json.dumps(result["milestones"], ensure_ascii=False, sort_keys=True))
    print()
    print("tick | action(进入→退出) | 着地 | 实际输入 | 路线复核 | prepared/批准 | session")
    jumps = []
    for row in rows:
        tick = row["movement_tick"]
        moved = row.get("applied_movement_at_observation") or {}
        keys = "".join(k[0] for k in ("forward", "jump", "sprint", "sneak") if moved.get(k))
        if moved.get("jump"):
            jumps.append(tick)
        validation = row.get("route_validation") or {}
        incumbent = validation.get("incumbent") or {}
        verdict = (f"{incumbent.get('action_index')}:{incumbent.get('disposition')}/{incumbent.get('reason')}"
                   if incumbent else "-")
        prepared = "/".join(str(row.get(k)) for k in ("prepared_entry_status", "supervisor_status")
                            if row.get(k) is not None) or "-"
        print(f"{tick} | {row.get('action_index_before_advance')}→{row.get('action_index_after')} | "
              f"{row.get('landing_observed')} | {keys or '-'} | {verdict} | {prepared} | "
              f"{row.get('session_state')}/{row.get('session_reason_after_effect')}")
    print()
    print("出现 jump 输入的 tick:", jumps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
