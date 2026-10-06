"""Controlled descent facts; the coordinator still owns landing and reanchoring."""
import math
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route import ControlledDropSegment
from mc2p.motion_nav.motion_risk import conservative_plain_fall_damage_points
from mc2p.motion_nav.motion_solver import MotionSolveKind
from mc2p.motion_nav.actions.contracts import ActionSpec, BodyCommitment, ControllerFamily, EntryObservation, StopHold
from mc2p.motion_nav.actions.existing import air_controller, surface_completed, surface_geometry


def expected_damage_points(action):
    return conservative_plain_fall_damage_points(max(
        0.0, action.start_surface.position[1] - action.end_surface.position[1]))


def damage_committed(action, frame, *, force=False):
    return force or not frame.body.is_on_ground


def solve_geometry(action, anchor):
    return surface_geometry(action, anchor, 1.0, 80)


def entry_observation(action, frame):
    height = action.start_surface.position[1] - action.end_surface.position[1]
    large = height > 1.0 + 1.0e-6
    recheck = can_acquire = False
    if frame is not None and frame.body.is_on_ground:
        x, y, z = frame.body.position
        recheck = abs(y - action.start_surface.position[1]) <= .25
        region = action.start_surface.region
        can_acquire = (large and abs(y - action.start_surface.position[1]) <= .10
                       and region.min_x + .05 <= x <= region.max_x - .05
                       and region.min_z + .05 <= z <= region.max_z - .05)
    landing_cell = (action.end_surface.node_id.column_x, math.floor(action.end_surface.position[1]),
                    action.end_surface.node_id.column_z)
    return EntryObservation(landing_cell, action.dependencies, action.entry_window,
                            recheck, can_acquire, large)


def precondition(route, action_index, frame, *, task_id, edge_probe=None, acquisition_grant=None):
    from mc2p.motion_nav.action_preconditions import (
        ActionPreconditionResult, ActionPreconditionStatus as Status,
        ActionPreconditionReason as Reason, AcquisitionSpec, _acquisition_id,
        DIRECT_DROP_SUPPORT_MAX_AGE_TICKS,
    )
    from mc2p.motion_nav.geometry import QueryStatus, query_support
    from mc2p.motion_nav.route_admission import direct_drop_visual_evidence_sufficient
    from mc2p.motion_nav.world_model import CellKnowledge
    action = route.action_route.actions[action_index]
    entry = entry_observation(action, frame)
    if not entry.needs_acquisition_before_solve:
        return ActionPreconditionResult(Status.READY, Reason.READY)
    current, target = frame.body.position, action.end_surface.position
    body = frame.body.body_box.moved(target[0] - current[0], target[1] - current[1], target[2] - current[2])
    support = query_support(body, frame.world)
    if support.status is QueryStatus.NEEDS_INFORMATION:
        return ActionPreconditionResult(Status.NEEDS_INFORMATION, Reason.LANDING_SUPPORT_INFORMATION_REQUIRED,
                                        missing_cells=support.missing_cells)
    if support.status is QueryStatus.UNSUPPORTED:
        return ActionPreconditionResult(Status.REJECTED, Reason.LANDING_SUPPORT_UNSUPPORTED)
    if support.status is not QueryStatus.FEASIBLE or support.support_fraction <= 0.0:
        return ActionPreconditionResult(Status.REJECTED, Reason.LANDING_SUPPORT_MISSING)
    stale_support_cells = []
    for position in support.dependencies:
        support_fact = frame.world.cell(position)
        if (support_fact.knowledge is CellKnowledge.BLOCK and support_fact.stamp is not None
                and frame.body.sequence_id - support_fact.stamp.sequence_id
                    > DIRECT_DROP_SUPPORT_MAX_AGE_TICKS):
            stale_support_cells.append(position)
    stale_support = tuple(sorted(stale_support_cells))
    fact = frame.world.cell(entry.landing_cell)
    if (fact.knowledge is CellKnowledge.BLOCK
            or acquisition_grant is not None and acquisition_grant.applies(route, action_index, frame)
            or direct_drop_visual_evidence_sufficient(frame, entry.landing_cell, edge_probe=edge_probe)):
        if stale_support:
            return ActionPreconditionResult(Status.NEEDS_INFORMATION, Reason.LANDING_SUPPORT_INFORMATION_REQUIRED,
                                            missing_cells=stale_support)
        return ActionPreconditionResult(Status.READY, Reason.READY)
    dependencies = tuple(sorted(set(action.dependencies) | set(support.dependencies) | {entry.landing_cell}))
    acquisition = AcquisitionSpec(_acquisition_id(route, action_index, entry.landing_cell),
        task_id, route.goal_id, route.goal_revision, route.route_id, route.route_revision,
        action_index, entry.landing_cell, route.world_session, frame.world.geometry_revision, dependencies)
    return ActionPreconditionResult(Status.NEEDS_ACQUISITION, Reason.LANDING_LOWER_EVIDENCE_REQUIRED,
                                    acquisition, (entry.landing_cell,))


CONTROLLED_DROP_SPEC = ActionSpec(
    segment_type=ControlledDropSegment, body_commitment=BodyCommitment.TRANSITION,
    requires_verified_motion=True, expected_damage_points=expected_damage_points,
    stop_hold=StopHold(True, MovementV1(sneak=True), MovementV1(sneak=True)),
    needs_background_solving=True, solve_kind=MotionSolveKind.CONTROLLED_DROP,
    solve_geometry=solve_geometry, controller_family=ControllerFamily.AIR,
    controller_factory=air_controller, completed=surface_completed,
    entry_observation=entry_observation, precondition=precondition,
    tracks_damage=True, damage_committed=damage_committed,
)
