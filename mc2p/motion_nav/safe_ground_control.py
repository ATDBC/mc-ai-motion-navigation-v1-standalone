"""Calculator proof for ordinary-ground protection and released-input tails."""
from __future__ import annotations

import math
from dataclasses import dataclass

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep, unknown_shape_owner_is_fully_covered
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.block_motion_traits import unsupported_motion_cells
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, PhysicsState, WorldShapeQuery
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge, WorldQueryCache


@dataclass(frozen=True, slots=True)
class VerifiedGroundRouteCandidate:
    status: QueryStatus
    trajectory: tuple[PhysicsState, ...]
    tracking_end: PhysicsState | None
    minimum_support: float
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...]
    physics_steps: int
    reason: str
    sneak_edge_clipped: bool = False
    support_boundary_rejected: bool = False


class CachedGroundPhysicsWorld(PhysicsWorldView):
    """Calculator shapes and material reads share the current control-frame cache."""
    def __init__(self, frame: NavigationFrame, cache: WorldQueryCache):
        super().__init__(frame.world, JAVA_1_21_RULESET)
        self.cache = cache
        # One verifier frame asks the calculator about the same swept cell
        # sets for many members of the closed 18-candidate family.  The world
        # view is immutable, so the complete typed query can be shared.
        self._shape_cache: dict[
            tuple[BlockPos, ...], WorldShapeQuery
        ] = {}

    def cell(self, position):
        return self.cache.cell(position)

    def shapes(self, positions):
        requested = tuple(sorted(set(positions)))
        cached = self._shape_cache.get(requested)
        if cached is not None:
            return cached
        dependencies = set(requested)
        boxes, missing, unsupported = [], [], []
        for position in sorted(dependencies):
            fact = self.cache.cell(position)
            if fact.knowledge is CellKnowledge.UNKNOWN:
                above = (position[0], position[1] + 1, position[2])
                dependencies.add(above)
                if not unknown_shape_owner_is_fully_covered(
                        self.cache.world, position, query_cache=self.cache):
                    missing.append(position)
            elif fact.block is not None:
                if fact.block.fluid or fact.block.collision_kind == "unsupported":
                    unsupported.append(position)
                else:
                    boxes.extend(self.cache.collision_boxes(position))
        result = WorldShapeQuery(
            tuple(boxes), tuple(sorted(dependencies)),
            tuple(missing), tuple(unsupported),
        )
        self._shape_cache[requested] = result
        return result


def ground_route_state_matches(frame: NavigationFrame, state: PhysicsState | None,
                               *, edge_guard: bool = False) -> bool:
    """Only the complete state of this observation can authorize contact replay.

    Body health has no PhysicsState counterpart and is not a projection input;
    it remains outside this state comparison.
    """
    return (type(state) is PhysicsState
            and state.session == frame.session
            and state.ruleset_id == JAVA_1_21_RULESET.ruleset_id
            and state.state_schema == JAVA_1_21_RULESET.state_schema
            and state.position == frame.body.position
            and all(abs(a-b) <= 1.e-9 for a, b in
                    zip(state.body_box.as_tuple(), frame.body.body_box.as_tuple()))
            and state.yaw_radians == frame.body.yaw_radians
            and state.pitch_radians == frame.body.pitch_radians
            and state.pose == frame.body.pose
            and state.pose in (("standing", "crouching") if edge_guard else ("standing",))
            and state.on_ground and frame.body.is_on_ground
            and state.horizontal_collision == frame.body.horizontal_collision
            and state.vertical_collision == frame.body.vertical_collision
            and state.sprinting == frame.body.is_sprinting
            and state.sneaking == frame.body.is_sneaking
            and state.swimming == frame.body.is_swimming
            and state.submerged_in_water == frame.body.is_submerged_in_water
            and state.climbing == frame.body.is_climbing
            and state.fall_flying == frame.body.is_fall_flying
            and state.flying == frame.body.is_flying
            and state.allow_flying == frame.body.allow_flying
            and state.is_using_item == frame.body.is_using_item
            and state.fall_distance_blocks == frame.body.fall_distance_blocks
            and state.game_mode == frame.body.game_mode
            and state.food_points == frame.body.food_points
            and state.saturation_points == frame.body.saturation_points
            and tuple((e.effect_id, e.amplifier, e.duration_ticks) for e in state.status_effects)
                == tuple((e.effect_id, e.amplifier, e.duration_ticks) for e in frame.body.status_effects)
            and (frame.body.movement_tick_id is None
                 or state.movement_tick_id == frame.body.movement_tick_id)
            and all(abs(state.velocity_blocks_per_tick[i] * 20
                        - frame.body.velocity_blocks_per_second[i]) <= 1.e-9 for i in range(3))
            and not any((state.sneaking and not edge_guard, state.sprinting, state.swimming,
                         state.submerged_in_water, state.climbing, state.fall_flying,
                         state.flying)))


