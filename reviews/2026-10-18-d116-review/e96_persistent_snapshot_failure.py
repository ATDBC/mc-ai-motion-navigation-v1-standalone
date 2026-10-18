"""E96：快照借用持续失败时，prepared 后继有没有回退或有界出口。

复用项目脚本 scripts/d116_handoff_timeline.py 的 run_representative_timeline()（进程内恢复
prepare_upcoming_action()，其余是正式链），max_ticks 放到 80。唯一的注入是让
WorldKnowledge.borrow_snapshot_version() 一律返回 None，相当于 receiver 每次借快照都撞上
观察写入持有的同一把锁。现行生产链（不恢复自动准备）不调用这个借用入口（见 E86）。
逐 tick 记录：
  - 执行器 action index、是否仍要求 prepared 后继（prepared_successor_required）；
  - 协调器 _retire_work() 的原因；
  - 会话原因；
以及终态、原因、监视器违规。在哪棵树里运行，就验证哪棵树（原样或临时对照树）。
"""
import collections
import json
from dataclasses import replace

from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.world_model import WorldKnowledge
from scripts import d116_handoff_timeline as timeline_module


def main() -> int:
    original_case = timeline_module._representative
    timeline_module._representative = lambda: replace(original_case(), max_ticks=80)
    WorldKnowledge.borrow_snapshot_version = lambda self, bounds, *, ruleset: None

    per_tick, retired = {}, collections.defaultdict(list)
    original_decide = MotionRouteCoordinator.decide
    original_retire = MotionRouteCoordinator._retire_work

    def decide(self, frame, anchor, ledger, world, **kwargs):
        decision = original_decide(self, frame, anchor, ledger, world, **kwargs)
        per_tick[anchor.movement_tick_id] = (self.executor.action_index,
                                             self.executor.prepared_successor_required)
        return decision

    def retire(self, reason, *args, **kwargs):
        retired[reason].append(max(per_tick, default=None))
        return original_retire(self, reason, *args, **kwargs)

    MotionRouteCoordinator.decide = decide
    MotionRouteCoordinator._retire_work = retire
    result = timeline_module.run_representative_timeline()
    rows = {row["movement_tick"]: row for row in result["timeline"]}
    print("注入：borrow_snapshot_version() 一律返回 None；max_ticks=80")
    print(f"终态={result['outcome']} 原因={result['reason']} 伤害={result['damage']}")
    print("监视器违规:", [(tick, code) for tick, code, _ in result["violations"]])
    print("出现 jump 输入:", result["second_jump_started"] and "第二跳已开始" or "第二跳未开始")
    print("协调器退役原因（原因 → 发生时最近一次 decide 的 tick）:", json.dumps(retired, ensure_ascii=False))
    print()
    print("tick | action index | 仍要求 prepared 后继 | 会话原因")
    shown = sorted(per_tick)
    for tick in shown:
        index, required = per_tick[tick]
        reason = rows.get(tick, {}).get("session_route_reason")
        if tick <= 24 or tick % 10 == 0 or tick == shown[-1]:
            print(f"{tick} | {index} | {required} | {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
