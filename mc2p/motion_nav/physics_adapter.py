"""Build complete B09-R simulation state from formal observations and known world facts."""
from __future__ import annotations

from typing import Mapping

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_types import (
    PhysicsEffect, PhysicsRuleset, PhysicsState, StateBuildResult, StateBuildStatus,
    WorldShapeQuery,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge, WorldSessionId, WorldView


_REQUIRED_ASSUMPTIONS = (
    "jumping_cooldown_ticks",
    "movement_speed_attribute",
    "step_height_blocks",
    "gravity_attribute",
    "jump_strength_attribute",
)


def build_physics_state(frame: NavigationFrame, ruleset: PhysicsRuleset,
                        explicit_assumptions: Mapping[str, object]) -> StateBuildResult:
    if type(frame) is not NavigationFrame or type(ruleset) is not PhysicsRuleset:
        raise ContractViolation("state construction requires a navigation frame and ruleset")
    if not isinstance(explicit_assumptions, Mapping):
        raise ContractViolation("explicit assumptions must be a mapping")
    assumptions = dict(explicit_assumptions)
    body = frame.body
    session_conflicts = []
    if body.session != frame.session:
        session_conflicts.append("body_session_mismatch")
    if body.stamp.session != frame.session:
        session_conflicts.append("body_stamp_session_mismatch")
    if frame.world.session != frame.session:
        session_conflicts.append("world_session_mismatch")
    if session_conflicts:
        return StateBuildResult(
            StateBuildStatus.INVALID_INPUT,
            conflicts=tuple(sorted(session_conflicts)),
        )
    observed = {
        "game_mode": body.game_mode,
        "pose": body.pose,
        "food_points": body.food_points,
        "saturation_points": body.saturation_points,
    }
    unknown = tuple(sorted(set(assumptions) - set(_REQUIRED_ASSUMPTIONS) - set(observed)))
    conflicts = tuple(sorted(
        key for key in assumptions if key in observed and assumptions[key] != observed[key]
    ))
    if unknown or conflicts:
        return StateBuildResult(StateBuildStatus.INVALID_INPUT,
                                conflicts=tuple(sorted(set(unknown + conflicts))))
    missing = tuple(sorted(key for key in _REQUIRED_ASSUMPTIONS if key not in assumptions))
    if missing:
        return StateBuildResult(StateBuildStatus.NEEDS_STATE, missing_fields=missing)
    effects = tuple(sorted(
        (PhysicsEffect(effect.effect_id, effect.amplifier, effect.duration_ticks)
         for effect in body.status_effects),
        key=lambda value: (value.effect_id, value.amplifier, value.duration_ticks),
    ))
    width = body.body_box.max_x - body.body_box.min_x
    height = body.body_box.max_y - body.body_box.min_y
    state = PhysicsState(
        ruleset_id=ruleset.ruleset_id, state_schema=ruleset.state_schema,
        session=frame.session, movement_tick_id=0, position=body.position,
        velocity_blocks_per_tick=tuple(
            value / 20.0 for value in body.velocity_blocks_per_second),
        yaw_radians=body.yaw_radians, pitch_radians=body.pitch_radians,
        pose=body.pose, body_width=width, body_height=height,
        on_ground=body.is_on_ground,
        horizontal_collision=body.horizontal_collision,
        vertical_collision=body.vertical_collision, sprinting=body.is_sprinting,
        sneaking=body.is_sneaking,
        jumping_cooldown_ticks=assumptions["jumping_cooldown_ticks"],
        fall_distance_blocks=body.fall_distance_blocks,
        movement_speed_attribute=assumptions["movement_speed_attribute"],
        step_height_blocks=assumptions["step_height_blocks"],
        gravity_attribute=assumptions["gravity_attribute"],
        jump_strength_attribute=assumptions["jump_strength_attribute"],
        food_points=body.food_points, saturation_points=body.saturation_points,
        game_mode=body.game_mode, status_effects=effects,
        swimming=body.is_swimming,
        submerged_in_water=body.is_submerged_in_water,
        climbing=body.is_climbing, fall_flying=body.is_fall_flying,
        flying=body.is_flying, allow_flying=body.allow_flying,
        is_using_item=body.is_using_item,
    )
    provenance = tuple(sorted(
        [(name, "formal_observation") for name in observed]
        + [(name, "explicit_assumption") for name in _REQUIRED_ASSUMPTIONS]
    ))
    return StateBuildResult(StateBuildStatus.READY, state=state, provenance=provenance)


class PhysicsWorldView:
    """Narrow, read-only shape access. It performs no observation or world mutation."""

    def __init__(self, world: WorldView, ruleset: PhysicsRuleset,
                 *, expected_session: WorldSessionId | None = None) -> None:
        if type(world) is not WorldView or type(ruleset) is not PhysicsRuleset:
            raise ContractViolation("physics world requires a world view and ruleset")
        if expected_session is not None and world.session != expected_session:
            raise ContractViolation("physics world belongs to another session")
        self._world = world
        self.ruleset = ruleset

    @property
    def session(self) -> WorldSessionId:
        return self._world.session

    def cell(self, position: BlockPos):
        return self._world.cell(position)

    def shapes(self, positions: tuple[BlockPos, ...]) -> WorldShapeQuery:
        if type(positions) is not tuple:
            raise ContractViolation("shape positions must be immutable")
        dependencies = tuple(sorted(set(positions)))
        boxes = []
        missing = []
        unsupported = []
        for position in dependencies:
            fact = self._world.cell(position)
            if fact.knowledge is CellKnowledge.UNKNOWN:
                missing.append(position)
            elif fact.knowledge is CellKnowledge.BLOCK:
                assert fact.block is not None
                if fact.block.fluid or fact.block.collision_kind == "unsupported":
                    unsupported.append(position)
                else:
                    boxes.extend(fact.block.world_boxes(position))
        return WorldShapeQuery(tuple(boxes), dependencies, tuple(missing), tuple(unsupported))
