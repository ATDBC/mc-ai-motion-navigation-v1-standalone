"""Immediate checks that must hold when a route action is about to start."""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.actions.registry import action_spec
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe
from mc2p.motion_nav.route_admission import ActiveRoute
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge


from mc2p.motion_nav.action_requirements import (
    ActionPreconditionStatus, ActionPreconditionReason, ActionPreconditionResult,
    AcquisitionSpec, AcquisitionGrant, acquisition_id,
)
from mc2p.motion_nav.landing_evidence import DIRECT_DROP_SUPPORT_MAX_AGE_TICKS


def check_action_precondition(
    route: ActiveRoute,
    action_index: int,
    frame: NavigationFrame,
    *,
    task_id: str,
    edge_probe: LandingEdgeProbe | None = None,
    acquisition_grant: AcquisitionGrant | None = None,
) -> ActionPreconditionResult:
    """Check facts that are meaningful only at the current action boundary."""
    if type(route) is not ActiveRoute or type(frame) is not NavigationFrame:
        raise ContractViolation("action precondition requires route and frame")
    require_identifier(task_id, "action precondition task id")
    if (type(action_index) is not int
            or not 0 <= action_index < len(route.action_route.actions)):
        return ActionPreconditionResult(
            ActionPreconditionStatus.REJECTED,
            ActionPreconditionReason.ACTION_INDEX_INVALID,
        )
    return action_spec(route.action_route.actions[action_index]).precondition(
        route, action_index, frame, task_id=task_id, edge_probe=edge_probe,
        acquisition_grant=acquisition_grant,
    )


@dataclass(frozen=True, slots=True)
class ActionBoundarySelection:
    action_index: int


def select_current_boundary(route, current_index, requested_index, frame, *,
                            started, through_grounded_departure):
    """Select a boundary from immutable snapshots without starting acquisition."""
    index = current_index if requested_index is None else requested_index
    if type(index) is not int or not 0 <= index < len(route.action_route.actions):
        return None
    if index == current_index and started:
        entry = action_spec(route.action_route.actions[index]).entry_observation(
            route.action_route.actions[index], frame)
        if not through_grounded_departure or entry is None or not entry.recheck_started_action:
            return None
    return ActionBoundarySelection(index)


def select_upcoming_boundary(route, current_index, frame, *, probe_binding, grant):
    """Keep an existing acquisition binding or choose an observed next entry."""
    index = current_index + 1
    if not 0 <= index < len(route.action_route.actions):
        return None
    action = route.action_route.actions[index]
    entry = action_spec(action).entry_observation(action, frame)
    if entry is None or not entry.needs_acquisition_before_solve:
        return None
    binding = (route.route_id, route.route_revision, index)
    grant_binding = None if grant is None else (grant.route_id, grant.route_revision, grant.action_index)
    if probe_binding == binding or grant_binding == binding or entry.can_begin_acquisition:
        return ActionBoundarySelection(index)
    return None
