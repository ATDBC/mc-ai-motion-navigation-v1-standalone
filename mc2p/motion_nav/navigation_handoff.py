"""One owner for goal revisions that must wait for body release."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.body_control import (
    HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.navigation_owners import PendingGoalRevision
from mc2p.motion_nav.world_model import BlockPos
from mc2p.motion_nav.retry_ledger import RetryCause, RetryLedger, RetryVerdict
from mc2p.motion_nav.runtime_adapter import NavigationFrame


class HandoffDestination(StrEnum):
    REPLAN = "replan"
    WAIT_FOR_INFORMATION = "wait_for_information"
    FAIL = "fail"
    COMPLETE = "complete"
    CANCEL = "cancel"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class NavigationStopRequest:
    cause: StopCause
    reason: str
    goal_revision: int | None = None
    destination: HandoffDestination = HandoffDestination.REPLAN
    request_id: str | None = None
    retry_cause: RetryCause | None = None
    missing_cells: tuple[BlockPos, ...] = ()

    def __post_init__(self) -> None:
        if type(self.cause) is not StopCause:
            raise ContractViolation("navigation stop cause must be typed")
        require_identifier(self.reason, "navigation stop reason")
        if type(self.destination) is not HandoffDestination:
            raise ContractViolation("navigation stop destination must be typed")
        if (type(self.missing_cells) is not tuple
                or self.missing_cells != tuple(sorted(set(self.missing_cells)))):
            raise ContractViolation("navigation recovery missing cells must be sorted and unique")
        if self.request_id is not None:
            require_identifier(self.request_id, "navigation recovery request")
        if self.retry_cause is not None and type(self.retry_cause) is not RetryCause:
            raise ContractViolation("navigation recovery retry cause must be typed")
        if (self.goal_revision is not None
                and (type(self.goal_revision) is not int
                     or self.goal_revision < 0)):
            raise ContractViolation("navigation stop goal revision is invalid")


@dataclass(frozen=True, slots=True)
class NavigationHandoffResolution:
    destination: HandoffDestination
    reason: str
    pending_goal: PendingGoalRevision | None = None
    missing_cells: tuple[BlockPos, ...] = ()
    handoff: HandoffEvidence | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.destination) is not HandoffDestination:
            raise ContractViolation("navigation handoff destination must be typed")
        require_identifier(self.reason, "navigation handoff reason")
        if (self.pending_goal is not None
                and type(self.pending_goal) is not PendingGoalRevision):
            raise ContractViolation("navigation handoff goal must be typed")
        if (type(self.missing_cells) is not tuple
                or self.missing_cells != tuple(sorted(set(self.missing_cells)))):
            raise ContractViolation(
                "navigation handoff missing cells must be sorted and unique"
            )


class NavigationHandoffCoordinator:
    """Stages one goal revision and resolves it after verified body release."""

    def __init__(self) -> None:
        self._pending_goal: PendingGoalRevision | None = None
        self._stop_request: NavigationStopRequest | None = None

    @property
    def pending_goal(self) -> PendingGoalRevision | None:
        return self._pending_goal

    @property
    def stop_request(self) -> NavigationStopRequest | None:
        return self._stop_request

    @property
    def ending(self) -> bool:
        return self._stop_request is not None and self._stop_request.destination in {
            HandoffDestination.FAIL, HandoffDestination.COMPLETE,
            HandoffDestination.CANCEL, HandoffDestination.CLOSE,
        }

    def finish_ending(self) -> NavigationHandoffResolution:
        if not self.ending:
            raise ContractViolation("navigation handoff has no ending request")
        request = self._stop_request
        self._stop_request = None
        self._pending_goal = None
        return NavigationHandoffResolution(
            request.destination, request.reason, request_id=request.request_id,
        )

    def request_recovery(
        self, *, request_id: str, destination: HandoffDestination, reason: str,
        budget: RetryLedger | None, cause: StopCause = StopCause.MOTION_UNSOLVABLE,
        retry_cause: RetryCause = RetryCause.EXECUTION,
        missing_cells: tuple[BlockPos, ...] = (),
    ) -> bool:
        require_identifier(request_id, "navigation recovery request")
        require_identifier(reason, "navigation recovery reason")
        if type(destination) is not HandoffDestination or type(cause) is not StopCause:
            raise ContractViolation("navigation recovery requires typed purpose and cause")
        if (type(retry_cause) is not RetryCause or type(missing_cells) is not tuple
                or missing_cells != tuple(sorted(set(missing_cells)))):
            raise ContractViolation("navigation recovery facts are invalid")
        current = self._stop_request
        if self.ending or current is not None and current.request_id == request_id:
            return False
        if current is not None and destination in {
            HandoffDestination.REPLAN, HandoffDestination.WAIT_FOR_INFORMATION,
        }:
            return False
        registration_cause = None
        if destination is HandoffDestination.REPLAN:
            if type(budget) is not RetryLedger:
                raise ContractViolation("navigation recovery requires its task budget")
            registration = budget.record_failure(request_id, retry_cause)
            if not registration.first_seen:
                return False
            registration_cause = retry_cause
            if registration.verdict is not RetryVerdict.RETRY:
                destination, reason = HandoffDestination.FAIL, "replan_retry_exhausted"
        self._stop_request = NavigationStopRequest(
            cause, reason, destination=destination, request_id=request_id,
            retry_cause=registration_cause, missing_cells=missing_cells,
        )
        return True

    def advance(
        self, frame: NavigationFrame, *, handoff: HandoffEvidence,
        goal_ready: bool, start_ready: bool, missing_cells: tuple[BlockPos, ...],
        unavailable_reason: str,
    ) -> NavigationHandoffResolution | None:
        request = self._stop_request
        if request is None:
            return None
        if type(goal_ready) is not bool or type(start_ready) is not bool:
            raise ContractViolation("navigation recovery readiness must be typed")
        if (type(frame) is not NavigationFrame or type(handoff) is not HandoffEvidence
                or handoff.world_session != frame.session
                or handoff.observation_sequence_id != frame.body.sequence_id
                or handoff.disposition is not HandoffDisposition.QUIESCENT):
            return None
        destination, reason = request.destination, request.reason
        if destination is HandoffDestination.REPLAN:
            if not (goal_ready and start_ready):
                destination = HandoffDestination.WAIT_FOR_INFORMATION if missing_cells else HandoffDestination.FAIL
                reason = ("current_surface_requires_information" if missing_cells else unavailable_reason)
        resolution = NavigationHandoffResolution(
            destination, reason, self._pending_goal, missing_cells, handoff, request.request_id,
        )
        self._stop_request = None
        self._pending_goal = None
        return resolution

    def stage_goal(
        self,
        pending_goal: PendingGoalRevision,
        cause: StopCause,
        reason: str,
    ) -> None:
        if self.ending:
            raise ContractViolation("navigation task is already ending")
        self._set_newer_goal(pending_goal)
        self._stop_request = NavigationStopRequest(
            cause, reason, pending_goal.goal_revision,
        )

    def stage_waiting_goal(self, pending_goal: PendingGoalRevision) -> None:
        """Remember a goal that needs facts but has no body owner to stop."""
        self._set_newer_goal(pending_goal)
        self._stop_request = None

    def _set_newer_goal(self, pending_goal: PendingGoalRevision) -> None:
        if type(pending_goal) is not PendingGoalRevision:
            raise ContractViolation("pending navigation goal must be typed")
        current = self._pending_goal
        if current == pending_goal:
            return
        if (current is not None
                and pending_goal.goal_id != current.goal_id):
            raise ContractViolation("pending navigation goal identity changed")
        if (current is not None
                and pending_goal.goal_revision <= current.goal_revision):
            raise ContractViolation(
                "pending navigation goal revision must be newer"
            )
        self._pending_goal = pending_goal

    def clear_goal(self) -> PendingGoalRevision | None:
        pending = self._pending_goal
        self._pending_goal = None
        self._stop_request = None
        return pending

    def resolve_goal(
        self,
        handoff: HandoffEvidence,
        *,
        goal_ready: bool,
        start_ready: bool,
        missing_cells: tuple[BlockPos, ...],
        unavailable_reason: str,
    ) -> NavigationHandoffResolution:
        pending = self._pending_goal
        if pending is None or self._stop_request is None:
            raise ContractViolation("navigation handoff has no pending goal")
        if (type(handoff) is not HandoffEvidence
                or handoff.disposition is not HandoffDisposition.QUIESCENT):
            raise ContractViolation(
                "navigation goal handoff requires quiescent evidence"
            )
        require_identifier(unavailable_reason, "navigation unavailable reason")
        if (type(goal_ready) is not bool or type(start_ready) is not bool
                or type(missing_cells) is not tuple
                or missing_cells != tuple(sorted(set(missing_cells)))):
            raise ContractViolation("navigation handoff facts are invalid")
        if goal_ready and start_ready:
            destination = HandoffDestination.REPLAN
            reason = "pending_goal_ready_after_handoff"
        elif missing_cells:
            destination = HandoffDestination.WAIT_FOR_INFORMATION
            reason = (
                "goal_surface_requires_information"
                if not goal_ready else "current_surface_requires_information"
            )
        else:
            destination = HandoffDestination.FAIL
            reason = unavailable_reason
        self._pending_goal = None
        self._stop_request = None
        return NavigationHandoffResolution(
            destination, reason, pending, missing_cells, handoff,
        )
