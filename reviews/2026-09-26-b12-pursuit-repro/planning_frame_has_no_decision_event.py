"""A planning frame submits a neutral navigation intent but no reason event.

Run from the repository root of commit 3a1fdf0:

    PYTHONPATH=. python -B <this-review-branch>/reviews/2026-09-26-b12-pursuit-repro/planning_frame_has_no_decision_event.py

The planner holds the first job (``hold_first=True``), which is what a live
session looks like while the background planner has not returned yet.
"""
from __future__ import annotations

import unittest

from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.world_model import BlockGeometry
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav.test_navigation_session import (
    NavigationSessionTests, _InlinePlanner, _goal, _known_world, _nodes, _source,
)


def main() -> None:
    world = _known_world({
        (-1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
    })
    start, _, goal = _nodes(world, (-1, 0, 1))
    request = SurfacePlanningRequest(
        1, "request-1", "goal-1", 1, world.session.value,
        start.node_id, goal.node_id, goal_state=_goal(goal.position),
    )
    profiles = NavigationSessionTests("run").profiles()
    session = NavigationSession(
        "planning-frame", profiles,
        planner_worker=_InlinePlanner(hold_first=True),
        clock_ns=lambda: 1_000_000_000,
    )
    session.bind_source(_source())
    initial = frame(world, 0, start.position)
    session.start(request, initial)
    try:
        for tick in range(3):
            proposal = session.propose(
                frame(world, tick, start.position), None, 2_000_000_000,
            )
            control = proposal.control_frame
            intents = control.intents
            print(
                f"frame {tick}: state={proposal.report.state.value} "
                f"reason={proposal.report.reason} "
                f"intents={len(intents)} "
                f"movement={intents[0].intent.movement if intents else None} "
                f"task_events={len(control.task_events)} "
                f"route_decision={proposal.route_decision}"
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