def verified_ground_route_candidate(
    frame: NavigationFrame, state: PhysicsState | None, command: MovementV1,
    *, control_ticks: int, tail_ticks: int, minimum_support: float,
    profile: GroundMotionProfile, query_cache: WorldQueryCache,
    edge_guard: bool = False,
) -> VerifiedGroundRouteCandidate:
    """Replay the complete lease and neutral drift; preserve collision positions."""
    query_cache.validate_for(frame.world)
    trajectory: list[PhysicsState] = []
    dependencies: set[BlockPos] = set()
    lowest_support = 1.0
    tracking_end = state if control_ticks == 0 and state is not None else None
    steps = 0
    clipped = False

    def finish(status, reason, missing=(), *, support_boundary=False):
        return VerifiedGroundRouteCandidate(status, tuple(trajectory), tracking_end,
                                            lowest_support, tuple(sorted(dependencies)),
                                            tuple(sorted(set(missing))), steps, reason,
                                            clipped, support_boundary)

    if not ground_route_state_matches(frame, state, edge_guard=edge_guard):
        return finish(QueryStatus.UNSUPPORTED, "state_unavailable_or_mismatched")
    if (command.jump or command.sprint or (command.sneak and not edge_guard)
            or profile.motion_catalog is None or profile.ground_model_id is None):
        return finish(QueryStatus.UNSUPPORTED, "ordinary_model_required")
    assert state is not None
    clearance = sweep(state.body_box, (0., 0., 0.), frame.world, query_cache=query_cache)
    dependencies.update(clearance.dependencies)
    if clearance.status is not QueryStatus.FEASIBLE:
        return finish(clearance.status, "initial_clearance_rejected", clearance.missing_cells)
    current = state
    world = CachedGroundPhysicsWorld(frame, query_cache)
    trajectory.append(state)
    for index in range(control_ticks + tail_ticks):
        if (index >= control_ticks
                and math.hypot(current.velocity_blocks_per_tick[0],
                               current.velocity_blocks_per_tick[2]) <= 1.e-9):
            return finish(QueryStatus.FEASIBLE, "verified")
        projected = project_movement_command(
            current, command if index < control_ticks else MovementV1(sneak=command.sneak))
        if projected.status is not ProjectionStatus.READY:
            return finish(QueryStatus.UNSUPPORTED, "command_projection_failed")
        steps += 1
        calculated = physics_step(current, projected.tick_input, world, JAVA_1_21_RULESET)
        dependencies.update(calculated.dependencies)
        if calculated.status is CalculationStatus.NEEDS_WORLD:
            return finish(QueryStatus.NEEDS_INFORMATION, "missing_world", calculated.missing_cells)
        if calculated.status is not CalculationStatus.OK:
            return finish(QueryStatus.UNSUPPORTED, "calculator_rejected")
        assert calculated.next_state is not None
        clipped = clipped or "sneak_edge_clipped" in calculated.events
        current = calculated.next_state
        trajectory.append(current)
        if index + 1 == control_ticks:
            tracking_end = current
        if (not current.on_ground or abs(current.position[1] - state.position[1]) > 1.e-9
                or current.pose not in (("standing", "crouching") if edge_guard else ("standing",))
                or current.fall_distance_blocks > 0):
            return finish(QueryStatus.BLOCKED, "vertical_motion_or_lost_ground",
                          support_boundary=not current.on_ground)
        support = query_support(current.body_box, frame.world, query_cache=query_cache)
        dependencies.update(support.dependencies)
        if unsupported_motion_cells(profile.motion_catalog, frame.world,
                                    tuple(sorted(dependencies)), profile.ground_model_id,
                                    query_cache=query_cache):
            return finish(QueryStatus.UNSUPPORTED, "material_outside_active_model")
        if support.status is not QueryStatus.FEASIBLE:
            return finish(support.status, "support_rejected", support.missing_cells,
                          support_boundary=support.status is QueryStatus.BLOCKED)
        lowest_support = min(lowest_support, support.support_fraction)
        if (not set(support.support_materials).issubset(profile.support_materials)
                or support.support_fraction < minimum_support):
            return finish(QueryStatus.BLOCKED, "insufficient_support", support_boundary=True)
    if math.hypot(current.velocity_blocks_per_tick[0], current.velocity_blocks_per_tick[2]) > 1.e-9:
        return finish(QueryStatus.BLOCKED, "stop_tail_did_not_settle")
    return finish(QueryStatus.FEASIBLE, "verified")


