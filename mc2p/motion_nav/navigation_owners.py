"""Small state owners used by the navigation coordinator.

They deliberately contain no policy.  Their job is to make it impossible for
the session, planner and information controller to each keep a different copy
of the same long-lived fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mc2p.motion_nav.action_preconditions import AcquisitionGrant
from mc2p.motion_nav.world_model import BlockPos


@dataclass(slots=True)
class GoalRequestLedger:
    """Own the current planning request and a not-yet-activated goal."""

    request: Any | None = None
    pending_goal: Any | None = None


@dataclass(slots=True)
class PlanningPipelineState:
    """Own snapshot construction and request-local world changes."""

    builder: Any | None = None
    snapshot: Any | None = None
    snapshot_request_id: str | None = None
    changed_cells: set[BlockPos] = field(default_factory=set)

    def clear(self) -> None:
        self.builder = None
        self.snapshot = None
        self.snapshot_request_id = None
        self.changed_cells.clear()


@dataclass(slots=True)
class InformationAcquisitionState:
    """Own the facts and wait state for the one active information need."""

    missing_cells: tuple[BlockPos, ...] = ()
    residual_missing_cells: tuple[BlockPos, ...] = ()
    statuses: dict[BlockPos, str] = field(default_factory=dict)
    lower_required: set[BlockPos] = field(default_factory=set)
    completed_grant: AcquisitionGrant | None = None
    wait_frames: int = 0

    def clear_request(self) -> None:
        self.missing_cells = ()
        self.statuses.clear()
        self.lower_required.clear()
        self.wait_frames = 0
