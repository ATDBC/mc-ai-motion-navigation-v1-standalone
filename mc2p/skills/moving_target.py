"""Pure stand-off goal updates for one bounded moving target."""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from mc2p.contracts.common import (
    ContractViolation, require_finite, require_identifier,
    require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.movement_transition import GoalState
from mc2p.skills.engagement_memory import TargetPositionFactV1
from mc2p.skills.fixed_melee import (
    COMBAT_GOAL_RADIUS_BLOCKS,
    COMBAT_STAND_DISTANCE_BLOCKS,
    MAX_COARSE_ATTACK_DISTANCE_BLOCKS,
    combat_standoff_goal_state,
)


TARGET_MOVEMENT_THRESHOLD_BLOCKS = .75
COMBAT_ATTACK_MARGIN_BLOCKS = .05


@dataclass(frozen=True, slots=True)
class MovingGoalDecisionV1:
    goal_state: GoalState
    stand_position: Vec3V0
    revision: int
    changed: bool
    reason: str
    adopted_target_position: Vec3V0
    support_region: tuple[int, int, int]
    observation_sequence_id: int
    schema_version: str = field(default="mc2p.moving-goal-decision.v1", init=False)

    def __post_init__(self) -> None:
        if type(self.goal_state) is not GoalState:
            raise ContractViolation("moving goal requires GoalState")
        if type(self.stand_position) is not Vec3V0:
            raise ContractViolation("moving goal stand position is invalid")
        require_nonnegative_int(self.revision, "moving goal revision")
        if type(self.changed) is not bool or type(self.reason) is not str or not self.reason:
            raise ContractViolation("moving goal change metadata is invalid")
        if type(self.adopted_target_position) is not Vec3V0:
            raise ContractViolation("moving goal target position is invalid")
        if (type(self.support_region) is not tuple or len(self.support_region) != 3
                or any(type(value) is not int for value in self.support_region)):
            raise ContractViolation("moving goal support region is invalid")
        require_nonnegative_int(self.observation_sequence_id, "moving goal observation sequence")


def _support_region(position: Vec3V0) -> tuple[int, int, int]:
    return math.floor(position.x), math.floor(position.y) - 1, math.floor(position.z)


def decide_moving_goal(
    fact: TargetPositionFactV1,
    self_position: Vec3V0,
    scope_id: str,
    deadline_ns: int,
    *,
    previous: MovingGoalDecisionV1 | None = None,
    old_endpoint_attack_valid: bool = True,
    route_connectable: bool = True,
    movement_threshold_blocks: float = TARGET_MOVEMENT_THRESHOLD_BLOCKS,
) -> MovingGoalDecisionV1:
    if type(fact) is not TargetPositionFactV1 or type(self_position) is not Vec3V0:
        raise ContractViolation("moving goal requires typed position facts")
    require_identifier(scope_id, "moving goal scope")
    require_nonnegative_int(deadline_ns, "moving goal deadline")
    if previous is not None and type(previous) is not MovingGoalDecisionV1:
        raise ContractViolation("previous moving goal is invalid")
    if type(old_endpoint_attack_valid) is not bool or type(route_connectable) is not bool:
        raise ContractViolation("moving goal route facts must be boolean")
    require_finite(movement_threshold_blocks, "moving target threshold")
    if movement_threshold_blocks <= 0:
        raise ContractViolation("moving target threshold must be positive")

    target_position = Vec3V0(
        self_position.x + fact.relative_position.x,
        self_position.y + fact.relative_position.y,
        self_position.z + fact.relative_position.z,
    )
    stand, goal = combat_standoff_goal_state(
        self_position, fact.relative_position,
    )
    support = _support_region(target_position)
    reason = "initial_goal"
    changed = previous is None
    if previous is not None:
        endpoint_distance = math.hypot(
            target_position.x - previous.stand_position.x,
            target_position.z - previous.stand_position.z,
        )
        old_endpoint_attack_valid = bool(
            old_endpoint_attack_valid
            and endpoint_distance + math.sqrt(2.0) * COMBAT_GOAL_RADIUS_BLOCKS
                <= MAX_COARSE_ATTACK_DISTANCE_BLOCKS - COMBAT_ATTACK_MARGIN_BLOCKS
        )
        movement = math.hypot(
            target_position.x - previous.adopted_target_position.x,
            target_position.z - previous.adopted_target_position.z,
        )
        if movement > movement_threshold_blocks:
            changed, reason = True, "target_moved"
        elif support != previous.support_region:
            changed, reason = True, "support_region_changed"
        elif not old_endpoint_attack_valid:
            changed, reason = True, "old_endpoint_invalid"
        elif not route_connectable:
            changed, reason = True, "route_unconnectable"
        else:
            return MovingGoalDecisionV1(
                previous.goal_state, previous.stand_position,
                previous.revision, False, "within_reuse_bounds",
                previous.adopted_target_position, previous.support_region,
                fact.observation_sequence_id,
            )

    revision = 1 if previous is None else previous.revision + 1
    return MovingGoalDecisionV1(
        goal, stand, revision, changed, reason, target_position, support,
        fact.observation_sequence_id,
    )
