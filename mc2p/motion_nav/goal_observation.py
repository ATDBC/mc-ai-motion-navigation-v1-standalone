"""Check a goal against facts observed from the current body and world."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.motion_nav.geometry import QueryStatus, query_support
from mc2p.motion_nav.ground_modes import observed_ground_mode
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, ResourceState
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos


class ObservedGoalStatus(StrEnum):
    SATISFIED = "satisfied"
    HEADING_ONLY = "heading_only"
    NOT_SATISFIED = "not_satisfied"
    NEEDS_INFORMATION = "needs_information"
    RESOURCE_UNOBSERVABLE = "resource_unobservable"


@dataclass(frozen=True, slots=True)
class ObservedGoal:
    status: ObservedGoalStatus
    missing_cells: tuple[BlockPos, ...] = ()
    heading_delta_degrees: float = 0.0


def evaluate_observed_goal(
    frame: NavigationFrame, goal: GoalState, risk_policy_id: str,
) -> ObservedGoal:
    """The body reports food; no planned resource balance counts as observation."""
    if any(name != "food_points" for name, _ in goal.minimum_resources.values):
        return ObservedGoal(ObservedGoalStatus.RESOURCE_UNOBSERVABLE)
    support_query = query_support(frame.body.body_box, frame.world)
    if support_query.status is QueryStatus.NEEDS_INFORMATION:
        return ObservedGoal(
            ObservedGoalStatus.NEEDS_INFORMATION, support_query.missing_cells,
        )
    support = (GoalSupport.SOLID if (
        support_query.status is QueryStatus.FEASIBLE and frame.body.is_on_ground
    ) else None)
    if support is None:
        return ObservedGoal(ObservedGoalStatus.NOT_SATISFIED)
    speed = math.hypot(*(
        frame.body.velocity_blocks_per_second[index] for index in (0, 2)
    ))
    facts = dict(
        support=support,
        mode=observed_ground_mode(frame.body), pose=frame.body.pose,
        speed_blocks_per_second=speed,
        resources=ResourceState((("food_points", float(frame.body.food_points)),)),
        applied_risk_policy_id=risk_policy_id,
    )
    # A completion decision is followed by at least one neutral input sample.
    # Require the body's current horizontal velocity to remain inside the goal
    # through that tick; otherwise a result can become complete at the inner
    # edge and drift outside before the release is observed.
    projected_position = (
        frame.body.position[0]
        + frame.body.velocity_blocks_per_second[0] / 20.0,
        frame.body.position[1],
        frame.body.position[2]
        + frame.body.velocity_blocks_per_second[2] / 20.0,
    )

    def accepts(yaw_radians: float) -> bool:
        return all(
            goal.accepts(
                **facts, position=position, yaw_radians=yaw_radians,
            )
            for position in (frame.body.position, projected_position)
        )

    if accepts(frame.body.yaw_radians):
        return ObservedGoal(ObservedGoalStatus.SATISFIED)
    if (goal.required_yaw_radians is not None
            and accepts(goal.required_yaw_radians)):
        delta = math.atan2(
            math.sin(goal.required_yaw_radians - frame.body.yaw_radians),
            math.cos(goal.required_yaw_radians - frame.body.yaw_radians),
        )
        return ObservedGoal(
            ObservedGoalStatus.HEADING_ONLY,
            heading_delta_degrees=math.degrees(delta),
        )
    return ObservedGoal(ObservedGoalStatus.NOT_SATISFIED)
