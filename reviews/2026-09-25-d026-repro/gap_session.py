"""Shared fixture: the JumpGap world and session from the formal session test.

Copied from ``reviews/2026-09-25-c1r-rereview-2-repro/`` (docstring only changed).  Mirrors
``tests/motion_nav/test_navigation_session.py::
test_gap_route_is_solved_by_the_session_coordinator`` so each repro script
starts from the same admitted JumpGap route.  Imported by
``neutral_landing_frame_breaks_b10_probe_ledger.py``; run it from the repository root
of a checkout of dd38c6b with ``PYTHONPATH=.:reviews/2026-09-25-d026-repro``.
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


def gap_session():
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


def drive_until_verified_command(session, current, anchor, *, seconds: float = 3.0):
    """Propose with anchor and ledger until the verified jump command is submitted."""
    ledger = InputApplicationLedger(max_records=64)
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        proposal = session.propose(
            current, anchor, 2_000_000_000, input_ledger=ledger,
        )
        decision = proposal.route_decision
        if decision is not None and decision.submit_input:
            return proposal, ledger
        time.sleep(.01)
    raise RuntimeError("verified jump command was not submitted")
