"""E86：不加任何诊断补丁时，正式链实际走了哪些动作准备入口。

用项目夹具 tests.motion_nav.test_action_continuity_formal._gap_case（Walk → JumpGap
→ Walk，四个方向）和 D116 代表场景（Walk → JumpGap → JumpGap，
scripts.d116_handoff_timeline._representative），经 tests.sim.runner.run 跑正式链。
只计数，不改行为：统计下列方法在整场中被调用的次数——
  MotionRouteCoordinator._submit_action          普通 SOLVE 提交
  MotionRouteCoordinator.prepare_upcoming_action 严格后继的选择（prepared 链入口）
  MotionRouteCoordinator.prepare_action          prepared 证明提交
  MotionResultInbox._seal_snapshot               receiver 为 prepared／selection 封存快照
  ActionRouteExecutor.commit_prepared_activation prepared 激活提交
"""
from dataclasses import replace

from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.motion_worker import MotionResultInbox
from scripts.d116_handoff_timeline import _representative
from tests.motion_nav.test_action_continuity_formal import _gap_case
from tests.sim.runner import run

TARGETS = (
    (MotionRouteCoordinator, "_submit_action"),
    (MotionRouteCoordinator, "prepare_upcoming_action"),
    (MotionRouteCoordinator, "prepare_action"),
    (MotionResultInbox, "_seal_snapshot"),
    (ActionRouteExecutor, "commit_prepared_activation"),
)


def main() -> int:
    counts = {}
    originals = {}
    for owner, name in TARGETS:
        original = getattr(owner, name)
        originals[(owner, name)] = original

        def counted(*args, _original=original, _name=name, **kwargs):
            counts[_name] = counts.get(_name, 0) + 1
            return _original(*args, **kwargs)

        setattr(owner, name, counted)
    cases = [_gap_case(turns) for turns in range(4)]
    cases.append(replace(_representative(), max_ticks=80))
    print("场景 | 终态 | 原因 | " + " | ".join(name for _owner, name in TARGETS))
    try:
        for case in cases:
            counts.clear()
            result = run(case)
            print(f"{case.name} | {result.outcome} | {result.reason} | "
                  + " | ".join(str(counts.get(name, 0)) for _owner, name in TARGETS))
    finally:
        for (owner, name), original in originals.items():
            setattr(owner, name, original)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