_RECOVERY_MOVEMENTS = tuple(
    MovementV1(forward=forward, strafe=strafe, sneak=True)
    for forward, strafe in (
        (-1, 0), (1, 0), (0, -1), (0, 1),
        (-1, -1), (-1, 1), (1, -1), (1, 1),
    )
)


def verified_ground_rollout(
    frame: NavigationFrame, state: PhysicsState | None,
    command: MovementV1, *, control_ticks: int, tail_ticks: int,
    minimum_support: float,
) -> PhysicsState | None:
    """Prove each sampled tick; unknown material or support blocks release."""
    if (state is None or state.session != frame.session
            or state.ruleset_id != JAVA_1_21_RULESET.ruleset_id
            or math.dist(state.position, frame.body.position) > .02
            or not state.on_ground):
        return None
    world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
    current = state
    for index in range(control_ticks + tail_ticks):
        movement = command if index < control_ticks else MovementV1(
            sneak=command.sneak if control_ticks else False,
        )
        projected = project_movement_command(current, movement)
        if projected.status is not ProjectionStatus.READY:
            return None
        assert projected.tick_input is not None
        result = physics_step(
            current, projected.tick_input, world, JAVA_1_21_RULESET,
        )
        if result.next_state is None or not result.next_state.on_ground:
            return None
        next_state = result.next_state
        delta = tuple(next_state.position[i] - current.position[i]
                      for i in range(3))
        support = query_support(next_state.body_box, frame.world)
        if (sweep(current.body_box, delta, frame.world).status
                is not QueryStatus.FEASIBLE
                or support.status is not QueryStatus.FEASIBLE
                or support.support_fraction < minimum_support):
            return None
        current = next_state
    return current


def verified_ground_recovery_movement(
    frame: NavigationFrame,
    state: PhysicsState | None,
) -> MovementV1 | None:
    """Choose one proved tick that increases support before releasing input."""
    current_support = query_support(frame.body.body_box, frame.world)
    if current_support.status is not QueryStatus.FEASIBLE:
        return None
    best_movement = None
    best_support = current_support.support_fraction
    for movement in _RECOVERY_MOVEMENTS:
        predicted = verified_ground_rollout(
            frame, state, movement,
            control_ticks=1,
            tail_ticks=8,
            minimum_support=1.0e-4,
        )
        if predicted is None:
            continue
        support = query_support(predicted.body_box, frame.world)
        if (support.status is QueryStatus.FEASIBLE
                and support.support_fraction > best_support + 1.0e-6):
            best_movement = movement
            best_support = support.support_fraction
    return best_movement


def verified_ground_target_movement(
    frame: NavigationFrame,
    state: PhysicsState | None,
    target_position: tuple[float, float, float],
) -> MovementV1 | None:
    """Choose one safe sneak tick whose released tail approaches a target."""
    if (type(target_position) is not tuple or len(target_position) != 3
            or any(type(value) not in (int, float)
                   or not math.isfinite(float(value))
                   for value in target_position)):
        return None
    current_distance = math.hypot(
        frame.body.position[0] - target_position[0],
        frame.body.position[2] - target_position[2],
    )
    best_movement = None
    best_distance = current_distance
    for movement in _RECOVERY_MOVEMENTS:
        predicted = verified_ground_rollout(
            frame, state, movement,
            control_ticks=1, tail_ticks=8,
            minimum_support=1.0e-4,
        )
        if predicted is None:
            continue
        distance = math.hypot(
            predicted.position[0] - target_position[0],
            predicted.position[2] - target_position[2],
        )
        if distance + 1.0e-6 < best_distance:
            best_movement = movement
            best_distance = distance
    return best_movement
