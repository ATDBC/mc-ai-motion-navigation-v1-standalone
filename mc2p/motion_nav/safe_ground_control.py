"""Calculator proof for ordinary-ground protection and released-input tails."""
from __future__ import annotations

import math

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.runtime_adapter import NavigationFrame


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
