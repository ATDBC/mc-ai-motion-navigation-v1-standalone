"""One owner for goal revisions that must wait for body release."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.body_control import (
    HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.navigation_owners import PendingGoalRevision
from mc2p.motion_nav.planning_coordinator import (
    PlanningAttemptPermitKind, PlanningCoordinator, PlanningFailure,
)
from mc2p.motion_nav.known_map_planner import PlanningRequest, SurfacePlanningRequest
from mc2p.motion_nav.world_model import BlockPos
from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence, RecoveryFinishEvidence, RecoveryFinishKind,
    RecoveryIdentity, RecoveryLimitStatus, RetryCause, RetryLedger,
    TaskDemandState,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.probe_body_controller import ProbeOutcome, ProbeOutcomeKind
from mc2p.motion_nav.route_admission import ActiveRoute


class ProbeHandoffAction(StrEnum):
    FINISH_ENDING = "finish_ending"
    STOP_SUSPENDED_ROUTE = "stop_suspended_route"
    ACQUISITION_FAILED = "acquisition_failed"
    RESOLVE_RECOVERY = "resolve_recovery"
    WAIT_FOR_ROUTE_RELEASE = "wait_for_route_release"
    WAIT_FOR_ROUTE_INPUT = "wait_for_route_input"
    REPLAN = "replan"
    CONTINUE_ROUTE = "continue_route"


@dataclass(frozen=True, slots=True)
class ProbeHandoffResolution:
    action: ProbeHandoffAction
    reason: str
    cause: StopCause | None = None

    def __post_init__(self) -> None:
        if type(self.action) is not ProbeHandoffAction:
            raise ContractViolation("probe destination must be typed")
        require_identifier(self.reason, "probe handoff reason")
        if self.cause is not None and type(self.cause) is not StopCause:
            raise ContractViolation("probe handoff stop cause must be typed")


class HandoffDestination(StrEnum):
    REPLAN = "replan"
    WAIT_FOR_INFORMATION = "wait_for_information"
    FAIL = "fail"
    COMPLETE = "complete"
    CANCEL = "cancel"
    CLOSE = "close"


class RecoveryRequestStatus(StrEnum):
    STAGED = "staged"
    DUPLICATE = "duplicate"
    STALE = "stale"
    BUSY = "busy"
    ACTIVITY_REQUIRED = "activity_required"
    LIMIT_EXHAUSTED = "limit_exhausted"


@dataclass(frozen=True, slots=True)
class RecoveryActivityPermit:
    """One task/frame evaluation required before recovery can be purchased."""

    task_id: str
    observation_sequence: int
    permit_sequence: int
    limit_status: RecoveryLimitStatus

    def __post_init__(self) -> None:
        require_identifier(self.task_id, "recovery activity task")
        if (type(self.observation_sequence) is not int
                or self.observation_sequence < 0
                or type(self.permit_sequence) is not int
                or self.permit_sequence < 0):
            raise ContractViolation("recovery activity permit sequence is invalid")
        if type(self.limit_status) is not RecoveryLimitStatus:
            raise ContractViolation("recovery activity limit must be typed")


@dataclass(frozen=True, slots=True)
class RecoveryRequestResult:
    status: RecoveryRequestStatus
    limit_status: RecoveryLimitStatus | None = None
    recovery_identity: RecoveryIdentity | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not RecoveryRequestStatus:
            raise ContractViolation("recovery request status must be typed")
        if (self.limit_status is not None
                and type(self.limit_status) is not RecoveryLimitStatus):
            raise ContractViolation("recovery request limit must be typed")
        if (self.recovery_identity is not None
                and type(self.recovery_identity) is not RecoveryIdentity):
            raise ContractViolation("recovery request identity must be typed")

    def __bool__(self) -> bool:
        return self.status is RecoveryRequestStatus.STAGED


@dataclass(frozen=True, slots=True)
class NavigationStopRequest:
    cause: StopCause
    reason: str
    goal_revision: int | None = None
    destination: HandoffDestination = HandoffDestination.REPLAN
    request_id: str | None = None
    retry_cause: RetryCause | None = None
    missing_cells: tuple[BlockPos, ...] = ()
    planning_permit_kind: PlanningAttemptPermitKind = PlanningAttemptPermitKind.PROGRESS
    planning_failure: PlanningFailure | None = None
    recovery_identity: RecoveryIdentity | None = None
    recovery_limit_status: RecoveryLimitStatus | None = None

    def __post_init__(self) -> None:
        if type(self.cause) is not StopCause:
            raise ContractViolation("navigation stop cause must be typed")
        require_identifier(self.reason, "navigation stop reason")
        if type(self.destination) is not HandoffDestination:
            raise ContractViolation("navigation stop destination must be typed")
        if type(self.planning_permit_kind) is not PlanningAttemptPermitKind:
            raise ContractViolation("navigation reanchor permit must be typed")
        if self.planning_failure is not None and (
                type(self.planning_failure) is not PlanningFailure
                or self.destination is not HandoffDestination.FAIL
                or self.goal_revision != self.planning_failure.goal_revision):
            raise ContractViolation("deferred planning failure requires its typed FAIL destination")
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
        if (self.recovery_identity is not None
                and type(self.recovery_identity) is not RecoveryIdentity):
            raise ContractViolation("navigation recovery identity must be typed")
        if (self.recovery_limit_status is not None
                and type(self.recovery_limit_status) is not RecoveryLimitStatus):
            raise ContractViolation("navigation recovery limit must be typed")


@dataclass(frozen=True, slots=True)
class NavigationHandoffResolution:
    destination: HandoffDestination
    reason: str
    pending_goal: PendingGoalRevision | None = None
    missing_cells: tuple[BlockPos, ...] = ()
    handoff: HandoffEvidence | None = None
    request_id: str | None = None
    planning_permit_kind: PlanningAttemptPermitKind = PlanningAttemptPermitKind.PROGRESS
    recovery_identity: RecoveryIdentity | None = None
    recovery_limit_status: RecoveryLimitStatus | None = None

    def __post_init__(self) -> None:
        if type(self.destination) is not HandoffDestination:
            raise ContractViolation("navigation handoff destination must be typed")
        if type(self.planning_permit_kind) is not PlanningAttemptPermitKind:
            raise ContractViolation("navigation reanchor permit must be typed")
        require_identifier(self.reason, "navigation handoff reason")
        if (self.pending_goal is not None
                and type(self.pending_goal) is not PendingGoalRevision):
            raise ContractViolation("navigation handoff goal must be typed")
        if (type(self.missing_cells) is not tuple
                or self.missing_cells != tuple(sorted(set(self.missing_cells)))):
            raise ContractViolation(
                "navigation handoff missing cells must be sorted and unique"
            )
        if (self.recovery_identity is not None
                and type(self.recovery_identity) is not RecoveryIdentity):
            raise ContractViolation("navigation handoff recovery identity must be typed")
        if (self.recovery_limit_status is not None
                and type(self.recovery_limit_status) is not RecoveryLimitStatus):
            raise ContractViolation("navigation handoff recovery limit must be typed")


class NavigationHandoffCoordinator:
    """Stages one goal revision and resolves it after verified body release."""

    def __init__(self) -> None:
        self._pending_goal: PendingGoalRevision | None = None
        self._stop_request: NavigationStopRequest | None = None
        self._task_id: str | None = None
        self._permit_sequence = 0
        self._activity_permit: RecoveryActivityPermit | None = None
        self._activity_finalized = False
        self._activity_consumed = False

    @staticmethod
    def _limit_reason(status: RecoveryLimitStatus) -> str:
        return {
            RecoveryLimitStatus.FINITE_TOTAL_EXHAUSTED:
                "task_recovery_budget_exhausted",
            RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED:
                "task_recovery_rate_exhausted",
            RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED:
                "single_recovery_deadline_exhausted",
            RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED:
                "task_no_progress_deadline_exhausted",
            RecoveryLimitStatus.TARGET_ACTIONS_STOPPED:
                "task_target_actions_stopped",
        }.get(status, "task_recovery_limit_exhausted")

    def begin_task_activity(
        self, *, budget: RetryLedger, observation_sequence: int,
        demand_state: TaskDemandState,
    ) -> None:
        """Open one frame and collect demand without deciding its limits yet."""
        if type(budget) is not RetryLedger:
            raise ContractViolation("task activity requires its retry ledger")
        if type(observation_sequence) is not int or observation_sequence < 0:
            raise ContractViolation("task activity observation is invalid")
        if type(demand_state) is not TaskDemandState:
            raise ContractViolation("task activity demand must be typed")
        if self._task_id is None:
            self._task_id = budget.task_id
        elif self._task_id != budget.task_id:
            raise ContractViolation("navigation handoff task identity changed")
        previous = self._activity_permit
        if (previous is not None
                and observation_sequence < previous.observation_sequence):
            raise ContractViolation("task activity observation regressed")
        if (previous is not None
                and observation_sequence == previous.observation_sequence):
            if not self._activity_finalized:
                budget.record_task_activity(demand_state)
            return
        budget.record_task_activity(demand_state)
        self._activity_permit = RecoveryActivityPermit(
            budget.task_id, observation_sequence,
            self._permit_sequence, RecoveryLimitStatus.ALLOWED,
        )
        self._permit_sequence += 1
        self._activity_finalized = False
        self._activity_consumed = False

    def record_task_progress(
        self, *, budget: RetryLedger, observation_sequence: int,
        demand_state: TaskDemandState,
        progress: tuple[ProgressEvidence, ...],
    ) -> None:
        permit = self._activity_permit
        if (permit is None or permit.task_id != budget.task_id
                or permit.observation_sequence != observation_sequence
                or self._activity_finalized):
            raise ContractViolation("task progress requires the open current frame")
        if (type(progress) is not tuple
                or any(type(item) is not ProgressEvidence for item in progress)):
            raise ContractViolation("task activity progress must be typed")
        for evidence in progress:
            budget.record_task_activity(demand_state, evidence)

    def finalize_task_activity(
        self, *, budget: RetryLedger, observation_sequence: int,
    ) -> RecoveryActivityPermit:
        permit = self._activity_permit
        if (permit is None or permit.task_id != budget.task_id
                or permit.observation_sequence != observation_sequence):
            raise ContractViolation("task activity finalization requires current frame")
        if self._activity_finalized:
            return permit
        status = budget.finalize_task_activity()
        permit = RecoveryActivityPermit(
            budget.task_id, observation_sequence,
            permit.permit_sequence, status,
        )
        self._activity_permit = permit
        self._activity_finalized = True
        if status is not RecoveryLimitStatus.ALLOWED:
            self._stage_limit_failure(status)
        return permit

    def observe_task_activity(
        self, *, budget: RetryLedger, observation_sequence: int,
        demand_state: TaskDemandState,
        progress: tuple[ProgressEvidence, ...] = (),
    ) -> RecoveryActivityPermit:
        """Compatibility helper for one-shot component checks."""
        self.begin_task_activity(
            budget=budget, observation_sequence=observation_sequence,
            demand_state=demand_state,
        )
        if progress:
            self.record_task_progress(
                budget=budget, observation_sequence=observation_sequence,
                demand_state=demand_state, progress=progress,
            )
        return self.finalize_task_activity(
            budget=budget, observation_sequence=observation_sequence,
        )

    def _stage_limit_failure(self, status: RecoveryLimitStatus) -> None:
        current = self._stop_request
        if self.accepted_ending:
            return
        reason = self._limit_reason(status)
        if current is None:
            self._stop_request = NavigationStopRequest(
                StopCause.MOTION_UNSOLVABLE, reason,
                destination=HandoffDestination.FAIL,
                request_id=(None if self._task_id is None else
                            f"{self._task_id}/recovery-limit"),
                recovery_limit_status=status,
            )
            return
        self._stop_request = replace(
            current, destination=HandoffDestination.FAIL, reason=reason,
            planning_failure=None, recovery_limit_status=status,
        )

    def resolve_probe(
        self, frame: NavigationFrame, *, outcome: ProbeOutcome | None,
        handoff: HandoffEvidence | None, route: ActiveRoute | None,
        action_index: int | None, current_request_id: str | None,
        pending_route_id: str | None,
    ) -> ProbeHandoffResolution | None:
        """Choose one destination from the probe fact and existing owner facts."""
        if (outcome is None or type(outcome) is not ProbeOutcome
                or outcome.world_session != frame.session
                or outcome.observation_sequence_id != frame.body.sequence_id):
            return None
        if outcome.kind is ProbeOutcomeKind.READY:
            if self.ending:
                # Completion is an information fact, never a substitute for
                # the release proof required by an accepted terminal request.
                return None
            if route is not None and (outcome.route_id, outcome.route_revision,
                    outcome.action_index) == (route.route_id, route.route_revision, action_index):
                return ProbeHandoffResolution(ProbeHandoffAction.CONTINUE_ROUTE,
                                              "action_acquisition_complete")
            return ProbeHandoffResolution(ProbeHandoffAction.REPLAN,
                                          "landing_edge_probe_complete")
        if (type(handoff) is not HandoffEvidence
                or handoff.owner_id != outcome.owner_id
                or handoff.world_session != frame.session
                or handoff.observation_sequence_id != frame.body.sequence_id
                or handoff.disposition is not HandoffDisposition.QUIESCENT):
            return None
        request = self._stop_request
        if self.ending:
            return ProbeHandoffResolution(
                ProbeHandoffAction.STOP_SUSPENDED_ROUTE if route is not None
                else ProbeHandoffAction.FINISH_ENDING, request.reason,
                outcome.stop_cause or StopCause.ACQUISITION_TIMED_OUT)
        if outcome.kind is ProbeOutcomeKind.TIMED_OUT:
            return ProbeHandoffResolution(ProbeHandoffAction.ACQUISITION_FAILED,
                                          "edge_probe_acquisition_timeout", StopCause.CANCELLED)
        if request is not None and request.retry_cause is not None:
            return ProbeHandoffResolution(ProbeHandoffAction.RESOLVE_RECOVERY, request.reason)
        if (route is not None and current_request_id is not None
                and route.source_request_id != current_request_id):
            return ProbeHandoffResolution(ProbeHandoffAction.WAIT_FOR_ROUTE_RELEASE,
                                          "replacement_route_waiting_for_release")
        if pending_route_id is not None:
            return ProbeHandoffResolution(ProbeHandoffAction.WAIT_FOR_ROUTE_INPUT,
                                          "successor_route_waiting_for_motion")
        return ProbeHandoffResolution(ProbeHandoffAction.REPLAN, "edge_probe_stopped_for_replan")

    @property
    def pending_goal(self) -> PendingGoalRevision | None:
        return self._pending_goal

    @property
    def stop_request(self) -> NavigationStopRequest | None:
        return self._stop_request

    @property
    def activity_permit(self) -> RecoveryActivityPermit | None:
        return self._activity_permit

    @property
    def activity_finalized(self) -> bool:
        return self._activity_finalized

    @property
    def ending(self) -> bool:
        return self._stop_request is not None and self._stop_request.destination in {
            HandoffDestination.FAIL, HandoffDestination.COMPLETE,
            HandoffDestination.CANCEL, HandoffDestination.CLOSE,
        }

    @property
    def accepted_ending(self) -> bool:
        """Formal terminal requests are immutable; unpublished planning facts are not."""
        return self.ending and self._stop_request.planning_failure is None

    def stage_planning_failure(
        self, failure: PlanningFailure,
        current_request: PlanningRequest | SurfacePlanningRequest,
        *, planning: PlanningCoordinator,
    ) -> bool:
        """Keep the current request's negative fact until its body owner releases."""
        if (type(failure) is not PlanningFailure
                or type(current_request) not in {PlanningRequest, SurfacePlanningRequest}
                or type(planning) is not PlanningCoordinator):
            raise ContractViolation("planning handoff requires its typed failure, request and producer")
        if (failure.request_id != current_request.request_id
                or failure.goal_revision != current_request.goal_revision
                or failure.world_session_id != current_request.world_session
                or self.accepted_ending
                or not planning.failure_matches_retired_work(failure)):
            return False
        current = self._stop_request
        if current is not None and current.planning_failure is not None:
            return False
        if current is None:
            self._stop_request = NavigationStopRequest(
                StopCause.MOTION_UNSOLVABLE, failure.reason, failure.goal_revision,
                HandoffDestination.FAIL, failure.request_id,
                planning_failure=failure,
            )
        else:
            self._stop_request = replace(
                current, cause=StopCause.MOTION_UNSOLVABLE, reason=failure.reason,
                goal_revision=failure.goal_revision, destination=HandoffDestination.FAIL,
                planning_failure=failure,
            )
        self._pending_goal = None
        return True

    def finish_ending(
        self, *, budget: RetryLedger | None = None,
        handoff: HandoffEvidence | None = None,
        frame: NavigationFrame | None = None,
    ) -> NavigationHandoffResolution:
        if not self.ending:
            raise ContractViolation("navigation handoff has no ending request")
        request = self._stop_request
        self._finish_recovery_from_handoff(
            request, budget=budget, handoff=handoff, frame=frame,
        )
        self._stop_request = None
        self._pending_goal = None
        return NavigationHandoffResolution(
            request.destination, request.reason, request_id=request.request_id,
            recovery_identity=request.recovery_identity,
            recovery_limit_status=request.recovery_limit_status,
        )

    def finish_control_unavailable(self, *, budget: RetryLedger | None) -> None:
        """End bookkeeping after control loss without claiming a safe handoff."""
        request = self._stop_request
        if request is not None and request.recovery_identity is not None:
            if type(budget) is not RetryLedger:
                raise ContractViolation("control loss requires the owning recovery budget")
            budget.finish_recovery(request.recovery_identity, RecoveryFinishEvidence(
                RecoveryFinishKind.CONTROL_UNAVAILABLE,
            ))
        self._stop_request = None
        self._pending_goal = None

    def request_recovery(
        self, *, request_id: str, destination: HandoffDestination, reason: str,
        budget: RetryLedger | None, cause: StopCause = StopCause.MOTION_UNSOLVABLE,
        retry_cause: RetryCause = RetryCause.EXECUTION,
        missing_cells: tuple[BlockPos, ...] = (),
        activity_permit: RecoveryActivityPermit | None = None,
        recovery_identity: RecoveryIdentity | None = None,
    ) -> RecoveryRequestResult:
        require_identifier(request_id, "navigation recovery request")
        require_identifier(reason, "navigation recovery reason")
        if type(destination) is not HandoffDestination or type(cause) is not StopCause:
            raise ContractViolation("navigation recovery requires typed purpose and cause")
        if (type(retry_cause) is not RetryCause or type(missing_cells) is not tuple
                or missing_cells != tuple(sorted(set(missing_cells)))):
            raise ContractViolation("navigation recovery facts are invalid")
        current = self._stop_request
        if current is not None and current.request_id == request_id:
            return RecoveryRequestResult(
                RecoveryRequestStatus.DUPLICATE,
                current.recovery_limit_status,
                current.recovery_identity,
            )
        if (destination is HandoffDestination.REPLAN
                and type(activity_permit) is RecoveryActivityPermit
                and activity_permit is self._activity_permit
                and type(budget) is RetryLedger
                and activity_permit.task_id == budget.task_id
                and activity_permit.limit_status is not RecoveryLimitStatus.ALLOWED):
            self._stage_limit_failure(activity_permit.limit_status)
            return RecoveryRequestResult(
                RecoveryRequestStatus.LIMIT_EXHAUSTED,
                activity_permit.limit_status,
            )
        if self.accepted_ending:
            return RecoveryRequestResult(RecoveryRequestStatus.BUSY)
        if current is not None and destination in {
            HandoffDestination.REPLAN, HandoffDestination.WAIT_FOR_INFORMATION,
        }:
            return RecoveryRequestResult(RecoveryRequestStatus.BUSY)
        registration_cause = None
        if destination is HandoffDestination.REPLAN:
            if type(budget) is not RetryLedger:
                raise ContractViolation("navigation recovery requires its task budget")
            if (activity_permit is None
                    or type(activity_permit) is not RecoveryActivityPermit
                    or activity_permit is not self._activity_permit
                    or activity_permit.task_id != budget.task_id
                    or not self._activity_finalized
                    or type(recovery_identity) is not RecoveryIdentity
                    or recovery_identity.sequence
                        > activity_permit.observation_sequence):
                return RecoveryRequestResult(
                    RecoveryRequestStatus.ACTIVITY_REQUIRED,
                )
            if recovery_identity.sequence < activity_permit.observation_sequence:
                return RecoveryRequestResult(
                    RecoveryRequestStatus.STALE,
                    budget.recovery_limit_status,
                    recovery_identity,
                )
            if self._activity_consumed:
                return RecoveryRequestResult(
                    RecoveryRequestStatus.ACTIVITY_REQUIRED,
                )
            current_limit = budget.current_limit_status()
            if current_limit is not RecoveryLimitStatus.ALLOWED:
                self._stage_limit_failure(current_limit)
                self._activity_consumed = True
                return RecoveryRequestResult(
                    RecoveryRequestStatus.LIMIT_EXHAUSTED,
                    current_limit,
                    recovery_identity,
                )
            self._activity_consumed = True
            registration = budget.begin_recovery(recovery_identity, retry_cause)
            if registration.status is not RecoveryLimitStatus.ALLOWED:
                self._stage_limit_failure(registration.status)
                return RecoveryRequestResult(
                    RecoveryRequestStatus.LIMIT_EXHAUSTED,
                    registration.status,
                    recovery_identity,
                )
            if not registration.first_seen:
                return RecoveryRequestResult(
                    RecoveryRequestStatus.DUPLICATE,
                    registration.status,
                    recovery_identity,
                )
            registration_cause = retry_cause
        elif current is not None:
            # A terminal request replaces the target purpose but keeps the
            # already-paid recovery responsible for the body tail.
            recovery_identity = current.recovery_identity
        else:
            recovery_identity = None
        self._stop_request = NavigationStopRequest(
            cause, reason, destination=destination, request_id=request_id,
            retry_cause=registration_cause, missing_cells=missing_cells,
            planning_permit_kind=(PlanningAttemptPermitKind.RETRY if registration_cause is not None
                                  else PlanningAttemptPermitKind.PROGRESS),
            recovery_identity=recovery_identity,
        )
        return RecoveryRequestResult(
            RecoveryRequestStatus.STAGED,
            recovery_identity=recovery_identity,
        )

    def _finish_recovery_from_handoff(
        self, request: NavigationStopRequest, *, budget: RetryLedger | None,
        handoff: HandoffEvidence | None, frame: NavigationFrame | None,
    ) -> None:
        identity = request.recovery_identity
        if identity is None:
            return
        if type(budget) is not RetryLedger:
            raise ContractViolation("recovery completion requires its task budget")
        if (type(frame) is not NavigationFrame
                or type(handoff) is not HandoffEvidence
                or handoff.world_session != frame.session
                or handoff.observation_sequence_id != frame.body.sequence_id
                or handoff.disposition not in {
                    HandoffDisposition.QUIESCENT,
                    HandoffDisposition.TRANSFERABLE,
                }):
            raise ContractViolation(
                "recovery completion requires verified handoff evidence"
            )
        kind = (
            RecoveryFinishKind.SAFE_RELEASE
            if handoff.disposition is HandoffDisposition.QUIESCENT
            else RecoveryFinishKind.BODY_HANDOFF
        )
        budget.finish_recovery(identity, RecoveryFinishEvidence(kind))

    def stage_reanchor(
        self, *, request_id: str, cause: StopCause, reason: str,
        planning_permit_kind: PlanningAttemptPermitKind = PlanningAttemptPermitKind.PROGRESS,
    ) -> bool:
        """A task update or existing progress can re-anchor without buying a retry."""
        require_identifier(request_id, "navigation reanchor request")
        if planning_permit_kind not in {
            PlanningAttemptPermitKind.PROGRESS, PlanningAttemptPermitKind.TASK_UPDATE,
        } or type(planning_permit_kind) is not PlanningAttemptPermitKind:
            raise ContractViolation("reanchor requires progress or a task update permit")
        request = NavigationStopRequest(cause, reason, request_id=request_id,
                                       planning_permit_kind=planning_permit_kind)
        if self._stop_request is not None:
            return False
        self._stop_request = request
        return True

    def advance(
        self, frame: NavigationFrame, *, handoff: HandoffEvidence,
        goal_ready: bool, start_ready: bool, missing_cells: tuple[BlockPos, ...],
        unavailable_reason: str,
        budget: RetryLedger | None = None,
    ) -> NavigationHandoffResolution | None:
        request = self._stop_request
        if request is None:
            return None
        if (type(goal_ready) is not bool or type(start_ready) is not bool
                or type(missing_cells) is not tuple
                or missing_cells != tuple(sorted(set(missing_cells)))):
            raise ContractViolation("navigation recovery readiness must be typed")
        if (type(frame) is not NavigationFrame or type(handoff) is not HandoffEvidence
                or handoff.world_session != frame.session
                or handoff.observation_sequence_id != frame.body.sequence_id
                or handoff.disposition is not HandoffDisposition.QUIESCENT):
            return None
        destination, reason = request.destination, request.reason
        if destination is HandoffDestination.REPLAN:
            if goal_ready and start_ready and self._pending_goal is not None:
                reason = "pending_goal_ready_after_handoff"
            elif not (goal_ready and start_ready):
                destination = HandoffDestination.WAIT_FOR_INFORMATION if missing_cells else HandoffDestination.FAIL
                reason = (("goal_surface_requires_information" if not goal_ready
                           else "current_surface_requires_information") if missing_cells else unavailable_reason)
        resolution = NavigationHandoffResolution(
            destination, reason, self._pending_goal, missing_cells, handoff, request.request_id,
            request.planning_permit_kind,
            request.recovery_identity,
            request.recovery_limit_status,
        )
        self._finish_recovery_from_handoff(
            request, budget=budget, handoff=handoff, frame=frame,
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
        if self.accepted_ending:
            raise ContractViolation("navigation task is already ending")
        self._set_newer_goal(pending_goal)
        if (self._stop_request is None
                or (self._stop_request.planning_failure is not None
                    and self._stop_request.planning_permit_kind is not PlanningAttemptPermitKind.RETRY)):
            self._stop_request = NavigationStopRequest(
                cause, reason, pending_goal.goal_revision,
                request_id=f"{pending_goal.goal_id}/revision-{pending_goal.goal_revision}/body-release",
                planning_permit_kind=PlanningAttemptPermitKind.TASK_UPDATE,
            )
        else:
            self._stop_request = replace(self._stop_request, cause=cause, reason=reason,
                                         goal_revision=pending_goal.goal_revision,
                                         destination=HandoffDestination.REPLAN,
                                         planning_failure=None)

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

    def clear_waiting_goal(self) -> None:
        """A new planning request cannot revoke an incumbent's exit purpose."""
        if self._stop_request is None:
            self._pending_goal = None

    def resolve_goal(
        self,
        frame: NavigationFrame,
        handoff: HandoffEvidence,
        *,
        goal_ready: bool,
        start_ready: bool,
        missing_cells: tuple[BlockPos, ...],
        unavailable_reason: str,
        budget: RetryLedger | None = None,
    ) -> NavigationHandoffResolution | None:
        pending = self._pending_goal
        if pending is None or self._stop_request is None:
            raise ContractViolation("navigation handoff has no pending goal")
        require_identifier(unavailable_reason, "navigation unavailable reason")
        return self.advance(
            frame, handoff=handoff, goal_ready=goal_ready, start_ready=start_ready,
            missing_cells=missing_cells, unavailable_reason=unavailable_reason,
            budget=budget,
        )
