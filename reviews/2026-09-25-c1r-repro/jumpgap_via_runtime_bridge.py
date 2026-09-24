"""JumpGap through NavigationSession: test path versus the formal Runtime bridge.

Run from the repository root of a checkout of commit 1c6a0de:

    PYTHONPATH=. python reviews/2026-09-25-c1r-repro/jumpgap_via_runtime_bridge.py

It reuses the world, anchor and profiles of
``tests/motion_nav/test_navigation_session.py::test_gap_route_is_solved_by_the_session_coordinator``
and drives the same session two ways:

* with a state anchor and input ledger, as that test does;
* with ``(None, None)``, as ``RuntimeNavigationDriver.tick()`` does
  (``mc2p/skills/navigation_session_driver.py:134``).

Observed at 1c6a0de: the first submits a verified jump command; the second
stays in ``awaiting_verified_motion`` for the whole polling window.
"""
from __future__ import annotations

from dataclasses import replace
import time

import tests.motion_nav.test_navigation_session as fixtures
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger


POLL_SECONDS = 3.0


def _gap_session():
    anchor, _, _, _ = fixtures.gap_fixture()
    knowledge = fixtures.WorldKnowledge(anchor.session)
    known = fixtures.ObservationStamp(anchor.session, 1, 1, "test", 1)
    knowledge.confirm_air(known, tuple(
        (x, y, z)
        for x in range(-2, 3)
        for y in range(60, 71)
        for z in range(-2, 5)
    ))
    knowledge.observe_blocks(known, {
        (0, 63, 0): fixtures.BlockGeometry.full_cube("minecraft:grass_block"),
        (0, 63, 2): fixtures.BlockGeometry.full_cube("minecraft:grass_block"),
    })
    world = knowledge.view()
    goal = fixtures.query_support_surfaces(world, 0, 2, 64, 64).surfaces[0]
    state = anchor.physics_state
    x, y, z = state.position
    body = fixtures.BodyState(
        state.session, anchor.observation_sequence_id,
        fixtures.ObservationStamp(
            state.session, anchor.observation_sequence_id,
            anchor.movement_tick_id, "test", 1_000_000_000,
        ),
        state.position,
        tuple(value * 20.0 for value in state.velocity_blocks_per_tick),
        state.yaw_radians, state.pitch_radians, state.pose,
        fixtures.Aabb(
            x - state.body_width / 2, y, z - state.body_width / 2,
            x + state.body_width / 2, y + state.body_height,
            z + state.body_width / 2,
        ),
        state.on_ground, state.horizontal_collision, state.vertical_collision,
        is_sprinting=state.sprinting, is_sneaking=state.sneaking,
        food_points=state.food_points,
        saturation_points=state.saturation_points,
    )
    current = fixtures.NavigationFrame(state.session, body, world, "fabric")
    profiles = NavigationSessionProfiles(
        replace(
            fixtures.ordinary_profile(),
            support_materials=frozenset({"minecraft:grass_block"}),
        ),
        fixtures.jump_profile(), fixtures.step_profile(),
        air=(fixtures.air_profile(MovementMode.JUMP_GAP),),
    )
    session = NavigationSession(
        "gap-session", profiles, planner_worker=fixtures._InlinePlanner(),
        clock_ns=lambda: 1_000_000_000,
    )
    session.bind_source(fixtures._source())
    session.start_goal("gap-goal", 1, fixtures._goal(goal.position), current)
    return session, current, anchor


def _drive(with_anchor: bool) -> tuple[bool, int, set[str]]:
    session, current, anchor = _gap_session()
    ledger = InputApplicationLedger(max_records=64)
    reasons: set[str] = set()
    polls = 0
    deadline = time.perf_counter() + POLL_SECONDS
    try:
        while time.perf_counter() < deadline:
            proposal = session.propose(
                current,
                anchor if with_anchor else None,
                2_000_000_000,
                input_ledger=ledger if with_anchor else None,
            )
            polls += 1
            decision = proposal.route_decision
            if decision is not None:
                reasons.add(decision.reason_code)
                if decision.submit_input:
                    return True, polls, reasons
            time.sleep(.01)
        return False, polls, reasons
    finally:
        session.close()


def main() -> None:
    for label, with_anchor in (
        ("anchor + ledger (session test path)", True),
        ("None, None (RuntimeNavigationDriver path)", False),
    ):
        submitted, polls, reasons = _drive(with_anchor)
        print(f"{label}: submitted_input={submitted}, polls={polls}, "
              f"reasons={sorted(reasons)}")


if __name__ == "__main__":
    main()
