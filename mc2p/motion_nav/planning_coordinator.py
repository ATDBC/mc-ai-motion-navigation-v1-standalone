"""Own snapshot, background planning and route-admission behaviour.

The coordinator deliberately does not own navigation lifecycle state or body
control.  It turns one permitted planning attempt into a typed update for the
session router.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import hashlib
import math
from itertools import chain
from typing import Callable

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.async_work import (
    AsyncWorkLifecycle,
    AsyncComputationScope,
    ComputationInvalidationCause,
    WorkCheck,
    AsyncAdmissionDisposition,
    AsyncAdmissionRecord,
    AsyncWorkIdentity,
    AsyncWorkKind,
    AsyncWorkWindow,
    AsyncOwnerDiagnostics,
    AsyncFactQueryEvidence,
    WorkRetirementSummary,
)
from mc2p.motion_nav.air_motion import AirMotionProfile
from mc2p.motion_nav.bridge_planner import (
    BridgeInteractionPlan,
    BridgePlacementPolicy,
    plan_next_bridge_interaction,
)
from mc2p.motion_nav.ground_modes import GroundModeProfiles
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.jump_up import JumpUpProfile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    KnownMapSnapshotBuilder,
    PlanningBlocker,
    PlanningBlockerKind,
    PlanningFactRequirement,
    PlanningFactRequirementKind,
    PlanningFrontierKind,
    PlanningInformationNeed,
    PlanningRequest,
    PlanningStatus,
    SnapshotBuildStatus,
    SurfacePlanningRequest,
    SurfacePlanningStatus,
    SurfaceRouteCandidate,
)
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.navigation_owners import (
    GoalRequestLedger,
    PlanningPipelineState,
)
from mc2p.motion_nav.online_motion import StateAnchor
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.planner_worker import PlannerWorkerPort, PlanningSubmissionStatus
from mc2p.motion_nav.retry_ledger import (
    LocalAttemptChain,
    LocalAttemptVerdict,
    ProgressEvidence,
    ProgressKind,
    RecoveryIdentity,
    RetryCause,
    RetryLedger,
)
from mc2p.motion_nav.route_admission import (
    ActiveRoute,
    AdmissionReason,
    AdmissionStatus,
    RouteAdmitter,
    direct_drop_visual_evidence_sufficient,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.step_transition import StepProfile
from mc2p.motion_nav.support_surfaces import SurfaceNodeId, query_support_surfaces
from mc2p.motion_nav.world_model import (
    COLLISION_OWNER_BELOW_REACH_CELLS,
    CellKnowledge,
)


_INFORMATION_WORK_LIMIT_NS = 2_000_000_000
_PLANNING_HISTORY_LIMIT = 64


class PlanningHistoryCapacityExceeded(ContractViolation):
    """Planning history cannot discard an identity still owned by the coordinator."""


class PlanningAttemptPermitKind(StrEnum):
    TASK_UPDATE = "task_update"
    PROGRESS = "progress"
    RETRY = "retry"


class PlanningRetryTrigger(StrEnum):
    """Typed reason why an existing task asks planning to rebuild locally."""

    WORK_FAILURE = "work_failure"
    LOCAL_RESULT_INVALID = "local_result_invalid"
    RECOVERY_REANCHOR = "recovery_reanchor"


@dataclass(frozen=True, slots=True)
class PlanningAttemptPermit:
    permit_id: str
    task_id: str
    goal_revision: int
    source_event_id: str
    kind: PlanningAttemptPermitKind
    recovery_identity: RecoveryIdentity | None = None

    def __post_init__(self) -> None:
        require_identifier(self.permit_id, "planning permit id")
        require_identifier(self.task_id, "planning permit task")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("planning permit goal revision is invalid")
        require_identifier(self.source_event_id, "planning permit source event")
        if type(self.kind) is not PlanningAttemptPermitKind:
            raise ContractViolation("planning permit kind must be typed")
        if (self.recovery_identity is not None
                and type(self.recovery_identity) is not RecoveryIdentity):
            raise ContractViolation("planning recovery identity must be typed")


@dataclass(frozen=True, slots=True)
class PlanningCalculationBasis:
    """Frozen producer facts; the lifecycle alone owns the work window."""

    request: PlanningRequest | SurfacePlanningRequest
    permit: PlanningAttemptPermit
    bounds: KnownMapBounds
    work_identity: AsyncWorkIdentity
    capabilities: PlanningCapabilities
    submitted_request: PlanningRequest | SurfacePlanningRequest | None = None

    @property
    def scope(self) -> AsyncComputationScope:
        return self.work_identity.scope


@dataclass(frozen=True, slots=True)
class PlanningCapabilities:
    ground: GroundMotionProfile
    step: StepProfile
    jump_up: JumpUpProfile
    air: tuple[AirMotionProfile, ...]
    ground_modes: GroundModeProfiles | None

    def __post_init__(self) -> None:
        if type(self.ground) is not GroundMotionProfile:
            raise ContractViolation("planning ground profile must be typed")
        if type(self.step) is not StepProfile:
            raise ContractViolation("planning step profile must be typed")
        if type(self.jump_up) is not JumpUpProfile:
            raise ContractViolation("planning jump profile must be typed")
        if (type(self.air) is not tuple
                or any(type(profile) is not AirMotionProfile for profile in self.air)):
            raise ContractViolation("planning air profiles must be immutable")
        if (self.ground_modes is not None
                and type(self.ground_modes) is not GroundModeProfiles):
            raise ContractViolation("planning ground modes must be typed")


class PlanningUpdateKind(StrEnum):
    RUNNING = "running"
    ROUTE_READY = "route_ready"
    NEEDS_INFORMATION = "needs_information"
    REQUIRES_INTERACTION = "requires_interaction"
    FAILED = "failed"
    INFORMATION_ACQUIRED = "information_acquired"
    DISCARDED = "discarded"


class InformationOutcome(StrEnum):
    ACQUIRED = "acquired"
    CURRENTLY_UNAVAILABLE = "currently_unavailable"
    TIMED_OUT = "timed_out"


class _BasisChange(StrEnum):
    PROGRESS = "progress"
    DEPENDENCY = "dependency"
    CHECKING = "checking"


@dataclass(frozen=True, slots=True)
class PlanningFailure:
    reason: str
    world_session_id: str
    attempt_id: str
    request_id: str
    goal_revision: int

    def __post_init__(self) -> None:
        require_identifier(self.reason, "planning failure reason")
        require_identifier(self.world_session_id, "planning failure world")
        require_identifier(self.attempt_id, "planning failure attempt")
        require_identifier(self.request_id, "planning failure request")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("planning failure goal revision is invalid")


@dataclass(frozen=True, slots=True)
class PlanningUpdate:
    kind: PlanningUpdateKind
    world_session_id: str
    attempt_id: str
    request_id: str
    goal_revision: int
    reason: str
    request: PlanningRequest | SurfacePlanningRequest
    route: ActiveRoute | None = None
    information_need: PlanningInformationNeed | None = None
    interaction: BridgeInteractionPlan | None = None
    failure: PlanningFailure | None = None
    information_identity: AsyncWorkIdentity | None = None
    landing_probe_cell: tuple[int, int, int] | None = None

    @property
    def missing_cells(self) -> tuple[tuple[int, int, int], ...]:
        return (() if self.information_need is None else tuple(dict.fromkeys(
            blocker.position for blocker in self.information_need.blockers))[:64])

    def __post_init__(self) -> None:
        if type(self.kind) is not PlanningUpdateKind:
            raise ContractViolation("planning update kind must be typed")
        require_identifier(self.world_session_id, "planning update world")
        require_identifier(self.attempt_id, "planning update attempt")
        require_identifier(self.request_id, "planning update request")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("planning update goal revision is invalid")
        require_identifier(self.reason, "planning update reason")
        if (type(self.request) not in (PlanningRequest, SurfacePlanningRequest)
                or self.request.world_session != self.world_session_id
                or self.request.request_id != self.request_id
                or self.request.goal_revision != self.goal_revision):
            raise ContractViolation("planning update and delivered request disagree")
        if self.kind is PlanningUpdateKind.ROUTE_READY and self.route is None:
            raise ContractViolation("route-ready planning update requires a route")
        if (self.kind is PlanningUpdateKind.NEEDS_INFORMATION
                and self.information_need is None):
            raise ContractViolation("information planning update requires facts")
        if self.kind is PlanningUpdateKind.NEEDS_INFORMATION:
            identity = self.information_identity
            if (type(identity) is not AsyncWorkIdentity
                    or identity.work_kind is not AsyncWorkKind.INFORMATION
                    or identity.world_session_id != self.world_session_id
                    or identity.key != self.attempt_id):
                raise ContractViolation("information notification requires its own work identity")
            need = self.information_need
            if (need.world_session_id != self.world_session_id
                    or need.request_id != self.request_id
                    or need.goal_revision != self.goal_revision):
                raise ContractViolation("information notification and fact demand disagree")
        if (self.kind is PlanningUpdateKind.REQUIRES_INTERACTION
                and self.interaction is None):
            raise ContractViolation("interaction planning update requires an action")
        if self.kind is PlanningUpdateKind.FAILED and self.failure is None:
            raise ContractViolation("failed planning update requires a failure")
        if self.failure is not None:
            if (type(self.failure) is not PlanningFailure
                    or self.failure.world_session_id != self.world_session_id
                    or self.failure.attempt_id != self.attempt_id
                    or self.failure.request_id != self.request_id
                    or self.failure.goal_revision != self.goal_revision):
                raise ContractViolation("planning failure and notification identity disagree")


@dataclass(frozen=True, slots=True)
class PlanningWorkDiagnostics:
    """Identity and deadline facts used by the read-only invariant monitor."""

    attempt_id: str | None
    request_id: str | None
    goal_revision: int | None
    has_owned_work: bool
    work_identity_valid: bool
    permit_identity_valid: bool
    information_identity_valid: bool
    submitted_request_id: str | None
    movement_tick: int
    result_deadline_monotonic_ns: int | None


@dataclass(slots=True)
class _PlanningWork:
    lifecycle: AsyncWorkLifecycle = field(default_factory=AsyncWorkLifecycle)
    pipeline: PlanningPipelineState = field(default_factory=PlanningPipelineState)
    basis: PlanningCalculationBasis | None = None
    attempt_id: str | None = None
    candidate: object | None = None
    basis_scan: object | None = None
    basis_scan_revision: int | None = None
    basis_scan_progress: bool = False
    receipt_identity: AsyncWorkIdentity | None = None

    @property
    def active(self) -> bool:
        return self.lifecycle.identity is not None


class PlanningCoordinator:
    """Own one task's bounded planning attempts and worker result lifecycle."""

    def __init__(
        self,
        task_id: str,
        capabilities: PlanningCapabilities,
        *,
        planner_worker: PlannerWorkerPort,
        route_admitter: RouteAdmitter,
        retry_ledger: RetryLedger,
        clock_ns: Callable[[], int],
        snapshot_cells_per_step: int = 4096,
        planning_margin_cells: int = 1,
        pipeline: PlanningPipelineState | None = None,
        bridge_policy: BridgePlacementPolicy | None = None,
        bridge_remaining: int | None = None,
        request_ledger: GoalRequestLedger | None = None,
        request_id_prefix: str | None = None,
    ) -> None:
        require_identifier(task_id, "planning coordinator task")
        if type(capabilities) is not PlanningCapabilities:
            raise ContractViolation("planning capabilities must be typed")
        if not isinstance(planner_worker, PlannerWorkerPort):
            raise ContractViolation("planning worker does not satisfy its protocol")
        if type(route_admitter) is not RouteAdmitter:
            raise ContractViolation("planning route admitter must be typed")
        if type(retry_ledger) is not RetryLedger:
            raise ContractViolation("planning retry ledger must be typed")
        if type(snapshot_cells_per_step) is not int or snapshot_cells_per_step < 1:
            raise ContractViolation("planning snapshot budget must be positive")
        if (type(planning_margin_cells) is not int
                or not 0 <= planning_margin_cells <= 16):
            raise ContractViolation("planning margin must be within 0..16")
        if (bridge_policy is not None
                and type(bridge_policy) is not BridgePlacementPolicy):
            raise ContractViolation("planning bridge policy must be typed")
        self.task_id = task_id
        self.capabilities = capabilities
        self._planner = planner_worker
        self._admitter = route_admitter
        self._retry_ledger = retry_ledger
        self._clock = clock_ns
        self._snapshot_cells_per_step = snapshot_cells_per_step
        self._planning_margin = planning_margin_cells
        if pipeline is not None and type(pipeline) is not PlanningPipelineState:
            raise ContractViolation("planning pipeline owner must be typed")
        self._selected = _PlanningWork(pipeline=pipeline or PlanningPipelineState())
        self._works: list[_PlanningWork] = []
        self._event_history = []
        self._retired: dict[AsyncWorkIdentity, WorkRetirementSummary] = {}
        self._remaining_accesses = snapshot_cells_per_step
        self._discarded_result = False
        if request_ledger is not None and type(request_ledger) is not GoalRequestLedger:
            raise ContractViolation("planning request ledger must be typed")
        self._request_ledger = request_ledger or GoalRequestLedger()
        self._request_id_prefix = request_id_prefix or task_id
        require_identifier(self._request_id_prefix, "planning request prefix")
        self._attempt_sequence = 0
        from mc2p.motion_nav.async_work import AsyncOwnerScope
        self._owner_instance_id = AsyncOwnerScope().allocate()
        self._last_admission: AsyncAdmissionRecord | None = None
        self._admission_records: list[AsyncAdmissionRecord] = []
        self._known_work_windows: dict[AsyncWorkIdentity, AsyncWorkWindow] = {}
        self._unidentified_results = 0
        self._used_permits: set[str] = set()
        self._terminal_update: PlanningUpdate | None = None
        self._deferred_failure: tuple[PlanningCalculationBasis | AsyncWorkIdentity, PlanningUpdate] | None = None
        self._terminal_failure_origin: PlanningCalculationBasis | AsyncWorkIdentity | None = None
        self._observation_update: tuple[int, PlanningUpdate] | None = None
        self._bridge_policy = bridge_policy
        default_bridge_remaining = (
            0 if bridge_policy is None else bridge_policy.maximum_blocks
        )
        self._bridge_remaining = (
            default_bridge_remaining
            if bridge_remaining is None else bridge_remaining
        )
        if (type(self._bridge_remaining) is not int
                or self._bridge_remaining < 0):
            raise ContractViolation("planning bridge remainder is invalid")
        self._required_interaction: BridgeInteractionPlan | None = None
        self._confirmed_interaction_id: str | None = None
        self._interaction_approach_pending = False
        self._information_need: PlanningInformationNeed | None = None
        self._information_offset = 0
        self._information_unavailable: set[
            tuple[tuple[int, int, int], str, str]
        ] = set()
        self._pending_information_progress: tuple[
            tuple[tuple[int, int, int], str, str], int
        ] | None = None
        self._fact_queries: list[AsyncFactQueryEvidence] = []
        self._local_attempts = LocalAttemptChain(maximum_failures=3)

    @property
    def pipeline(self) -> PlanningPipelineState:
        self._select_live_work()
        return self._selected.pipeline

    @property
    def request(self) -> PlanningRequest | SurfacePlanningRequest | None:
        return self._request_ledger.request

    @property
    def calculation_basis(self) -> PlanningCalculationBasis | None:
        self._select_live_work()
        return self._selected.basis

    @property
    def _active_permit(self):
        return None if self._selected.basis is None else self._selected.basis.permit

    @property
    def _submitted_request(self):
        return None if self._selected.basis is None else self._selected.basis.submitted_request

    @property
    def _calculation_request(self):
        return (self.request if self._selected.basis is None
                else self._selected.basis.request)

    def revise_request(self, request: PlanningRequest | SurfacePlanningRequest) -> None:
        """Update the business request without touching the running producer."""
        previous = self.request
        if (type(request) not in (PlanningRequest, SurfacePlanningRequest)
                or previous is None or type(previous) is not type(request)
                or request.world_session != previous.world_session
                or request.goal_id != previous.goal_id
                or request.goal_revision < previous.goal_revision
                or (self._selected.basis is not None
                    and (request.request_id == self._selected.basis.request.request_id
                         or request.goal_revision <= self._selected.basis.request.goal_revision))):
            raise ContractViolation("planning revision requires a newer same-world goal")
        self._request_ledger.accept(request)

    @property
    def attempt_id(self) -> str | None:
        self._select_live_work()
        return self._selected.attempt_id

    @property
    def work_identity(self) -> AsyncWorkIdentity | None:
        self._select_live_work()
        return self._work_identity

    @property
    def _work_identity(self):
        return self._selected.lifecycle.identity

    @property
    def _work_window(self):
        return self._selected.lifecycle.window

    @property
    def work_window(self) -> AsyncWorkWindow | None:
        self._select_live_work()
        return self._work_window

    @property
    def last_admission(self) -> AsyncAdmissionRecord | None:
        return self._last_admission

    @property
    def admission_records(self) -> tuple[AsyncAdmissionRecord, ...]:
        return tuple(self._admission_records)

    @property
    def async_diagnostics(self) -> AsyncOwnerDiagnostics:
        records = tuple(self._works) or (self._selected,)
        active = tuple((work.lifecycle.identity, work.lifecycle.window)
            for work in records if work.lifecycle.identity is not None)
        resources = tuple((work.lifecycle.identity, name)
            for work in records if work.lifecycle.identity is not None
            for name, value in (
                ("builder", work.pipeline.builder),
                ("snapshot", work.pipeline.snapshot),
                ("submitted_request", None if work.basis is None else work.basis.submitted_request),
                ("candidate", work.candidate),
            ) if value is not None)
        if self.current_information_need is not None:
            resources += ((self._work_identity, "information_batch"),)
        events = tuple(sorted(dict.fromkeys((*self._event_history,
            *(event for work in records for event in work.lifecycle.events))),
            key=lambda event: (event.monotonic_ns, event.identity.revision)))
        return AsyncOwnerDiagnostics(self._owner_instance_id, self._work_identity,
                                    self._work_window, events,
                                    self.admission_records, resources, tuple(self._fact_queries),
                                    planning_work=active,
                                    pending_planning_receipts=tuple(
                                        work.receipt_identity for work in records
                                        if work.receipt_identity is not None))

    @property
    def unidentified_results(self) -> int:
        return self._unidentified_results

    @property
    def bridge_remaining(self) -> int:
        return self._bridge_remaining

    @property
    def required_interaction(self) -> BridgeInteractionPlan | None:
        return self._required_interaction

    @property
    def current_information_update(self) -> PlanningUpdate | None:
        update = self._terminal_update
        return (update if update is not None
                and update.kind is PlanningUpdateKind.NEEDS_INFORMATION else None)

    @property
    def current_information_need(self) -> PlanningInformationNeed | None:
        update = self.current_information_update
        return None if update is None else update.information_need

    @property
    def snapshot(self):
        return self._selected.pipeline.snapshot

    @property
    def has_owned_work(self) -> bool:
        return any(work.active for work in self._works)

    @property
    def local_attempt_failures(self) -> int:
        return self._local_attempts.failure_count

    def failure_matches_retired_work(self, failure: PlanningFailure) -> bool:
        """Validate against our produced terminal notification, never the incoming identity."""
        if type(failure) is not PlanningFailure:
            raise ContractViolation("planning failure verification requires a typed fact")
        update = self._terminal_update
        request = self.request
        origin = self._terminal_failure_origin
        identity = origin.work_identity if type(origin) is PlanningCalculationBasis else origin
        if (self.has_owned_work or identity is None
                or update is None or update.kind is not PlanningUpdateKind.FAILED
                or request is None or identity.key != update.attempt_id
                or identity not in self._retired):
            return False
        return (
            failure.world_session_id == update.world_session_id == request.world_session
            and failure.request_id == update.request_id == request.request_id
            and failure.goal_revision == update.goal_revision == request.goal_revision
            and failure.attempt_id == update.attempt_id
        )

    def clear_submission(self) -> None:
        self._selected.pipeline.clear_submission()

    def retire(
        self,
        identity: AsyncWorkIdentity,
        cause: str,
    ) -> WorkRetirementSummary:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("planning retirement identity must be typed")
        require_identifier(cause, "planning retirement cause")
        previous = self._retired.get(identity)
        if previous is not None:
            return WorkRetirementSummary(identity, previous.cause, True)
        if self._work_identity != identity:
            return WorkRetirementSummary(identity, cause, True)
        self._selected.pipeline.clear()
        self._selected.attempt_id = None
        self._selected.candidate = None
        self._selected.basis_scan = None
        self._selected.basis_scan_revision = None
        summary = self._selected.lifecycle.finish(identity, cause, self._clock())
        self._remember_history(self._retired, identity, summary)
        self._selected.basis = None
        self._terminal_update = None
        self._required_interaction = None
        self._confirmed_interaction_id = None
        self._interaction_approach_pending = False
        self._information_need = None
        self._information_offset = 0
        self._information_unavailable.clear()
        self._pending_information_progress = None
        return summary

    def cancel_work(self, cause: str = "planning_cancelled") -> None:
        self._deferred_failure = None
        self._terminal_failure_origin = None
        self._observation_update = None
        for work in tuple(self._works):
            self._selected = work
            self._retire_active_identity(cause)
        self._prune_work()
        identity = self._work_identity
        if identity is None:
            self._selected.pipeline.clear()
            self._selected.candidate = None
            self._selected.basis_scan = None
            self._selected.basis_scan_revision = None
            self._selected.attempt_id = None
            self._terminal_update = None
            self._selected.basis = None
            self._information_need = None
            self._information_offset = 0
            self._information_unavailable.clear()
            self._pending_information_progress = None
            return
        self.retire(identity, cause)

    def clear_changes(self) -> None:
        self._selected.pipeline.changed_cells.clear()

    def restart_from_current(
        self,
        frame: NavigationFrame,
        *,
        permit: PlanningAttemptPermit,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        request = self._replacement_request(frame, remaining_damage_budget)
        if request is None:
            return self._finish_failure("current_surface_unavailable")
        self.begin(
            request,
            frame,
            permit=permit,
            state_anchor=None,
            remaining_damage_budget=remaining_damage_budget,
        )
        return self._update(
            PlanningUpdateKind.RUNNING,
            "planning_restart_permitted",
        )

    def retry_from_current(
        self,
        frame: NavigationFrame,
        *,
        trigger: PlanningRetryTrigger,
        cause: RetryCause,
        failure_id: str,
        remaining_damage_budget: TaskDamageBudget,
        permit: PlanningAttemptPermit | None = None,
    ) -> PlanningUpdate:
        require_identifier(failure_id, "planning retry failure id")
        if type(trigger) is not PlanningRetryTrigger:
            raise ContractViolation("planning retry trigger must be typed")
        if type(cause) is not RetryCause:
            raise ContractViolation("planning retry cause must be typed")
        if trigger is PlanningRetryTrigger.RECOVERY_REANCHOR:
            if (type(permit) is not PlanningAttemptPermit
                    or permit.kind is not PlanningAttemptPermitKind.RETRY
                    or permit.recovery_identity is None
                    or permit.source_event_id != failure_id):
                raise ContractViolation(
                    "recovery reanchor requires its F8 handoff permit"
                )
        elif permit is not None:
            raise ContractViolation(
                "local planning retry cannot consume a handoff permit"
            )
        registration = self._local_attempts.record(failure_id)
        if registration.verdict is LocalAttemptVerdict.EXHAUSTED:
            return self._finish_failure("planning_retry_exhausted")
        assert self.request is not None
        retry_permit = permit or PlanningAttemptPermit(
            f"{failure_id}/retry",
            self.task_id,
            self.request.goal_revision,
            failure_id,
            PlanningAttemptPermitKind.RETRY,
        )
        return self.restart_from_current(
            frame,
            permit=retry_permit,
            remaining_damage_budget=remaining_damage_budget,
        )

    def confirm_route_admitted(self, route: ActiveRoute) -> None:
        """Reset the local chain only after Session installs this exact route."""
        if type(route) is not ActiveRoute:
            raise ContractViolation("planning admission confirmation must be typed")
        delivered = self._terminal_update
        if (delivered is None
                or delivered.kind is not PlanningUpdateKind.ROUTE_READY
                or delivered.route != route):
            raise ContractViolation(
                "planning admission confirmation is stale or foreign"
            )
        self._local_attempts.reset()

    def confirm_nonplanner_route_admitted(
        self,
        route: ActiveRoute,
        *,
        current_scope: AsyncComputationScope,
    ) -> None:
        """Record progress from an exact route built outside PlannerWorker."""
        if (type(route) is not ActiveRoute
                or type(current_scope) is not AsyncComputationScope):
            raise ContractViolation(
                "non-planner admission confirmation must be typed"
            )
        request = self.request
        expected_scope = self._request_ledger.current_computation_scope
        if (type(request) is not SurfacePlanningRequest
                or current_scope != expected_scope
                or current_scope.task_id != self.task_id
                or current_scope.world_session_id != route.world_session
                or self.has_owned_work
                or request.work_identity is not None
                or route.work_identity is not None
                or route.source_request_id != request.request_id
                or route.goal_id != request.goal_id
                or route.goal_revision != request.goal_revision
                or route.world_session != request.world_session
                or route.planning_generation != request.sequence):
            raise ContractViolation(
                "non-planner admission confirmation is stale or foreign"
            )
        self._local_attempts.reset()

    def confirm_interaction(self, interaction_id: str) -> None:
        require_identifier(interaction_id, "confirmed planning interaction")
        if (self._required_interaction is None
                or self._required_interaction.requirement.interaction_id
                    != interaction_id):
            raise ContractViolation("planning has no matching interaction")
        if self._bridge_remaining < 1:
            raise ContractViolation("planning bridge budget is exhausted")
        self._bridge_remaining -= 1
        self._confirmed_interaction_id = interaction_id

    def interaction_was_confirmed(self, interaction_id: str) -> bool:
        require_identifier(interaction_id, "planning interaction identity")
        return self._confirmed_interaction_id == interaction_id

    def resume_after_confirmed_interaction(
        self,
        frame: NavigationFrame,
        interaction_id: str,
        *,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        require_identifier(interaction_id, "confirmed planning interaction")
        if self._confirmed_interaction_id != interaction_id:
            raise ContractViolation("planning interaction was not confirmed")
        progressed = self._retry_ledger.record_progress(ProgressEvidence(
            ProgressKind.ACTION_COMPLETED,
            frame.body.sequence_id,
            action_id=interaction_id,
        ))
        if not progressed:
            return self._finish_failure("interaction_progress_not_fresh")
        self._local_attempts.reset()
        request = self._replacement_request(frame, remaining_damage_budget)
        if request is None:
            return self._finish_failure("current_surface_unavailable")
        self.begin(
            request,
            frame,
            permit=PlanningAttemptPermit(
                f"{interaction_id}/observation-{frame.body.sequence_id}",
                self.task_id,
                request.goal_revision,
                interaction_id,
                PlanningAttemptPermitKind.PROGRESS,
            ),
            state_anchor=None,
            remaining_damage_budget=remaining_damage_budget,
        )
        self._confirmed_interaction_id = None
        return self._update(
            PlanningUpdateKind.RUNNING,
            "planning_interaction_confirmed",
        )

    def begin(
        self,
        request: PlanningRequest | SurfacePlanningRequest,
        frame: NavigationFrame,
        *,
        permit: PlanningAttemptPermit,
        state_anchor: StateAnchor | None,
        remaining_damage_budget: TaskDamageBudget,
        _append: bool = False,
    ) -> None:
        if type(request) not in (PlanningRequest, SurfacePlanningRequest):
            raise ContractViolation("planning begin requires a typed request")
        if type(frame) is not NavigationFrame:
            raise ContractViolation("planning begin requires a navigation frame")
        if type(permit) is not PlanningAttemptPermit:
            raise ContractViolation("planning begin requires a typed permit")
        if type(remaining_damage_budget) is not TaskDamageBudget:
            raise ContractViolation("planning begin requires a damage budget")
        if permit.permit_id in self._used_permits:
            raise ContractViolation("planning attempt permit was already consumed")
        if permit.task_id != self.task_id:
            raise ContractViolation("planning permit belongs to another task")
        if permit.goal_revision != request.goal_revision:
            raise ContractViolation("planning permit goal revision is stale")
        if request.world_session != frame.session.value:
            raise ContractViolation("planning request belongs to another world")
        if len(self._used_permits) >= 4096:
            raise ContractViolation("planning permit ledger is full")
        if not _append:
            self._deferred_failure = None
            self._terminal_failure_origin = None
            self._observation_update = None
            self._retire_all_work("planning_work_replaced")
        self._prune_work()
        if len(self._works) >= 2:
            # Accepted transport identities keep their slots until a real
            # receipt. Keep only the ledger's latest request while waiting.
            self._request_ledger.accept(request)
            self._used_permits.add(permit.permit_id)
            self._terminal_update = None
            return
        identity = AsyncWorkIdentity(
            self._request_ledger.bind_computation_scope(self.task_id, request.world_session),
            self._owner_instance_id,
            AsyncWorkKind.PLANNING,
            request.request_id,
            self._attempt_sequence + 1,
        )
        started_ns = self._clock()
        window = AsyncWorkWindow(
            self._movement_tick(frame),
            started_ns,
            started_ns + int(
                request.maximum_planning_seconds * 1_000_000_000
            ),
        )
        self._remember_work_window(identity, window)
        self._selected = _PlanningWork()
        self._works.append(self._selected)
        self._used_permits.add(permit.permit_id)
        self._attempt_sequence += 1
        self._selected.lifecycle.begin(identity, window)
        self._selected.attempt_id = identity.key
        request = replace(request, work_identity=self._work_identity)
        if type(request) is SurfacePlanningRequest:
            request = replace(request, damage_budget=remaining_damage_budget)
        self._selected.basis = PlanningCalculationBasis(
            request, permit, self._bounds(request), identity, self.capabilities,
        )
        self._request_ledger.accept(request)
        self._terminal_update = None
        self._required_interaction = None
        self._confirmed_interaction_id = None
        self._interaction_approach_pending = False
        self._information_need = None
        self._information_offset = 0
        self._information_unavailable.clear()
        self._pending_information_progress = None
        self._selected.pipeline.clear()
        self._selected.pipeline.attempt_started_movement_tick = (
            self._work_window.started_movement_tick
        )
        self._selected.pipeline.attempt_started_monotonic_ns = (
            self._work_window.started_monotonic_ns
        )
        self._selected.pipeline.attempt_deadline_monotonic_ns = (
            self._work_window.deadline_monotonic_ns
        )
        self._selected.pipeline.builder = KnownMapSnapshotBuilder(
            frame.world, self._selected.basis.bounds,
        )

    def observe_changes(self, changed_cells: tuple[tuple[int, int, int], ...]) -> None:
        if type(changed_cells) is not tuple:
            raise ContractViolation("planning world changes must be immutable")
        for work in self._works:
            work.pipeline.changed_cells.update(changed_cells)

    def expire_at_observation(
        self, frame: NavigationFrame, *, remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate | None:
        """Retire expired planning before the newly observed frame is reported."""
        if (not self._selected.active or self._boundary_update(frame) is not None
                or not self._attempt_expired(frame)):
            return None
        update = self._retry_or_fail(
            frame, RetryCause.PLANNING, "planning_timeout", remaining_damage_budget,
        )
        if update.kind is PlanningUpdateKind.RUNNING:
            # Starting a retry is this observation's result. Do not advance
            # the new job again when propose consumes the same boundary.
            self._observation_update = (self._movement_tick(frame), update)
        return update

    def _boundary_update(self, frame: NavigationFrame) -> PlanningUpdate | None:
        observation = self._observation_update
        if observation is not None:
            if observation[0] == self._movement_tick(frame):
                return observation[1]
            self._observation_update = None
        update = self._terminal_update
        if update is not None and update.kind is PlanningUpdateKind.RUNNING:
            self._terminal_update = None
            return None
        return update

    def cancel(self, request_id: str) -> None:
        require_identifier(request_id, "cancelled planning request")
        if self.request is not None and self.request.request_id == request_id:
            self.cancel_work("planning_request_cancelled")

    def report_information_outcome(
        self,
        selection_revision: int,
        blocker_key: tuple[tuple[int, int, int], str, str],
        outcome: InformationOutcome,
        *,
        observation_sequence: int | None = None,
    ) -> PlanningUpdate | None:
        """Legacy synchronous component entry; async delivery uses reconcile_information."""
        if type(selection_revision) is not int or selection_revision < 0:
            raise ContractViolation("information selection revision is invalid")
        if type(outcome) is not InformationOutcome:
            raise ContractViolation("information outcome must be typed")
        if outcome is InformationOutcome.ACQUIRED:
            raise ContractViolation(
                "acquired information must pass the typed fact query"
            )
        current = self.current_information_need
        if current is None or current.selection_revision != selection_revision:
            raise ContractViolation("synchronous information outcome is stale or foreign")
        return self._apply_information_outcome(
            selection_revision,
            blocker_key,
            outcome,
            observation_sequence=observation_sequence,
        )

    def confirm_information_fact(
        self,
        selection_revision: int,
        blocker_key: tuple[tuple[int, int, int], str, str],
        frame: NavigationFrame,
        *,
        edge_probe: LandingEdgeProbe | None,
    ) -> PlanningUpdate | None:
        """Legacy synchronous component query; not an asynchronous delivery entry."""
        current = self.current_information_need
        if current is None or current.selection_revision != selection_revision:
            raise ContractViolation("information confirmation is stale or foreign")
        blocker = next(
            (item for item in current.blockers if item.blocker_key == blocker_key),
            None,
        )
        if blocker is None:
            raise ContractViolation("information confirmation names an unselected fact")
        if not self.information_fact_is_acquired(
            blocker, frame, edge_probe=edge_probe,
        ):
            return self._terminal_update
        return self._apply_information_outcome(
            selection_revision,
            blocker_key,
            InformationOutcome.ACQUIRED,
            observation_sequence=frame.body.sequence_id,
        )

    def reconcile_information(
        self, selection: PlanningUpdate, frame: NavigationFrame, *,
        current_scope: AsyncComputationScope,
        edge_probe: LandingEdgeProbe | None = None,
        outcomes: tuple = (),
    ) -> PlanningUpdate:
        """Consume a complete batch once; stale delivery is a normal disposition."""
        if (type(selection) is not PlanningUpdate or type(frame) is not NavigationFrame
                or selection.kind is not PlanningUpdateKind.NEEDS_INFORMATION
                or selection.information_need is None
                or selection.information_identity is None):
            raise ContractViolation("information reconciliation requires a complete notification and frame")
        current = self.current_information_need
        if (current is None or frame.session.value != selection.world_session_id
                or self._selected.lifecycle.check(selection.information_identity, self._clock(),
                                    current_scope=current_scope) is WorkCheck.STALE_SCOPE
                or current != selection.information_need
                or self._work_identity != selection.information_identity
                or self._selected.attempt_id != selection.attempt_id
                or self.current_information_update != selection):
            # A delivered notification outlives the coordinator's active state.
            # Discard it using its original identity, without borrowing a successor.
            return replace(selection, kind=PlanningUpdateKind.DISCARDED,
                           reason="stale_information_batch")
        if self._work_window is None or self._work_window.expired(self._clock()):
            self._record_admission(AsyncAdmissionDisposition.TERMINATED,
                                   facts_valid=None, identity_matched=True)
            return self._finish_failure("information_timeout")
        for blocker in current.blockers:
            if self.information_fact_is_acquired(blocker, frame, edge_probe=edge_probe):
                result = self._apply_information_outcome(
                    current.selection_revision, blocker.blocker_key,
                    InformationOutcome.ACQUIRED,
                    observation_sequence=frame.body.sequence_id,
                )
                if result is not None:
                    return result
                return self._update(PlanningUpdateKind.INFORMATION_ACQUIRED, "information_fact_acquired")
        for key, outcome in outcomes:
            if self.current_information_need is not current:
                break
            result = self.report_information_outcome(
                current.selection_revision, key, outcome,
                observation_sequence=frame.body.sequence_id,
            )
            if result is not None and result.kind is not PlanningUpdateKind.NEEDS_INFORMATION:
                return result
        return self._terminal_update or self._update(PlanningUpdateKind.DISCARDED, "stale_information_batch")

    def _apply_information_outcome(
        self,
        selection_revision: int,
        blocker_key: tuple[tuple[int, int, int], str, str],
        outcome: InformationOutcome,
        *,
        observation_sequence: int | None,
    ) -> PlanningUpdate | None:
        current = self.current_information_need
        if current is None or current.selection_revision != selection_revision:
            return self._terminal_update or self._update(PlanningUpdateKind.DISCARDED, "stale_information_batch")
        if (self._work_identity is None
                or self._work_identity.work_kind is not AsyncWorkKind.INFORMATION
                or self._work_window is None):
            raise ContractViolation("information outcome has no active work identity")
        if self._work_window.expired(self._clock()):
            self._record_admission(
                AsyncAdmissionDisposition.TERMINATED,
                facts_valid=None,
                identity_matched=True,
            )
            return self._finish_failure("information_timeout")
        blocker = next(
            (item for item in current.blockers if item.blocker_key == blocker_key),
            None,
        )
        if blocker is None:
            raise ContractViolation("information outcome names an unselected fact")
        if outcome is InformationOutcome.ACQUIRED:
            if (type(observation_sequence) is not int
                    or observation_sequence < 0):
                raise ContractViolation(
                    "acquired information requires an observation sequence"
                )
            fact_id = self._blocker_id(blocker_key)
            if not self._record_admission(
                AsyncAdmissionDisposition.APPLIED,
                facts_valid=True,
                identity_matched=True,
            ):
                return self._finish_failure("information_timeout")
            progressed = self._retry_ledger.record_progress(
                ProgressEvidence(
                    ProgressKind.BLOCKING_FACT,
                    observation_sequence,
                    fact_id=fact_id,
                )
            )
            if not progressed:
                return self._terminal_update
            self._local_attempts.reset()
            self._pending_information_progress = (
                blocker_key, observation_sequence,
            )
            self._terminal_update = None
            self._complete_active_work("information_fact_acquired")
            return None
        self._information_unavailable.add(blocker_key)
        if not all(
            item.blocker_key in self._information_unavailable
            for item in current.blockers
        ):
            return self._terminal_update
        assert self._information_need is not None
        self._information_offset += len(current.blockers)
        if self._information_offset < len(self._information_need.blockers):
            self._terminal_update = self._information_update()
            return self._terminal_update
        reason = (
            "information_frontier_truncated"
            if self._information_need.truncated
            else "information_frontier_unavailable"
        )
        self._record_admission(
            AsyncAdmissionDisposition.TERMINATED,
            facts_valid=False,
            identity_matched=True,
        )
        return self._finish_failure(reason)

    def information_fact_is_acquired(
        self,
        blocker: PlanningBlocker,
        frame: NavigationFrame,
        *,
        edge_probe: LandingEdgeProbe | None,
    ) -> bool:
        """Re-run the typed fact query before declaring information acquired."""
        if type(blocker) is not PlanningBlocker:
            raise ContractViolation("information query requires a typed blocker")
        if type(frame) is not NavigationFrame:
            raise ContractViolation("information query requires a navigation frame")
        requirement = blocker.fact_requirement
        fact = frame.world.cell(requirement.position)
        if requirement.kind is PlanningFactRequirementKind.CELL_KNOWLEDGE:
            acquired = fact.knowledge is not CellKnowledge.UNKNOWN
        elif requirement.kind is PlanningFactRequirementKind.LANDING_VISUAL_EVIDENCE:
            acquired = direct_drop_visual_evidence_sufficient(
                frame,
                requirement.position,
                edge_probe=edge_probe,
            )
        else:
            raise ContractViolation("unsupported planning fact requirement")
        if self._work_identity is not None and self._work_identity.work_kind is AsyncWorkKind.INFORMATION:
            evidence = AsyncFactQueryEvidence(self._work_identity, frame.body.sequence_id,
                requirement.position, requirement.kind.value, fact, acquired)
            if not self._fact_queries or self._fact_queries[-1] != evidence:
                if len(self._fact_queries) >= 64:
                    del self._fact_queries[0]
                self._fact_queries.append(evidence)
        return acquired

    def resume_after_information(
        self,
        frame: NavigationFrame,
        *,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        if self._pending_information_progress is None:
            raise ContractViolation("planning has no acquired information permit")
        blocker_key, observation_sequence = self._pending_information_progress
        request = self._replacement_request(frame, remaining_damage_budget)
        if request is None:
            return self._finish_failure("current_surface_unavailable")
        source = self._blocker_id(blocker_key)
        self.begin(
            request,
            frame,
            permit=PlanningAttemptPermit(
                f"{source}/observation-{observation_sequence}",
                self.task_id,
                request.goal_revision,
                source,
                PlanningAttemptPermitKind.PROGRESS,
            ),
            state_anchor=None,
            remaining_damage_budget=remaining_damage_budget,
        )
        return self._update(
            PlanningUpdateKind.RUNNING,
            "planning_information_updated",
        )

    def _prune_work(self) -> None:
        for work in tuple(self._works):
            if work.lifecycle.identity is None and work.receipt_identity is None:
                self._event_history.extend(work.lifecycle.events)
                self._event_history = self._event_history[-2 * _PLANNING_HISTORY_LIMIT:]
                for identity, summary in work.lifecycle.retirements.items():
                    self._remember_history(self._retired, identity, summary)
                self._works.remove(work)

    def _select_live_work(self) -> None:
        if not self._selected.active:
            self._selected = next((work for work in self._works if work.active), self._selected)

    def _retire_all_work(self, cause: str) -> None:
        selected = self._selected
        for work in tuple(self._works):
            self._selected = work
            self._retire_active_identity(cause)
        self._selected = selected
        self._prune_work()

    def _poll_results(self) -> None:
        for candidate in self._planner.poll_available():
            work = next((item for item in self._works
                if candidate.work_identity is not None
                and candidate.work_identity in (
                    item.lifecycle.identity, item.receipt_identity)), None)
            if work is None:
                self._discarded_result = True
                if candidate.work_identity is None:
                    self._unidentified_results += 1
                    continue
                self._record_admission(AsyncAdmissionDisposition.DISCARDED_LATE,
                    facts_valid=None, identity_matched=False,
                    result_identity=candidate.work_identity)
                continue
            work.receipt_identity = None
            if work.lifecycle.identity is None:
                self._discarded_result = True
                self._record_admission(AsyncAdmissionDisposition.DISCARDED_LATE,
                    facts_valid=None, identity_matched=False,
                    result_identity=candidate.work_identity)
            elif work.candidate is None:
                work.candidate = candidate
        self._prune_work()

    def _ensure_latest_work(self, frame, state_anchor, remaining_damage_budget) -> None:
        request = self.request
        failure = self._current_deferred_failure()
        if failure is not None:
            return
        if request is None or len(self._works) >= 2 or any(
                work.basis is not None and work.basis.request.request_id == request.request_id
                and work.active for work in self._works):
            return
        selected = self._selected
        permit_id = f"{request.request_id}/task-update"
        if permit_id in self._used_permits:
            if any(work.active for work in self._works):
                # A failed speculative revision cannot repeatedly restart
                # while the older, still legal computation can satisfy it.
                return
            permit_id += f"/capacity-release-{self._attempt_sequence + 1}"
        self.begin(request, frame,
            permit=PlanningAttemptPermit(
                permit_id, self.task_id,
                request.goal_revision, f"request/{request.request_id}",
                PlanningAttemptPermitKind.TASK_UPDATE),
            state_anchor=state_anchor, remaining_damage_budget=remaining_damage_budget,
            _append=True)
        if selected.active:
            self._selected = selected

    def _claim_access_budget(self) -> int:
        builders = sum(work.pipeline.builder is not None and work.active
            for work in self._works)
        budget = min(self._remaining_accesses,
            max(1, self._snapshot_cells_per_step // max(1, builders)))
        self._remaining_accesses -= budget
        return budget

    def advance(
        self, frame: NavigationFrame, *, current_scope: AsyncComputationScope,
        state_anchor: StateAnchor | None, edge_probe: LandingEdgeProbe | None,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        if type(current_scope) is not AsyncComputationScope:
            raise ContractViolation("planning advance requires current computation scope")
        if type(remaining_damage_budget) is not TaskDamageBudget:
            raise ContractViolation("planning advance requires a damage budget")
        boundary = self._boundary_update(frame)
        if boundary is not None:
            return boundary
        if self.request is None or self._selected.attempt_id is None:
            raise ContractViolation("planning advance has no active attempt")
        self._remaining_accesses = self._snapshot_cells_per_step
        self._discarded_result = False
        self._poll_results()
        if frame.session.value != self.request.world_session:
            return self._finish_task_failure("world_session_changed")
        scope_invalidated = False
        for work in tuple(self._works):
            if work.active and work.lifecycle.identity.scope != current_scope:
                scope_invalidated = True
                self._selected = work
                if work.candidate is not None:
                    self._record_admission(AsyncAdmissionDisposition.DISCARDED_LATE,
                        facts_valid=None, identity_matched=False,
                        result_identity=work.candidate.work_identity)
                self._retire_active_identity("planning_scope_invalidated")
        self._prune_work()
        self._ensure_latest_work(frame, state_anchor, remaining_damage_budget)
        if scope_invalidated:
            return self._update(PlanningUpdateKind.RUNNING, "stale_result_discarded")
        # A ready latest result is checked first. An old complete result remains
        # a valid fallback and can avoid submitting the newly prepared work.
        ordered = sorted((work for work in self._works if work.active),
            key=lambda work: (work.candidate is None,
                -(work.basis.request.goal_revision if work.candidate is not None
                  and work.basis is not None else 0)))
        update = None
        processed = set()
        for _ in range(2):
            work = next((work for work in ordered if id(work) not in processed
                and work.active), None)
            if work is None:
                break
            processed.add(id(work))
            self._selected = work
            update = self._advance_work(frame, current_scope=current_scope,
                state_anchor=state_anchor, edge_probe=edge_probe,
                remaining_damage_budget=remaining_damage_budget)
            if update.kind is not PlanningUpdateKind.RUNNING:
                return update
            if (self._selected is not work and self._selected.basis is not None
                    and self._selected.basis.permit.kind in {
                        PlanningAttemptPermitKind.RETRY, PlanningAttemptPermitKind.PROGRESS}):
                return update
            self._prune_work()
            self._ensure_latest_work(frame, state_anchor, remaining_damage_budget)
            ordered.extend(work for work in self._works
                if all(work is not existing for existing in ordered))
            ordered.sort(key=lambda work: (work.candidate is None,
                work.pipeline.builder is None,
                -(work.basis.request.goal_revision if work.candidate is not None
                  and work.basis is not None else 0)))
        active = next((work for work in self._works if work.active), None)
        if active is not None:
            self._selected = active
        else:
            failure = self._current_deferred_failure()
            if failure is not None:
                self._terminal_failure_origin = self._deferred_failure[0]
                self._terminal_update = failure
                self._complete_active_work("planning_failed")
                return failure
        return self._update(PlanningUpdateKind.RUNNING,
            "planning_capacity_wait" if active is None else
            ("goal_revision_planning_started" if update is None else update.reason))

    def _advance_work(
        self,
        frame: NavigationFrame,
        *,
        current_scope: AsyncComputationScope,
        state_anchor: StateAnchor | None,
        edge_probe: LandingEdgeProbe | None,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        request = self._calculation_request
        if request is None or self._selected.attempt_id is None:
            raise ContractViolation("planning advance has no active attempt")
        if type(remaining_damage_budget) is not TaskDamageBudget:
            raise ContractViolation("planning advance requires a damage budget")
        if type(current_scope) is not AsyncComputationScope:
            raise ContractViolation("planning advance requires current computation scope")
        boundary = self._boundary_update(frame)
        if boundary is not None:
            return boundary
        if frame.session.value != request.world_session:
            return self._finish_failure("world_session_changed")
        if self._attempt_expired(frame):
            return self._retry_or_fail(
                frame, RetryCause.PLANNING, "planning_timeout",
                remaining_damage_budget,
            )

        builder = self._selected.pipeline.builder
        if builder is not None:
            budget = self._claim_access_budget()
            if budget == 0:
                return self._update(PlanningUpdateKind.RUNNING, "snapshot_budget_wait")
            progress = builder.advance(frame.world, budget)
            if progress.status is SnapshotBuildStatus.STALE:
                exact_changes = tuple(self._selected.pipeline.changed_cells)
                if builder.accept_changes_outside_bounds(
                    frame.world, exact_changes,
                ):
                    self._selected.pipeline.changed_cells.clear()
                    progress = builder.advance(
                        frame.world, budget,
                    )
                if progress.status is not SnapshotBuildStatus.STALE:
                    if progress.status is SnapshotBuildStatus.BUILDING:
                        return self._update(
                            PlanningUpdateKind.RUNNING, "snapshot_building",
                        )
                    if progress.snapshot is None:
                        return self._finish_calculation_failure(
                            frame, "snapshot_missing_after_completion", remaining_damage_budget,
                        )
                    # Continue below and submit the completed snapshot.
                else:
                    # STALE copied no cells. Its unused reservation is shared
                    # with the bounded check of the already copied facts.
                    self._remaining_accesses += budget
                    relevant_known_change = self._stale_builder_has_known_change(
                        builder, frame,
                    )
                    if relevant_known_change is _BasisChange.CHECKING:
                        return self._update(PlanningUpdateKind.RUNNING,
                            "snapshot_basis_verifying")
                    if relevant_known_change:
                        return self._retry_or_fail(
                            frame,
                            RetryCause.DEPENDENCY,
                            "snapshot_dependency_changed",
                            remaining_damage_budget,
                        )
                    self._selected.pipeline.builder = KnownMapSnapshotBuilder(
                        frame.world, self._selected.basis.bounds,
                    )
                    self._selected.pipeline.changed_cells.clear()
                    return self._update(
                        PlanningUpdateKind.RUNNING, "snapshot_restarted"
                    )
            if progress.status is SnapshotBuildStatus.BUILDING:
                return self._update(PlanningUpdateKind.RUNNING, "snapshot_building")
            if progress.snapshot is None:
                return self._finish_calculation_failure(
                    frame, "snapshot_missing_after_completion", remaining_damage_budget)
            if type(request) is PlanningRequest and progress.missing_cells:
                return self._finish_calculation_failure(
                    frame, "legacy_requires_complete_snapshot", remaining_damage_budget)
            self._selected.pipeline.snapshot = progress.snapshot
            self._selected.pipeline.snapshot_request_id = request.request_id
            self._selected.pipeline.builder = None
            submitted = self._submit(request, progress.snapshot, frame, state_anchor)
            if submitted is PlanningSubmissionStatus.BUSY:
                return self._update(PlanningUpdateKind.RUNNING, "planner_capacity_wait")
            if not submitted:
                return self._finish_calculation_failure(
                    frame, "planner_submission_rejected", remaining_damage_budget)

        if (self._selected.pipeline.builder is None
                and self._selected.pipeline.snapshot is not None
                and self._selected.basis.submitted_request is None):
            submitted = self._submit(request, self._selected.pipeline.snapshot, frame, state_anchor)
            if submitted is PlanningSubmissionStatus.BUSY:
                return self._update(PlanningUpdateKind.RUNNING, "planner_capacity_wait")
            if not submitted:
                return self._finish_calculation_failure(
                    frame, "planner_submission_rejected", remaining_damage_budget)
        candidate = self._selected.candidate
        if candidate is None and not self._planner.is_alive():
            return self._retry_or_fail(
                frame, RetryCause.PLANNING, "planner_worker_died",
                remaining_damage_budget,
            )
        if self._submission_expired(frame):
            return self._retry_or_fail(
                frame, RetryCause.PLANNING, "planning_timeout",
                remaining_damage_budget,
            )
        if candidate is None:
            return self._update(PlanningUpdateKind.RUNNING,
                "stale_result_discarded" if self._discarded_result else "planning_submitted")
        submitted_request = self._submitted_request or request
        identity_matched = (
            candidate.work_identity is not None
            and self._selected.lifecycle.check(candidate.work_identity, self._clock(),
                                 current_scope=current_scope) is WorkCheck.READY
        )
        if (not identity_matched
                or candidate.request_id != submitted_request.request_id
                or candidate.goal_id != submitted_request.goal_id
                or candidate.goal_revision != submitted_request.goal_revision
                or candidate.world_session != submitted_request.world_session):
            if candidate.work_identity is None:
                self._unidentified_results += 1
            else:
                self._record_admission(
                    AsyncAdmissionDisposition.DISCARDED_LATE,
                    facts_valid=None,
                    identity_matched=False,
                    result_identity=candidate.work_identity,
                )
            return self._update(PlanningUpdateKind.RUNNING, "stale_result_discarded")
        self._selected.pipeline.clear_submission()

        basis_change = self._candidate_basis_change(frame, candidate)
        if basis_change is _BasisChange.CHECKING:
            self._selected.candidate = candidate
            return self._update(PlanningUpdateKind.RUNNING, "planning_basis_verifying")
        self._selected.candidate = None
        if basis_change is not None:
            self._record_admission(
                AsyncAdmissionDisposition.RECOMPUTE,
                facts_valid=False,
                identity_matched=True,
            )
            if basis_change is _BasisChange.PROGRESS:
                reason = (
                    "planning_information_updated"
                    if type(candidate) is SurfaceRouteCandidate
                    and candidate.information_need is not None
                    else "planning_world_progressed"
                )
                return self._restart_for_world_progress(
                    frame,
                    candidate,
                    reason,
                    remaining_damage_budget,
                )
            return self._retry_or_fail(
                frame,
                RetryCause.DEPENDENCY,
                "route_dependencies_changed",
                remaining_damage_budget,
            )
        # This candidate's actual fact basis has been checked against this view.
        candidate = replace(candidate, geometry_revision=frame.world.geometry_revision)

        if self._has_revised_request() and (
                candidate.status not in {SurfacePlanningStatus.COMPLETE, PlanningStatus.COMPLETE}
                or self._interaction_approach_pending):
            # Only a complete positive route may be checked for the current
            # goal. Old negative conclusions retain their original scope.
            self._record_admission(AsyncAdmissionDisposition.RECOMPUTE,
                                   facts_valid=True, identity_matched=True)
            return self._start_revised_request(frame, remaining_damage_budget)

        if (type(candidate) is SurfaceRouteCandidate
                and candidate.status is SurfacePlanningStatus.NO_KNOWN_ROUTE
                and candidate.information_need is not None):
            self._information_need = candidate.information_need
            self._information_offset = 0
            self._information_unavailable.clear()
            need = self._selected_information_batch()
            self._retry_ledger.set_blockers(tuple(
                self._blocker_id(blocker.blocker_key)
                for blocker in self._information_need.blockers
            ))
            if not self._record_admission(
                AsyncAdmissionDisposition.APPLIED,
                facts_valid=True,
                identity_matched=True,
            ):
                return self._finish_failure("planning_timeout")
            self._begin_information_work(frame, need)
            self._terminal_update = self._information_update(need)
            return self._terminal_update

        no_route = (
            candidate.status in {
                SurfacePlanningStatus.NO_KNOWN_ROUTE,
                SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE,
            }
            if type(candidate) is SurfaceRouteCandidate
            else candidate.status in {
                PlanningStatus.NO_KNOWN_ROUTE,
                PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE,
            }
        )
        if no_route and self._interaction_approach_pending:
            return self._finish_failure("interaction_work_route_unavailable")
        if (no_route
                and self._bridge_policy is not None
                and self._bridge_remaining > 0
                and type(request) is SurfacePlanningRequest
                and type(candidate) is SurfaceRouteCandidate
                and self._selected.pipeline.snapshot is not None
                and self._selected.pipeline.snapshot_request_id == request.request_id):
            interaction = plan_next_bridge_interaction(
                self._selected.pipeline.snapshot,
                request,
                replace(
                    self._bridge_policy,
                    maximum_blocks=min(
                        self._bridge_policy.maximum_blocks,
                        self._bridge_remaining,
                    ),
                ),
            )
            if interaction is not None:
                self._required_interaction = interaction
                if interaction.work_node == request.start:
                    if not self._record_admission(
                        AsyncAdmissionDisposition.APPLIED,
                        facts_valid=True,
                        identity_matched=True,
                    ):
                        return self._finish_failure("planning_timeout")
                    self._terminal_update = PlanningUpdate(
                        PlanningUpdateKind.REQUIRES_INTERACTION,
                        request.world_session,
                        self._selected.attempt_id,
                        request.request_id,
                        request.goal_revision,
                        "world_interaction_required",
                        request=request,
                        interaction=interaction,
                    )
                    self._complete_active_work("interaction_result_transferred")
                    return self._terminal_update
                approach = replace(
                    request,
                    goal=interaction.work_node,
                    goal_state=None,
                    entry_physics_state=(
                        state_anchor.physics_state
                        if state_anchor is not None
                        and state_anchor.session == frame.session
                        else None
                    ),
                )
                if not self._record_admission(
                    AsyncAdmissionDisposition.APPLIED,
                    facts_valid=True,
                    identity_matched=True,
                ):
                    return self._finish_failure("planning_timeout")
                approach = self._begin_bridge_approach_work(approach)
                submission = self._submit(
                    approach, self._selected.pipeline.snapshot, frame, state_anchor,
                    poll_result=False,
                )
                if submission is PlanningSubmissionStatus.BUSY:
                    self._interaction_approach_pending = True
                    return self._update(PlanningUpdateKind.RUNNING, "planner_capacity_wait")
                if not submission:
                    return self._finish_failure(
                        "interaction_work_submission_rejected"
                    )
                self._interaction_approach_pending = True
                return self._update(
                    PlanningUpdateKind.RUNNING,
                    "interaction_work_route_submitted",
                )

        status = candidate.status
        complete = (
            status is SurfacePlanningStatus.COMPLETE
            if type(candidate) is SurfaceRouteCandidate
            else status is PlanningStatus.COMPLETE
        )
        if not complete:
            if (status in {SurfacePlanningStatus.INTERNAL_ERROR, PlanningStatus.INTERNAL_ERROR}
                    and not self._planner.is_alive()):
                self._record_admission(AsyncAdmissionDisposition.TERMINATED,
                    facts_valid=True, identity_matched=True)
                return self._retry_or_fail(frame, RetryCause.PLANNING,
                    "planner_worker_died", remaining_damage_budget)
            self._record_admission(
                AsyncAdmissionDisposition.TERMINATED,
                facts_valid=True,
                identity_matched=True,
            )
            failure_reason = (
                status.value
                if status in {
                    SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE,
                    PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE,
                }
                else f"planning_{status.value}"
            )
            return self._finish_failure(failure_reason)
        basis = self._selected.basis
        assert basis is not None and self.request is not None
        if not self._candidate_capabilities_current(candidate, state_anchor):
            self._record_admission(AsyncAdmissionDisposition.RECOMPUTE,
                facts_valid=False, identity_matched=True)
            if self._has_revised_request():
                return self._start_revised_request(frame, remaining_damage_budget)
            return self._retry_or_fail(frame, RetryCause.DEPENDENCY,
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED.value, remaining_damage_budget)
        admitted = self._admitter.admit_current_request(
            candidate, submitted_request,
            (submitted_request if self._interaction_approach_pending else self.request), frame,
            remaining_damage_budget=remaining_damage_budget,
            changed_cells=tuple(sorted(self._selected.pipeline.changed_cells)),
            edge_probe=edge_probe,
        )
        if admitted.status is not AdmissionStatus.ACCEPTED or admitted.route is None:
            if self._has_revised_request():
                self._record_admission(AsyncAdmissionDisposition.RECOMPUTE,
                    facts_valid=False, identity_matched=True)
                return self._start_revised_request(frame, remaining_damage_budget)
            if (type(request) is SurfacePlanningRequest
                    and request.damage_budget != remaining_damage_budget
                    and admitted.reason in {
                        AdmissionReason.ROUTE_RISK_POLICY_CHANGED,
                        AdmissionReason.ROUTE_RESOURCES_UNAVAILABLE,
                    }):
                self._record_admission(AsyncAdmissionDisposition.RECOMPUTE,
                    facts_valid=False, identity_matched=True)
                return self._retry_or_fail(frame, RetryCause.PLANNING,
                    "damage_budget_changed_before_admission", remaining_damage_budget)
            if admitted.reason is AdmissionReason.LANDING_VISUAL_EVIDENCE_MISSING:
                snapshot = self._selected.pipeline.snapshot
                assert snapshot is not None
                blockers = tuple(
                    PlanningBlocker(
                        position,
                        PlanningBlockerKind.ACTION_PRECONDITION,
                        "landing-visual-evidence",
                        PlanningFrontierKind.ACTION,
                        f"landing:{request.request_id}",
                        PlanningFactRequirement(
                            PlanningFactRequirementKind.LANDING_VISUAL_EVIDENCE,
                            position,
                        ),
                    )
                    for position in admitted.missing_cells[:128]
                )
                need = PlanningInformationNeed(
                    request.world_session,
                    snapshot.snapshot_id,
                    request.request_id,
                    request.goal_id,
                    request.goal_revision,
                    request.sequence,
                    blockers,
                    len(admitted.missing_cells) > 128,
                )
                self._retry_ledger.set_blockers(tuple(
                    self._blocker_id(blocker.blocker_key)
                    for blocker in need.blockers
                ))
                self._information_need = need
                self._information_offset = 0
                self._information_unavailable.clear()
                if not self._record_admission(
                    AsyncAdmissionDisposition.APPLIED,
                    facts_valid=True,
                    identity_matched=True,
                ):
                    return self._finish_failure("planning_timeout")
                self._begin_information_work(frame, need)
                self._terminal_update = self._update(
                    PlanningUpdateKind.NEEDS_INFORMATION,
                    admitted.reason.value,
                    information_need=need,
                    landing_probe_cell=(admitted.missing_cells[0]
                                        if len(admitted.missing_cells) == 1 else None),
                )
                return self._terminal_update
            if admitted.reason is AdmissionReason.ROUTE_DEPENDENCIES_CHANGED:
                self._record_admission(
                    AsyncAdmissionDisposition.RECOMPUTE,
                    facts_valid=False,
                    identity_matched=True,
                )
                return self._retry_or_fail(
                    frame,
                    RetryCause.DEPENDENCY,
                    admitted.reason.value,
                    remaining_damage_budget,
                )
            self._record_admission(
                AsyncAdmissionDisposition.TERMINATED,
                facts_valid=False,
                identity_matched=True,
            )
            return self._finish_failure(admitted.reason.value)
        if self._attempt_expired(frame):
            return self._retry_or_fail(frame, RetryCause.PLANNING, "planning_timeout", remaining_damage_budget)
        if not self._record_admission(
            AsyncAdmissionDisposition.APPLIED,
            facts_valid=True,
            identity_matched=True,
        ):
            return self._finish_failure("planning_timeout")
        self._terminal_update = self._update(
            PlanningUpdateKind.ROUTE_READY,
            ("interaction_work_route_admitted"
             if self._interaction_approach_pending else "route_admitted"),
            route=admitted.route,
            interaction=self._required_interaction,
        )
        self._complete_active_work("route_result_transferred")
        return self._terminal_update

    def _candidate_capabilities_current(self, candidate, state_anchor) -> bool:
        basis = self._selected.basis
        assert basis is not None
        if basis.capabilities != self.capabilities:
            return False
        profiles = (self.capabilities.ground, self.capabilities.step,
                    self.capabilities.jump_up, *self.capabilities.air)
        if self.capabilities.ground_modes is not None:
            profiles += tuple(profile.motion for profile in self.capabilities.ground_modes.modes.values())
        available = {(profile.environment_id, profile.profile_id) for profile in profiles}
        if any(edge.transition is not None and (
                edge.transition.environment_id, edge.transition.trajectory_profile_id) not in available
                for edge in candidate.segments):
            return False
        ruleset_id = (state_anchor.ruleset_id if state_anchor is not None
                      else JAVA_1_21_RULESET.ruleset_id)
        return all(plan.ruleset_id == ruleset_id
            and (state_anchor is None or plan.input_projection_version == state_anchor.input_projection_version)
            and plan.profile_id in {profile.profile_id for profile in profiles}
            for plan in getattr(candidate, 'ground_traversal_plans', ()))

    def diagnostics(self, frame: NavigationFrame) -> PlanningWorkDiagnostics:
        self._select_live_work()
        request = self._calculation_request
        movement_tick = self._movement_tick(frame)
        now = self._clock()
        has_builder = self._selected.pipeline.builder is not None
        has_submission = self._selected.pipeline.submitted_request_id is not None
        retained = self._terminal_update
        has_retained_route = (
            retained is not None
            and retained.kind is PlanningUpdateKind.ROUTE_READY
        )
        has_owned_work = self.has_owned_work or any(
            work.receipt_identity is not None for work in self._works)
        capacity_wait_valid = (has_owned_work and not any(work.active for work in self._works)
            and all(work.receipt_identity is not None for work in self._works))
        builder_valid = (
            has_builder
            and self._selected.pipeline.submitted_request_id is None
            and self._work_identity is not None
            and self._work_window is not None
            and self._selected.pipeline.attempt_deadline_monotonic_ns
                == self._work_window.deadline_monotonic_ns
            and not self._attempt_expired(frame)
        )
        maximum_ticks = (
            0 if request is None else
            max(1, math.ceil(request.maximum_planning_seconds * 20.0))
        )
        submission_valid = (
            has_submission
            and request is not None
            and self._submitted_request is not None
            and self._selected.pipeline.submitted_request_id == request.request_id
            and self._submitted_request.request_id == request.request_id
            and self._selected.pipeline.submitted_movement_tick is not None
            and self._selected.pipeline.result_deadline_monotonic_ns is not None
            and now < self._selected.pipeline.result_deadline_monotonic_ns
            and movement_tick - self._selected.pipeline.submitted_movement_tick
                < maximum_ticks
        )
        retained_route_valid = (
            has_retained_route
            and request is not None
            and retained is not None
            and retained.world_session_id == request.world_session
            and retained.request_id == request.request_id
            and retained.goal_revision == request.goal_revision
            and retained.attempt_id == self._selected.attempt_id
            and retained.route is not None
        )
        permit = self._active_permit
        permit_valid = (
            request is not None
            and permit is not None
            and permit.permit_id in self._used_permits
            and permit.task_id == self.task_id
            and permit.goal_revision == request.goal_revision
            and self._work_identity is not None
            and self._selected.lifecycle.check(self._work_identity, now,
                current_scope=self._request_ledger.current_computation_scope)
                in (WorkCheck.READY, WorkCheck.DUPLICATE)
        )
        need = self.current_information_need
        information_valid = True
        if need is not None:
            information_valid = (
                request is not None
                and need.world_session_id == request.world_session
                and bool(need.snapshot_id)
                and need.request_id == request.request_id
                and need.goal_id == request.goal_id
                and need.goal_revision == request.goal_revision
                and bool(need.blockers)
                and all(
                    type(blocker.kind) is PlanningBlockerKind
                    and type(blocker.frontier_kind) is PlanningFrontierKind
                    and type(blocker.fact_requirement)
                        is PlanningFactRequirement
                    and bool(blocker.requirement_key)
                    and bool(blocker.frontier_key)
                    for blocker in need.blockers
                )
            )
        information_work_valid = (
            has_owned_work
            and self._work_identity is not None
            and self._work_identity.work_kind is AsyncWorkKind.INFORMATION
            and self._work_window is not None
            and not self._work_window.expired(now)
            and need is not None
            and information_valid
        )
        candidate_valid = (self._selected.candidate is not None
            and self._selected.active and self._work_window is not None
            and not self._attempt_expired(frame))
        prepared_valid = (self._selected.pipeline.snapshot is not None
            and self._selected.basis is not None
            and self._selected.basis.submitted_request is None
            and self._selected.active and not self._attempt_expired(frame))
        planning_work_valid = builder_valid or submission_valid or candidate_valid or prepared_valid
        return PlanningWorkDiagnostics(
            self._selected.attempt_id,
            None if request is None else request.request_id,
            None if request is None else request.goal_revision,
            has_owned_work,
            planning_work_valid or information_work_valid or capacity_wait_valid,
            permit_valid or information_work_valid or capacity_wait_valid or not has_owned_work,
            information_valid,
            self._selected.pipeline.submitted_request_id,
            movement_tick,
            (None if self._work_window is None
             else self._work_window.deadline_monotonic_ns),
        )

    def _begin_bridge_approach_work(self, request: SurfacePlanningRequest) -> SurfacePlanningRequest:
        """Transfer the consumed result to fresh work within its parent window."""
        basis, window = self._selected.basis, self._work_window
        assert basis is not None and window is not None
        self._selected.lifecycle.finish(basis.work_identity, "planning_result_transferred_to_bridge_approach", self._clock())
        self._attempt_sequence += 1
        identity = AsyncWorkIdentity(basis.scope, self._owner_instance_id,
            AsyncWorkKind.PLANNING, request.request_id, self._attempt_sequence)
        self._selected.lifecycle.begin(identity, window)
        self._remember_work_window(identity, window)
        request = replace(request, work_identity=identity)
        self._selected.basis = replace(basis, request=request,
            work_identity=identity, submitted_request=None)
        return request

    def _submit(self, request, snapshot, frame, state_anchor, *, poll_result=True):
        assert self._selected.basis is not None
        capabilities = self._selected.basis.capabilities
        if type(request) is SurfacePlanningRequest:
            planning_request = replace(
                request,
                entry_physics_state=(
                    state_anchor.physics_state
                    if state_anchor is not None and state_anchor.session == frame.session
                    else None
                ),
            )
            mode = (
                None if capabilities.ground_modes is None
                else capabilities.ground_modes.require(MovementMode.WALK)
            )
            submitted = self._planner.submit_surface_snapshot(
                snapshot,
                capabilities.ground,
                capabilities.step,
                planning_request,
                capabilities.jump_up,
                air_profiles=capabilities.air,
                ground_mode_profile=mode,
            )
        else:
            submitted = self._planner.submit_snapshot(
                snapshot,
                capabilities.ground,
                request,
                capabilities.jump_up,
            )
        if submitted:
            self._selected.receipt_identity = request.work_identity
            self._selected.basis = replace(
                self._selected.basis,
                submitted_request=(planning_request if type(request) is SurfacePlanningRequest else request),
            )
            now = self._clock()
            self._selected.pipeline.submitted_request_id = request.request_id
            self._selected.pipeline.submitted_movement_tick = self._movement_tick(frame)
            self._selected.pipeline.submitted_monotonic_ns = now
            if self._work_window is None:
                raise ContractViolation("planning submission has no work window")
            self._selected.pipeline.result_deadline_monotonic_ns = (
                self._work_window.deadline_monotonic_ns
            )
            if poll_result:
                self._poll_results()
        return submitted

    def _submission_expired(self, frame: NavigationFrame) -> bool:
        request = self._calculation_request
        pipeline = self._selected.pipeline
        if (request is None
                or pipeline.submitted_request_id != request.request_id
                or pipeline.submitted_movement_tick is None
                or pipeline.result_deadline_monotonic_ns is None):
            return False
        return self._attempt_expired(frame)

    def _attempt_expired(self, frame: NavigationFrame) -> bool:
        request = self._calculation_request
        window = self._work_window
        if request is None or window is None:
            return False
        maximum_ticks = max(
            1, math.ceil(request.maximum_planning_seconds * 20.0),
        )
        return (
            window.expired(self._clock())
            or self._movement_tick(frame) - window.started_movement_tick
                >= maximum_ticks
        )

    def _stale_builder_has_known_change(
        self,
        builder: KnownMapSnapshotBuilder,
        frame: NavigationFrame,
    ) -> bool | _BasisChange:
        changes = tuple(self._selected.pipeline.changed_cells)
        if not changes:
            # The section revision changed without an exact change list.  It
            # is unsafe to label that as harmless knowledge gain.
            return True
        if self._selected.basis_scan_revision != frame.world.geometry_revision:
            self._selected.basis_scan = iter(position for position in changes
                if builder.contains_position(position) and builder.was_scanned(position)
                and builder.copied_fact(position) is not None)
            self._selected.basis_scan_revision = frame.world.geometry_revision
        limit = max(1, self._snapshot_cells_per_step
            // max(1, sum(work.active for work in self._works)))
        for _ in range(limit):
            position = next(self._selected.basis_scan, None)
            if position is None:
                self._selected.basis_scan = None
                self._selected.basis_scan_revision = None
                return False
            if (not builder.contains_position(position)
                    or not builder.was_scanned(position)):
                continue
            old = builder.copied_fact(position)
            if old is None:
                # A scanned UNKNOWN becoming known is progress, not a failed
                # attempt.  The restarted snapshot will include it.
                continue
            if self._remaining_accesses == 0:
                self._selected.basis_scan = chain((position,), self._selected.basis_scan)
                return _BasisChange.CHECKING
            self._remaining_accesses -= 1
            current = frame.world.cell(position)
            if (old.knowledge is not current.knowledge
                    or old.block != current.block):
                self._selected.basis_scan = None
                self._selected.basis_scan_revision = None
                return True
        position = next(self._selected.basis_scan, None)
        if position is None:
            self._selected.basis_scan = None
            self._selected.basis_scan_revision = None
            return False
        self._selected.basis_scan = chain((position,), self._selected.basis_scan)
        return _BasisChange.CHECKING

    def _candidate_basis_change(self, frame: NavigationFrame, candidate):
        snapshot = self._selected.pipeline.snapshot
        if snapshot is None:
            return None
        if snapshot.world.geometry_revision == frame.world.geometry_revision:
            return None
        complete = candidate.status in {SurfacePlanningStatus.COMPLETE, PlanningStatus.COMPLETE}
        if self._selected.basis_scan_revision != frame.world.geometry_revision:
            changes = frame.world.changes_since(snapshot.world.geometry_revision)
            dependencies = set(candidate.dependencies) if complete else set()
            if changes is not None:
                positions = (position for position in changes
                             if (position in dependencies if complete
                                 else self._snapshot_contains(snapshot.bounds, position)))
            elif complete:
                positions = iter(candidate.dependencies)
            else:
                bounds = snapshot.bounds
                positions = ((x, y, z)
                             for x in range(bounds.min_x, bounds.max_x + 1)
                             for y in range(bounds.min_feet_y - 1 - COLLISION_OWNER_BELOW_REACH_CELLS,
                                            bounds.max_feet_y + 2 + bounds.extra_top_clearance_cells)
                             for z in range(bounds.min_z, bounds.max_z + 1))
            self._selected.basis_scan = iter(positions)
            self._selected.basis_scan_revision = frame.world.geometry_revision
            self._selected.basis_scan_progress = False
        checked = 0
        limit = max(1, self._snapshot_cells_per_step
            // max(1, sum(work.active for work in self._works)))
        while True:
            position = next(self._selected.basis_scan, None)
            if position is None:
                self._selected.basis_scan = None
                self._selected.basis_scan_revision = None
                return _BasisChange.PROGRESS if self._selected.basis_scan_progress else None
            if self._remaining_accesses == 0 or checked >= limit:
                self._selected.basis_scan = chain((position,), self._selected.basis_scan)
                return _BasisChange.CHECKING
            self._remaining_accesses -= 1
            checked += 1
            before = snapshot.world.cell(position)
            current = frame.world.cell(position)
            if (before.knowledge is current.knowledge
                    and before.block == current.block):
                continue
            if (before.knowledge is CellKnowledge.UNKNOWN
                    and current.knowledge is not CellKnowledge.UNKNOWN):
                self._selected.basis_scan_progress = True
                continue
            self._selected.basis_scan = None
            self._selected.basis_scan_revision = None
            return _BasisChange.DEPENDENCY
        return _BasisChange.CHECKING

    @staticmethod
    def _snapshot_contains(bounds: KnownMapBounds, position) -> bool:
        x, y, z = position
        return (
            bounds.min_x <= x <= bounds.max_x
            and bounds.min_z <= z <= bounds.max_z
            and bounds.min_feet_y - 1
                - COLLISION_OWNER_BELOW_REACH_CELLS <= y
            <= bounds.max_feet_y + 1 + bounds.extra_top_clearance_cells
        )

    def _restart_for_world_progress(
        self,
        frame: NavigationFrame,
        candidate,
        reason: str,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        if self._has_revised_request():
            return self._start_revised_request(frame, remaining_damage_budget)
        blockers = (
            () if type(candidate) is not SurfaceRouteCandidate
            or candidate.information_need is None
            else candidate.information_need.blockers
        )
        fact_id = (
            self._blocker_id(blockers[0].blocker_key)
            if blockers else
            f"planning-world/{self._selected.attempt_id}/{frame.body.sequence_id}"
        )
        if blockers:
            self._retry_ledger.set_blockers(tuple(
                self._blocker_id(blocker.blocker_key)
                for blocker in blockers
            ))
        else:
            self._retry_ledger.set_blockers((fact_id,))
        progressed = self._retry_ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT,
            frame.body.sequence_id,
            fact_id=fact_id,
        ))
        if not progressed:
            return self._finish_failure("planning_progress_not_fresh")
        self._local_attempts.reset()
        request = self._replacement_request(frame, remaining_damage_budget)
        if request is None:
            return self._finish_failure("current_surface_unavailable")
        self.begin(
            request,
            frame,
            permit=PlanningAttemptPermit(
                f"{fact_id}/observation-{frame.body.sequence_id}",
                self.task_id,
                request.goal_revision,
                fact_id,
                PlanningAttemptPermitKind.PROGRESS,
            ),
            state_anchor=None,
            remaining_damage_budget=remaining_damage_budget,
        )
        return self._update(PlanningUpdateKind.RUNNING, reason)

    def _bounds(self, request) -> KnownMapBounds:
        if type(request) is SurfacePlanningRequest:
            start_x, start_z, start_y = (
                request.start.column_x,
                request.start.column_z,
                request.start.vertical_band,
            )
            goal_x, goal_z, goal_y = (
                request.goal.column_x,
                request.goal.column_z,
                request.goal.vertical_band,
            )
        else:
            start_x, start_y, start_z = request.start
            goal_x, goal_y, goal_z = request.goal
        extra_top = max((
            math.ceil(max(
                (point[1] for point in profile.reference_positions),
                default=0.0,
            ))
            for profile in self.capabilities.air
        ), default=0)
        margin = self._planning_margin
        return KnownMapBounds(
            min(start_x, goal_x) - margin,
            max(start_x, goal_x) + margin,
            min(start_y, goal_y),
            max(start_y, goal_y),
            min(start_z, goal_z) - margin,
            max(start_z, goal_z) + margin,
            True,
            max(0, extra_top),
        )

    def _update(
        self, kind, reason, *, route=None, information_need=None,
        interaction=None, landing_probe_cell=None,
    ):
        assert self.request is not None and self._selected.attempt_id is not None
        return PlanningUpdate(
            kind,
            self.request.world_session,
            self._selected.attempt_id,
            self.request.request_id,
            self.request.goal_revision,
            reason,
            request=self.request,
            route=route,
            information_need=information_need,
            interaction=interaction,
            landing_probe_cell=landing_probe_cell,
            information_identity=(self._work_identity
                                  if kind is PlanningUpdateKind.NEEDS_INFORMATION else None),
        )

    def _failure_update(self, failure_reason: str, reason: str | None):
        assert self.request is not None and self._selected.attempt_id is not None
        return PlanningUpdate(
            PlanningUpdateKind.FAILED,
            self.request.world_session,
            self._selected.attempt_id,
            self.request.request_id,
            self.request.goal_revision,
            reason or failure_reason,
            request=self.request,
            failure=PlanningFailure(failure_reason, self.request.world_session,
                                    self._selected.attempt_id, self.request.request_id,
                                    self.request.goal_revision),
        )
    def _current_deferred_failure(self) -> PlanningUpdate | None:
        deferred = self._deferred_failure
        if deferred is None:
            return None
        origin, update = deferred
        identity = origin.work_identity if type(origin) is PlanningCalculationBasis else origin
        if (self.request is None or update.request_id != self.request.request_id
                or update.goal_revision != self.request.goal_revision
                or identity.scope != self._request_ledger.current_computation_scope):
            self._deferred_failure = None
            return None
        return update

    def _finish_failure(self, failure_reason: str, *, reason: str | None = None):
        if self._work_identity is not None and any(
                work is not self._selected and work.active
                and work.lifecycle.identity.scope == self._request_ledger.current_computation_scope
                and not work.lifecycle.window.expired(self._clock())
                for work in self._works):
            self._deferred_failure = (self._selected.basis or self._work_identity,
                self._failure_update(failure_reason, reason))
            self._retire_active_identity("planning_work_failed_waiting_for_alternative")
            self._terminal_update = None
            return self._update(PlanningUpdateKind.RUNNING, "planning_waits_for_other_work")
        return self._finish_task_failure(failure_reason, reason=reason)

    def _finish_task_failure(self, failure_reason: str, *, reason: str | None = None):
        self._selected.pipeline.clear()
        self._terminal_failure_origin = (self._selected.basis or self._work_identity
            or next((identity for identity in self._known_work_windows
                if identity.key == self._selected.attempt_id), None))
        self._terminal_update = self._failure_update(failure_reason, reason)
        self._complete_active_work("planning_failed")
        return self._terminal_update

    def _retry_or_fail(
        self,
        frame: NavigationFrame,
        cause: RetryCause,
        failure_reason: str,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        assert self._selected.attempt_id is not None and self.request is not None
        if cause is RetryCause.DEPENDENCY:
            self._request_ledger.invalidate_computation(ComputationInvalidationCause.BASIS_INVALIDATED)
        if self._has_revised_request():
            return self._start_revised_request(frame, remaining_damage_budget)
        failed_attempt = self._selected.attempt_id
        registration = self._local_attempts.record(failed_attempt)
        if cause is RetryCause.PLANNING and any(
                work is not self._selected and work.active
                for work in self._works):
            self._retire_active_identity(failure_reason)
            return self._update(PlanningUpdateKind.RUNNING,
                f"{failure_reason}_waits_for_other_work")
        if registration.verdict is LocalAttemptVerdict.EXHAUSTED:
            return self._finish_failure(
                f"{failure_reason}_retry_exhausted",
            )
        request = self._replacement_request(frame, remaining_damage_budget)
        if request is None:
            _, missing = self._surface_for_body(frame)
            return self._finish_failure(
                "current_surface_requires_information"
                if missing else "current_surface_unavailable"
            )
        self.begin(
            request,
            frame,
            permit=PlanningAttemptPermit(
                f"{failed_attempt}/retry",
                self.task_id,
                request.goal_revision,
                failed_attempt,
                PlanningAttemptPermitKind.RETRY,
            ),
            state_anchor=None,
            remaining_damage_budget=remaining_damage_budget,
        )
        return self._update(
            PlanningUpdateKind.RUNNING,
            f"{failure_reason}_retry_started",
        )

    def _has_revised_request(self) -> bool:
        return (self._selected.basis is not None and self.request is not None
                and self.request.request_id != self._selected.basis.request.request_id)

    def _finish_calculation_failure(self, frame, reason, remaining_damage_budget):
        if self._has_revised_request():
            return self._start_revised_request(frame, remaining_damage_budget)
        return self._finish_failure(reason)

    def _start_revised_request(self, frame, remaining_damage_budget) -> PlanningUpdate:
        """Retire the old conclusion and consume a task-update permit only."""
        self._retire_active_identity("planning_revision_result_unusable")
        self._prune_work()
        self._ensure_latest_work(frame, None, remaining_damage_budget)
        return self._update(PlanningUpdateKind.RUNNING, "goal_revision_planning_started")

    def _replacement_request(
        self,
        frame: NavigationFrame,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningRequest | SurfacePlanningRequest | None:
        assert self.request is not None
        request = self.request
        if type(request) is SurfacePlanningRequest:
            start, _ = self._surface_for_body(frame)
            if start is None:
                return None
            return self._request_ledger.advance(
                self._request_id_prefix,
                start=start,
                damage_budget=remaining_damage_budget,
            )
        x, y, z = frame.body.position
        return self._request_ledger.advance(
            self._request_id_prefix,
            start=(math.floor(x), math.floor(y), math.floor(z)),
        )

    def _selected_information_batch(self) -> PlanningInformationNeed:
        assert self._information_need is not None
        selected = self._information_need.blockers[
            self._information_offset:self._information_offset + 64
        ]
        return replace(
            self._information_need,
            selection_revision=(
                self._information_need.selection_revision
                + self._information_offset // 64
            ),
            blockers=selected,
            truncated=(
                self._information_need.truncated
                or self._information_offset + len(selected)
                    < len(self._information_need.blockers)
            ),
        )

    def _information_update(
        self,
        need: PlanningInformationNeed | None = None,
    ) -> PlanningUpdate:
        if need is None:
            need = self._selected_information_batch()
        return self._update(
            PlanningUpdateKind.NEEDS_INFORMATION,
            "no_known_route_requires_information",
            information_need=need,
        )

    @staticmethod
    def _surface_for_body(
        frame: NavigationFrame,
    ) -> tuple[SurfaceNodeId | None, tuple[tuple[int, int, int], ...]]:
        x, y, z = frame.body.position
        body = frame.body.body_box
        max_x = math.floor(math.nextafter(body.max_x, -math.inf))
        max_z = math.floor(math.nextafter(body.max_z, -math.inf))
        candidates = []
        missing: set[tuple[int, int, int]] = set()
        for column_x in range(math.floor(body.min_x), max_x + 1):
            for column_z in range(math.floor(body.min_z), max_z + 1):
                result = query_support_surfaces(
                    frame.world, column_x, column_z, y - 1.0, y + 1.0,
                )
                missing.update(result.missing_cells)
                for surface in result.surfaces:
                    region = surface.region
                    if (min(body.max_x, region.max_x)
                            > max(body.min_x, region.min_x) + 1.0e-9
                            and min(body.max_z, region.max_z)
                            > max(body.min_z, region.min_z) + 1.0e-9):
                        candidates.append(surface)
        if not candidates:
            return None, tuple(sorted(missing))
        return min(
            candidates,
            key=lambda surface: (
                abs(surface.position[1] - body.min_y),
                math.hypot(
                    surface.position[0] - x,
                    surface.position[2] - z,
                ),
                surface.node_id,
            ),
        ).node_id, tuple(sorted(missing))

    @staticmethod
    def _movement_tick(frame: NavigationFrame) -> int:
        return (
            frame.body.movement_tick_id
            if frame.body.movement_tick_id is not None
            else frame.body.sequence_id
        )

    @staticmethod
    def _blocker_id(key) -> str:
        position, requirement, frontier = key
        payload = (
            f"{position[0]},{position[1]},{position[2]}|"
            f"{requirement}|{frontier}"
        ).encode("utf-8")
        return f"planning-blocker/{hashlib.sha256(payload).hexdigest()[:24]}"

    def _begin_information_work(
        self,
        frame: NavigationFrame,
        need: PlanningInformationNeed,
    ) -> None:
        self._retire_active_identity(
            "planning_result_transferred_to_information"
        )
        now = self._clock()
        identity = AsyncWorkIdentity(
            self._request_ledger.current_computation_scope,
            self._owner_instance_id,
            AsyncWorkKind.INFORMATION,
            f"{need.request_id}:{need.selection_revision}",
            max(1, self._attempt_sequence),
        )
        window = AsyncWorkWindow(
            self._movement_tick(frame),
            now,
            now + _INFORMATION_WORK_LIMIT_NS,
        )
        self._selected.lifecycle.begin(identity, window)
        self._selected.attempt_id = identity.key
        self._remember_work_window(self._work_identity, self._work_window)
        self._selected.basis = None
        self._selected.pipeline.clear()

    def _complete_active_work(self, cause: str) -> None:
        require_identifier(cause, "planning completion cause")
        self._deferred_failure = None
        if self._terminal_update is None or self._terminal_update.kind is not PlanningUpdateKind.FAILED:
            self._terminal_failure_origin = None
        self._observation_update = None
        self._retire_active_identity(cause)
        self._retire_all_work("planning_result_already_delivered")

    def _retire_active_identity(self, cause: str) -> None:
        require_identifier(cause, "planning retirement cause")
        identity = self._work_identity
        self._selected.pipeline.clear()
        self._selected.candidate = None
        self._selected.basis_scan = None
        self._selected.basis_scan_revision = None
        if identity is not None:
            summary = self._selected.lifecycle.finish(identity, cause, self._clock())
            self._remember_history(self._retired, identity, summary)
        self._selected.basis = None

    def _record_admission(
        self,
        disposition: AsyncAdmissionDisposition,
        *,
        facts_valid: bool | None,
        identity_matched: bool,
        result_identity: AsyncWorkIdentity | None = None,
    ) -> bool:
        identity = result_identity or self._work_identity
        if identity is None:
            self._unidentified_results += 1
            return False
        window = self._known_work_windows.get(identity)
        deadline = 0 if window is None else window.deadline_monotonic_ns
        now = self._clock()
        applied = True
        if disposition is AsyncAdmissionDisposition.APPLIED:
            applied = self._selected.lifecycle.try_apply(identity, now,
                current_scope=self._request_ledger.current_computation_scope)
            if not applied:
                disposition = AsyncAdmissionDisposition.TERMINATED
                facts_valid = None
        record = AsyncAdmissionRecord(
            identity,
            now,
            deadline,
            identity_matched,
            facts_valid,
            disposition,
        )
        self._last_admission = record
        if len(self._admission_records) >= 64:
            del self._admission_records[0]
        self._admission_records.append(record)
        return applied

    def _remember_work_window(
        self,
        identity: AsyncWorkIdentity,
        window: AsyncWorkWindow,
    ) -> None:
        self._remember_history(self._known_work_windows, identity, window)

    def _remember_history(self, history: dict, identity: AsyncWorkIdentity, value) -> None:
        if identity not in history and len(history) >= _PLANNING_HISTORY_LIMIT:
            protected = {identity for work in self._works
                for identity in (work.lifecycle.identity, work.receipt_identity)
                if identity is not None}
            for origin in (self._terminal_failure_origin,
                    None if self._deferred_failure is None else self._deferred_failure[0]):
                if origin is not None:
                    protected.add(origin.work_identity
                        if type(origin) is PlanningCalculationBasis else origin)
            oldest = next((item for item in history if item not in protected), None)
            if oldest is None:
                raise PlanningHistoryCapacityExceeded("planning history retains only owned identities")
            del history[oldest]
        history[identity] = value
