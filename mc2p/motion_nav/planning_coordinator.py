"""Own snapshot, background planning and route-admission behaviour.

The coordinator deliberately does not own navigation lifecycle state or body
control.  It turns one permitted planning attempt into a typed update for the
session router.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import math
from typing import Callable

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.async_work import (
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
    ReplacementPlanningFailure,
)
from mc2p.motion_nav.online_motion import StateAnchor
from mc2p.motion_nav.planner_worker import PlannerWorkerPort
from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence,
    ProgressKind,
    RetryCause,
    RetryLedger,
    RetryVerdict,
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


class PlanningAttemptPermitKind(StrEnum):
    TASK_UPDATE = "task_update"
    PROGRESS = "progress"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class PlanningAttemptPermit:
    permit_id: str
    task_id: str
    goal_revision: int
    source_event_id: str
    kind: PlanningAttemptPermitKind

    def __post_init__(self) -> None:
        require_identifier(self.permit_id, "planning permit id")
        require_identifier(self.task_id, "planning permit task")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("planning permit goal revision is invalid")
        require_identifier(self.source_event_id, "planning permit source event")
        if type(self.kind) is not PlanningAttemptPermitKind:
            raise ContractViolation("planning permit kind must be typed")


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

    def __post_init__(self) -> None:
        require_identifier(self.reason, "planning failure reason")


@dataclass(frozen=True, slots=True)
class PlanningUpdate:
    kind: PlanningUpdateKind
    world_session_id: str
    attempt_id: str
    request_id: str
    goal_revision: int
    reason: str
    route: ActiveRoute | None = None
    information_need: PlanningInformationNeed | None = None
    interaction: BridgeInteractionPlan | None = None
    failure: PlanningFailure | None = None
    information_identity: AsyncWorkIdentity | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not PlanningUpdateKind:
            raise ContractViolation("planning update kind must be typed")
        require_identifier(self.world_session_id, "planning update world")
        require_identifier(self.attempt_id, "planning update attempt")
        require_identifier(self.request_id, "planning update request")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("planning update goal revision is invalid")
        require_identifier(self.reason, "planning update reason")
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
        self._pipeline = pipeline or PlanningPipelineState()
        if request_ledger is not None and type(request_ledger) is not GoalRequestLedger:
            raise ContractViolation("planning request ledger must be typed")
        self._request_ledger = request_ledger or GoalRequestLedger()
        self._request_id_prefix = request_id_prefix or task_id
        require_identifier(self._request_id_prefix, "planning request prefix")
        self._attempt_sequence = 0
        self._attempt_id: str | None = None
        from mc2p.motion_nav.async_work import AsyncWorkLifecycle, AsyncOwnerScope
        self._work = AsyncWorkLifecycle()
        self._owner_instance_id = AsyncOwnerScope().allocate()
        self._last_admission: AsyncAdmissionRecord | None = None
        self._admission_records: list[AsyncAdmissionRecord] = []
        self._known_work_windows: dict[AsyncWorkIdentity, AsyncWorkWindow] = {}
        self._unidentified_results = 0
        self._retired = self._work.retirements
        self._work_active = False
        self._active_permit: PlanningAttemptPermit | None = None
        self._used_permits: set[str] = set()
        self._terminal_update: PlanningUpdate | None = None
        self._submitted_request: PlanningRequest | SurfacePlanningRequest | None = None
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
        self._basis_candidate = None
        self._basis_scan = None
        self._basis_scan_revision = None
        self._basis_scan_progress = False
        self._fact_queries: list[AsyncFactQueryEvidence] = []

    @property
    def pipeline(self) -> PlanningPipelineState:
        return self._pipeline

    @property
    def request(self) -> PlanningRequest | SurfacePlanningRequest | None:
        return self._request_ledger.request

    @property
    def attempt_id(self) -> str | None:
        return self._attempt_id

    @property
    def work_identity(self) -> AsyncWorkIdentity | None:
        return self._work_identity

    @property
    def _work_identity(self):
        return self._work.identity

    @property
    def _work_window(self):
        return self._work.window

    @property
    def work_window(self) -> AsyncWorkWindow | None:
        return self._work_window

    @property
    def last_admission(self) -> AsyncAdmissionRecord | None:
        return self._last_admission

    @property
    def admission_records(self) -> tuple[AsyncAdmissionRecord, ...]:
        return tuple(self._admission_records)

    @property
    def async_diagnostics(self) -> AsyncOwnerDiagnostics:
        resources = tuple((self._work_identity, name) for name, value in (
            ("builder", self._pipeline.builder),
            ("snapshot", self._pipeline.snapshot),
            ("submitted_request", self._submitted_request),
            ("candidate", self._basis_candidate),
            ("information_batch", self.current_information_need),
        ) if value is not None)
        return AsyncOwnerDiagnostics(self._owner_instance_id, self._work_identity,
                                    self._work_window, self._work.events,
                                    self.admission_records, resources, tuple(self._fact_queries))

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
        return self._pipeline.snapshot

    @property
    def has_owned_work(self) -> bool:
        return self._work_active

    def clear_submission(self) -> None:
        self._pipeline.clear_submission()

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
        self._pipeline.clear()
        self._attempt_id = None
        self._basis_candidate = None
        self._basis_scan = None
        self._basis_scan_revision = None
        summary = self._work.finish(identity, cause, self._clock())
        self._work_active = False
        self._active_permit = None
        self._terminal_update = None
        self._submitted_request = None
        self._required_interaction = None
        self._confirmed_interaction_id = None
        self._interaction_approach_pending = False
        self._information_need = None
        self._information_offset = 0
        self._information_unavailable.clear()
        self._pending_information_progress = None
        return summary

    def cancel_work(self, cause: str = "planning_cancelled") -> None:
        identity = self._work_identity
        if identity is None:
            self._pipeline.clear()
            self._basis_candidate = None
            self._basis_scan = None
            self._basis_scan_revision = None
            self._attempt_id = None
            self._work_active = False
            self._terminal_update = None
            self._submitted_request = None
            self._information_need = None
            self._information_offset = 0
            self._information_unavailable.clear()
            self._pending_information_progress = None
            return
        self.retire(identity, cause)

    def clear_changes(self) -> None:
        self._pipeline.changed_cells.clear()

    def preserve_replacement_failure(self, reason: str) -> None:
        self._pipeline.replacement_failure = ReplacementPlanningFailure(reason)

    def consume_replacement_failure(self) -> str | None:
        failure = self._pipeline.replacement_failure
        self._pipeline.replacement_failure = None
        return None if failure is None else failure.reason

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
        cause: RetryCause,
        failure_id: str,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        require_identifier(failure_id, "planning retry failure id")
        registration = self._retry_ledger.record_failure(failure_id, cause)
        if registration.verdict is not RetryVerdict.RETRY:
            return self._finish_failure("planning_retry_exhausted")
        assert self.request is not None
        return self.restart_from_current(
            frame,
            permit=PlanningAttemptPermit(
                f"{failure_id}/retry",
                self.task_id,
                self.request.goal_revision,
                failure_id,
                PlanningAttemptPermitKind.RETRY,
            ),
            remaining_damage_budget=remaining_damage_budget,
        )

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
        self._retire_active_identity("planning_work_replaced")
        self._used_permits.add(permit.permit_id)
        self._active_permit = permit
        self._attempt_sequence += 1
        identity = AsyncWorkIdentity(
            request.world_session,
            self.task_id,
            self._owner_instance_id,
            AsyncWorkKind.PLANNING,
            request.request_id,
            self._attempt_sequence,
        )
        started_ns = self._clock()
        window = AsyncWorkWindow(
            self._movement_tick(frame),
            started_ns,
            started_ns + int(
                request.maximum_planning_seconds * 1_000_000_000
            ),
        )
        self._work.begin(identity, window)
        self._attempt_id = identity.key
        self._remember_work_window(self._work_identity, self._work_window)
        self._work_active = True
        request = replace(request, work_identity=self._work_identity)
        if type(request) is SurfacePlanningRequest:
            request = replace(request, damage_budget=remaining_damage_budget)
        self._request_ledger.accept(request)
        self._terminal_update = None
        self._submitted_request = None
        self._required_interaction = None
        self._confirmed_interaction_id = None
        self._interaction_approach_pending = False
        self._information_need = None
        self._information_offset = 0
        self._information_unavailable.clear()
        self._pending_information_progress = None
        self._pipeline.clear()
        self._pipeline.attempt_started_movement_tick = (
            self._work_window.started_movement_tick
        )
        self._pipeline.attempt_started_monotonic_ns = (
            self._work_window.started_monotonic_ns
        )
        self._pipeline.attempt_deadline_monotonic_ns = (
            self._work_window.deadline_monotonic_ns
        )
        self._pipeline.builder = KnownMapSnapshotBuilder(
            frame.world, self._bounds(request),
        )

    def observe_changes(self, changed_cells: tuple[tuple[int, int, int], ...]) -> None:
        if type(changed_cells) is not tuple:
            raise ContractViolation("planning world changes must be immutable")
        self._pipeline.changed_cells.update(changed_cells)

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
                or current != selection.information_need
                or self._work_identity != selection.information_identity
                or self._attempt_id != selection.attempt_id
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

    def advance(
        self,
        frame: NavigationFrame,
        *,
        state_anchor: StateAnchor | None,
        edge_probe: LandingEdgeProbe | None,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        request = self.request
        if request is None or self._attempt_id is None:
            raise ContractViolation("planning advance has no active attempt")
        if type(remaining_damage_budget) is not TaskDamageBudget:
            raise ContractViolation("planning advance requires a damage budget")
        if self._terminal_update is not None:
            return self._terminal_update
        if frame.session.value != request.world_session:
            return self._finish_failure("world_session_changed")
        if self._attempt_expired(frame):
            return self._retry_or_fail(
                frame, RetryCause.PLANNING, "planning_timeout",
                remaining_damage_budget,
            )

        builder = self._pipeline.builder
        if builder is not None:
            progress = builder.advance(frame.world, self._snapshot_cells_per_step)
            if progress.status is SnapshotBuildStatus.STALE:
                exact_changes = tuple(self._pipeline.changed_cells)
                if builder.accept_changes_outside_bounds(
                    frame.world, exact_changes,
                ):
                    self._pipeline.changed_cells.clear()
                    progress = builder.advance(
                        frame.world, self._snapshot_cells_per_step,
                    )
                if progress.status is not SnapshotBuildStatus.STALE:
                    if progress.status is SnapshotBuildStatus.BUILDING:
                        return self._update(
                            PlanningUpdateKind.RUNNING, "snapshot_building",
                        )
                    if progress.snapshot is None:
                        return self._finish_failure(
                            "snapshot_missing_after_completion"
                        )
                    # Continue below and submit the completed snapshot.
                else:
                    relevant_known_change = self._stale_builder_has_known_change(
                        builder, frame,
                    )
                    if relevant_known_change:
                        return self._retry_or_fail(
                            frame,
                            RetryCause.DEPENDENCY,
                            "snapshot_dependency_changed",
                            remaining_damage_budget,
                        )
                    self._pipeline.builder = KnownMapSnapshotBuilder(
                        frame.world, self._bounds(request),
                    )
                    self._pipeline.changed_cells.clear()
                    return self._update(
                        PlanningUpdateKind.RUNNING, "snapshot_restarted"
                    )
            if progress.status is SnapshotBuildStatus.BUILDING:
                return self._update(PlanningUpdateKind.RUNNING, "snapshot_building")
            if progress.snapshot is None:
                return self._finish_failure("snapshot_missing_after_completion")
            if type(request) is PlanningRequest and progress.missing_cells:
                return self._finish_failure("legacy_requires_complete_snapshot")
            self._pipeline.snapshot = progress.snapshot
            self._pipeline.snapshot_request_id = request.request_id
            submitted = self._submit(request, progress.snapshot, frame, state_anchor)
            if not submitted:
                return self._finish_failure("planner_submission_rejected")
            self._pipeline.builder = None

        candidate = self._basis_candidate or self._planner.poll_latest()
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
            return self._update(PlanningUpdateKind.RUNNING, "planning_submitted")
        submitted_request = self._submitted_request or request
        identity_matched = (
            candidate.work_identity is not None
            and candidate.work_identity == self._work_identity
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
        self._pipeline.clear_submission()

        basis_change = self._candidate_basis_change(frame, candidate)
        if basis_change is _BasisChange.CHECKING:
            self._basis_candidate = candidate
            return self._update(PlanningUpdateKind.RUNNING, "planning_basis_verifying")
        self._basis_candidate = None
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
                and self._pipeline.snapshot is not None
                and self._pipeline.snapshot_request_id == request.request_id):
            interaction = plan_next_bridge_interaction(
                self._pipeline.snapshot,
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
                        self._attempt_id,
                        request.request_id,
                        request.goal_revision,
                        "world_interaction_required",
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
                if not self._submit(
                    approach, self._pipeline.snapshot, frame, state_anchor,
                ):
                    return self._finish_failure(
                        "interaction_work_submission_rejected"
                    )
                self._interaction_approach_pending = True
                if not self._record_admission(
                    AsyncAdmissionDisposition.APPLIED,
                    facts_valid=True,
                    identity_matched=True,
                ):
                    return self._finish_failure("planning_timeout")
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
        if type(candidate) is SurfaceRouteCandidate:
            admitted = self._admitter.admit_surface(
                candidate,
                frame,
                expected_request_id=request.request_id,
                goal_id=request.goal_id,
                goal_revision=request.goal_revision,
                changed_cells=tuple(sorted(self._pipeline.changed_cells)),
                edge_probe=edge_probe,
            )
        else:
            admitted = self._admitter.admit(
                candidate,
                frame,
                expected_request_id=request.request_id,
                goal_id=request.goal_id,
                goal_revision=request.goal_revision,
                changed_cells=tuple(sorted(self._pipeline.changed_cells)),
            )
        if admitted.status is not AdmissionStatus.ACCEPTED or admitted.route is None:
            if admitted.reason in {
                    AdmissionReason.LANDING_VISUAL_EVIDENCE_MISSING,
                    AdmissionReason.TERMINAL_APPROACH_NEEDS_INFORMATION}:
                landing_evidence = admitted.reason is AdmissionReason.LANDING_VISUAL_EVIDENCE_MISSING
                snapshot = self._pipeline.snapshot
                assert snapshot is not None
                blockers = tuple(
                    PlanningBlocker(
                        position,
                        PlanningBlockerKind.ACTION_PRECONDITION,
                        "landing-visual-evidence" if landing_evidence else "terminal-collision-evidence",
                        PlanningFrontierKind.ACTION,
                        f"landing:{request.request_id}",
                        PlanningFactRequirement(
                            (PlanningFactRequirementKind.LANDING_VISUAL_EVIDENCE if landing_evidence
                             else PlanningFactRequirementKind.CELL_KNOWLEDGE),
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

    def diagnostics(self, frame: NavigationFrame) -> PlanningWorkDiagnostics:
        request = self.request
        movement_tick = self._movement_tick(frame)
        now = self._clock()
        has_builder = self._pipeline.builder is not None
        has_submission = self._pipeline.submitted_request_id is not None
        retained = self._terminal_update
        has_retained_route = (
            retained is not None
            and retained.kind is PlanningUpdateKind.ROUTE_READY
        )
        has_owned_work = self._work_active
        builder_valid = (
            has_builder
            and self._pipeline.submitted_request_id is None
            and self._work_identity is not None
            and self._work_window is not None
            and self._pipeline.attempt_deadline_monotonic_ns
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
            and self._pipeline.submitted_request_id == request.request_id
            and self._submitted_request.request_id == request.request_id
            and self._pipeline.submitted_movement_tick is not None
            and self._pipeline.result_deadline_monotonic_ns is not None
            and now < self._pipeline.result_deadline_monotonic_ns
            and movement_tick - self._pipeline.submitted_movement_tick
                < maximum_ticks
        )
        retained_route_valid = (
            has_retained_route
            and request is not None
            and retained is not None
            and retained.world_session_id == request.world_session
            and retained.request_id == request.request_id
            and retained.goal_revision == request.goal_revision
            and retained.attempt_id == self._attempt_id
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
            and self._attempt_id == self._work_identity.key
            and self._work_identity.owner_instance_id
                == self._owner_instance_id
            and self._work_identity.subject_id == request.request_id
            and self._work_identity.revision == self._attempt_sequence
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
        planning_work_valid = builder_valid or submission_valid
        return PlanningWorkDiagnostics(
            self._attempt_id,
            None if request is None else request.request_id,
            None if request is None else request.goal_revision,
            has_owned_work,
            planning_work_valid or information_work_valid,
            permit_valid or information_work_valid or not has_owned_work,
            information_valid,
            self._pipeline.submitted_request_id,
            movement_tick,
            (None if self._work_window is None
             else self._work_window.deadline_monotonic_ns),
        )

    def _submit(self, request, snapshot, frame, state_anchor) -> bool:
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
                None if self.capabilities.ground_modes is None
                else self.capabilities.ground_modes.require(MovementMode.WALK)
            )
            submitted = self._planner.submit_surface_snapshot(
                snapshot,
                self.capabilities.ground,
                self.capabilities.step,
                planning_request,
                self.capabilities.jump_up,
                air_profiles=self.capabilities.air,
                ground_mode_profile=mode,
            )
        else:
            submitted = self._planner.submit_snapshot(
                snapshot,
                self.capabilities.ground,
                request,
                self.capabilities.jump_up,
            )
        if submitted:
            self._submitted_request = request
            now = self._clock()
            self._pipeline.submitted_request_id = request.request_id
            self._pipeline.submitted_movement_tick = self._movement_tick(frame)
            self._pipeline.submitted_monotonic_ns = now
            if self._work_window is None:
                raise ContractViolation("planning submission has no work window")
            self._pipeline.result_deadline_monotonic_ns = (
                self._work_window.deadline_monotonic_ns
            )
        return submitted

    def _submission_expired(self, frame: NavigationFrame) -> bool:
        request = self.request
        pipeline = self._pipeline
        if (request is None
                or pipeline.submitted_request_id != request.request_id
                or pipeline.submitted_movement_tick is None
                or pipeline.result_deadline_monotonic_ns is None):
            return False
        return self._attempt_expired(frame)

    def _attempt_expired(self, frame: NavigationFrame) -> bool:
        request = self.request
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
    ) -> bool:
        changes = tuple(self._pipeline.changed_cells)
        if not changes:
            # The section revision changed without an exact change list.  It
            # is unsafe to label that as harmless knowledge gain.
            return True
        for position in changes:
            if (not builder.contains_position(position)
                    or not builder.was_scanned(position)):
                continue
            old = builder.copied_fact(position)
            if old is None:
                # A scanned UNKNOWN becoming known is progress, not a failed
                # attempt.  The restarted snapshot will include it.
                continue
            current = frame.world.cell(position)
            if (old.knowledge is not current.knowledge
                    or old.block != current.block):
                return True
        return False

    def _candidate_basis_change(self, frame: NavigationFrame, candidate):
        snapshot = self._pipeline.snapshot
        if snapshot is None:
            return None
        complete = candidate.status in {SurfacePlanningStatus.COMPLETE, PlanningStatus.COMPLETE}
        if self._basis_scan_revision != frame.world.geometry_revision:
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
            self._basis_scan = iter(positions)
            self._basis_scan_revision = frame.world.geometry_revision
            self._basis_scan_progress = False
        for _ in range(self._snapshot_cells_per_step):
            position = next(self._basis_scan, None)
            if position is None:
                self._basis_scan = None
                self._basis_scan_revision = None
                return _BasisChange.PROGRESS if self._basis_scan_progress else None
            before = snapshot.world.cell(position)
            current = frame.world.cell(position)
            if (before.knowledge is current.knowledge
                    and before.block == current.block):
                continue
            if (before.knowledge is CellKnowledge.UNKNOWN
                    and current.knowledge is not CellKnowledge.UNKNOWN):
                self._basis_scan_progress = True
                continue
            self._basis_scan = None
            self._basis_scan_revision = None
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
        blockers = (
            () if type(candidate) is not SurfaceRouteCandidate
            or candidate.information_need is None
            else candidate.information_need.blockers
        )
        fact_id = (
            self._blocker_id(blockers[0].blocker_key)
            if blockers else
            f"planning-world/{self._attempt_id}/{frame.body.sequence_id}"
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
        interaction=None,
    ):
        assert self.request is not None and self._attempt_id is not None
        return PlanningUpdate(
            kind,
            self.request.world_session,
            self._attempt_id,
            self.request.request_id,
            self.request.goal_revision,
            reason,
            route=route,
            information_need=information_need,
            interaction=interaction,
            information_identity=(self._work_identity
                                  if kind is PlanningUpdateKind.NEEDS_INFORMATION else None),
        )

    def _finish_failure(self, failure_reason: str, *, reason: str | None = None):
        self._pipeline.clear()
        assert self.request is not None and self._attempt_id is not None
        self._terminal_update = PlanningUpdate(
            PlanningUpdateKind.FAILED,
            self.request.world_session,
            self._attempt_id,
            self.request.request_id,
            self.request.goal_revision,
            reason or failure_reason,
            failure=PlanningFailure(failure_reason),
        )
        self._complete_active_work("planning_failed")
        return self._terminal_update

    def _retry_or_fail(
        self,
        frame: NavigationFrame,
        cause: RetryCause,
        failure_reason: str,
        remaining_damage_budget: TaskDamageBudget,
    ) -> PlanningUpdate:
        assert self._attempt_id is not None and self.request is not None
        failed_attempt = self._attempt_id
        registration = self._retry_ledger.record_failure(failed_attempt, cause)
        if registration.verdict is not RetryVerdict.RETRY:
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
            need.world_session_id,
            self.task_id,
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
        self._work.begin(identity, window)
        self._attempt_id = identity.key
        self._remember_work_window(self._work_identity, self._work_window)
        self._work_active = True
        self._active_permit = None
        self._submitted_request = None
        self._pipeline.clear()

    def _complete_active_work(self, cause: str) -> None:
        require_identifier(cause, "planning completion cause")
        self._retire_active_identity(cause)

    def _retire_active_identity(self, cause: str) -> None:
        require_identifier(cause, "planning retirement cause")
        identity = self._work_identity
        self._pipeline.clear()
        self._basis_candidate = None
        self._basis_scan = None
        self._basis_scan_revision = None
        if identity is not None:
            self._work.finish(identity, cause, self._clock())
        self._work_active = False
        self._active_permit = None
        self._submitted_request = None

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
            applied = self._work.try_apply(identity, now)
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
        if len(self._known_work_windows) >= 64 and identity not in self._known_work_windows:
            del self._known_work_windows[next(iter(self._known_work_windows))]
        self._known_work_windows[identity] = window
