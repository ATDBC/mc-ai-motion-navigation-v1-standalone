"""Round-15 review: a missing cell beyond 16 blocks keeps the session turning forever.

The native air probe returns status 2 both for "outside the 120-degree view" and for
"farther than 16 blocks"; SurfaceSensor maps 2 to outside_view.  _information_look()
treats outside_view as "turn toward it", and any turn candidate resets the new
40-frame wait counter.  Once the bot already faces a far cell, the status stays
outside_view, the look delta is ~0, and the wait never ends.

This reuses the upstream test fixture of test_structurally_occluded_information_wait_is_bounded
and changes only the reported status.

Run from the repository root of a 4b9e73e checkout:
    PYTHONPATH=. python -B <this script>
"""
from dataclasses import replace

from mc2p.contracts.observation_v3 import AirQueryResultV3
from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionState
from tests.motion_nav.test_navigation_session import (
    NavigationSessionTests, _InlinePlanner, _goal, _known_endpoints_with_unknown_gap, _nodes,
    _source, frame,
)


def run(status: str, frames: int = 200):
    world, unknown_gap = _known_endpoints_with_unknown_gap()
    start, goal = _nodes(world, (-1, 1))
    initial = replace(frame(world, 0, start.position),
                      air_query_results=(AirQueryResultV3(unknown_gap, status),))
    session = NavigationSession("far-cell", NavigationSessionTests().profiles(),
                                planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000)
    session.bind_source(_source())
    session.start(SurfacePlanningRequest(
        1, "far-request", "far-goal", 1, world.session.value, start.node_id, goal.node_id,
        goal_state=_goal(goal.position)), initial)
    session.propose(initial, None, 2_000_000_000)
    looks = 0
    for sequence in range(1, frames + 1):
        look = session._information_look(replace(initial, body=replace(initial.body, sequence_id=sequence)))
        looks += look is not None
    return session.report.state.value, session.report.reason, looks


for status in ("occluded", "outside_view"):
    state, reason, looks = run(status)
    print(f"status {status:<13} after 200 frames: state={state:<18} reason={reason:<52} look proposals={looks}")
