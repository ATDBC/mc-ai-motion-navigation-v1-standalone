"""Common combat conditions under which the motion residual cannot be computed.

Run from the repository root of a checkout of commit 1c6a0de:

    PYTHONPATH=. python reviews/2026-09-25-c1r-repro/residual_unsupported_conditions.py

The motion residual replays applied inputs through ``physics_1_21.step``.
When ``step`` returns UNSUPPORTED, ``DamageKnockbackDetector`` reports
``motion_residual_unavailable`` and, after its 8-tick window, drops the damage
fact without an external-motion event (``mc2p/motion_nav/external_motion.py``).

The state and world follow ``tests/motion_nav/test_b09r_physics_step.py``.
"""
from __future__ import annotations

from dataclasses import replace

from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import (
    JAVA_1_21_RULESET, PhysicsEffect, TickInput,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockGeometry, WorldKnowledge
from tests.observation_v3_fixtures import valid_snapshot_v3


def main() -> None:
    frame = NavigationObservationAdapter().ingest(valid_snapshot_v3(sequence=7))
    assumptions = dict(
        jumping_cooldown_ticks=0, movement_speed_attribute=0.1,
        step_height_blocks=0.6, gravity_attribute=0.08,
        jump_strength_attribute=0.42,
    )
    base = build_physics_state(frame, JAVA_1_21_RULESET, assumptions).require_state()
    base = replace(
        base, position=(0.5, 64.0, 0.5),
        velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
        on_ground=True, vertical_collision=True, game_mode="survival",
        flying=False, allow_flying=False, status_effects=(),
    )

    def floor(material: str) -> PhysicsWorldView:
        world = WorldKnowledge(frame.session)
        world.confirm_air(frame.body.stamp, tuple(
            (x, y, z)
            for x in range(-3, 4) for y in range(62, 68) for z in range(-3, 4)
        ))
        world.observe_blocks(frame.body.stamp, {
            (x, 63, z): BlockGeometry.full_cube(material)
            for x in range(-3, 4) for z in range(-3, 4)
        })
        return PhysicsWorldView(world.view(), JAVA_1_21_RULESET)

    cases = (
        ("stone floor, no effects (baseline)", base, "minecraft:stone"),
        ("absorption effect (golden apple hearts)", replace(
            base, status_effects=(PhysicsEffect("minecraft:absorption", 0, 2400),),
        ), "minecraft:stone"),
        ("regeneration effect", replace(
            base, status_effects=(PhysicsEffect("minecraft:regeneration", 1, 100),),
        ), "minecraft:stone"),
        ("eating / using an item", replace(base, is_using_item=True), "minecraft:stone"),
        ("cobblestone floor", base, "minecraft:cobblestone"),
        ("sand floor", base, "minecraft:sand"),
        ("deepslate floor", base, "minecraft:deepslate"),
    )
    for label, state, material in cases:
        result = step(
            state, TickInput(1, 0, False, False, False, 0),
            floor(material), JAVA_1_21_RULESET,
        )
        print(f"{label:42s} -> {result.status.value:12s} "
              f"{result.unsupported_reasons}")


if __name__ == "__main__":
    main()
