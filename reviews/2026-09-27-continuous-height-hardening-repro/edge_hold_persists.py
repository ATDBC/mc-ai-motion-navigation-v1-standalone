"""Round-17 review: does the sneak edge-probe hold outlive the drop it was for?

Session flow with the formal profile set (NavigationSessionProfiles.load) and
the repository's inline test planner:
  1. a task whose route is a 4-block direct drop off a platform edge with a
     1-point damage budget.  The landing air was confirmed without near,
     lower-part visual evidence, so admission returns
     landing_visual_evidence_missing and the session sneaks toward the edge
     (information_probe_movement), setting _information_edge_hold;
  2. the same task then moves its goal back along the platform (plain Walk,
     no drop, no verified motion command);
  3. print what the route wants and what the session actually submits.

Run from the repository root of a 5fa2f33 checkout:
    PYTHONPATH=. python -B <this script>
"""
from dataclasses import replace
from pathlib import Path

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from mc2p.contracts.observation_v3 import AirQueryResultV3
from tests.motion_nav.test_b07_step_transition import frame
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal, _source

STONE = BlockGeometry.full_cube("minecraft:stone")
session_id = WorldSessionId("edge-hold-world")
knowledge = WorldKnowledge(session_id)
stamp = ObservationStamp(session_id, 1, 1, "test-clock", 1)
blocks = {**{(0, 63, z): STONE for z in range(-4, 1)}, (1, 59, 0): STONE}
knowledge.confirm_air(stamp, tuple(
    (x, y, z) for x in range(-3, 5) for y in range(55, 71) for z in range(-7, 4)
    if (x, y, z) not in blocks
))
knowledge.observe_blocks(stamp, blocks)

profiles = NavigationSessionProfiles.load(Path("config/motion-navigation"))
session = NavigationSession("edge-hold", profiles, planner_worker=_InlinePlanner(),
                            clock_ns=lambda: 1_000_000_000)
session.bind_source(_source())
body = (.5, 64.0, .5)          # facing +x (the helper's default yaw), toward the drop


LANDING_BODY_CELL = (1, 60, 0)


def show(label, sequence, air=()):
    current = replace(frame(knowledge, sequence, body), air_query_results=air)
    proposal = session.propose(current, None, 2_000_000_000)
    intents = proposal.control_frame.intents if proposal.control_frame else ()
    submitted = intents[0].intent.movement if intents else None
    route = proposal.route_decision
    wanted = None if route is None else route.movement
    print(f"{label} frame {sequence:>2}: {proposal.report.state.value:<17} {str(proposal.report.reason):<34} "
          f"route wants {wanted}\n{'':>19}submitted {submitted}  hold={session._information_edge_hold}")


session.start_goal("task", 1, replace(_goal((1.5, 60.0, .5)), risk_policy_id="one"),
                   frame(knowledge, 0, body), damage_budget=TaskDamageBudget("one", 1.0))
for sequence in range(1, 4):
    # the formal sensor reports the lower landing cell as hidden by the platform
    show("drop task", sequence, (AirQueryResultV3(LANDING_BODY_CELL, "occluded"),))
session.update_goal("task", 2, replace(_goal((.5, 64.0, -3.5)), risk_policy_id="one"))
for sequence in range(4, 10):
    show("walk goal", sequence)

