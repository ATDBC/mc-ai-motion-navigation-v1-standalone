"""Small state owners used by the navigation coordinator.

They deliberately contain no policy.  Their job is to make it impossible for
the session, planner and information controller to each keep a different copy
of the same long-lived fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_preconditions import AcquisitionGrant
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.movement_transition import GoalState
from mc2p.motion_nav.world_model import BlockPos


@dataclass(frozen=True, slots=True)
class PendingGoalRevision:
    """One validated goal revision waiting for information or body release."""

    goal_id: str
    goal_revision: int
    goal_state: GoalState
    damage_budget: TaskDamageBudget

    def __post_init__(self) -> None:
        if not isinstance(self.goal_id, str) or not self.goal_id:
            raise ContractViolation("pending goal id is required")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("pending goal revision is invalid")
        if type(self.goal_state) is not GoalState:
            raise ContractViolation("pending goal state must be typed")
        if type(self.damage_budget) is not TaskDamageBudget:
            raise ContractViolation("pending goal damage budget must be typed")


@dataclass(slots=True)
class GoalRequestLedger:
    """Own the one planning request currently visible to the planner."""

    request: Any | None = None

    def accept(self, request: Any | None) -> None:
        self.request = request

    def advance(self, session_id: str, **changes: Any) -> Any:
        """Create the sole next request generation for this task."""
        if self.request is None:
            raise ContractViolation("goal request ledger has no active request")
        if not isinstance(session_id, str) or not session_id:
            raise ContractViolation("goal request session id is required")
        sequence = self.request.sequence + 1
        advanced = replace(
            self.request,
            sequence=sequence,
            request_id=f"{session_id}-request-{sequence}",
            **changes,
        )
        self.request = advanced
        return advanced


@dataclass(slots=True)
class PlanningPipelineState:
    """Own snapshot construction and request-local world changes."""

    builder: Any | None = None
    snapshot: Any | None = None
    snapshot_request_id: str | None = None
    changed_cells: set[BlockPos] = field(default_factory=set)
    replacement_failure: "ReplacementPlanningFailure | None" = None
    attempt_started_movement_tick: int | None = None
    attempt_started_monotonic_ns: int | None = None
    attempt_deadline_monotonic_ns: int | None = None
    submitted_request_id: str | None = None
    submitted_movement_tick: int | None = None
    submitted_monotonic_ns: int | None = None
    result_deadline_monotonic_ns: int | None = None

    def clear_submission(self) -> None:
        self.submitted_request_id = None
        self.submitted_movement_tick = None
        self.submitted_monotonic_ns = None
        self.result_deadline_monotonic_ns = None

    def clear(self) -> None:
        self.builder = None
        self.snapshot = None
        self.snapshot_request_id = None
        self.changed_cells.clear()
        self.replacement_failure = None
        self.attempt_started_movement_tick = None
        self.attempt_started_monotonic_ns = None
        self.attempt_deadline_monotonic_ns = None
        self.clear_submission()


@dataclass(frozen=True, slots=True)
class ReplacementPlanningFailure:
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise ContractViolation("replacement planning failure needs a reason")


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
