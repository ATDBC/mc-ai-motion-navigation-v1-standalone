"""Round-17 review: is the remaining task budget re-checked when a plan is admitted?

The session snapshots its remaining damage budget into each planning request.
Damage is booked when a ControlledDropSegment completes.  If a damaging drop
completes after a request was issued but before that request's candidate is
admitted (the old route keeps executing as a safe prefix while the background
planner works, e.g. during pursuit goal updates), the candidate is admitted
against the stale budget.

Reproduced with session internals, in the style of the upstream unit test
test_replanning_keeps_damage_already_spent_by_the_same_task:
  1. task budget 2; start_goal issues a request (budget 2) for a route whose
     only way down is a 5-block drop (conservative damage 2);
  2. before the planner result is admitted, an earlier executor reports a
     completed 2-point drop (Mock, as upstream), so 2 points are spent;
  3. the next propose admits the pending candidate.

Run from the repository root of a 5fa2f33 checkout:
    PYTHONPATH=. python -B <this script>
"""
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, VisualAirEvidence, WorldKnowledge, WorldSessionId,
)
from tests.motion_nav.test_b07_step_transition import frame
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal, _source

STONE = BlockGeometry.full_cube("minecraft:stone")
session_id = WorldSessionId("stale-budget-world")
knowledge = WorldKnowledge(session_id)
stamp = ObservationStamp(session_id, 1, 1, "test-clock", 1)
blocks = {(0, 63, 0): STONE, (1, 58, 0): STONE}
landing_cell = (1, 59, 0)
air = tuple((x, y, z) for x in range(-3, 5) for y in range(55, 71) for z in range(-3, 4)
            if (x, y, z) not in blocks)
# Landing evidence that satisfies the gate outright (near, lower part seen).
knowledge.confirm_air(stamp, air, visual_evidence={
    landing_cell: VisualAirEvidence(stamp, observer_distance_blocks=2.0, lower_region_visible=True),
})
knowledge.observe_blocks(stamp, blocks)

profiles = NavigationSessionProfiles.load(Path("config/motion-navigation"))
session = NavigationSession("stale-budget", profiles, planner_worker=_InlinePlanner(),
                            clock_ns=lambda: 1_000_000_000)
session.bind_source(_source())
body = (.5, 64.0, .5)
session.start_goal("task", 1, replace(_goal((1.5, 59.0, .5)), risk_policy_id="two"),
                   frame(knowledge, 1, body), damage_budget=TaskDamageBudget("two", 2.0))
print("request budget when issued      :", session._request.damage_budget.maximum_expected_damage_points)

# An earlier drop of the same task completes before this request's plan is admitted.
session._executor = Mock(completed_movement_damage_points=2.0)
session._record_completed_movement_damage()
session._clear_active_execution()
print("remaining task budget now       :", session._remaining_damage_budget().maximum_expected_damage_points)

proposal = session.propose(frame(knowledge, 2, body), None, 2_000_000_000)
print("session after next propose      :", proposal.report.state.value, proposal.report.reason)
route = session._active_route
if route is not None:
    actions = route.action_route.actions
    print("admitted actions                :", [type(a).__name__ for a in actions])
    for action in actions:
        if type(action).__name__ == "ControlledDropSegment":
            height = action.start_surface.position[1] - action.end_surface.position[1]
            print(f"  drop of {height:g} blocks, conservative damage {max(0, int(height - 3 + .999))}")
    print("budget bound to the new executor:", session._executor._damage_budget.maximum_expected_damage_points)
