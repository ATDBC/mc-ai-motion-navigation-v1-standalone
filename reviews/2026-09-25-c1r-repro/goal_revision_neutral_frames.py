"""Neutral movement emitted by NavigationSession after a goal revision.

Run from the repository root of a checkout of commit 1c6a0de:

    PYTHONPATH=. python reviews/2026-09-25-c1r-repro/goal_revision_neutral_frames.py

A straight, fully known stone corridor is planned with the real background
``PlannerWorker``.  After the route is executing, the goal is revised once, as
``MovingMeleeDriver`` does when the target moves more than 0.75 blocks.
``update_goal`` -> ``_replace_request`` -> ``_retire_route`` drops the active
route, and ``propose`` returns neutral movement until the new route is admitted.
Frames are paced at 20 Hz.
"""
from __future__ import annotations

import time

import tests.motion_nav.test_navigation_session as fixtures
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.planner_worker import PlannerWorker


FRAME_SECONDS = .05


def main() -> None:
    world = fixtures._known_world({
        (x, 0, 0): fixtures.BlockGeometry.full_cube("minecraft:stone")
        for x in range(-1, 9)
    })
    nodes = fixtures._nodes(world, tuple(range(-1, 9)))
    start, first_goal, revised_goal = nodes[0], nodes[6], nodes[8]
    profiles = fixtures.NavigationSessionTests().profiles()
    sequence = 0

    with PlannerWorker() as planner:
        session = NavigationSession("revision-session", profiles, planner_worker=planner)
        session.bind_source(fixtures._source())
        session.start(SurfacePlanningRequest(
            1, "request-1", "goal", 1, world.session.value,
            start.node_id, first_goal.node_id,
            goal_state=fixtures._goal(first_goal.position),
        ), fixtures.frame(world, sequence, start.position))

        def frame_movement() -> tuple[str, MovementV1]:
            nonlocal sequence
            sequence += 1
            proposal = session.propose(
                fixtures.frame(world, sequence, start.position), None,
                time.perf_counter_ns() + 2_000_000_000,
            )
            intents = () if proposal.control_frame is None else proposal.control_frame.intents
            movement = intents[0].intent.movement if intents else MovementV1()
            return proposal.report.state.value, movement

        initial = 0
        while True:
            initial += 1
            _, movement = frame_movement()
            if movement != MovementV1():
                break
            time.sleep(FRAME_SECONDS)

        session.update_goal("goal", 2, fixtures._goal(revised_goal.position))
        states = []
        while True:
            state, movement = frame_movement()
            states.append(state)
            if movement != MovementV1():
                break
            time.sleep(FRAME_SECONDS)
        session.close()

    print(f"initial plan: {initial} frame(s) until the first movement")
    print(f"after one goal revision: {len(states) - 1} neutral frame(s) "
          f"before movement resumed; states={states}")


if __name__ == "__main__":
    main()
