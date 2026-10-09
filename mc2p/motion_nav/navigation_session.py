"""Online owner for planning, route admission and route execution.

The session never advances a backend. It turns the newest navigation frame
into an ordered control-frame proposal and keeps asynchronous results bound to
the request and goal revision that produced them.
"""
from __future__ import annotations

from mc2p.motion_nav.actions.registry import action_spec
from mc2p.motion_nav.action_preconditions import select_current_boundary, select_upcoming_boundary

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import math
from pathlib import Path
import time
from typing import Callable, Protocol, runtime_checkable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import (
    ActionIntentV1, LookV1, MovementTickWindowV1, MovementV1,
)
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.contracts.intent_source import (
    ControlFrameEventV1,
    ControlFrameProposalV1,
    IntentSourceV1,
    OrderedIntentV1,
    ordered_intent_id,
)
from mc2p.contracts.observation_request_v3 import (
    ObservationRequestV3, merge_observation_requests,
)
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.motion_nav.action_route import (
    JumpGapSegment, JumpUpSegment, WalkSegment,
)
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision,
    ActionRouteExecutor,
    ActionRouteState,
)
from mc2p.motion_nav.action_preconditions import (
    AcquisitionGrant,
    ActionPreconditionReason,
    ActionPreconditionResult,
    ActionPreconditionStatus,
    check_action_precondition,
)
from mc2p.motion_nav.air_motion import AirMotionProfile
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.execution_supervisor import (
    BodyFrameAdvance, BodyFrameResult, BodyRouteValidation, BodySelectionKind,
    ExecutionSupervisor, RouteControl,
)
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationDisposition,
    GroundCapabilityIdentity,
)
from mc2p.motion_nav.body_control import BodyControlActivity
from mc2p.motion_nav.body_control import (
    BodyControlProgress, HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.goal_observation import (
    ObservedGoal, ObservedGoalStatus, evaluate_observed_goal,
)
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.geometry import QueryStatus, query_support
from mc2p.motion_nav.ground_modes import GroundModeProfiles, observed_ground_mode
from mc2p.motion_nav.ground_modes import load_ground_mode_profiles
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.jump_up import JumpUpProfile, load_jump_up_profile
from mc2p.motion_nav.landing_edge_probe import (
    DIRECT_DROP_EDGE_PROBE_MAX_FRAMES,
    LandingEdgeProbe,
    LandingEdgeProbeState,
    information_probe_movement,
)
from mc2p.motion_nav.known_map_planner import (
    PlanningRequest,
    SurfacePlanningRequest,
    SurfaceSearchNeed, surface_search_need,
)
from mc2p.motion_nav.bridge_planner import (
    BridgeInteractionPlan, BridgePlacementPolicy,
)
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkKind, ComputationInvalidationCause
from mc2p.motion_nav.motion_solver import (
    DEFAULT_GAP_SOLVER_POLICY, GapSolverPolicy, load_gap_solver_policy,
)
from mc2p.motion_nav.motion_worker import (
    MotionResultInbox, MotionSolverWorker, MotionWorkerPort,
)
from mc2p.motion_nav.motion_risk import (
    RiskActionRecord, RiskActionState, RiskCommitEvidence, RiskCommitKind,
    RiskReleaseEvidence, RiskReservationStatus, RiskSubmissionStatus,
    TaskDamageBudget,
    TaskRiskLedger,
)
from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence, ProgressKind, RecoveryBudgetKind,
    RecoveryBudgetPolicy, RecoveryIdentity, RecoveryLimitStatus, RetryCause, RetryLedger,
    RetryLedgerCapacityExceeded, WaitPolicy, WaitVerdict,
    TaskDemandState,
)
from mc2p.motion_nav.movement_transition import (
    GoalState, MovementMode, ResourceState,
)
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputResponsibilityDisposition, StateAnchor,
    assess_input_responsibility,
)
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult, MotionResidualStatus, MotionResidualTracker,
)
from mc2p.motion_nav.navigation_owners import (
    GoalRequestLedger,
    InformationAcquisitionState,
    information_look_for_missing_cells,
    PendingGoalRevision,
)
from mc2p.motion_nav.navigation_lifecycle import (
    NavigationLifecycle,
    NavigationSessionEvent,
    NavigationSessionState,
    NavigationTransitionAction,
    SessionEventPolicy,
)
from mc2p.motion_nav.navigation_handoff import (
    HandoffDestination, NavigationHandoffCoordinator, RecoveryActivityPermit,
    ProbeHandoffAction, ProbeHandoffResolution,
)
from mc2p.motion_nav.probe_body_controller import ProbeOutcome
from mc2p.motion_nav.planner_worker import PlannerWorker, PlannerWorkerPort
from mc2p.motion_nav.planning_coordinator import (
    InformationOutcome,
    PlanningAttemptPermit,
    PlanningAttemptPermitKind,
    PlanningRetryTrigger,
    PlanningCapabilities,
    PlanningCoordinator,
    PlanningFailure,
    PlanningUpdate, PlanningUpdateKind,
)
from mc2p.motion_nav.route_admission import (
    ActiveRoute,
    AdmissionReason,
    AdmissionStatus,
    LocalDirectAdmissionEvidence,
    RouteAdmitter,
)
from mc2p.motion_nav.runtime_adapter import (
    NavigationFrame,
    NavigationObservationAdapter,
    world_session_from_observation,
)
from mc2p.motion_nav.safe_ground_control import (
    verified_ground_recovery_movement,
)
from mc2p.motion_nav.step_transition import StepProfile, load_step_profile
from mc2p.motion_nav.support_surfaces import (
    SurfaceNodeId, query_support_surfaces, standable_point_in_region,
    standable_region_in_goal,
)
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge, WorldQueryCache


_INFORMATION_WAIT_LIMIT_FRAMES = 40
_INFORMATION_WAIT_LIMIT_NS = 2_000_000_000


class ExternalMotionReentryStatus(StrEnum):
    CONTINUE_NAVIGATION = "continue_navigation"
    REQUIRES_BODY_RECOVERY = "requires_body_recovery"


@dataclass(frozen=True, slots=True)
class ExternalMotionReentryDecision:
    status: ExternalMotionReentryStatus
    reason: str

    def __post_init__(self) -> None:
        if type(self.status) is not ExternalMotionReentryStatus:
            raise ContractViolation("external-motion reentry status must be typed")
        require_identifier(self.reason, "external-motion reentry reason")


@dataclass(frozen=True, slots=True)
class NavigationSessionProfiles:
    ground: GroundMotionProfile
    jump_up: JumpUpProfile
    step: StepProfile
    ground_modes: GroundModeProfiles | None = None
    air: tuple[AirMotionProfile, ...] = ()
    gap_solver: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY

    def __post_init__(self) -> None:
        if (type(self.ground) is not GroundMotionProfile
                or type(self.jump_up) is not JumpUpProfile
                or type(self.step) is not StepProfile):
            raise ContractViolation("navigation session requires calibrated profiles")
        if (self.ground_modes is not None
                and type(self.ground_modes) is not GroundModeProfiles):
            raise ContractViolation("navigation ground modes must be typed")
        if (type(self.air) is not tuple
                or any(type(profile) is not AirMotionProfile for profile in self.air)):
            raise ContractViolation("navigation air profiles must be immutable")
        if type(self.gap_solver) is not GapSolverPolicy:
            raise ContractViolation("navigation gap solver policy must be typed")

    @classmethod
    def load(cls, config_root: Path) -> "NavigationSessionProfiles":
        """Load the one frozen Java 1.21 profile set used by formal sessions."""
        if not isinstance(config_root, Path):
            raise ContractViolation("navigation profile root must be a Path")
        environment = load_frozen_environment(config_root / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            config_root / "block-motion-traits-v1.json",
            config_root / "vanilla-block-registry-1_21.json",
        )
        modes = load_ground_mode_profiles(
            config_root / "ground-modes-b08-v1.json",
            environment=environment,
            catalog=catalog,
        )
        return cls(
            # B08's walk entry contains the same calibrated dynamics and
            # shape-material catalog as B07, and is the profile the mode
            # controller actually executes.  Planning and execution must use
            # that one object instead of mixing two profile identities.
            ground=modes.require(MovementMode.WALK).motion,
            jump_up=load_jump_up_profile(
                config_root / "jump-up-b06-v1.json",
                environment=environment,
                catalog=catalog,
            ),
            step=load_step_profile(
                config_root / "step-b07-v1.json",
                environment=environment,
            ),
            ground_modes=modes,
            air=load_air_motion_profiles(
                config_root / "air-motions-b09-v1.json",
                environment=environment,
                catalog=catalog,
            ),
            gap_solver=load_gap_solver_policy(
                config_root / "air-motions-b09-v1.json",
            ),
        )


@dataclass(frozen=True, slots=True)
class NavigationSessionReport:
    session_id: str
    state: NavigationSessionState
    reason: str
    goal_id: str | None
    goal_revision: int | None
    request_id: str | None
    planning_generation: int
    route_id: str | None
    action_index: int | None
    missing_cells: tuple[BlockPos, ...]
    terminal: bool
    required_interaction_id: str | None = None
    schema_version: str = "mc2p.navigation-session-report.v1"
    planning_deadline_monotonic_ns: int | None = None
    reach_policy: GoalReachPolicy = GoalReachPolicy.COMPLETE_ON_REACH
    observed_goal_status: ObservedGoalStatus | None = None
    planning_policy: GoalPlanningPolicy = GoalPlanningPolicy.BACKGROUND_PLANNER


@dataclass(frozen=True, slots=True)
class NavigationDiagnostics:
    """Read-only facts for checking the formal navigation path each tick."""

    state: NavigationSessionState
    controller_ids: tuple[str, ...]
    controller_phase: str | None
    action_kind: str | None
    action_index: int | None
    request_generation: int | None
    goal_revision: int | None
    route_id: str | None
    source_bound: bool
    pending_goal_revision: int | None
    last_decision_state: str | None
    handoff_reason: str | None
    damage_limit: float
    damage_spent: float
    replan_attempts: int
    information_wait_frames: int
    reason: str
    handoff: HandoffEvidence | None = None
    incumbent_route_id: str | None = None
    pending_route_id: str | None = None
    retry_round_failures: int = 0
    retry_total_failures: int = 0
    retry_pending_attempt_id: str | None = None
    retry_progress_capacity_exhausted: bool = False
    risk_actions: tuple[RiskActionRecord, ...] = ()
    risk_available_points: float = 0.0
    risk_policy_revision: int = 0
    risk_submission_capacity_exhausted: bool = False
    retry_last_failure_attempt_id: str | None = None
    retry_cause_counts: tuple[tuple[RetryCause, int], ...] = ()
    retry_progress_version: int = 0
    retry_progress_evidence: ProgressEvidence | None = None
    recovery_wait_status: WaitVerdict | None = None
    recovery_wait_capacity_exhausted: bool = False
    retry_approved_round: int = 0
    retry_approved_total: int = 0
    retry_approved_cause_counts: tuple[tuple[RetryCause, int], ...] = ()
    support_fraction: float | None = None
    transition_count: int = 0
    illegal_transition_count: int = 0
    body_control_progress: BodyControlProgress | None = None
    active_waits: tuple[tuple[str, str], ...] = ()
    planning_work_owned: bool = False
    planning_work_identity_valid: bool = True
    planning_permit_identity_valid: bool = True
    planning_information_identity_valid: bool = True
    body_control_activities: tuple[BodyControlActivity, ...] = ()
    recovery_budget_kind: RecoveryBudgetKind | None = None
    recovery_maximum_starts: int = 0
    recovery_window_starts: int = 0
    recovery_total_starts: int = 0
    recovery_limit_status: RecoveryLimitStatus | None = None
    recovery_limit_window_starts: int | None = None
    route_validation: BodyRouteValidation | None = None
    local_direct_admission: LocalDirectAdmissionEvidence | None = None


@dataclass(frozen=True, slots=True)
class NavigationSessionProposal:
    control_frame: ControlFrameProposalV1 | None
    report: NavigationSessionReport
    route_decision: ActionRouteDecision | None = None
    route_owner_id: str | None = None
    risk_action_id: str | None = None
    body_activity: BodyControlActivity | None = None


@dataclass(frozen=True, slots=True)
class SameTaskContinuationEvidence:
    """Released task state that may move to one fresh Session shell."""

    source_session_id: str
    task_id: str
    reach_policy: GoalReachPolicy
    planning_policy: GoalPlanningPolicy
    retry_ledger: RetryLedger
    risk_ledger: TaskRiskLedger
    damage_budget: TaskDamageBudget
    movement_damage_spent_points: float
    bridge_remaining: int

    def __post_init__(self) -> None:
        require_identifier(self.source_session_id, "continuation source session")
        require_identifier(self.task_id, "continuation task")
        if type(self.reach_policy) is not GoalReachPolicy:
            raise ContractViolation("continuation reach policy must be typed")
        if type(self.planning_policy) is not GoalPlanningPolicy:
            raise ContractViolation("continuation planning policy must be typed")
        if (type(self.retry_ledger) is not RetryLedger
                or self.retry_ledger.task_id != self.task_id):
            raise ContractViolation("continuation retry ledger belongs to another task")
        if (type(self.risk_ledger) is not TaskRiskLedger
                or self.risk_ledger.task_id != self.task_id):
            raise ContractViolation("continuation risk ledger belongs to another task")
        if type(self.damage_budget) is not TaskDamageBudget:
            raise ContractViolation("continuation damage budget must be typed")
        if (type(self.movement_damage_spent_points) not in (int, float)
                or not math.isfinite(self.movement_damage_spent_points)
                or self.movement_damage_spent_points < 0):
            raise ContractViolation("continuation damage spent must be non-negative")
        if type(self.bridge_remaining) is not int or self.bridge_remaining < 0:
            raise ContractViolation("continuation bridge remainder is invalid")


@dataclass(frozen=True, slots=True)
class PendingPlanningRecovery:
    reason: str
    cause: RetryCause

    def __post_init__(self) -> None:
        require_identifier(self.reason, "pending planning recovery reason")
        if type(self.cause) is not RetryCause:
            raise ContractViolation("pending planning recovery cause must be typed")


@runtime_checkable
class NavigationSessionPort(Protocol):
    def attach_observation_adapter(
        self, adapter: NavigationObservationAdapter,
    ) -> None: ...

    """Small runtime-facing contract; tests can replace planning, not semantics."""

    @property
    def report(self) -> NavigationSessionReport: ...

    def bind_source(self, source: IntentSourceV1) -> None: ...
    def unbind_source(self, source: IntentSourceV1) -> None: ...
    def ingest(self, snapshot: ObservationSnapshotV3) -> NavigationFrame: ...
    def motion_residual(
        self, snapshot: ObservationSnapshotV3, ledger: InputApplicationLedger,
    ) -> MotionResidualResult | None: ...
    def execution_anchor(
        self, snapshot: ObservationSnapshotV3, ledger: InputApplicationLedger,
    ) -> StateAnchor | None: ...
    def body_handoff(
        self, snapshot: ObservationSnapshotV3, ledger: InputApplicationLedger,
    ) -> HandoffEvidence: ...
    def discard_prepared_proposal(
        self, proposal: NavigationSessionProposal, frame: NavigationFrame,
    ) -> None: ...
    def external_motion_reentry(
        self, snapshot: ObservationSnapshotV3,
    ) -> ExternalMotionReentryDecision: ...
    def observation_request(
        self, *, max_positions: int = 128,
    ) -> ObservationRequestV3: ...
    def current_action_requires_route_look(self) -> bool: ...
    def start_goal(
        self, goal_id: str, goal_revision: int, goal_state: GoalState,
        frame: NavigationFrame, *, maximum_expansions: int = 100_000,
        maximum_planning_seconds: float = .5,
        damage_budget: TaskDamageBudget | None = None,
        task_id: str | None = None,
        reach_policy: GoalReachPolicy = GoalReachPolicy.COMPLETE_ON_REACH,
        planning_policy: GoalPlanningPolicy = (
            GoalPlanningPolicy.BACKGROUND_PLANNER
        ),
    ) -> None: ...
    def update_goal(
        self, goal_id: str, goal_revision: int, goal_state: GoalState, *,
        damage_budget: TaskDamageBudget | None = None,
        task_id: str | None = None,
    ) -> bool: ...
    def propose(
        self, frame: NavigationFrame, state_anchor: StateAnchor | None,
        deadline_ns: int, *, input_ledger: InputApplicationLedger | None = None,
        conditioned_yaw_delta_degrees: float | None = None,
        conditioned_look_intent_id: str | None = None,
    ) -> NavigationSessionProposal: ...
    def register_verified_submission(
        self, proposal: NavigationSessionProposal, *, control_sequence: int,
        actual_movement: MovementV1 | None = None,
    ) -> None: ...
    def reject_unselected_route_proposal(
        self, proposal: NavigationSessionProposal,
        frame: NavigationFrame,
    ) -> None: ...
    def cancel(self, reason: str) -> None: ...
    def handle_control_unavailable(self) -> None: ...
    def handle_internal_contract_failure(self, reason: str) -> None: ...
    def contract_stop_proposal(self, frame: NavigationFrame, state_anchor: StateAnchor | None,
                              deadline_ns: int, *, input_ledger: InputApplicationLedger) -> NavigationSessionProposal: ...
    def spawn_successor(
        self, session_id: str, *, task_id: str,
    ) -> "NavigationSessionPort": ...
    def same_task_continuation_evidence(
        self,
    ) -> "SameTaskContinuationEvidence | None": ...
    def rebuild_same_task(
        self, session_id: str, evidence: "SameTaskContinuationEvidence",
    ) -> "NavigationSessionPort": ...
    def close(self) -> None: ...


class NavigationSession:
    """Own one goal/request/route lifecycle while reading one world owner."""

    def __init__(
        self,
        session_id: str,
        profiles: NavigationSessionProfiles,
        *,
        planner_worker: PlannerWorkerPort | None = None,
        owns_planner_worker: bool = True,
        motion_worker: MotionWorkerPort | None = None,
        observation_adapter: NavigationObservationAdapter | None = None,
        route_admitter: RouteAdmitter | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        snapshot_cells_per_step: int = 4096,
        planning_margin_cells: int = 1,
        bridge_policy: BridgePlacementPolicy | None = None,
        retry_ledger: RetryLedger | None = None,
        risk_ledger: TaskRiskLedger | None = None,
    ) -> None:
        require_identifier(session_id, "navigation session id")
        if type(profiles) is not NavigationSessionProfiles:
            raise ContractViolation("navigation session profiles are invalid")
        if type(owns_planner_worker) is not bool:
            raise ContractViolation("planner worker ownership must be bool")
        if planner_worker is None and not owns_planner_worker:
            raise ContractViolation(
                "a borrowed planner worker must be supplied explicitly"
            )
        if planner_worker is not None and not isinstance(planner_worker, PlannerWorkerPort):
            raise ContractViolation("planner worker does not satisfy its protocol")
        if motion_worker is not None and not isinstance(motion_worker, MotionWorkerPort):
            raise ContractViolation("motion worker does not satisfy its protocol")
        if type(snapshot_cells_per_step) is not int or snapshot_cells_per_step < 1:
            raise ContractViolation("snapshot step budget must be positive")
        if type(planning_margin_cells) is not int or not 0 <= planning_margin_cells <= 16:
            raise ContractViolation("planning margin must be within 0..16 cells")
        if bridge_policy is not None and type(bridge_policy) is not BridgePlacementPolicy:
            raise ContractViolation("navigation bridge policy must be typed")
        if retry_ledger is not None and type(retry_ledger) is not RetryLedger:
            raise ContractViolation("navigation retry ledger must be typed")
        if risk_ledger is not None and type(risk_ledger) is not TaskRiskLedger:
            raise ContractViolation("navigation risk ledger must be typed")
        self.session_id = session_id
        self.profiles = profiles
        self._planner = planner_worker or PlannerWorker()
        self._owns_planner_worker = owns_planner_worker
        self._motion_worker = motion_worker
        self._motion_result_inbox = MotionResultInbox(max_results=64)
        from mc2p.motion_nav.async_work import AsyncOwnerScope
        self._execution_instances = AsyncOwnerScope()
        self._motion_result_poll_sequence = 0
        self._owns_motion_worker = False
        self._adapter = observation_adapter or NavigationObservationAdapter()
        self._admitter = route_admitter or RouteAdmitter()
        self._clock = clock_ns
        self._snapshot_cells_per_step = snapshot_cells_per_step
        self._planning_margin = planning_margin_cells
        self._bridge_policy = bridge_policy
        self._bridge_remaining = (
            0 if bridge_policy is None else bridge_policy.maximum_blocks
        )
        self._source: IntentSourceV1 | None = None
        self._intent_sequence = 0
        self._lifecycle = NavigationLifecycle()
        self._reason = "not_started"
        self._goal_requests = GoalRequestLedger()
        self._handoff = NavigationHandoffCoordinator()
        self._planning_coordinator: PlanningCoordinator | None = None
        self._information = InformationAcquisitionState()
        self._frame: NavigationFrame | None = None
        self._control_ledger: InputApplicationLedger | None = None
        self._control_anchor: StateAnchor | None = None
        self._supervisor = ExecutionSupervisor()
        self._required_interaction: BridgeInteractionPlan | None = None
        self._interaction_approach_pending = False
        self._local_goal_request_id: str | None = None
        self._direct_goal_request_id: str | None = None
        self._direct_planning_permit: PlanningAttemptPermit | None = None
        self._local_input_floor: int | None = None
        self._local_mode_wait_frames = 0
        self._last_decision: ActionRouteDecision | None = None
        self._task_damage_budget = TaskDamageBudget()
        self._movement_damage_spent_points = 0.0
        self._executor_reported_damage_points = 0.0
        self._motion_residual = MotionResidualTracker()
        self._retry_ledger: RetryLedger | None = retry_ledger
        self._risk_ledger: TaskRiskLedger | None = risk_ledger
        # Assigned once when spawning; first start cannot choose another task.
        self._declared_successor_task_id: str | None = None
        self._risk_action_id: str | None = None
        self._risk_action_key: tuple[str, int] | None = None
        self._risk_failure_reason: str | None = None
        self._risk_submission_capacity_exhausted = False
        self._recovery_wait_status: WaitVerdict | None = None
        self._recovery_wait_capacity_exhausted = False
        self._last_retry_route_id: str | None = None
        self._last_retry_action_index: int | None = None
        self._last_retry_body_cell: tuple[int, int, int] | None = None
        self._closed = False
        self._close_requested = False
        self._pending_planning_recovery: PendingPlanningRecovery | None = None
        self._route_validation: BodyRouteValidation | None = None
        self._last_local_direct_admission: (
            LocalDirectAdmissionEvidence | None
        ) = None

    @staticmethod
    def _recovery_budget_policy(
        reach_policy: GoalReachPolicy,
    ) -> RecoveryBudgetPolicy:
        if type(reach_policy) is not GoalReachPolicy:
            raise ContractViolation("navigation recovery reach policy must be typed")
        return (
            RecoveryBudgetPolicy.persistent()
            if reach_policy is GoalReachPolicy.KEEP_ACTIVE_ON_REACH
            else RecoveryBudgetPolicy.finite()
        )

    def _ensure_task_retry_ledger(
        self, task_id: str, *,
        initial_support: tuple[int, int, int] | None = None,
    ) -> RetryLedger:
        expected = self._recovery_budget_policy(
            self._goal_requests.reach_policy,
        )
        ledger = self._retry_ledger
        if ledger is None:
            ledger = RetryLedger(
                task_id,
                initial_support=initial_support,
                policy=expected,
                clock_ns=self._clock,
            )
            self._retry_ledger = ledger
        elif ledger.task_id != task_id:
            raise ContractViolation("navigation request changed task ledger identity")
        elif ledger.policy != expected:
            raise ContractViolation(
                "navigation retry ledger recovery policy does not match task"
            )
        return ledger

    @property
    def required_interaction(self) -> BridgeInteractionPlan | None:
        return self._required_interaction

    @property
    def async_work_diagnostics(self):
        planning = (() if self._planning_coordinator is None else
                    (self._planning_coordinator.async_diagnostics,))
        return planning + self._supervisor.async_work_diagnostics

    @property
    def planning_information_update(self) -> PlanningUpdate | None:
        """Read the immutable current notification without creating work or permits."""
        return (None if self._planning_coordinator is None
                else self._planning_coordinator.current_information_update)

    @property
    def active_motion_mailboxes(self):
        return self._motion_result_inbox.active_identities

    @property
    def body_route_snapshot(self) -> ActiveRoute | None:
        """Read the immutable route currently owned by the body's controller.

        A pending candidate is never returned. Reading does not advance work,
        change a goal, or grant input authority.
        """
        controller = self._supervisor.route
        return None if controller is None else controller.route

    @property
    def _edge_probe(self) -> LandingEdgeProbe | None:
        return self._supervisor.probe

    @_edge_probe.setter
    def _edge_probe(self, value: LandingEdgeProbe | None) -> None:
        self._supervisor.probe = value

    def _information_wait_owner_id(self) -> str:
        return f"navigation-session/{self.session_id}/information"

    def _probe_wait_owner_id(self) -> str:
        probe = self._edge_probe
        return (
            probe.owner_id if probe is not None
            else f"navigation-session/{self.session_id}/recovery"
        )

    def _end_probe_waits(self, probe: LandingEdgeProbe | None) -> None:
        if probe is not None and self._retry_ledger is not None:
            self._retry_ledger.end_owner_waits(probe.owner_id)

    def _end_session_waits(self) -> None:
        """End waits whose session or action owner has left the task."""
        ledger = self._retry_ledger
        if ledger is None:
            return
        owners = {
            self._information_wait_owner_id(),
            f"navigation-session/{self.session_id}/recovery",
        }
        for owner_id in owners:
            ledger.end_owner_waits(owner_id)
        if ledger.active_waits():
            raise ContractViolation(
                "session resources cannot close while an action owner still waits"
            )
        self._recovery_wait_status = None

    def _end_information_wait(self) -> bool:
        if self._retry_ledger is None:
            return False
        return self._information.end_wait(
            self._retry_ledger, self._information_wait_owner_id(),
        )

    def _end_session_recovery_wait(self) -> bool:
        if self._retry_ledger is None:
            return False
        return self._retry_ledger.end_wait_owned(
            "recovery", f"navigation-session/{self.session_id}/recovery",
        )

    @property
    def _state(self) -> NavigationSessionState:
        return self._lifecycle.state

    def _transition(
        self,
        action: NavigationTransitionAction,
        reason: str,
        *,
        handoff: HandoffEvidence | None = None,
    ) -> None:
        if not isinstance(reason, str) or not reason:
            raise ContractViolation("navigation transition reason must be non-empty")
        if action in {
            NavigationTransitionAction.RESUME_EXECUTION_AFTER_HANDOFF,
            NavigationTransitionAction.RESUME_ACTIVE_IDLE_AFTER_HANDOFF,
            NavigationTransitionAction.REPLAN_AFTER_HANDOFF,
        }:
            if (handoff is None
                    or handoff is not self._supervisor.last_handoff
                    or self._frame is None
                    or handoff.world_session != self._frame.session
                    or handoff.observation_sequence_id
                        != self._frame.body.sequence_id):
                raise ContractViolation(
                    "navigation handoff evidence is absent, stale, or foreign"
                )
        transition = self._lifecycle.transition(action, handoff=handoff)
        self._reason = reason
        if transition.current in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            if self._handoff.ending:
                self._handoff.finish_ending(
                    budget=self._retry_ledger,
                    handoff=self._supervisor.last_handoff,
                    frame=self._frame,
                )
            self._end_session_waits()
            if self._planning_coordinator is not None:
                self._planning_coordinator.cancel_work(
                    f"session_terminal_{transition.current.value}",
                )

    def _continue_execution(self, reason: str) -> None:
        """Report incumbent progress without bypassing a pending handoff."""
        if self._state is NavigationSessionState.STOPPING:
            self._reason = reason
            return
        self._transition(NavigationTransitionAction.BEGIN_EXECUTION, reason)

    def handle_internal_contract_failure(self, reason: str) -> None:
        """Keep the body owner while a formal driver contains an internal fault."""
        require_identifier(reason, "navigation internal failure reason")
        if self._state in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            return
        self._request_ending(HandoffDestination.FAIL, StopCause.CANCELLED, reason)

    def handle_control_unavailable(self) -> None:
        """Control loss is a terminal failure, never evidence of safe landing."""
        if self.report.terminal:
            return
        self._handoff.finish_control_unavailable(budget=self._retry_ledger)
        self._supervisor.request_route_stop(StopCause.INPUT_LOST)
        if self._coordinator is not None:
            self._coordinator.cancel_work("control_unavailable")
        self._transition(NavigationTransitionAction.MARK_FAILED, "control_unavailable")

    def contract_stop_proposal(self, frame: NavigationFrame, state_anchor: StateAnchor | None,
                              deadline_ns: int, *, input_ledger: InputApplicationLedger) -> NavigationSessionProposal:
        """Submit one neutral frame without deciding the same controller twice."""
        self.handle_internal_contract_failure("navigation_internal_contract_failure")
        self._control_ledger, self._control_anchor = input_ledger, state_anchor
        self._begin_task_activity(frame, input_ledger, state_anchor)
        return self._proposal(MovementV1(), None, 1, deadline_ns, retain_body_input=True)

    def _admit_command_event(self, event: NavigationSessionEvent) -> None:
        """Reject an invalid synchronous command before it mutates the task."""
        policy = self._lifecycle.admit_event(event)
        if policy is not SessionEventPolicy.HANDLE:
            raise ContractViolation(
                "navigation lifecycle event "
                f"{event.value} is {policy.value} in {self._state.value}"
            )

    @property
    def _request(self) -> PlanningRequest | SurfacePlanningRequest | None:
        return self._goal_requests.request

    @_request.setter
    def _request(self, value: PlanningRequest | SurfacePlanningRequest | None) -> None:
        if value is not None and self._retry_ledger is not None:
            self._goal_requests.bind_computation_scope(self._retry_ledger.task_id, value.world_session)
        if (value is not None
                and (type(value) is not SurfacePlanningRequest
                     or surface_search_need(value)
                        is not SurfaceSearchNeed.SAME_SUPPORT_LOCAL_GOAL)):
            self._last_local_direct_admission = None
        self._goal_requests.accept(value)

    @property
    def current_computation_scope(self) -> AsyncComputationScope:
        return self._goal_requests.current_computation_scope

    @property
    def _pending_goal(self) -> PendingGoalRevision | None:
        return self._handoff.pending_goal

    @property
    def _pending_retry(self):
        request = self._handoff.stop_request
        return request if request is not None and request.retry_cause is not None else None

    @_pending_goal.setter
    def _pending_goal(self, value: PendingGoalRevision | None) -> None:
        if value is None:
            self._handoff.clear_waiting_goal()
        else:
            self._handoff.stage_waiting_goal(value)

    def _ensure_planning_coordinator(self) -> PlanningCoordinator:
        coordinator = self._planning_coordinator
        if coordinator is not None:
            return coordinator
        if self._retry_ledger is None:
            raise ContractViolation(
                "planning coordinator requires the task retry ledger"
            )
        coordinator = PlanningCoordinator(
            self._retry_ledger.task_id,
            PlanningCapabilities(
                self.profiles.ground,
                self.profiles.step,
                self.profiles.jump_up,
                self.profiles.air,
                self.profiles.ground_modes,
            ),
            planner_worker=self._planner,
            route_admitter=self._admitter,
            retry_ledger=self._retry_ledger,
            clock_ns=self._clock,
            snapshot_cells_per_step=self._snapshot_cells_per_step,
            planning_margin_cells=self._planning_margin,
            bridge_policy=self._bridge_policy,
            bridge_remaining=self._bridge_remaining,
            request_ledger=self._goal_requests,
            request_id_prefix=self.session_id,
        )
        self._planning_coordinator = coordinator
        return coordinator

    @property
    def _snapshot_missing(self) -> tuple[BlockPos, ...]:
        return self._information.missing_cells

    @_snapshot_missing.setter
    def _snapshot_missing(self, value: tuple[BlockPos, ...]) -> None:
        self._information.missing_cells = value

    @property
    def _residual_missing(self) -> tuple[BlockPos, ...]:
        return self._information.residual_missing_cells

    @_residual_missing.setter
    def _residual_missing(self, value: tuple[BlockPos, ...]) -> None:
        self._information.residual_missing_cells = value

    @property
    def _completed_acquisition(self) -> AcquisitionGrant | None:
        return self._information.completed_grant

    @_completed_acquisition.setter
    def _completed_acquisition(self, value: AcquisitionGrant | None) -> None:
        self._information.completed_grant = value

    @property
    def _information_wait_frames(self) -> int:
        return self._information.wait_frames

    @_information_wait_frames.setter
    def _information_wait_frames(self, value: int) -> None:
        self._information.wait_frames = value

    @property
    def _active_route(self) -> ActiveRoute | None:
        control = self._supervisor.route
        return None if control is None else control.route

    @property
    def _executor(self) -> ActionRouteExecutor | None:
        control = self._supervisor.route
        return None if control is None else control.executor

    @property
    def _coordinator(self) -> MotionRouteCoordinator | None:
        control = self._supervisor.route
        return None if control is None else control.coordinator

    @property
    def bridge_remaining(self) -> int:
        if self._planning_coordinator is not None:
            return self._planning_coordinator.bridge_remaining
        return self._bridge_remaining

    def confirm_required_interaction(self, interaction_id: str) -> None:
        """Consume one authorized placement after its transaction confirms it."""
        require_identifier(interaction_id, "confirmed interaction id")
        if (self._required_interaction is None
                or self._required_interaction.requirement.interaction_id != interaction_id
                or self._state is not NavigationSessionState.REQUIRES_INTERACTION):
            raise ContractViolation("navigation has no matching required interaction")
        self._admit_command_event(NavigationSessionEvent.INTERACTION_CONFIRMED)
        if self._planning_coordinator is not None:
            self._planning_coordinator.confirm_interaction(interaction_id)
            self._bridge_remaining = self._planning_coordinator.bridge_remaining
        else:
            if self._bridge_remaining < 1:
                raise ContractViolation("navigation bridge budget is exhausted")
            self._bridge_remaining -= 1

    @property
    def active_route(self) -> ActiveRoute | None:
        return self._active_route

    def current_action_requires_route_look(self) -> bool:
        """Keep combat gaze out of actions whose proof owns body orientation."""
        if self._executor is None:
            return False
        route = getattr(self._executor, "route", None)
        action_index = getattr(self._executor, "action_index", -1)
        if (route is None or not 0 <= action_index < len(route.actions)):
            return False
        action = route.actions[action_index]
        return not (
            type(action) is WalkSegment
            and (
                action.transition is None
                or action.transition.mode is MovementMode.WALK
            )
        )

    @property
    def report(self) -> NavigationSessionReport:
        request = self._request
        route = self._active_route
        pending = self._pending_goal
        observed = None
        if self._goal_requests.reach_policy is GoalReachPolicy.KEEP_ACTIVE_ON_REACH:
            current = self._adapter.latest_frame or self._frame
            if current is not None:
                observed = self._observed_goal(current)
        return NavigationSessionReport(
            self.session_id,
            self._state,
            self._reason,
            (pending.goal_id if request is None and pending is not None
             else None if request is None else request.goal_id),
            (pending.goal_revision if request is None and pending is not None
             else None if request is None else request.goal_revision),
            None if request is None else request.request_id,
            0 if request is None else request.sequence,
            None if route is None else route.route_id,
            None if self._last_decision is None else self._last_decision.action_index,
            tuple(sorted(set(self._snapshot_missing) | set(self._residual_missing))),
            self._state in {
                NavigationSessionState.COMPLETE,
                NavigationSessionState.CANCELLED,
                NavigationSessionState.FAILED,
                NavigationSessionState.CLOSED,
            },
            (None if self._required_interaction is None else
             self._required_interaction.requirement.interaction_id),
            planning_deadline_monotonic_ns=(
                self._planning_coordinator.work_window.deadline_monotonic_ns
                if self._planning_coordinator is not None
                and self._planning_coordinator.has_owned_work
                and self._planning_coordinator.work_window is not None else None),
            reach_policy=self._goal_requests.reach_policy,
            observed_goal_status=None if observed is None else observed.status,
            planning_policy=self._goal_requests.planning_policy,
        )

    def _observed_goal(self, frame: NavigationFrame) -> ObservedGoal | None:
        goal = (self._pending_goal.goal_state if self._pending_goal is not None else
                None if self._request is None else self._request.goal_state)
        if goal is None:
            return None
        return evaluate_observed_goal(frame, goal, self._task_damage_budget.risk_policy_id)

    def _task_demand_state(
        self, frame: NavigationFrame,
        input_ledger: InputApplicationLedger | None,
        state_anchor: StateAnchor | None,
    ) -> TaskDemandState:
        if self._handoff.accepted_ending or self._state in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            return TaskDemandState.TERMINAL_CLEANUP
        if self._goal_requests.reach_policy is not GoalReachPolicy.KEEP_ACTIVE_ON_REACH:
            return TaskDemandState.UNMET
        observed = self._observed_goal(frame)
        if (observed is None
                or observed.status is not ObservedGoalStatus.SATISFIED
                or self._pending_goal is not None
                or self._handoff.stop_request is not None
                or self._has_body_owner()):
            return TaskDemandState.UNMET
        handoff = self._supervisor.evaluate_quiescence(
            frame, input_ledger, state_anchor,
        )
        return (
            TaskDemandState.STABLE_SATISFIED
            if handoff.disposition is HandoffDisposition.QUIESCENT
            else TaskDemandState.UNMET
        )

    def _begin_task_activity(
        self, frame: NavigationFrame,
        input_ledger: InputApplicationLedger | None,
        state_anchor: StateAnchor | None,
    ) -> None:
        ledger = self._retry_ledger
        if ledger is None:
            return
        self._handoff.begin_task_activity(
            budget=ledger,
            observation_sequence=frame.body.sequence_id,
            demand_state=self._task_demand_state(
                frame, input_ledger, state_anchor,
            ),
        )

    def _record_task_progress(
        self, frame: NavigationFrame,
        input_ledger: InputApplicationLedger | None,
        state_anchor: StateAnchor | None,
        progress: tuple[ProgressEvidence, ...],
    ) -> None:
        if not progress or self._retry_ledger is None:
            return
        self._handoff.record_task_progress(
            budget=self._retry_ledger,
            observation_sequence=frame.body.sequence_id,
            demand_state=self._task_demand_state(
                frame, input_ledger, state_anchor,
            ),
            progress=progress,
        )

    def _finalize_task_activity(
        self, frame: NavigationFrame,
    ) -> tuple[RecoveryActivityPermit | None, bool]:
        ledger = self._retry_ledger
        if ledger is None:
            return None, False
        was_ending = self._handoff.accepted_ending
        was_finalized = self._handoff.activity_finalized
        permit = self._handoff.finalize_task_activity(
            budget=ledger,
            observation_sequence=frame.body.sequence_id,
        )
        newly_exhausted = (
            permit.limit_status is not RecoveryLimitStatus.ALLOWED
            and not was_finalized
            and not was_ending
        )
        if newly_exhausted:
            request = self._handoff.stop_request
            if request is None or request.destination is not HandoffDestination.FAIL:
                raise ContractViolation(
                    "recovery limit did not stage a typed failure"
                )
            self._request_ending(
                HandoffDestination.FAIL, request.cause, request.reason,
                stopping_reason=request.reason,
            )
        return permit, newly_exhausted

    def _retain_satisfied_goal(
        self, frame: NavigationFrame, input_ledger: InputApplicationLedger | None,
        state_anchor: StateAnchor | None,
    ) -> bool:
        """Release an old body owner, while the same task continues to exist."""
        if (self._goal_requests.reach_policy is not GoalReachPolicy.KEEP_ACTIVE_ON_REACH
                or self._task_stop_requested() or self._pending_goal is not None):
            return False
        observed = self._observed_goal(frame)
        if observed is None or observed.status is not ObservedGoalStatus.SATISFIED:
            return False
        if self._supervisor.route is None:
            handoff = self._supervisor.evaluate_quiescence(frame, input_ledger, state_anchor)
            if handoff.disposition is not HandoffDisposition.QUIESCENT:
                return False
        if not self._clear_active_execution():
            return False
        if self._planning_coordinator is not None:
            self._planning_coordinator.cancel_work("goal_state_satisfied")
        self._end_session_waits()
        if self._state is NavigationSessionState.STOPPING:
            self._transition(
                NavigationTransitionAction.RESUME_ACTIVE_IDLE_AFTER_HANDOFF,
                "goal_state_satisfied",
                handoff=self._supervisor.last_handoff,
            )
        else:
            self._continue_execution("goal_state_satisfied")
        self._begin_task_activity(frame, input_ledger, state_anchor)
        return True

    @property
    def has_owned_body_control(self) -> bool:
        return self._supervisor.has_owned_body_control(
            route_source_bound=self._source is not None)

    @property
    def diagnostics(self) -> NavigationDiagnostics:
        holders: list[str] = []
        phase = None
        kind = None
        action_index = None
        if self._edge_probe is not None and self._edge_probe.owned:
            holders.append("landing_edge_probe")
            phase = self._edge_probe.state.value
        activities = (() if self._frame is None else
                      self._supervisor.activities(self._frame, self._last_decision,
                                                  source_bound=self._source is not None))
        retained = self._supervisor.incumbent_route
        if retained is not None and self._source is not None:
            holders.append("route_executor")
            phase = retained.executor.state.value if phase is None else "overlap"
            route = retained.executor.route
            index = retained.executor.action_index
            if route is not None and 0 <= index < len(route.actions):
                kind = type(route.actions[index]).__name__
                action_index = index
        request = self._request
        progress_owner = None
        if self._edge_probe is not None and self._edge_probe.owned:
            progress_owner = (
                f"landing-edge-probe/{self._edge_probe.goal_id}/"
                f"{self._edge_probe.goal_revision}"
            )
        elif self._active_route is not None:
            progress_owner = f"route/{self._active_route.route_id}"
        confirmed_tick = None
        if (self._control_anchor is not None
                and self._control_anchor.confirmed_control_tick_range is not None):
            confirmed_tick = self._control_anchor.confirmed_control_tick_range[1]
        body_progress = None
        if progress_owner is not None:
            progress_phase = (
                self._last_decision.reason_code
                if self._last_decision is not None else
                phase or self._reason
            )
            body_progress = BodyControlProgress(
                progress_owner,
                progress_phase,
                (0 if self._retry_ledger is None else
                 self._retry_ledger.progress_version),
                action_index,
                confirmed_tick,
                (40 if progress_phase in {
                    "recovering_grounded_verified_entry",
                    "settling_grounded_verified_entry",
                    "releasing_grounded_verified_entry",
                    "repreparing_grounded_verified_motion",
                } else None),
            )
        planning_diagnostics = (
            None if self._planning_coordinator is None or self._frame is None
            else self._planning_coordinator.diagnostics(self._frame)
        )
        return NavigationDiagnostics(
            self._state, tuple(holders), phase, kind, action_index,
            None if request is None else request.sequence,
            None if request is None else request.goal_revision,
            None if self._active_route is None else self._active_route.route_id,
            self._source is not None,
            None if self._pending_goal is None else self._pending_goal.goal_revision,
            None if self._last_decision is None else self._last_decision.state.value,
            None if self._edge_probe is None else self._edge_probe.ended_reason,
            self._task_damage_budget.maximum_expected_damage_points,
            self._movement_damage_spent_points,
            (0 if self._retry_ledger is None else
             dict(self._retry_ledger.recovery_cause_counts).get(
                 RetryCause.EXECUTION, 0,
             )),
            self._information_wait_frames, self._reason,
            self._supervisor.last_handoff,
            (None if self._supervisor.incumbent_route is None else
             self._supervisor.incumbent_route.route.route_id),
            (None if not self._supervisor.has_pending_route else
             self._supervisor.route.route.route_id),
            0,
            0,
            (None if self._pending_retry is None else self._pending_retry.request_id),
            (False if self._retry_ledger is None else
             self._retry_ledger.progress_capacity_exhausted),
            (() if self._risk_ledger is None else
             self._risk_ledger.snapshot_actions()),
            (0.0 if self._risk_ledger is None else
             self._risk_ledger.available_points),
            (0 if self._risk_ledger is None else
             self._risk_ledger.policy_revision),
            self._risk_submission_capacity_exhausted,
            None,
            (() if self._retry_ledger is None else
             self._retry_ledger.recovery_cause_counts),
            (0 if self._retry_ledger is None else
             self._retry_ledger.progress_version),
            (None if self._retry_ledger is None else
             self._retry_ledger.last_progress_evidence),
            self._recovery_wait_status,
            self._recovery_wait_capacity_exhausted,
            0,
            0,
            (),
            self._supervisor.support_fraction,
            self._lifecycle.transition_count,
            self._lifecycle.illegal_transition_count,
            body_progress,
            (() if self._retry_ledger is None else tuple(
                (token.wait_id, token.owner_id)
                for token in self._retry_ledger.active_waits()
            )),
            (
                planning_diagnostics is not None
                and planning_diagnostics.has_owned_work
            ),
            (True if planning_diagnostics is None else
             planning_diagnostics.work_identity_valid),
            (True if planning_diagnostics is None else
             planning_diagnostics.permit_identity_valid),
            (True if planning_diagnostics is None else
             planning_diagnostics.information_identity_valid),
            activities,
            (None if self._retry_ledger is None else
             self._retry_ledger.policy.kind),
            (0 if self._retry_ledger is None else
             self._retry_ledger.policy.maximum_recoveries),
            (0 if self._retry_ledger is None else
             self._retry_ledger.recovery_starts_in_window),
            (0 if self._retry_ledger is None else
             self._retry_ledger.total_recovery_starts),
            (None if self._retry_ledger is None else
             self._retry_ledger.recovery_limit_status),
            (None if self._retry_ledger is None else
             self._retry_ledger.recovery_starts_at_limit_decision),
            route_validation=self._route_validation,
            local_direct_admission=self._last_local_direct_admission,
        )

    def bind_source(self, source: IntentSourceV1) -> None:
        if type(source) is not IntentSourceV1:
            raise ContractViolation("navigation source must be ordered")
        if self._source is not None and self._source != source:
            raise ContractViolation("navigation session already has an input source")
        self._source = source

    def attach_observation_adapter(
        self, adapter: NavigationObservationAdapter,
    ) -> None:
        """Bind a fresh session to the Runtime-owned world projection."""
        if type(adapter) is not NavigationObservationAdapter:
            raise ContractViolation("navigation world owner is invalid")
        if adapter is self._adapter:
            return
        if (self._frame is not None or self._request is not None
                or self._active_route is not None
                or self._state is not NavigationSessionState.READY):
            raise ContractViolation(
                "navigation world owner cannot change after session start"
            )
        self._adapter = adapter

    def unbind_source(self, source: IntentSourceV1) -> None:
        if type(source) is not IntentSourceV1 or self._source != source:
            raise ContractViolation("navigation source does not own this session")
        self._source = None

    def ingest(self, snapshot: ObservationSnapshotV3) -> NavigationFrame:
        """Project one formal observation through this session's sole adapter."""
        if type(snapshot) is not ObservationSnapshotV3:
            raise ContractViolation("navigation session requires Observation V3")
        if (self._frame is not None
                and snapshot.sequence_id == self._frame.body.sequence_id
                and world_session_from_observation(snapshot) == self._frame.session):
            return self._frame
        frame = self._adapter.ingest(snapshot)
        self._route_validation = None
        self.observe(frame, frame.changed_cells)
        return frame

    def motion_residual(
        self,
        snapshot: ObservationSnapshotV3,
        ledger: InputApplicationLedger,
    ) -> MotionResidualResult | None:
        """Compare one new body observation with the sole applied-input ledger."""
        if type(ledger) is not InputApplicationLedger:
            raise ContractViolation("navigation residual requires the Runtime input ledger")
        frame = self.ingest(snapshot)
        result = self._motion_residual.observe(snapshot, frame, ledger)
        if result is not None:
            self._residual_missing = (
                result.missing_cells
                if result.status is MotionResidualStatus.NEEDS_WORLD
                else ()
            )
        return result

    def execution_anchor(
        self,
        snapshot: ObservationSnapshotV3,
        ledger: InputApplicationLedger,
    ) -> StateAnchor | None:
        """Return a current, ledger-backed anchor for verified route motion."""
        self.motion_residual(snapshot, ledger)
        anchor = self._motion_residual.anchor
        own = snapshot.self_state.value
        if (anchor is None or own is None or own.movement_tick_id is None
                or anchor.session != world_session_from_observation(snapshot)
                or anchor.observation_sequence_id != snapshot.sequence_id
                or anchor.movement_tick_id != own.movement_tick_id):
            return None
        return anchor

    def body_handoff(
        self,
        snapshot: ObservationSnapshotV3,
        ledger: InputApplicationLedger,
    ) -> HandoffEvidence:
        """Confirm source release, finalizing verified acquisition ownership."""
        if self._closed:
            # Closing workers does not close the Runtime input source. The
            # driver still needs the just-returned body sample to release it.
            frame = self._adapter.ingest(snapshot)
            self._motion_residual.observe(snapshot, frame, ledger)
            anchor = self._motion_residual.anchor
            own = snapshot.self_state.value
            if (anchor is None or own is None or own.movement_tick_id is None
                    or anchor.session != frame.session
                    or anchor.observation_sequence_id != snapshot.sequence_id
                    or anchor.movement_tick_id != own.movement_tick_id):
                anchor = None
        else:
            frame = self.ingest(snapshot)
            anchor = self.execution_anchor(snapshot, ledger)
        handoff = self._supervisor.evaluate_quiescence(frame, ledger, anchor)
        if (self._edge_probe is not None and self._edge_probe.owned
                and handoff.disposition is HandoffDisposition.QUIESCENT):
            # The driver can query release directly after a client failure,
            # without entering propose() again. Consume the same verified
            # probe exit before allowing its input source to disappear.
            self._control_ledger, self._control_anchor = ledger, anchor
            self._finalize_probe_handoff(frame, handoff)
            # A suspended route shares the source but has its own lifecycle.
            # Probe release alone cannot authorize source release.
            handoff = self._supervisor.evaluate_quiescence(frame, ledger, anchor)
            if (handoff.disposition is HandoffDisposition.QUIESCENT
                    and self._supervisor.incumbent_route is not None):
                if not self._clear_active_execution():
                    return replace(handoff, disposition=HandoffDisposition.RETAIN,
                                   reason="route_release_waiting_for_evidence")
                if self._handoff.ending:
                    self._finish_pending_probe_terminal(handoff.reason)
        return handoff

    def external_motion_reentry(
        self, snapshot: ObservationSnapshotV3,
    ) -> ExternalMotionReentryDecision:
        """Decide whether the existing navigation owner can absorb a shove."""
        frame = self.ingest(snapshot)
        if not frame.body.is_on_ground:
            return ExternalMotionReentryDecision(
                ExternalMotionReentryStatus.REQUIRES_BODY_RECOVERY,
                "external_motion_airborne",
            )
        if self._request is None or self._state in {
            NavigationSessionState.FAILED,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CLOSED,
        }:
            return ExternalMotionReentryDecision(
                ExternalMotionReentryStatus.REQUIRES_BODY_RECOVERY,
                "navigation_contract_unavailable",
            )
        support, missing = self._surface_for_body(frame)
        if support is None:
            return ExternalMotionReentryDecision(
                ExternalMotionReentryStatus.REQUIRES_BODY_RECOVERY,
                "current_support_unknown" if missing
                else "current_support_unavailable",
            )
        # The route executor already evaluates the observed continuous state on
        # its next decision.  Keeping that owner preserves the existing safety
        # checks and avoids turning every recoverable shove into a stop command.
        return ExternalMotionReentryDecision(
            ExternalMotionReentryStatus.CONTINUE_NAVIGATION,
            "current_support_allows_navigation_reentry",
        )

    def observation_request(self, *, max_positions: int = 128) -> ObservationRequestV3:
        if self.report.terminal:
            return ObservationRequestV3("navigation_v1")
        residual_missing = tuple(sorted(set(self._residual_missing)))
        planning_missing = tuple(sorted(
            set(self._snapshot_missing) - set(residual_missing)
        ))
        missing = residual_missing + planning_missing
        route_dependencies = self._supervisor.effective_dependencies
        departure_dependencies: tuple[BlockPos, ...] = ()
        if (self._frame is not None and self._frame.body.is_on_ground
                and self._executor is not None):
            route = getattr(self._executor, "route", None)
            action_index = getattr(self._executor, "action_index", None)
            if (route is not None and type(action_index) is int
                    and 0 <= action_index < len(route.actions)):
                action = route.actions[action_index]
                if action_spec(action).entry_observation(action, self._frame) is not None:
                    # Until the body really leaves the starting support, a
                    # changed landing support can still be answered by
                    # sneaking or stopping.  Recheck this small dependency set
                    # every frame instead of using the ordinary 20-tick map
                    # refresh interval.
                    current_precondition = self._current_action_precondition(
                        self._frame, through_grounded_departure=True,
                    )
                    departure_dependencies = tuple(dict.fromkeys(
                        (current_precondition.missing_cells
                         if current_precondition is not None
                         and current_precondition.status is
                            ActionPreconditionStatus.NEEDS_INFORMATION
                         else action.dependencies)
                    ))[:16]
        if not missing and not route_dependencies and not departure_dependencies:
            return ObservationRequestV3("navigation_v1")
        if not self._adapter.has_frame:
            return ObservationRequestV3(
                "navigation_v1", missing[:max_positions],
            )
        residual_request = ObservationRequestV3("navigation_v1")
        if residual_missing:
            residual_request, _ = self._adapter.air_request(
                residual_missing, max_positions=max_positions,
            )
        remaining = max_positions - len(residual_request.air_positions)
        probe_request = ObservationRequestV3("navigation_v1")
        if (remaining
                and self._edge_probe is not None
                and self._edge_probe.active
                and self._edge_probe.landing_cell in planning_missing):
            # An owned edge probe is already paying the movement cost to make
            # this exact cell visible.  Query it on every new frame instead of
            # waiting for the ordinary known-cell refresh interval.
            probe_positions = tuple(dict.fromkeys((
                self._edge_probe.landing_cell,
                *self._edge_probe.dependencies,
            )))[:remaining]
            probe_request = ObservationRequestV3(
                "navigation_v1", probe_positions,
            )
            planning_missing = tuple(
                position for position in planning_missing
                if position != self._edge_probe.landing_cell
            )
            remaining -= len(probe_positions)
        departure_request = ObservationRequestV3("navigation_v1")
        if departure_dependencies and remaining:
            departure_request = ObservationRequestV3(
                "navigation_v1", departure_dependencies[:remaining],
            )
            remaining -= len(departure_request.air_positions)
        planning_request = ObservationRequestV3("navigation_v1")
        if planning_missing and remaining:
            unknown = tuple(
                position for position in planning_missing
                if self._frame is None
                or self._frame.world.cell(position).knowledge
                    is CellKnowledge.UNKNOWN
            )
            unknown_set = set(unknown)
            known = tuple(
                position for position in planning_missing
                if position not in unknown_set
            )
            unknown_request, _ = self._adapter.air_request(
                unknown, max_positions=remaining,
            ) if unknown else (ObservationRequestV3("navigation_v1"), ())
            known_capacity = remaining - len(unknown_request.air_positions)
            known_request, _ = self._adapter.air_request(
                known, max_positions=known_capacity, include_known=True,
            ) if known and known_capacity else (ObservationRequestV3("navigation_v1"), ())
            planning_request = merge_observation_requests((
                unknown_request, known_request,
            ))
        remaining -= len(planning_request.air_positions)
        dependency_request = ObservationRequestV3("navigation_v1")
        if route_dependencies and remaining:
            dependency_request, _ = self._adapter.air_request(
                route_dependencies,
                max_positions=min(16, remaining),
                include_known=True,
            )
        return merge_observation_requests((
            residual_request, probe_request, departure_request,
            planning_request, dependency_request,
        ))

    def start(
        self,
        request: PlanningRequest | SurfacePlanningRequest,
        frame: NavigationFrame,
    ) -> None:
        if type(request) not in (PlanningRequest, SurfacePlanningRequest):
            raise ContractViolation("navigation session requires a planning request")
        if type(frame) is not NavigationFrame:
            raise ContractViolation("navigation session requires a navigation frame")
        if self._closed:
            raise ContractViolation("navigation session is closed")
        if request.world_session != frame.session.value:
            raise ContractViolation("navigation request belongs to another world")
        if self._request is not None and request.sequence <= self._request.sequence:
            raise ContractViolation("navigation request generation did not advance")
        self._validate_successor_task_id(request.goal_id)
        self._goal_requests.select_reach_policy(request.reach_policy)
        new_risk_ledger = self._risk_ledger is None
        if type(request) is SurfacePlanningRequest:
            self._task_damage_budget = request.damage_budget
            if new_risk_ledger:
                self._movement_damage_spent_points = 0.0
            self._executor_reported_damage_points = 0.0
        self._ensure_task_retry_ledger(request.goal_id)
        if new_risk_ledger:
            budget = (request.damage_budget if type(request) is SurfacePlanningRequest
                      else TaskDamageBudget())
            self._risk_ledger = TaskRiskLedger(request.goal_id, budget)
        elif self._risk_ledger.task_id != request.goal_id:
            raise ContractViolation("navigation request changed risk task identity")
        self._pending_goal = None
        self._replace_request(request, frame, "request_started")

    def start_goal(
        self,
        goal_id: str,
        goal_revision: int,
        goal_state: GoalState,
        frame: NavigationFrame,
        *,
        maximum_expansions: int = 100_000,
        maximum_planning_seconds: float = .5,
        damage_budget: TaskDamageBudget | None = None,
        task_id: str | None = None,
        reach_policy: GoalReachPolicy = GoalReachPolicy.COMPLETE_ON_REACH,
        planning_policy: GoalPlanningPolicy = (
            GoalPlanningPolicy.BACKGROUND_PLANNER
        ),
    ) -> None:
        self._validate_successor_task_id(goal_id if task_id is None else task_id)
        self._admit_command_event(NavigationSessionEvent.START_GOAL)
        self._start_goal_after_admission(
            goal_id, goal_revision, goal_state, frame,
            maximum_expansions=maximum_expansions,
            maximum_planning_seconds=maximum_planning_seconds,
            damage_budget=damage_budget,
            task_id=task_id,
            reach_policy=reach_policy,
            planning_policy=planning_policy,
        )

    def _start_goal_after_admission(
        self,
        goal_id: str,
        goal_revision: int,
        goal_state: GoalState,
        frame: NavigationFrame,
        *,
        maximum_expansions: int = 100_000,
        maximum_planning_seconds: float = .5,
        damage_budget: TaskDamageBudget | None = None,
        task_id: str | None = None,
        reach_policy: GoalReachPolicy | None = None,
        planning_policy: GoalPlanningPolicy | None = None,
    ) -> None:
        """Resolve current and goal support without creating point-goal state."""
        if self._request is not None:
            raise ContractViolation("navigation session already has a request")
        require_identifier(goal_id, "navigation goal id")
        if type(goal_revision) is not int or goal_revision < 0:
            raise ContractViolation("navigation goal revision is invalid")
        if type(goal_state) is not GoalState or type(frame) is not NavigationFrame:
            raise ContractViolation("navigation goal requires typed state and frame")
        if reach_policy is not None and type(reach_policy) is not GoalReachPolicy:
            raise ContractViolation("navigation goal reach policy must be typed")
        if (planning_policy is not None
                and type(planning_policy) is not GoalPlanningPolicy):
            raise ContractViolation("navigation goal planning policy must be typed")
        if damage_budget is None:
            damage_budget = (TaskDamageBudget() if self._risk_ledger is None
                             else self._risk_ledger.budget)
        if type(damage_budget) is not TaskDamageBudget:
            raise ContractViolation("navigation goal damage budget must be typed")
        identity = goal_id if task_id is None else task_id
        require_identifier(identity, "navigation task id")
        if reach_policy is not None:
            self._goal_requests.select_reach_policy(reach_policy)
        if planning_policy is not None:
            self._goal_requests.select_planning_policy(planning_policy)
        self._ensure_task_retry_ledger(identity, initial_support=(
            math.floor(frame.body.position[0]),
            math.floor(frame.body.position[1] + 1.0e-9),
            math.floor(frame.body.position[2]),
        ))
        self._goal_requests.bind_computation_scope(identity, frame.session.value)
        new_risk_ledger = self._risk_ledger is None
        if new_risk_ledger:
            self._risk_ledger = TaskRiskLedger(identity, damage_budget)
        elif self._risk_ledger.task_id != identity:
            raise ContractViolation("navigation goal changed risk task identity")
        elif self._risk_ledger.budget != damage_budget:
            self._risk_ledger.update_policy(
                damage_budget,
                revision=max(goal_revision, self._risk_ledger.policy_revision + 1),
            )
        self._task_damage_budget = damage_budget
        if new_risk_ledger:
            self._movement_damage_spent_points = 0.0
        self._executor_reported_damage_points = 0.0
        goal_node, missing = self._surface_for_goal(frame, goal_state)
        if goal_node is None:
            self._frame = frame
            self._pending_goal = PendingGoalRevision(
                goal_id, goal_revision, goal_state, damage_budget,
            )
            self._snapshot_missing = missing
            self._transition(
                (NavigationTransitionAction.WAIT_FOR_INFORMATION
                 if missing else NavigationTransitionAction.MARK_FAILED),
                ("goal_surface_requires_information"
                 if missing else "goal_surface_unavailable"),
            )
            return
        start_node, start_missing = self._surface_for_body(frame)
        if start_node is None:
            self._frame = frame
            self._pending_goal = PendingGoalRevision(
                goal_id, goal_revision, goal_state, damage_budget,
            )
            self._snapshot_missing = start_missing
            self._transition(
                (NavigationTransitionAction.WAIT_FOR_INFORMATION
                 if start_missing else NavigationTransitionAction.MARK_FAILED),
                ("current_surface_requires_information"
                 if start_missing else "current_surface_unavailable"),
            )
            return
        request = self._goal_request(
            goal_id, goal_revision, goal_state, start_node, goal_node, frame,
            maximum_expansions=maximum_expansions,
            maximum_planning_seconds=maximum_planning_seconds,
            damage_budget=damage_budget,
        )
        self._accept_goal_request(request, frame, "goal_started")

    def update_goal(
        self,
        goal_id: str,
        goal_revision: int,
        goal_state: GoalState,
        *,
        damage_budget: TaskDamageBudget | None = None,
        task_id: str | None = None,
    ) -> bool:
        # A task whose ending request was already accepted must not revive,
        # but a late update is an expected lifecycle race. Reject it without
        # mutating either side's authoritative goal revision.
        if self.report.terminal or self._handoff.accepted_ending:
            return False
        self._admit_command_event(NavigationSessionEvent.REVISE_GOAL)
        if self._frame is None:
            raise ContractViolation("surface goal update requires an active request")
        if (task_id is not None and (self._retry_ledger is None
                or self._retry_ledger.task_id != task_id)):
            raise ContractViolation("navigation goal changed task ledger identity")
        if (task_id is not None and (self._risk_ledger is None
                or self._risk_ledger.task_id != task_id)):
            raise ContractViolation("navigation goal changed risk task identity")
        if self._request is None:
            if self._pending_goal is None:
                raise ContractViolation(
                    "surface goal update requires an active request"
                )
            pending = self._pending_goal
            if (goal_id != pending.goal_id or type(goal_revision) is not int
                    or goal_revision <= pending.goal_revision
                    or type(goal_state) is not GoalState):
                raise ContractViolation(
                    "navigation goal identity or revision is invalid"
                )
            self._request_probe_stop(StopCause.GOAL_REVISED)
            self._pending_goal = None
            self._snapshot_missing = ()
            self._start_goal_after_admission(
                goal_id, goal_revision, goal_state, self._frame,
                damage_budget=(pending.damage_budget if damage_budget is None
                               else damage_budget),
                task_id=self._retry_ledger.task_id,
            )
            return True
        if type(self._request) is not SurfacePlanningRequest:
            raise ContractViolation("surface goal update requires an active request")
        if (goal_id != self._request.goal_id
                or type(goal_revision) is not int
                or goal_revision <= self._request.goal_revision
                or type(goal_state) is not GoalState):
            raise ContractViolation("navigation goal identity or revision is invalid")
        self._last_local_direct_admission = None
        self._request_probe_stop(StopCause.GOAL_REVISED)
        if damage_budget is not None:
            self._task_damage_budget = damage_budget
            if (self._risk_ledger is not None
                    and self._risk_ledger.budget != damage_budget):
                self._risk_ledger.update_policy(
                    damage_budget,
                    revision=max(goal_revision,
                                 self._risk_ledger.policy_revision + 1),
                )
        if type(self._task_damage_budget) is not TaskDamageBudget:
            raise ContractViolation("navigation goal damage budget must be typed")
        next_budget = self._remaining_damage_budget()
        pending = PendingGoalRevision(
            goal_id, goal_revision, goal_state, next_budget,
        )
        next_request = self._goal_requests.advance(
            self.session_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            goal_state=goal_state,
            damage_budget=next_budget,
        )
        goal_node, missing = self._surface_for_goal(self._frame, goal_state)
        if goal_node is None:
            self._request = next_request
            if (missing
                    and self._goal_requests.planning_policy is
                        GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
                    and self._state is not NavigationSessionState.STOPPING
                    and self._supervisor.incumbent_route is not None
                    and self._ordinary_walk_can_continue_during_direct(
                        self._frame
                    )):
                self._pending_goal = pending
                self._snapshot_missing = missing
                self._direct_goal_request_id = None
                self._direct_planning_permit = None
                self._supervisor.discard_pending_route()
                if self._planning_coordinator is not None:
                    self._planning_coordinator.cancel_work(
                        "revised_ground_direct_requires_information",
                    )
                self._continue_execution(
                    "incumbent_continues_during_ground_direct_information",
                )
                return True
            if self._has_body_owner() or self._state is NavigationSessionState.STOPPING:
                self._stage_goal_revision_for_body_release(
                    pending, missing, "goal_revision_waits_for_body_owner",
                )
            else:
                self._pending_goal = pending
                self._snapshot_missing = missing
                self._transition(
                    (NavigationTransitionAction.WAIT_FOR_INFORMATION
                     if missing else NavigationTransitionAction.MARK_FAILED),
                    ("goal_surface_requires_information"
                     if missing else "goal_surface_unavailable"),
                )
            return True
        if self._current_risk_action_has_started():
            # A submitted damaging action already owns the body's immediate
            # future.  Planning from the pre-landing pose would either count
            # that same drop twice or reject the replacement because its
            # allowance is currently held.  Record the new target, ask the
            # incumbent to finish safely, then re-anchor and plan from the
            # observed landing state.
            self._request = replace(
                next_request,
                goal=goal_node,
            )
            if self._planning_coordinator is not None:
                self._planning_coordinator.cancel_work()
            self._stage_goal_revision_for_body_release(
                pending, (),
                "goal_revision_waits_for_risk_action_terminal",
            )
            return True
        start_node, start_missing = self._surface_for_body(self._frame)
        if start_node is None:
            self._request = replace(next_request, goal=goal_node)
            if self._has_body_owner() or self._state is NavigationSessionState.STOPPING:
                self._stage_goal_revision_for_body_release(
                    pending, start_missing, "goal_revision_waits_for_body_owner",
                )
            else:
                self._pending_goal = pending
                self._snapshot_missing = start_missing
                self._transition(
                    (NavigationTransitionAction.WAIT_FOR_INFORMATION
                     if start_missing else NavigationTransitionAction.MARK_FAILED),
                    ("current_surface_requires_information"
                     if start_missing else "current_surface_unavailable"),
                )
            return True
        request = replace(
            next_request,
            start=start_node,
            goal=goal_node,
        )
        if self._state is NavigationSessionState.STOPPING:
            self._request = request
            self._stage_goal_revision_for_body_release(
                pending, (), "goal_revision_waits_for_body_owner",
            )
            return True
        self._pending_goal = None
        coordinator = self._planning_coordinator
        proved_ground_eligible = (
            self._can_attempt_ground_direct_request(request, self._frame)
            or self._can_attempt_local_direct_request(request, self._frame)
        )
        if (not proved_ground_eligible
                and coordinator is not None and coordinator.has_owned_work
                and coordinator.work_identity.work_kind is AsyncWorkKind.PLANNING):
            self._request = request
            coordinator.revise_request(request)
            self._local_goal_request_id = None
            self._snapshot_missing = ()
            self._information.select_missing(())
            self._transition(NavigationTransitionAction.BEGIN_PLANNING, "goal_revised")
            return True
        self._accept_goal_request(
            request, self._frame, "goal_revised",
            preserve_active_route=self._executor is not None,
        )
        return True

    def _goal_request(
        self,
        goal_id: str,
        goal_revision: int,
        goal_state: GoalState,
        start_node: SurfaceNodeId,
        goal_node: SurfaceNodeId,
        frame: NavigationFrame,
        *,
        maximum_expansions: int = 100_000,
        maximum_planning_seconds: float = .5,
        damage_budget: TaskDamageBudget = TaskDamageBudget(),
    ) -> SurfacePlanningRequest:
        return SurfacePlanningRequest(
            1, f"{self.session_id}-request-1", goal_id, goal_revision,
            frame.session.value, start_node, goal_node,
            maximum_expansions=maximum_expansions,
            initial_resources=ResourceState((
                ("food_points", float(frame.body.food_points)),
            )),
            goal_state=goal_state,
            damage_budget=damage_budget,
            maximum_planning_seconds=maximum_planning_seconds,
            reach_policy=self._goal_requests.reach_policy,
        )

    def _remaining_damage_budget(self) -> TaskDamageBudget:
        if self._risk_ledger is not None:
            return TaskDamageBudget(
                self._risk_ledger.budget.risk_policy_id,
                self._risk_ledger.available_points,
            )
        return TaskDamageBudget(
            self._task_damage_budget.risk_policy_id,
            max(
                0.0,
                self._task_damage_budget.maximum_expected_damage_points
                - self._movement_damage_spent_points,
            ),
        )

    def _record_completed_movement_damage(
        self, reported_points: float | None = None,
    ) -> None:
        if reported_points is None:
            if self._executor is None:
                return
            reported = getattr(
                self._executor, "completed_movement_damage_points", 0.0,
            )
        else:
            if (type(reported_points) not in (int, float)
                    or not math.isfinite(reported_points)
                    or reported_points < 0):
                raise ContractViolation("reported movement damage is invalid")
            reported = float(reported_points)
        if reported + 1.0e-9 < self._executor_reported_damage_points:
            raise ContractViolation("movement damage accounting regressed")
        self._movement_damage_spent_points += max(
            0.0, reported - self._executor_reported_damage_points,
        )
        self._executor_reported_damage_points = reported

    @staticmethod
    def _route_expected_damage_points(route: ActiveRoute) -> float:
        return sum(action_spec(action).expected_damage_points(action)
                   for action in route.action_route.actions)

    def _record_retry_route_progress(
        self, frame: NavigationFrame, decision: ActionRouteDecision,
    ) -> tuple[ProgressEvidence, ...]:
        ledger = self._retry_ledger
        route = self._active_route
        if ledger is None or route is None:
            return ()
        evidence: list[ProgressEvidence] = []
        previous_index = (
            self._last_retry_action_index
            if self._last_retry_route_id == route.route_id else None
        )
        if previous_index is not None and decision.action_index > previous_index:
            if (self._completed_acquisition is not None
                    and self._completed_acquisition.action_index
                        < decision.action_index):
                self._completed_acquisition = None
            for index in range(previous_index, decision.action_index):
                action = route.action_route.actions[index]
                if type(action) is WalkSegment:
                    endpoints = (
                        (action.fixed_route.points[0].x,
                         action.fixed_route.points[0].y,
                         action.fixed_route.points[0].z),
                        (action.fixed_route.points[-1].x,
                         action.fixed_route.points[-1].y,
                         action.fixed_route.points[-1].z),
                    )
                elif type(action) is JumpUpSegment:
                    endpoints = (action.edge.start, action.edge.end)
                else:
                    endpoints = (action.start_surface.node_id,
                                 action.end_surface.node_id)
                fingerprint = hashlib.sha256(
                    repr(endpoints).encode("utf-8")
                ).hexdigest()[:24]
                action_id = f"{type(action).__name__}/{fingerprint}"
                evidence.append(ProgressEvidence(
                    ProgressKind.ACTION_COMPLETED,
                    frame.body.sequence_id, action_id=action_id,
                ))
        self._last_retry_route_id = route.route_id
        self._last_retry_action_index = decision.action_index
        if not frame.body.is_on_ground:
            return tuple(evidence)
        position = frame.body.position
        cell = (math.floor(position[0]),
                math.floor(position[1] + 1.0e-9),
                math.floor(position[2]))
        if cell == self._last_retry_body_cell:
            return tuple(evidence)
        self._last_retry_body_cell = cell
        node, _ = self._surface_for_body(frame)
        if node is None:
            return tuple(evidence)
        corridor = tuple(
            (item.column_x, item.vertical_band, item.column_z)
            for item in route.corridor.node_ids
            if type(item) is SurfaceNodeId
        )
        support = (node.column_x, node.vertical_band, node.column_z)
        evidence.append(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, frame.body.sequence_id,
            support=support, valid_corridor=corridor,
        ))
        return tuple(evidence)

    def _reserve_route_risk(
        self, route_control: RouteControl,
        decision: ActionRouteDecision,
    ) -> tuple[RiskReservationStatus | None, str | None]:
        ledger = self._risk_ledger
        route = route_control.route
        if ledger is None or not decision.submit_input:
            return None, None
        if not 0 <= decision.action_index < len(route.action_route.actions):
            return None, None
        action = route.action_route.actions[decision.action_index]
        spec = action_spec(action)
        if not spec.tracks_damage:
            return None, None
        key = (f"{route.source_request_id}/{route.route_id}",
               decision.action_index)
        record = (None if self._risk_action_id is None else
                  ledger.action(self._risk_action_id))
        if (self._risk_action_key != key or record is None
                or record.state is RiskActionState.RELEASED):
            self._risk_action_key = key
            self._risk_action_id = ledger.next_action_id()
        expected = spec.expected_damage_points(action)
        result = ledger.reserve(
            self._risk_action_id, expected,
            policy_revision=ledger.policy_revision,
        )
        if (result.status in {RiskReservationStatus.RESERVED,
                              RiskReservationStatus.EXISTING}
                and self._frame is not None):
            ledger.observe_health(
                self._risk_action_id,
                observation_sequence=self._frame.body.sequence_id,
                health_points=self._frame.body.health_points,
                on_ground=self._frame.body.is_on_ground,
            )
        return result.status, self._risk_action_id

    @staticmethod
    def _needs_same_frame_stop_protection(
        advance: BodyFrameAdvance,
    ) -> bool:
        """Return whether this owner can depart before the next observation.

        An ordinary ground controller already produced a safe command for the
        current observation.  Its stop request takes effect on the following
        frame.  Verified or legacy air actions can cross an irreversible
        boundary with this frame's command, so they keep the existing
        same-frame protection path.
        """
        control = advance.route_advance.control
        executor = control.executor
        route = executor.route
        index = advance.route_advance.decision.action_index
        if route is None or not 0 <= index < len(route.actions):
            return False
        action = route.actions[index]
        return (
            executor.active_verified_entry_state() is not None
            or action_spec(action).stop_hold.same_frame_protection
        )

    def _current_risk_action_has_started(self) -> bool:
        ledger = self._risk_ledger
        if ledger is None or self._risk_action_id is None \
                or self._executor is None:
            return False
        record = ledger.action(self._risk_action_id)
        return record is not None and (
            record.state is RiskActionState.COMMITTED
            or (record.state is RiskActionState.RESERVED
                and bool(record.submitted_sequences))
        )

    def _release_unselected_risk(
        self, proposal: NavigationSessionProposal,
    ) -> None:
        action_id = proposal.risk_action_id
        if action_id is None or self._risk_ledger is None:
            return
        if self._risk_ledger.release_unstarted(
            action_id, RiskReleaseEvidence.ARBITRATION_LOST,
        ) and self._risk_action_id == action_id:
            self._risk_action_id = None
            self._risk_action_key = None

    def _risk_checked_decision(
        self, advance: BodyFrameAdvance,
        frame: NavigationFrame, state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None,
    ) -> tuple[BodyFrameAdvance, str | None]:
        route_control = advance.route_advance.control
        decision = advance.route_advance.decision
        status, action_id = self._reserve_route_risk(route_control, decision)
        if status in {None, RiskReservationStatus.RESERVED,
                      RiskReservationStatus.EXISTING}:
            return advance, action_id
        self._risk_failure_reason = f"risk_{status.value}"
        self._request_ending(HandoffDestination.FAIL, StopCause.CANCELLED,
                             self._risk_failure_reason)
        # The incumbent still owns the body. Its cancel path must provide the
        # protective input until Runtime verifies that it can be retired.
        return self._supervisor.stop_protection(
            advance, frame, input_ledger, state_anchor,
        current_scope=self.current_computation_scope), None

    def _commit_applied_risk(
        self, frame: NavigationFrame,
        input_ledger: InputApplicationLedger | None,
    ) -> None:
        ledger = self._risk_ledger
        if ledger is None:
            return
        if input_ledger is not None:
            for action in ledger.snapshot_actions():
                if action.state is not RiskActionState.RESERVED:
                    continue
                for sequence in action.submitted_sequences:
                    record = input_ledger.record(sequence)
                    if record is not None and record.applied_ticks:
                        ledger.commit(action.action_id, RiskCommitEvidence(
                            RiskCommitKind.APPLIED_COMMAND,
                            control_sequence=sequence,
                            movement_tick_id=record.applied_ticks[0],
                        ))
                        break
        if not frame.body.is_on_ground and self._risk_action_id is not None:
            action = ledger.action(self._risk_action_id)
            if action is not None and action.state is RiskActionState.RESERVED:
                ledger.commit(action.action_id, RiskCommitEvidence(
                    RiskCommitKind.OBSERVED_DEPARTURE,
                    observation_sequence=frame.body.sequence_id,
                ))
        for action in ledger.snapshot_actions():
            if action.state in {RiskActionState.RESERVED,
                                RiskActionState.COMMITTED}:
                ledger.observe_health(
                    action.action_id,
                    observation_sequence=frame.body.sequence_id,
                    health_points=frame.body.health_points,
                    on_ground=frame.body.is_on_ground,
                )

    def _close_finished_risk(
        self, frame: NavigationFrame,
        decision: ActionRouteDecision | None,
    ) -> None:
        ledger = self._risk_ledger
        if ledger is None or not frame.body.is_on_ground:
            return
        terminal = decision is None or decision.state in {
            ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
            ActionRouteState.FAILED, ActionRouteState.BLOCKED,
            ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
            ActionRouteState.NEEDS_REPLAN,
        }
        for action in ledger.snapshot_actions():
            if action.state is not RiskActionState.COMMITTED:
                continue
            if (action.action_id != self._risk_action_id
                    or terminal
                    or (self._risk_action_key is not None
                        and decision is not None
                        and decision.action_index != self._risk_action_key[1])):
                ledger.close_health_window(
                    action.action_id, on_ground=True,
                )

    def _landing_acquisition_pending(self) -> bool:
        probe = self._edge_probe
        return (
            probe is not None
            and probe.owned
            and probe.landing_cell in self._snapshot_missing
        )

    def _current_action_precondition(
        self, frame: NavigationFrame, *,
        through_grounded_departure: bool = False,
        action_index: int | None = None,
    ) -> ActionPreconditionResult | None:
        route = self._active_route
        executor = self._executor
        if (route is None or executor is None
                or self._state is NavigationSessionState.STOPPING):
            return None
        choice = select_current_boundary(
            route, getattr(executor, "action_index", None), action_index, frame,
            started=(hasattr(executor, "current_verified_action_started")
                     and executor.current_verified_action_started()),
            through_grounded_departure=through_grounded_departure,
        )
        if choice is None:
            return None
        task_id = (
            self._retry_ledger.task_id
            if self._retry_ledger is not None else route.goal_id
        )
        return check_action_precondition(
            route, choice.action_index, frame,
            task_id=task_id, edge_probe=self._edge_probe,
            acquisition_grant=self._completed_acquisition,
        )

    def _upcoming_action_precondition_index(
        self, frame: NavigationFrame,
    ) -> int | None:
        """Select the next acquisition without granting strict entry."""
        executor = self._executor
        active_route = self._active_route
        action_route = (
            None if executor is None else getattr(executor, "route", None)
        )
        if (executor is None or active_route is None or action_route is None
                or action_route is not active_route.action_route):
            return None
        probe = self._edge_probe
        binding = (None if probe is None or not probe.owned else
                   (probe.route_id, probe.route_revision, probe.action_index))
        choice = select_upcoming_boundary(
            active_route, executor.action_index, frame,
            probe_binding=binding, grant=self._completed_acquisition,
        )
        return None if choice is None else choice.action_index

    def _clear_pending_action_boundary(self) -> None:
        """Revoke staged strict entry when its owning intent is withdrawn."""
        seen: set[int] = set()
        for control in (
            self._supervisor.incumbent_route,
            self._supervisor.route,
        ):
            if control is None or id(control) in seen:
                continue
            seen.add(id(control))
            control.executor.clear_pending_action_boundary()

    def _begin_action_acquisition(
        self, result: ActionPreconditionResult, frame: NavigationFrame,
    ) -> bool:
        spec = result.acquisition
        if (result.status is not ActionPreconditionStatus.NEEDS_ACQUISITION
                or spec is None):
            raise ContractViolation(
                "action acquisition requires a typed acquisition result"
            )
        probe = self._edge_probe
        if probe is not None and probe.belongs_to_action(
                spec.route_id, spec.route_revision, spec.action_index):
            self._snapshot_missing = result.missing_cells
            self._transition(NavigationTransitionAction.WAIT_FOR_INFORMATION, result.reason)
            return True
        if probe is not None and probe.owned:
            self._request_probe_stop(StopCause.ROUTE_REPLACED)
            return False
        route = self._active_route
        if (route is None
                or (route.route_id, route.route_revision)
                    != (spec.route_id, spec.route_revision)
                or not 0 <= spec.action_index < len(route.action_route.actions)):
            raise ContractViolation(
                "action acquisition does not match the active route"
            )
        action = route.action_route.actions[spec.action_index]
        entry = action_spec(action).entry_observation(action, frame)
        if entry is None or entry.entry_window is None:
            raise ContractViolation(
                "action acquisition requires an observation entry window"
            )
        self._edge_probe = LandingEdgeProbe(
            spec.goal_id,
            spec.goal_revision,
            spec.landing_cell,
            frame.body.sequence_id,
            acquisition_id=spec.acquisition_id,
            route_id=spec.route_id,
            route_revision=spec.route_revision,
            action_index=spec.action_index,
            world_session=spec.world_session,
            geometry_revision=spec.geometry_revision,
            dependencies=spec.dependencies,
            entry_window=entry.entry_window,
        )
        self._snapshot_missing = result.missing_cells
        self._transition(NavigationTransitionAction.WAIT_FOR_INFORMATION, result.reason)
        if self._retry_ledger is not None:
            self._end_information_wait()
        return True

    def _resolve_pending_retry(self, frame: NavigationFrame) -> None:
        pending = self._handoff.stop_request
        assert pending is not None
        if not self._clear_active_execution():
            if (self._edge_probe is not None
                    and self._edge_probe.owned
                    and self._request_probe_stop(
                        StopCause.DEPENDENCY_CHANGED,
                    )):
                return
            self._transition(
                NavigationTransitionAction.BEGIN_STOPPING,
                "route_release_waiting_for_evidence",
            )
            return
        if self._pending_goal is not None:
            self._resume_pending_goal(frame)
            return
        start, missing = self._surface_for_body(frame)
        resolution = self._handoff.advance(
            frame, handoff=self._supervisor.last_handoff,
            goal_ready=True, start_ready=start is not None,
            missing_cells=missing, unavailable_reason="current_surface_unavailable",
            budget=self._retry_ledger,
        )
        if resolution is None:
            return
        if resolution.destination is HandoffDestination.REPLAN:
            planning_permit = None
            if resolution.planning_permit_kind is PlanningAttemptPermitKind.RETRY:
                if (resolution.request_id is None
                        or resolution.recovery_identity is None
                        or resolution.handoff is None
                        or self._request is None):
                    raise ContractViolation(
                        "recovery reanchor is missing F8 handoff evidence"
                    )
                planning_permit = PlanningAttemptPermit(
                    f"{resolution.request_id}/permit",
                    self._retry_ledger.task_id,
                    self._request.goal_revision,
                    resolution.request_id,
                    PlanningAttemptPermitKind.RETRY,
                    resolution.recovery_identity,
                )
            self._reissue_request_from_current(
                frame,
                resolution.reason,
                source_event_id=resolution.request_id,
                retry_trigger=(
                    PlanningRetryTrigger.RECOVERY_REANCHOR
                    if resolution.planning_permit_kind
                        is PlanningAttemptPermitKind.RETRY
                    else None
                ),
                retry_cause=(pending.retry_cause or RetryCause.PLANNING),
                planning_permit=planning_permit,
                computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR,
            )
        elif resolution.destination is HandoffDestination.WAIT_FOR_INFORMATION:
            self._snapshot_missing = resolution.missing_cells
            self._transition(NavigationTransitionAction.WAIT_FOR_INFORMATION, resolution.reason)
        else:
            self._transition(NavigationTransitionAction.MARK_FAILED, resolution.reason)
            self._snapshot_missing = resolution.missing_cells or pending.missing_cells

    def observe(
        self,
        frame: NavigationFrame,
        changed_cells: tuple[BlockPos, ...],
    ) -> None:
        if type(frame) is not NavigationFrame or type(changed_cells) is not tuple:
            raise ContractViolation("navigation observation requires typed current state")
        if self._closed:
            raise ContractViolation("navigation session is closed")
        support = query_support(frame.body.body_box, frame.world)
        self._supervisor.record_support_fraction(
            support.support_fraction
            if support.status is QueryStatus.FEASIBLE else None
        )
        if self._frame is not None:
            if frame.session != self._frame.session:
                if self._request is not None or self._pending_goal is not None:
                    self._goal_requests.invalidate_computation(ComputationInvalidationCause.WORLD_CHANGED,
                        world_session_id=frame.session.value)
                self._transition(NavigationTransitionAction.MARK_FAILED, 'world_session_changed')
                self._retire_route()
                self._frame = frame
                return
            if frame.body.sequence_id < self._frame.body.sequence_id:
                raise ContractViolation("navigation observation sequence regressed")
            if frame.body.sequence_id == self._frame.body.sequence_id:
                # ``ingest`` and ``propose`` can receive the same formal
                # observation in one public control tick.  Its world changes
                # already belong to the snapshot/request created from that
                # frame; replaying them would falsely invalidate the route as
                # though they happened after planning began.
                self._frame = frame
                return
        self._frame = frame
        if self._state in {
            NavigationSessionState.CLOSED,
            NavigationSessionState.FAILED,
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
        }:
            # A fresh observation is still needed to prove body handoff after
            # a terminal decision.  It may update current support evidence,
            # but it cannot restart planning or invalidate the already-final
            # task merely because the same world edit is observed again.
            return
        frame.world.set_protection(
            frame.body.position,
            self._supervisor.effective_dependencies,
        )
        if self._planning_coordinator is not None:
            self._planning_coordinator.observe_changes(changed_cells)
            expired = self._planning_coordinator.expire_at_observation(
                frame, remaining_damage_budget=self._remaining_damage_budget(),
            )
            if expired is not None and expired.kind is PlanningUpdateKind.FAILED:
                self._fail_planning_or_preserve_incumbent(expired.failure)
                return
        interaction_invalid = False
        if self._required_interaction is not None:
            requirement = self._required_interaction.requirement
            interaction_invalid = (
                bool(set(changed_cells).intersection(requirement.dependencies))
                or frame.world.cell(requirement.support).knowledge
                    is not CellKnowledge.BLOCK
                or frame.world.cell(requirement.destination).knowledge
                    is not CellKnowledge.AIR
            )
        if interaction_invalid:
            interaction_id = self._required_interaction.requirement.interaction_id
            self._required_interaction = None
            planning = self._planning_coordinator
            if (planning is not None
                    and planning.interaction_was_confirmed(interaction_id)):
                update = planning.resume_after_confirmed_interaction(
                    frame,
                    interaction_id,
                    remaining_damage_budget=self._remaining_damage_budget(),
                )
                self._request = update.request
                if update.kind is PlanningUpdateKind.FAILED:
                    assert update.failure is not None
                    self._fail_planning_or_preserve_incumbent(
                        update.failure,
                    )
                else:
                    self._transition(
                        NavigationTransitionAction.BEGIN_PLANNING,
                        update.reason,
                    )
            else:
                self._reissue_request_from_current(
                    frame,
                    "world_interaction_dependency_changed",
                    retry_trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
                    retry_cause=RetryCause.DEPENDENCY,
                    computation_invalidation=ComputationInvalidationCause.BASIS_INVALIDATED,
                )
            return
        selection = self.planning_information_update
        acquired = self._information.observe(
            frame, changed_cells,
            landing_acquisition_pending=self._landing_acquisition_pending(),
            edge_probe=self._edge_probe,
            ledger=self._retry_ledger if selection is None else None,
            wait_owner_id=(self._information_wait_owner_id()
                           if selection is None and self._retry_ledger is not None
                           else None),
        )
        if acquired.align_probe_entry:
            self._edge_probe.begin_entry_alignment(frame)
        planning_need = None if selection is None else selection.information_need
        information_updated = bool(acquired.acquired_cells)
        coordinator_progressed = False
        if planning_need is not None:
            assert self._planning_coordinator is not None
            outcome = self._planning_coordinator.reconcile_information(
                selection, frame,
                edge_probe=self._edge_probe,
                current_scope=self.current_computation_scope,
            )
            coordinator_progressed = outcome.kind is PlanningUpdateKind.INFORMATION_ACQUIRED
            if outcome.kind is PlanningUpdateKind.FAILED:
                self._end_information_wait()
                self._fail_planning_or_preserve_incumbent(outcome.failure)
                return
            # Sensor activity alone is not a planning progress permit.
            information_updated = coordinator_progressed
        pending_body_landed = (
            self._pending_goal is not None
            and self._executor is not None
            and frame.body.is_on_ground
        )
        if ((self._state is NavigationSessionState.NEEDS_INFORMATION
            or self._pending_goal is not None)
                and (information_updated or pending_body_landed)):
            if self._pending_goal is not None:
                pending = self._pending_goal
                if self._has_body_owner():
                    goal_node, goal_missing = self._surface_for_goal(
                        frame, pending.goal_state,
                    )
                    start_node, start_missing = self._surface_for_body(frame)
                    missing = tuple(sorted(
                        set(goal_missing) | set(start_missing)
                    ))
                    if (goal_node is not None
                            and start_node is not None
                            and self._goal_requests.planning_policy is
                                GoalPlanningPolicy
                                .PROVED_LOCAL_DIRECT_THEN_BACKGROUND
                            and self._ordinary_walk_can_continue_during_direct(
                                frame
                            )):
                        request = self._request
                        if type(request) is not SurfacePlanningRequest:
                            raise ContractViolation(
                                "pending ground direct requires a surface request"
                            )
                        request = replace(
                            request,
                            start=start_node,
                            goal=goal_node,
                            goal_state=pending.goal_state,
                            damage_budget=pending.damage_budget,
                        )
                        self._pending_goal = None
                        self._accept_goal_request(
                            request,
                            frame,
                            "revised_ground_direct_ready",
                            preserve_active_route=True,
                        )
                    else:
                        self._snapshot_missing = missing
                else:
                    self._resume_pending_goal(frame)
            elif self._request is not None:
                if coordinator_progressed:
                    update = self._planning_coordinator.resume_after_information(
                        frame,
                        remaining_damage_budget=self._remaining_damage_budget(),
                    )
                    self._request = update.request
                    self._snapshot_missing = ()
                    if self._retry_ledger is not None:
                        self._end_information_wait()
                    self._transition(
                        NavigationTransitionAction.BEGIN_PLANNING,
                        update.reason,
                    )
                else:
                    self._reissue_request_from_current(
                        frame, "planning_information_updated",
                        computation_invalidation=None,
                    )
    def propose(
        self,
        frame: NavigationFrame,
        state_anchor: StateAnchor | None,
        deadline_ns: int,
        *,
        input_ledger: InputApplicationLedger | None = None,
        conditioned_yaw_delta_degrees: float | None = None,
        conditioned_look_intent_id: str | None = None,
    ) -> NavigationSessionProposal:
        if type(frame) is not NavigationFrame:
            raise ContractViolation("navigation proposal requires a frame")
        if type(deadline_ns) is not int or deadline_ns <= self._clock():
            raise ContractViolation("navigation proposal deadline must be in the future")
        if ((conditioned_yaw_delta_degrees is None)
                != (conditioned_look_intent_id is None)):
            raise ContractViolation(
                "conditioned navigation requires both look identity and yaw"
            )
        if conditioned_yaw_delta_degrees is not None:
            if (type(conditioned_yaw_delta_degrees) not in (int, float)
                    or not math.isfinite(conditioned_yaw_delta_degrees)):
                raise ContractViolation("conditioned navigation yaw must be finite")
            require_identifier(
                conditioned_look_intent_id,
                "conditioned navigation look intent id",
            )
        self._route_validation = None
        self.observe(frame, frame.changed_cells)
        self._motion_result_poll_sequence += 1
        self._control_ledger = input_ledger
        self._control_anchor = state_anchor
        self._commit_applied_risk(frame, input_ledger)
        if self._executor is None:
            self._close_finished_risk(frame, None)
        self._begin_task_activity(frame, input_ledger, state_anchor)
        handoff_proposal = self._advance_existing_handoff(
            frame, state_anchor, input_ledger, deadline_ns,
        )
        if handoff_proposal is not None:
            return handoff_proposal
        if self._retain_satisfied_goal(frame, input_ledger, state_anchor):
            return self._proposal(MovementV1(), None, 1, deadline_ns, movement_required=False)
        # Classify existing information before planning. The original wait
        # entry remains where the information or action proposal consumes it;
        # a replacement behind an incumbent must not start an extra wait.
        if (self._state is NavigationSessionState.NEEDS_INFORMATION
                or not self._snapshot_missing):
            self._information.advance(
                frame, landing_acquisition_pending=self._landing_acquisition_pending(),
                edge_probe=self._edge_probe,
            )
        if (self._pending_goal is None
                and self._local_goal_request_id is not None
                and self._executor is None
                and not self._task_stop_requested()):
            self._activate_local_route(frame, state_anchor, input_ledger)
        if (self._pending_goal is None
                and self._direct_goal_request_id is not None
                and not self._task_stop_requested()):
            self._activate_ground_direct_route(
                frame, state_anchor, input_ledger,
            )
        if not self._task_stop_requested():
            self._advance_planning(frame, state_anchor, input_ledger)
        if self._active_route is None:
            return self._information_proposal(
                frame, deadline_ns, conditioned_look_intent_id)

        acquisition_proposal = self._prepare_route_action(frame, deadline_ns)
        if acquisition_proposal is not None:
            return acquisition_proposal
        advance = self._supervisor.advance_body(
            frame, input_ledger, state_anchor,
            conditioned_yaw_delta_degrees=conditioned_yaw_delta_degrees,
            allow_grounded_reprepare=(
                self._state is not NavigationSessionState.STOPPING
                and not self._task_stop_requested()
                and self._handoff.stop_request is None
                and self._active_route is not None
                and self._request is not None
                and self._active_route.source_request_id == self._request.request_id
            ),
            result_poll_sequence=self._motion_result_poll_sequence,
            current_scope=self.current_computation_scope,
        )
        validation = advance.route_validation
        if (validation is not None and validation.incumbent is not None
                and validation.incumbent.disposition
                    is ActiveRouteValidationDisposition.STOP):
            self._goal_requests.invalidate_computation(
                ComputationInvalidationCause.BASIS_INVALIDATED,
            )
            self._pending_planning_recovery = PendingPlanningRecovery(
                "active_route_dependency_changed",
                RetryCause.DEPENDENCY,
            )
            if self._edge_probe is not None and self._edge_probe.owned:
                self._request_probe_stop(StopCause.DEPENDENCY_CHANGED)
        elif (validation is not None and validation.pending is not None
                and validation.pending.disposition
                    is ActiveRouteValidationDisposition.STOP):
            self._reissue_request_from_current(
                frame, "successor_route_dependency_changed",
                computation_invalidation=(
                    ComputationInvalidationCause.BASIS_INVALIDATED
                ),
            )
        if advance.rejected_candidate is not None:
            self._reissue_request_from_current(
                frame, "successor_route_entry_changed",
                retry_trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
                retry_cause=RetryCause.EXECUTION,
                computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR,
            )
            advance = self._supervisor.continue_rejected_candidate(
                advance, frame, input_ledger, state_anchor,
                conditioned_yaw_delta_degrees=conditioned_yaw_delta_degrees,
                result_poll_sequence=self._motion_result_poll_sequence,
                current_scope=self.current_computation_scope,
            )
        progress = self._record_retry_route_progress(
            frame, advance.route_advance.decision,
        )
        self._record_task_progress(
            frame, input_ledger, state_anchor, progress,
        )
        activity_permit, newly_exhausted = self._finalize_task_activity(frame)
        if self._pending_planning_recovery is not None:
            pending_recovery = self._pending_planning_recovery
            self._pending_planning_recovery = None
            if not newly_exhausted:
                assert activity_permit is not None and self._retry_ledger is not None
                staged = self._handoff.request_recovery(
                    request_id=(
                        f"{self.session_id}/planning-recovery/"
                        f"{frame.body.sequence_id}"
                    ),
                    destination=HandoffDestination.REPLAN,
                    reason=pending_recovery.reason,
                    cause=StopCause.DEPENDENCY_CHANGED,
                    retry_cause=pending_recovery.cause,
                    budget=self._retry_ledger,
                    activity_permit=activity_permit,
                    recovery_identity=RecoveryIdentity(
                        frame.body.sequence_id,
                        pending_recovery.reason,
                    ),
                )
                if staged.limit_status is not None:
                    request = self._handoff.stop_request
                    assert request is not None
                    self._request_ending(
                        HandoffDestination.FAIL,
                        request.cause,
                        request.reason,
                        stopping_reason=request.reason,
                    )
        if (newly_exhausted
                and self._needs_same_frame_stop_protection(advance)):
            advance = self._supervisor.stop_protection(
                advance, frame, input_ledger, state_anchor,
                current_scope=self.current_computation_scope,
            )
        advance, risk_action_id = self._risk_checked_decision(
            advance,
            frame, state_anchor, input_ledger,
        )
        self._route_validation = advance.route_validation
        route_control = advance.route_advance.control
        decision = advance.route_advance.decision
        body = self._supervisor.select_body(
            advance, decision, frame, input_ledger, state_anchor,
        )
        selected_proposal = self._selected_body_proposal(
            body, frame, deadline_ns, route_control, risk_action_id, conditioned_look_intent_id,
        )
        if selected_proposal is not None:
            return selected_proposal
        routed_proposal = self._consume_route_decision(
            body.decision, frame, deadline_ns, activity_permit,
        )
        if routed_proposal is not None:
            return routed_proposal
        return self._route_proposal(
            body, frame, state_anchor, deadline_ns, risk_action_id, conditioned_look_intent_id,
        )

    def _advance_existing_handoff(
        self, frame: NavigationFrame, state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None, deadline_ns: int,
    ) -> NavigationSessionProposal | None:
        """Route already-owned probe release and stopping before new work."""
        if (self._edge_probe is not None
                and self._edge_probe.state is LandingEdgeProbeState.STOPPING):
            decision = self._supervisor.decide_probe(
                frame, input_ledger, state_anchor,
            )
            if decision.handoff.disposition is HandoffDisposition.QUIESCENT:
                self._finalize_probe_handoff(frame, decision.handoff)
            else:
                self._check_recovery_wait(frame)
                stopping_reason = (
                    "recovery_unresolved"
                    if (self._recovery_wait_capacity_exhausted or
                        self._recovery_wait_status in {
                            WaitVerdict.EXHAUSTED_TICKS,
                            WaitVerdict.EXHAUSTED_CLOCK,
                        }) else "edge_probe_stopping"
                )
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    stopping_reason,
                )
            return self._proposal(
                decision.movement, LookV1(0.0, 0.0), 1, deadline_ns,
                safety_guard=True,
            )
        if self._information.awaiting_probe_pose(frame):
            self._reason = (
                "successor_route_waiting_for_motion"
                if self._supervisor.has_pending_route else
                "edge_probe_mode_exit_pending"
            )
            return self._proposal(MovementV1(), None, 1, deadline_ns)
        if (self._state is NavigationSessionState.STOPPING
                and self._pending_goal is not None
                and not self._has_body_owner()):
            handoff = self._supervisor.evaluate_quiescence(
                frame, input_ledger, state_anchor,
            )
            if handoff.disposition is HandoffDisposition.QUIESCENT:
                self._resume_pending_goal(frame)
        if self._state in {
            NavigationSessionState.CLOSED,
            NavigationSessionState.FAILED,
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
        }:
            return self._proposal(MovementV1(), None, 1, deadline_ns)
        if (self._edge_probe is not None
                and self._edge_probe.positioning_entry):
            movement = self._probe_movement(frame)
            if not self._edge_probe.owned:
                self._transition(
                    NavigationTransitionAction.MARK_FAILED,
                    (self._edge_probe.ended_reason
                     or "landing_edge_probe_ended"),
                )
                ended_probe = self._edge_probe
                self._edge_probe = None
                self._end_probe_waits(ended_probe)
                return self._proposal(MovementV1(), None, 1, deadline_ns)
            return self._proposal(
                movement, LookV1(0.0, 0.0), 1, deadline_ns,
                safety_guard=True,
            )
        if self._edge_probe is not None and self._edge_probe.releasing:
            # Releasing the acquired view is still part of the bounded
            # acquisition.  Keep checking the same ledger deadline while the
            # camera and sneak state return to the route entry.  Previously
            # this branch bypassed ``_probe_movement`` and could wait forever
            # when a delayed look command never reached the required pose.
            release_movement = self._probe_movement(frame)
            if self._edge_probe.state is LandingEdgeProbeState.STOPPING:
                self._transition(NavigationTransitionAction.BEGIN_STOPPING, 'edge_probe_stopping')
                return self._proposal(
                    release_movement, LookV1(0.0, 0.0), 1, deadline_ns,
                    safety_guard=True,
                )
            outcome = self._supervisor.finish_probe_release(frame)
            if outcome is None:
                return self._proposal(
                    release_movement,
                    self._edge_probe.release_look(frame) or LookV1(0.0, 0.0),
                    1, deadline_ns, safety_guard=True,
                )
            resolution = self._resolve_probe_handoff(frame, outcome, None)
            if resolution is None:
                raise ContractViolation("completed probe has no current destination")
            if resolution.action is ProbeHandoffAction.CONTINUE_ROUTE:
                self._information.complete_probe(outcome)
            self._apply_probe_handoff_resolution(frame, resolution)
        return None

    def _information_proposal(
        self, frame: NavigationFrame, deadline_ns: int,
        conditioned_look_intent_id: str | None,
    ) -> NavigationSessionProposal:
        """Route information look and an already-granted probe's body guard."""
        if (self._state is NavigationSessionState.STOPPING
                and self._task_stop_requested()):
            self._finish_pending_probe_terminal("stopped_without_route")
        information_look = self._information_look(frame)
        information_movement = MovementV1()
        if conditioned_look_intent_id is not None and self._edge_probe is None:
            information_look = None
        if self._edge_probe is not None:
            information_movement = self._probe_movement(frame)
            if not self._edge_probe.owned:
                failure_reason = (
                    self._edge_probe.ended_reason
                    or "landing_edge_probe_ended"
                )
                ended_probe = self._edge_probe
                self._edge_probe = None
                self._end_probe_waits(ended_probe)
                self._transition(
                    NavigationTransitionAction.MARK_FAILED,
                    failure_reason,
                )
        if self._edge_probe is not None and self._edge_probe.owned:
            # Probe keys use the observed yaw. A SAFETY look keeps combat
            # from rotating those keys. To turn for information, hold
            # sneak for this frame and move again after the new sample.
            if (information_look is not None
                    and self._edge_probe.state is
                        LandingEdgeProbeState.HOLDING_EDGE):
                information_movement = MovementV1(sneak=True)
            else:
                information_look = None
            return self._proposal(
                information_movement,
                information_look or LookV1(0.0, 0.0),
                1, deadline_ns, safety_guard=True,
            )
        if information_movement != MovementV1():
            return self._proposal(
                information_movement,
                information_look or LookV1(0.0, 0.0),
                1, deadline_ns,
            )
        return self._proposal(
            MovementV1(), None, 1, deadline_ns,
            information_look=information_look,
        )


    def _prepare_route_action(
        self, frame: NavigationFrame, deadline_ns: int,
    ) -> NavigationSessionProposal | None:
        """Route the executor's action precondition to acquisition or stopping."""
        assert self._executor is not None
        active_route = self._active_route
        route_is_superseded = (
            active_route is not None
            and self._request is not None
            and active_route.goal_revision != self._request.goal_revision
        )
        if route_is_superseded:
            self._clear_pending_action_boundary()
        else:
            self._executor.clear_pending_action_boundary()
        precondition_action_index = (
            None if route_is_superseded
            else self._upcoming_action_precondition_index(frame)
        )
        executor_route = getattr(self._executor, "route", None)
        selected_action_index = (
            self._executor.action_index
            if precondition_action_index is None
            else precondition_action_index
        )
        current_executor_action = (
            None if executor_route is None
            or not 0 <= selected_action_index < len(executor_route.actions)
            else executor_route.actions[selected_action_index]
        )
        if (route_is_superseded
                and frame.body.is_on_ground
                and current_executor_action is not None
                and type(current_executor_action) is not WalkSegment
                and not self._executor.current_verified_action_started()):
            # A replacement request already defines the next destination.
            # Starting a new acquisition or air action for the superseded
            # route would move the body away from the replacement's anchor
            # and can make both routes invalidate each other indefinitely.
            self._supervisor.route.request_stop(StopCause.CANCELLED)
        precondition = (
            None if route_is_superseded
            else self._current_action_precondition(
                frame, through_grounded_departure=True,
                action_index=precondition_action_index,
            )
        )
        if precondition is not None:
            if (precondition.status is ActionPreconditionStatus.READY
                    and precondition_action_index is not None):
                entered = self._executor.enter_upcoming_action_boundary(
                    precondition_action_index, frame,
                )
                probe = self._edge_probe
                if (not entered and probe is not None and probe.ready
                        and self._active_route is not None
                        and probe.belongs_to_action(
                            self._active_route.route_id,
                            self._active_route.route_revision,
                            precondition_action_index,
                        )):
                    return self._proposal(
                        self._probe_movement(frame),
                        probe.release_look(frame) or LookV1(0.0, 0.0),
                        1, deadline_ns, safety_guard=True,
                    )
            if precondition.status is ActionPreconditionStatus.NEEDS_ACQUISITION:
                if self._begin_action_acquisition(precondition, frame):
                    movement = self._probe_movement(frame)
                    information_look = self._information_look(frame)
                    if (information_look is not None
                            and self._edge_probe is not None
                            and self._edge_probe.state
                                is LandingEdgeProbeState.HOLDING_EDGE):
                        movement = MovementV1(sneak=True)
                    else:
                        information_look = None
                    return self._proposal(
                        movement,
                        information_look or LookV1(0.0, 0.0),
                        1,
                        deadline_ns,
                        safety_guard=True,
                    )
                return self._proposal(
                    MovementV1(sneak=True), LookV1(0.0, 0.0), 1,
                    deadline_ns, safety_guard=True,
                )
            if precondition.status is ActionPreconditionStatus.NEEDS_INFORMATION:
                hold = (None if current_executor_action is None else
                        action_spec(current_executor_action).stop_hold.information_movement)
                guard = frame.body.is_on_ground and hold is not None
                self._transition(NavigationTransitionAction.WAIT_FOR_INFORMATION, precondition.reason)
                self._snapshot_missing = precondition.missing_cells
                return self._proposal(
                    hold if guard else MovementV1(),
                    None, 1, deadline_ns,
                    information_look=self._information_look(frame),
                    safety_guard=guard,
                )
            if precondition.status is ActionPreconditionStatus.REJECTED:
                if (self._edge_probe is not None
                        and self._edge_probe.owned
                        and self._request_probe_stop(
                            StopCause.DEPENDENCY_CHANGED,
                            terminal=NavigationSessionState.FAILED,
                            terminal_reason=precondition.reason.value,
                        )):
                    return self._proposal(
                        self._probe_movement(frame),
                        LookV1(0.0, 0.0),
                        1,
                        deadline_ns,
                        safety_guard=True,
                    )
                self._risk_failure_reason = precondition.reason.value
                self._request_ending(HandoffDestination.FAIL, StopCause.CANCELLED,
                                     precondition.reason.value)
                return self._proposal(MovementV1(), None, 1, deadline_ns)
        return None

    def _selected_body_proposal(
        self, body: BodyFrameResult, frame: NavigationFrame, deadline_ns: int,
        route_control: RouteControl, risk_action_id: str | None,
        conditioned_look_intent_id: str | None,
    ) -> NavigationSessionProposal | None:
        """Preserve the supervisor's probe, candidate-wait or incumbent selection."""
        conditioned_ordinary_walk = body.route_advance.conditioned_ordinary_walk
        if body.kind is BodySelectionKind.PROBE_STOP:
            successor_decision = (body.decision if body.waiting_candidate is None
                                  else body.waiting_candidate.decision)
            self._last_decision = successor_decision
            self._close_finished_risk(frame, successor_decision)
            if self._edge_probe.state is not LandingEdgeProbeState.STOPPING:
                self._request_probe_stop(StopCause.ROUTE_REPLACED)
            return self._proposal(
                self._probe_movement(frame), LookV1(0.0, 0.0),
                1, deadline_ns, safety_guard=True, retain_body_input=True,
            )
        if body.kind is BodySelectionKind.CANDIDATE_WAIT:
            successor_decision = body.waiting_candidate.decision
            self._last_decision = successor_decision
            self._close_finished_risk(frame, successor_decision)
            self._transition(
                NavigationTransitionAction.BEGIN_STOPPING,
                "successor_route_waiting_for_motion",
            )
            return self._proposal(
                MovementV1(), successor_decision.look or LookV1(0.0, 0.0),
                successor_decision.input_lease_ticks, deadline_ns,
                safety_guard=True, retain_body_input=True,
            )
        if body.kind is BodySelectionKind.INCUMBENT_PREFIX:
            self._last_decision = body.decision
            self._close_finished_risk(frame, body.decision)
            if self._risk_failure_reason is None:
                self._continue_execution("executing_safe_prefix_during_handoff")
            return self._proposal(
                body.decision.movement if body.decision.submit_input else MovementV1(),
                body.decision.look, body.decision.input_lease_ticks, deadline_ns,
                route_decision=body.decision, route_control=route_control,
                conditioned_look_intent_id=(conditioned_look_intent_id
                    if conditioned_ordinary_walk else None),
                risk_action_id=risk_action_id,
            )
        return None

    def _consume_route_decision(
        self, decision: ActionRouteDecision, frame: NavigationFrame, deadline_ns: int,
        activity_permit: RecoveryActivityPermit | None,
    ) -> NavigationSessionProposal | None:
        """Route body facts to the existing lifecycle and shared recovery entries."""
        self._last_decision = decision
        self._close_finished_risk(frame, decision)
        self._record_completed_movement_damage()
        active_request_id = self._active_route.source_request_id
        current_request_id = None if self._request is None else self._request.request_id
        terminal_decisions = {
            ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
            ActionRouteState.FAILED, ActionRouteState.BLOCKED,
            ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
            ActionRouteState.NEEDS_REPLAN,
        }
        if (self._pending_retry is not None
                and decision.state in terminal_decisions):
            self._resolve_pending_retry(frame)
        elif (self._state is NavigationSessionState.STOPPING
              and self._task_stop_requested()):
            if decision.state in terminal_decisions:
                if self._clear_active_execution():
                    self._finish_pending_probe_terminal(decision.reason_code)
                    self._risk_failure_reason = None
                else:
                    self._reason = "route_release_waiting_for_evidence"
            else:
                self._transition(NavigationTransitionAction.BEGIN_STOPPING, decision.reason_code)
        elif decision.state is ActionRouteState.INPUT_LOST:
            self._handoff.request_recovery(
                request_id=f"{self.session_id}/end", destination=HandoffDestination.FAIL,
                reason=decision.reason_code, cause=StopCause.INPUT_LOST, budget=self._retry_ledger,
            )
            if self._clear_active_execution():
                self._finish_pending_probe_terminal(decision.reason_code)
                self._snapshot_missing = decision.missing_cells
            else:
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    "route_release_waiting_for_evidence",
                )
        elif (self._edge_probe is not None and self._edge_probe.owned
                and decision.state in {
                    ActionRouteState.FAILED,
                    ActionRouteState.BLOCKED,
                    ActionRouteState.UNSUPPORTED,
                }):
            # An acquisition owner and its suspended route are one body
            # responsibility.  If solving the acquired action fails, stop the
            # probe before any generic route-restart path runs; otherwise the
            # route can disappear while the probe waits forever in READY.
            if not self._request_probe_stop(
                    StopCause.MOTION_UNSOLVABLE,
                    terminal=NavigationSessionState.FAILED,
                    terminal_reason=decision.reason_code):
                self._apply_decision_state(decision)
        elif (self._handoff.stop_request is not None
                and self._handoff.stop_request.destination is HandoffDestination.REPLAN
                and decision.state in terminal_decisions):
            self._resolve_pending_retry(frame)
        elif active_request_id != current_request_id:
            if decision.state in terminal_decisions:
                if self._clear_active_execution():
                    if self._pending_goal is not None:
                        self._resume_pending_goal(frame)
                    else:
                        # The replacement candidate may have been consumed
                        # while the incumbent still owned the body. Re-anchor
                        # from the observed release position.
                        self._reissue_request_from_current(
                            frame, "replacement_route_reanchored",
                            computation_invalidation=(
                                ComputationInvalidationCause.NEW_STATE_ANCHOR
                            ),
                        )
                else:
                    if (self._edge_probe is not None
                            and self._edge_probe.owned
                            and self._request_probe_stop(
                                StopCause.MOTION_UNSOLVABLE,
                            )):
                        return self._proposal(
                            self._probe_movement(frame),
                            LookV1(0.0, 0.0),
                            1,
                            deadline_ns,
                            safety_guard=True,
                        )
                    self._transition(
                        NavigationTransitionAction.BEGIN_STOPPING,
                        "route_release_waiting_for_evidence",
                    )
            else:
                self._continue_execution(
                    "executing_safe_prefix_during_replan",
                )
                if self._pending_goal is None:
                    self._snapshot_missing = decision.missing_cells
        elif (self._interaction_approach_pending
                and decision.state is ActionRouteState.COMPLETE):
            if self._clear_active_execution():
                self._interaction_approach_pending = False
                self._transition(
                    NavigationTransitionAction.REQUIRE_INTERACTION,
                    "interaction_work_position_reached",
                )
            else:
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    "route_release_waiting_for_evidence",
                )
        elif decision.state is ActionRouteState.NEEDS_REPLAN:
            assert self._active_route is not None and self._retry_ledger is not None
            attempt_id = (
                None if self._coordinator is None else
                self._coordinator.last_failure_attempt_id
            ) or (
                f"{self._active_route.source_request_id}:"
                f"{self._active_route.route_id}:"
                f"{decision.action_index}:{decision.reason_code}"
            )
            recovery = self._handoff.request_recovery(
                request_id=attempt_id, destination=HandoffDestination.REPLAN,
                reason=decision.reason_code, budget=self._retry_ledger,
                missing_cells=decision.missing_cells,
                activity_permit=activity_permit,
                recovery_identity=RecoveryIdentity(
                    frame.body.sequence_id, attempt_id,
                ),
            )
            if recovery.limit_status is not None:
                request = self._handoff.stop_request
                assert request is not None
                self._request_ending(
                    HandoffDestination.FAIL, request.cause, request.reason,
                    stopping_reason=request.reason,
                )
                if self._clear_active_execution():
                    self._finish_pending_probe_terminal(request.reason)
            elif self._pending_retry is None:
                self._transition(
                    NavigationTransitionAction.MARK_FAILED,
                    "replan_retry_event_already_consumed",
                )
            else:
                self._resolve_pending_retry(frame)
        elif (self._state is NavigationSessionState.STOPPING
              and self._edge_probe is not None
              and self._edge_probe.ready
              and decision.submit_input
              and decision.movement != MovementV1()):
            self._reason = "route_handoff_waiting_for_selected_command"
        else:
            self._apply_decision_state(decision)
        return None

    def _route_proposal(
        self, body: BodyFrameResult, frame: NavigationFrame, state_anchor: StateAnchor | None,
        deadline_ns: int, risk_action_id: str | None, conditioned_look_intent_id: str | None,
    ) -> NavigationSessionProposal:
        """Assemble a body decision with the existing bounded recovery guard."""
        decision = body.decision
        conditioned_ordinary_walk = body.route_advance.conditioned_ordinary_walk
        if (self._state is NavigationSessionState.STOPPING
                and (self._task_stop_requested()
                     or (self._handoff.stop_request is not None
                         and self._handoff.stop_request.planning_permit_kind
                             is not PlanningAttemptPermitKind.RETRY))
                and self._executor is not None
                and self._edge_probe is None):
            self._check_recovery_wait(frame)
            if (self._recovery_wait_capacity_exhausted
                    or self._recovery_wait_status in {
                        WaitVerdict.EXHAUSTED_TICKS,
                        WaitVerdict.EXHAUSTED_CLOCK,
                    }):
                self._reason = "recovery_unresolved"
        if (self._close_requested and self._executor is None
                and self._edge_probe is None):
            self._finalize_close()
        movement = decision.movement if decision.submit_input else MovementV1()
        recovery_override = False
        if (self._state is NavigationSessionState.STOPPING
                and self._executor is not None
                and (self._recovery_wait_capacity_exhausted
                     or self._recovery_wait_status in {
                         WaitVerdict.EXHAUSTED_TICKS,
                         WaitVerdict.EXHAUSTED_CLOCK,
                     })):
            recovery_movement = verified_ground_recovery_movement(
                frame,
                None if state_anchor is None else state_anchor.physics_state,
            )
            if recovery_movement is not None:
                movement = recovery_movement
                recovery_override = True
        if self._edge_probe is not None:
            if (decision.submit_input
                    and decision.verified_command_index is not None):
                # The route proposal has not won Runtime arbitration yet.
                # Keep acquisition ownership until adopt_result registers the
                # selected command and its actual control sequence.
                pass
            elif self._edge_probe.active:
                movement = MovementV1(sneak=True)
        return self._proposal(
            movement,
            (decision.look or LookV1(0.0, 0.0))
            if self._edge_probe is not None else decision.look,
            decision.input_lease_ticks,
            deadline_ns,
            route_decision=None if recovery_override else decision,
            conditioned_look_intent_id=(
                conditioned_look_intent_id
                if conditioned_ordinary_walk else None
            ),
            safety_guard=self._edge_probe is not None or recovery_override,
            risk_action_id=risk_action_id,
            retain_body_input=(
                self._state is NavigationSessionState.STOPPING
                and self._executor is not None
            ),
        )

    def register_verified_submission(
        self,
        proposal: NavigationSessionProposal,
        *,
        control_sequence: int,
        actual_movement: MovementV1 | None = None,
    ) -> None:
        decision = proposal.route_decision
        route_control = (None if proposal.route_owner_id is None else
                         self._supervisor.control_by_id(proposal.route_owner_id))
        if (decision is None or route_control is None):
            return
        if (actual_movement is not None
                and actual_movement != decision.movement):
            raise ContractViolation("selected route movement differs from proposal")
        if (decision.verified_command_index is not None
                and decision.expected_movement_tick is not None):
            route_control.executor.register_verified_submission(
                decision.verified_command_index,
                control_sequence=control_sequence,
                requested_movement_tick=decision.expected_movement_tick,
                requested_latest_movement_tick=decision.latest_movement_tick,
            )
        if proposal.risk_action_id is not None:
            assert self._risk_ledger is not None
            status = self._risk_ledger.mark_submitted(
                proposal.risk_action_id, control_sequence,
            )
            if status is RiskSubmissionStatus.CAPACITY_EXHAUSTED:
                self._risk_submission_capacity_exhausted = True
                self._risk_failure_reason = "risk_submission_capacity_exhausted"
                self._request_ending(HandoffDestination.FAIL, StopCause.CANCELLED,
                                     self._risk_failure_reason)
        if (self._supervisor.has_pending_route
                and self._supervisor.route is route_control
                and self._frame is not None
                and decision.submit_input
                and decision.movement != MovementV1()):
            handoff = self._supervisor.adopt_route_selection(
                self._frame, selected=True,
                control_sequence=control_sequence,
                movement=decision.movement,
            )
            if (handoff is not None
                    and self._state is NavigationSessionState.STOPPING):
                self._transition(
                    NavigationTransitionAction.RESUME_EXECUTION_AFTER_HANDOFF,
                    "selected_successor_route_command_received_handoff",
                    handoff=handoff,
                )
        if (self._edge_probe is not None and self._edge_probe.ready
                and self._frame is not None
                and proposal.report.route_id == route_control.route.route_id
                and proposal.report.goal_revision
                    == route_control.route.goal_revision
                and decision.submit_input
                and decision.movement != MovementV1()
                and self._edge_probe.belongs_to_action(
                    route_control.route.route_id,
                    route_control.route.route_revision,
                    decision.action_index,
                )):
            transferred_probe = self._edge_probe
            handoff = self._supervisor.transfer_probe_to_route(
                self._frame, route_id=route_control.route.route_id,
                route_revision=route_control.route.route_revision,
                action_index=decision.action_index,
                movement=decision.movement,
                control_sequence=control_sequence,
                movement_tick_id=None,
            )
            self._end_probe_waits(transferred_probe)
            if self._state is NavigationSessionState.STOPPING:
                self._transition(
                    NavigationTransitionAction.RESUME_EXECUTION_AFTER_HANDOFF,
                    "selected_route_command_received_probe_handoff",
                    handoff=handoff,
                )

    def reject_unselected_route_proposal(
        self, proposal: NavigationSessionProposal,
        frame: NavigationFrame,
    ) -> None:
        if type(frame) is not NavigationFrame:
            raise ContractViolation("route arbitration requires a current frame")
        self._release_unselected_risk(proposal)
        pending = self._supervisor.route
        if (not self._supervisor.has_pending_route
                or pending is None or proposal.route_decision is None
                or not proposal.route_decision.submit_input
                or proposal.route_owner_id != pending.route.route_id):
            return
        self._supervisor.discard_pending_route()
        self._reissue_request_from_current(
            frame,
            "route_handoff_not_selected",
            computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR,
        )

    def discard_prepared_proposal(
        self, proposal: NavigationSessionProposal,
        frame: NavigationFrame,
    ) -> None:
        """A parent discarded this frame before Runtime could arbitrate it."""
        if type(frame) is not NavigationFrame:
            raise ContractViolation("discarded navigation frame is invalid")
        self._release_unselected_risk(proposal)
        pending = self._supervisor.route
        if (self._supervisor.has_pending_route and pending is not None
                and proposal.route_owner_id == pending.route.route_id):
            self._supervisor.discard_pending_route()
            self._reissue_request_from_current(
                frame,
                "route_handoff_proposal_discarded",
                computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR,
            )

    def cancel(self, reason: str) -> None:
        if type(reason) is not str or not reason.strip():
            raise ContractViolation("navigation cancellation reason is required")
        if self._closed:
            raise ContractViolation("navigation session is closed")
        self._admit_command_event(NavigationSessionEvent.CANCEL)
        if self._request is not None or self._pending_goal is not None:
            self._goal_requests.invalidate_computation(ComputationInvalidationCause.CANCELLED)
        self._request_ending(HandoffDestination.CANCEL, StopCause.CANCELLED,
                             reason.strip(), stopping_reason="cancellation_requested")

    def close(self) -> None:
        if self._closed:
            return
        self._admit_command_event(NavigationSessionEvent.CLOSE)
        self._close_requested = True
        if self._state in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            # Task outcome and resource lifetime are separate.  Once the
            # task has a terminal outcome, closing workers must not rewrite
            # that outcome or re-enter body-control states.
            self._finalize_close()
            return
        self._request_ending(HandoffDestination.CLOSE, StopCause.CLOSED, "closed",
                             stopping_reason="closing_after_body_control")

    def _validate_successor_task_id(self, task_id: str) -> None:
        if (self._declared_successor_task_id is not None
                and task_id != self._declared_successor_task_id):
            raise ContractViolation("navigation successor start changed declared task identity")

    def spawn_successor(
        self, session_id: str, *, task_id: str,
    ) -> "NavigationSession":
        """Move reusable workers into a lifecycle declared for a new task."""
        require_identifier(session_id, "successor navigation session id")
        require_identifier(task_id, "successor navigation task id")
        original_task_id = (
            self._retry_ledger.task_id if self._retry_ledger is not None
            else self._risk_ledger.task_id if self._risk_ledger is not None
            else self._declared_successor_task_id
        )
        if task_id == original_task_id:
            raise ContractViolation("navigation successor requires a different task identity")
        if self._closed:
            raise ContractViolation("closed navigation session has no successor")
        if not self.report.terminal or self._source is not None:
            raise ContractViolation(
                "navigation successor requires a released terminal session"
            )
        successor = NavigationSession(
            session_id,
            self.profiles,
            planner_worker=self._planner,
            owns_planner_worker=self._owns_planner_worker,
            motion_worker=self._motion_worker,
            observation_adapter=self._adapter,
            route_admitter=self._admitter,
            clock_ns=self._clock,
            snapshot_cells_per_step=self._snapshot_cells_per_step,
            planning_margin_cells=self._planning_margin,
            bridge_policy=self._bridge_policy,
        )
        successor._declared_successor_task_id = task_id
        successor._owns_motion_worker = self._owns_motion_worker
        self._owns_planner_worker = False
        self._owns_motion_worker = False
        self._finalize_close()
        return successor

    def same_task_continuation_evidence(
        self,
    ) -> SameTaskContinuationEvidence | None:
        """Expose a continuation token only after every task owner released."""
        ledger = self._retry_ledger
        risk = self._risk_ledger
        if (not self.report.terminal or self._closed or self._source is not None
                or ledger is None or risk is None
                or not ledger.can_resume_same_task_continuation()
                or ledger.active_recovery_id is not None
                or ledger.active_waits()
                or risk.held_points > 1.0e-9
                or any(
                    action.state is not RiskActionState.SETTLED
                    for action in risk.snapshot_actions()
                )
                or self._supervisor.route is not None
                or self._edge_probe is not None):
            return None
        return SameTaskContinuationEvidence(
            self.session_id,
            ledger.task_id,
            self._goal_requests.reach_policy,
            self._goal_requests.planning_policy,
            ledger,
            risk,
            self._task_damage_budget,
            self._movement_damage_spent_points,
            self.bridge_remaining,
        )

    def rebuild_same_task(
        self,
        session_id: str,
        evidence: SameTaskContinuationEvidence,
    ) -> "NavigationSession":
        """Move one still-live task into a fresh Session shell."""
        require_identifier(session_id, "same-task navigation session id")
        if type(evidence) is not SameTaskContinuationEvidence:
            raise ContractViolation("same-task rebuild requires typed evidence")
        current = self.same_task_continuation_evidence()
        if current is None or evidence != current:
            raise ContractViolation(
                "same-task rebuild evidence is absent, stale, or foreign"
            )
        if not evidence.retry_ledger.resume_same_task_continuation():
            raise ContractViolation("same-task recovery budget cannot be reopened")
        continuation = NavigationSession(
            session_id,
            self.profiles,
            planner_worker=self._planner,
            owns_planner_worker=self._owns_planner_worker,
            motion_worker=self._motion_worker,
            observation_adapter=self._adapter,
            route_admitter=self._admitter,
            clock_ns=self._clock,
            snapshot_cells_per_step=self._snapshot_cells_per_step,
            planning_margin_cells=self._planning_margin,
            bridge_policy=self._bridge_policy,
            retry_ledger=evidence.retry_ledger,
            risk_ledger=evidence.risk_ledger,
        )
        continuation._goal_requests.select_reach_policy(evidence.reach_policy)
        continuation._goal_requests.select_planning_policy(
            evidence.planning_policy,
        )
        continuation._task_damage_budget = evidence.damage_budget
        continuation._movement_damage_spent_points = float(
            evidence.movement_damage_spent_points
        )
        continuation._bridge_remaining = evidence.bridge_remaining
        continuation._owns_motion_worker = self._owns_motion_worker
        self._owns_planner_worker = False
        self._owns_motion_worker = False
        self._finalize_close()
        continuation._goal_requests.resume_computation_after_reanchor(self._goal_requests)
        return continuation

    def _finalize_close(self) -> None:
        if self._closed:
            return
        self._end_session_waits()
        self._supervisor.request_route_stop(StopCause.CLOSED)
        if self._planning_coordinator is not None:
            self._planning_coordinator.cancel_work("session_closed")
        self._closed = True
        if self._owns_planner_worker and hasattr(self._planner, "close"):
            self._planner.close()
        if self._motion_worker is not None and self._owns_motion_worker:
            self._motion_worker.close()
        # COMPLETE/CANCELLED/FAILED describe the task result and remain
        # immutable after they are reached.  ``_closed`` separately records
        # that workers and input resources have been released.
        if self._state not in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
        }:
            self._transition(NavigationTransitionAction.MARK_CLOSED, 'closed')

    def _replace_request(
        self,
        request: PlanningRequest | SurfacePlanningRequest,
        frame: NavigationFrame,
        reason: str,
        *,
        preserve_active_route: bool = False,
        planning_permit: PlanningAttemptPermit | None = None,
    ) -> None:
        if (type(request) is SurfacePlanningRequest
                and surface_search_need(request)
                    is SurfaceSearchNeed.SAME_SUPPORT_LOCAL_GOAL):
            self._accept_goal_request(
                request, frame, reason,
                preserve_active_route=preserve_active_route,
                planning_permit=planning_permit,
            )
            return
        self._local_goal_request_id = None
        self._direct_goal_request_id = None
        self._direct_planning_permit = None
        self._local_input_floor = None
        # Replacing a planning request never revokes an active body's owner.
        # The admitted-route handoff below decides when that owner is safe to
        # replace.  Callers may still state the preservation intent explicitly
        # to make the reason visible, but an existing executor is authoritative.
        preserve_active_route = preserve_active_route or self._executor is not None
        if (self._edge_probe is not None
                and not self._edge_probe.belongs_to(
                    request.goal_id, request.goal_revision,
                )):
            self._request_probe_stop(StopCause.ROUTE_REPLACED)
        if not preserve_active_route:
            self._retire_route()
        self._request = request
        self._frame = frame
        self._snapshot_missing = ()
        self._information.select_missing(())
        self._required_interaction = None
        self._interaction_approach_pending = False
        coordinator = self._ensure_planning_coordinator()
        coordinator.begin(
            request,
            frame,
            permit=(planning_permit if planning_permit is not None else PlanningAttemptPermit(
                f"{request.request_id}/task-update",
                self._retry_ledger.task_id,
                request.goal_revision,
                f"request/{request.request_id}",
                PlanningAttemptPermitKind.TASK_UPDATE,
            )),
            state_anchor=None,
            remaining_damage_budget=self._remaining_damage_budget(),
        )
        if self._state is NavigationSessionState.STOPPING:
            handoff = self._supervisor.last_handoff
            if (handoff is not None
                    and handoff.disposition is HandoffDisposition.QUIESCENT
                    and handoff.world_session == frame.session
                    and handoff.observation_sequence_id
                        == frame.body.sequence_id):
                self._transition(
                    NavigationTransitionAction.REPLAN_AFTER_HANDOFF,
                    reason,
                    handoff=handoff,
                )
            else:
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    "replacement_request_waits_for_handoff_evidence",
                )
        else:
            self._transition(
                NavigationTransitionAction.BEGIN_PLANNING,
                reason,
            )

    def _fail_planning_or_preserve_incumbent(self, failure: PlanningFailure) -> None:
        """Route the planner's retired work to the sole owner of exit destinations."""
        if (self._request is None or self.report.terminal
                or self._planning_coordinator is None):
            return
        if not self._handoff.stage_planning_failure(
                failure, self._request, planning=self._planning_coordinator):
            return
        self._supervisor.discard_pending_route()
        self._supervisor.request_route_stop(StopCause.MOTION_UNSOLVABLE)
        if self._request_probe_stop(StopCause.MOTION_UNSOLVABLE):
            return
        if self._has_body_owner():
            self._transition(NavigationTransitionAction.BEGIN_STOPPING,
                             "replacement_failure_waits_for_body_release")
        else:
            self._finish_pending_probe_terminal(failure.reason)

    def _accept_goal_request(
        self, request: SurfacePlanningRequest, frame: NavigationFrame,
        reason: str, *, preserve_active_route: bool = False,
        planning_permit: PlanningAttemptPermit | None = None,
    ) -> None:
        search_need = surface_search_need(request)
        if search_need is SurfaceSearchNeed.GRAPH_SEARCH:
            if self._can_attempt_ground_direct_request(request, frame):
                request = replace(request, work_identity=None)
                self._request = request
                self._frame = frame
                self._pending_goal = None
                self._local_goal_request_id = None
                self._direct_goal_request_id = request.request_id
                self._direct_planning_permit = planning_permit
                if self._planning_coordinator is not None:
                    self._planning_coordinator.cancel_work(
                        "ground_direct_precedes_background_planning",
                    )
                self._snapshot_missing = ()
                self._information.select_missing(())
                self._required_interaction = None
                self._interaction_approach_pending = False
                self._continue_execution("ground_direct_pending")
                return
            self._replace_request(
                request, frame, reason,
                preserve_active_route=preserve_active_route,
                planning_permit=planning_permit,
            )
            return
        # The support graph has no movement edge to execute here.  Keep any
        # previous owner alive until its cancellation reaches a safe terminal.
        request = replace(request, work_identity=None)
        self._request = request
        self._frame = frame
        self._pending_goal = None
        self._local_goal_request_id = request.request_id
        self._direct_goal_request_id = None
        self._direct_planning_permit = None
        self._local_input_floor = None
        self._local_mode_wait_frames = 0
        if self._planning_coordinator is not None:
            self._planning_coordinator.cancel_work()
        self._snapshot_missing = ()
        self._required_interaction = None
        self._interaction_approach_pending = False
        self._continue_execution('same_support_local_goal')
        if self._executor is not None:
            self._supervisor.route.request_stop(StopCause.CANCELLED)

    def _can_attempt_ground_direct_request(
        self,
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
    ) -> bool:
        goal = request.goal_state
        return (
            self._goal_requests.planning_policy is
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            and surface_search_need(request) is SurfaceSearchNeed.GRAPH_SEARCH
            and request.start.vertical_band == request.goal.vertical_band
            and goal is not None
            and MovementMode.WALK in goal.allowed_modes
            and "standing" in goal.allowed_poses
            and self._ordinary_walk_can_continue_during_direct(frame)
        )

    def _can_attempt_local_direct_request(
        self,
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
    ) -> bool:
        goal = request.goal_state
        return (
            self._goal_requests.planning_policy is
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            and surface_search_need(request)
                is SurfaceSearchNeed.SAME_SUPPORT_LOCAL_GOAL
            and goal is not None
            and MovementMode.WALK in goal.allowed_modes
            and "standing" in goal.allowed_poses
            and self._ordinary_walk_can_continue_during_direct(frame)
        )

    def _ordinary_walk_can_continue_during_direct(
        self, frame: NavigationFrame,
    ) -> bool:
        if (not frame.body.is_on_ground
                or frame.body.pose != "standing"
                or observed_ground_mode(frame.body) is not MovementMode.WALK
                or self._current_risk_action_has_started()):
            return False
        incumbent = self._supervisor.incumbent_route
        if incumbent is None:
            return True
        action_index = incumbent.action_index
        actions = incumbent.route.action_route.actions
        if not 0 <= action_index < len(actions):
            return False
        action = actions[action_index]
        return (
            type(action) is WalkSegment
            and action.traversal_plan is None
            and not incumbent.requires_safe_handoff(frame)
        )

    def _activate_ground_direct_route(
        self, frame: NavigationFrame, state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None,
    ) -> None:
        request = self._request
        if (type(request) is not SurfacePlanningRequest
                or self._direct_goal_request_id != request.request_id):
            return
        admission = self._admitter.admit_ground_direct(
            request,
            frame,
            ground_profile=self.profiles.ground,
            capability_identity=GroundCapabilityIdentity.from_profile(
                self.profiles.ground,
            ),
        )
        if admission.status is AdmissionStatus.NEEDS_INFORMATION:
            self._snapshot_missing = admission.missing_cells
            if self._supervisor.incumbent_route is None:
                self._transition(
                    NavigationTransitionAction.WAIT_FOR_INFORMATION,
                    "ground_direct_requires_information",
                )
            else:
                self._continue_execution(
                    "incumbent_continues_during_ground_direct_information",
                )
            return
        if admission.status is AdmissionStatus.NOT_APPLICABLE:
            permit = self._direct_planning_permit
            self._replace_request(
                request,
                frame,
                "ground_direct_not_applicable",
                preserve_active_route=(
                    self._supervisor.incumbent_route is not None
                ),
                planning_permit=permit,
            )
            return
        if admission.status is AdmissionStatus.REJECTED:
            self._direct_goal_request_id = None
            self._direct_planning_permit = None
            self._request_ending(
                HandoffDestination.FAIL,
                StopCause.CANCELLED,
                admission.reason.value,
            )
            return
        assert admission.route is not None
        self._activate_planning_route(
            admission.route,
            frame,
            state_anchor,
            input_ledger,
            planning_result=False,
            preserve_incumbent_on_offer_failure=True,
        )
        control = self._supervisor.route
        if control is not None and control.route is admission.route:
            self._direct_goal_request_id = None
            self._direct_planning_permit = None

    def _activate_local_route(
        self, frame: NavigationFrame, state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None,
    ) -> None:
        request = self._request
        assert type(request) is SurfacePlanningRequest
        assert request.goal_state is not None
        goal = request.goal_state
        observed = evaluate_observed_goal(
            frame, goal, self._task_damage_budget.risk_policy_id,
        )
        if observed.status is ObservedGoalStatus.NEEDS_INFORMATION:
            self._transition(
                NavigationTransitionAction.WAIT_FOR_INFORMATION,
                "goal_support_requires_information",
            )
            self._snapshot_missing = observed.missing_cells
            return
        if observed.status is ObservedGoalStatus.RESOURCE_UNOBSERVABLE:
            self._transition(NavigationTransitionAction.MARK_FAILED, 'goal_resource_unobservable')
            return
        if self._local_input_floor is None:
            self._local_input_floor = max(
                (record.control_sequence for record in input_ledger.snapshot()),
                default=0,
            ) if input_ledger is not None else 0
        responsibility = assess_input_responsibility(
            input_ledger, state_anchor,
            previous_sequence_floor=self._local_input_floor,
        ).disposition
        if responsibility is InputResponsibilityDisposition.AMBIGUOUS_WAITING:
            self._transition(NavigationTransitionAction.MARK_FAILED, 'previous_input_application_ambiguous')
            return
        if responsibility is InputResponsibilityDisposition.IN_FLIGHT:
            self._continue_execution('waiting_for_previous_input')
            return
        if observed.status is ObservedGoalStatus.SATISFIED:
            if self._goal_requests.reach_policy is GoalReachPolicy.KEEP_ACTIVE_ON_REACH:
                self._continue_execution("goal_state_settling")
            else:
                self._transition(NavigationTransitionAction.MARK_COMPLETE, 'goal_state_satisfied')
            return
        if (MovementMode.WALK not in goal.allowed_modes
                or "standing" not in goal.allowed_poses):
            self._transition(NavigationTransitionAction.MARK_FAILED, 'same_support_local_mode_unsupported')
            return
        if (frame.body.is_on_ground
                and (frame.body.is_sneaking or frame.body.pose == "crouching")):
            self._local_mode_wait_frames += 1
            if self._local_mode_wait_frames > 8:
                self._transition(NavigationTransitionAction.MARK_FAILED, 'same_support_mode_exit_timeout')
            else:
                self._continue_execution(
                    "same_support_mode_exit_pending",
                )
            return
        self._local_mode_wait_frames = 0
        admission = self._admitter.admit_local_direct(
            request,
            frame,
            ground_profile=self.profiles.ground,
            capability_identity=GroundCapabilityIdentity.from_profile(
                self.profiles.ground
            ),
        )
        self._last_local_direct_admission = admission.local_direct_evidence
        if (admission.status is AdmissionStatus.REJECTED
                and admission.reason
                    is AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION):
            self._transition(
                NavigationTransitionAction.WAIT_FOR_INFORMATION,
                "same_support_local_requires_information",
            )
            self._snapshot_missing = admission.missing_cells
            return
        if (admission.status is AdmissionStatus.REJECTED
                and admission.reason
                    is AdmissionReason.CANDIDATE_HAS_NO_ACTIONS):
            self._continue_execution("same_support_local_settling")
            self._snapshot_missing = ()
            return
        if admission.status is AdmissionStatus.REJECTED:
            reason = (
                "same_support_local_mode_unsupported"
                if admission.reason
                    is AdmissionReason.ROUTE_CAPABILITIES_CHANGED
                else "same_support_local_path_unavailable"
            )
            self._transition(NavigationTransitionAction.MARK_FAILED, reason)
            return
        active_route = admission.route
        assert active_route is not None
        action_route = active_route.action_route
        executor = ActionRouteExecutor(
            self.profiles.ground, self.profiles.jump_up, self.profiles.step,
            self.profiles.ground_modes, self.profiles.air,
            gap_solver_policy=self.profiles.gap_solver,
        )
        executor.start(
            action_route, frame, damage_budget=self._remaining_damage_budget(),
        )
        if not self._supervisor.offer_route(
            RouteControl(active_route, executor), frame,
            input_ledger, state_anchor,
        ):
            raise ContractViolation("local route cannot replace active body owner")
        if self._planning_coordinator is not None:
            self._planning_coordinator.confirm_nonplanner_route_admitted(
                active_route,
                current_scope=self.current_computation_scope,
            )
        self._continue_execution('same_support_local_route_started')
        self._snapshot_missing = ()

    def _clear_active_execution(self) -> bool:
        if self._frame is None and self._supervisor.route is not None:
            return False
        if (self._frame is not None and not self._supervisor.retire_route(
                self._frame, self._control_ledger, self._control_anchor,
        )):
            return False
        self._end_session_recovery_wait()
        self._last_decision = None
        self._executor_reported_damage_points = 0.0
        return True

    def _finalize_probe_handoff(
        self, frame: NavigationFrame, handoff: HandoffEvidence,
    ) -> None:
        outcome = self._supervisor.probe_outcome(frame)
        resolution = self._resolve_probe_handoff(frame, outcome, handoff)
        if resolution is None:
            raise ContractViolation("quiescent probe has no current destination")
        retired_probe = self._edge_probe
        self._supervisor.retire_quiescent_probe(frame)
        self._end_probe_waits(retired_probe)
        self._recovery_wait_status = None
        if resolution.action in {ProbeHandoffAction.WAIT_FOR_ROUTE_INPUT,
                                 ProbeHandoffAction.REPLAN}:
            self._information.complete_probe(outcome)
        self._apply_probe_handoff_resolution(frame, resolution)

    def _resolve_probe_handoff(
        self, frame: NavigationFrame, outcome: ProbeOutcome | None,
        handoff: HandoffEvidence | None,
    ) -> ProbeHandoffResolution | None:
        control = self._supervisor.route
        action_index = None if control is None else control.action_index
        upcoming_index = self._upcoming_action_precondition_index(frame)
        if upcoming_index is not None:
            action_index = upcoming_index
        return self._handoff.resolve_probe(
            frame, outcome=outcome, handoff=handoff,
            route=None if control is None else control.route,
            action_index=action_index,
            current_request_id=None if self._request is None else self._request.request_id,
            pending_route_id=(None if not self._supervisor.has_pending_route
                              else self._supervisor.route.route.route_id),
        )

    def _apply_probe_handoff_resolution(
        self, frame: NavigationFrame, resolution: ProbeHandoffResolution,
    ) -> None:
        """Route one owner's typed decision to the existing lifecycle actions."""
        match resolution.action:
            case ProbeHandoffAction.FINISH_ENDING:
                self._supervisor.discard_pending_route()
                self._finish_pending_probe_terminal(resolution.reason)
            case ProbeHandoffAction.STOP_SUSPENDED_ROUTE:
                self._supervisor.discard_pending_route()
                self._supervisor.incumbent_route.request_stop(resolution.cause)
                self._transition(NavigationTransitionAction.BEGIN_STOPPING, resolution.reason)
            case ProbeHandoffAction.ACQUISITION_FAILED:
                control = self._supervisor.route
                if control is not None:
                    self._request_ending(
                        HandoffDestination.FAIL,
                        StopCause.CANCELLED,
                        resolution.reason,
                        stopping_reason=resolution.reason,
                    )
                else:
                    self._transition(
                        NavigationTransitionAction.MARK_FAILED,
                        resolution.reason,
                    )
            case ProbeHandoffAction.RESOLVE_RECOVERY:
                self._resolve_pending_retry(frame)
            case ProbeHandoffAction.WAIT_FOR_ROUTE_RELEASE | ProbeHandoffAction.WAIT_FOR_ROUTE_INPUT:
                self._reason = resolution.reason
            case ProbeHandoffAction.REPLAN:
                self._reissue_request_from_current(frame, resolution.reason, computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR)
            case ProbeHandoffAction.CONTINUE_ROUTE:
                if self._state is NavigationSessionState.STOPPING:
                    self._reason = "action_acquisition_waiting_for_selected_command"
                else:
                    self._transition(NavigationTransitionAction.BEGIN_EXECUTION, resolution.reason)

    def _request_probe_stop(
        self, cause: StopCause, *,
        terminal: NavigationSessionState | None = None,
        terminal_reason: str | None = None,
    ) -> bool:
        if terminal is not None:
            destination = {
                NavigationSessionState.COMPLETE: HandoffDestination.COMPLETE,
                NavigationSessionState.CANCELLED: HandoffDestination.CANCEL,
                NavigationSessionState.FAILED: HandoffDestination.FAIL,
                NavigationSessionState.CLOSED: HandoffDestination.CLOSE,
            }.get(terminal)
            if destination is None:
                raise ContractViolation("probe stop requires a terminal destination")
            self._handoff.request_recovery(
                request_id=f"{self.session_id}/end", destination=destination,
                reason=terminal_reason or cause.value, cause=cause, budget=self._retry_ledger,
            )
        self._clear_pending_action_boundary()
        if not self._supervisor.request_probe_stop(cause):
            return False
        if self._retry_ledger is not None and self._frame is not None:
            frame = self._frame
            movement_tick = (frame.body.movement_tick_id
                             if frame.body.movement_tick_id is not None
                             else frame.body.sequence_id)
            try:
                self._retry_ledger.begin_wait(
                    "recovery", self._probe_wait_owner_id(),
                    WaitPolicy(_INFORMATION_WAIT_LIMIT_FRAMES,
                               _INFORMATION_WAIT_LIMIT_NS),
                    movement_tick, self._clock(),
                )
            except RetryLedgerCapacityExceeded:
                self._recovery_wait_capacity_exhausted = True
        self._transition(NavigationTransitionAction.BEGIN_STOPPING, 'edge_probe_stopping')
        return True

    def _request_ending(
        self, destination: HandoffDestination, cause: StopCause, reason: str,
        *, stopping_reason: str | None = None,
    ) -> None:
        self._handoff.request_recovery(
            request_id=f"{self.session_id}/end", destination=destination,
            reason=reason, cause=cause, budget=self._retry_ledger,
        )
        request = self._handoff.stop_request
        self._supervisor.request_route_stop(request.cause)
        if self._planning_coordinator is not None:
            self._planning_coordinator.cancel_work("navigation_ending")
        if self._request_probe_stop(request.cause):
            return
        if self._executor is not None:
            self._transition(NavigationTransitionAction.BEGIN_STOPPING,
                             stopping_reason or request.reason)
        else:
            self._finish_pending_probe_terminal(request.reason)
            self._required_interaction = None
            self._interaction_approach_pending = False

    def _has_body_owner(self) -> bool:
        return (
            (self._edge_probe is not None and self._edge_probe.owned)
            or self._supervisor.incumbent_route is not None
            or self._supervisor.has_pending_route
        )

    def _task_stop_requested(self) -> bool:
        return self._handoff.ending

    def _finish_pending_probe_terminal(self, fallback_reason: str) -> None:
        """Publish a deferred terminal only after every body owner has left."""
        if self._has_body_owner():
            return
        resolution = self._handoff.finish_ending(
            budget=self._retry_ledger,
            handoff=self._supervisor.last_handoff,
            frame=self._frame,
        )
        terminal_action = {
            HandoffDestination.COMPLETE:
                NavigationTransitionAction.MARK_COMPLETE,
            HandoffDestination.CANCEL:
                NavigationTransitionAction.MARK_CANCELLED,
            HandoffDestination.FAIL:
                NavigationTransitionAction.MARK_FAILED,
            HandoffDestination.CLOSE:
                NavigationTransitionAction.MARK_CLOSED,
        }.get(resolution.destination)
        if terminal_action is None:
            raise ContractViolation("pending probe terminal state must be terminal")
        self._transition(terminal_action, resolution.reason)
        if resolution.destination is HandoffDestination.CLOSE:
            self._finalize_close()

    def _stage_goal_revision_for_body_release(
        self,
        pending: PendingGoalRevision,
        missing: tuple[BlockPos, ...],
        reason: str,
    ) -> None:
        """Commit a validated revision while the incumbent exits safely."""
        self._handoff.stage_goal(pending, StopCause.GOAL_REVISED, reason)
        self._snapshot_missing = missing
        self._supervisor.discard_pending_route()
        route_control = self._supervisor.incumbent_route
        if route_control is not None:
            route_control.request_stop(StopCause.GOAL_REVISED)
        probe_stopping = self._request_probe_stop(StopCause.GOAL_REVISED)
        if not probe_stopping:
            self._transition(NavigationTransitionAction.BEGIN_STOPPING, reason)
        else:
            self._reason = reason

    def _resume_pending_goal(self, frame: NavigationFrame) -> None:
        """Resolve the newest committed revision after body ownership is clear."""
        pending = self._pending_goal
        if pending is None:
            return
        if self._request is None:
            self._handoff.clear_goal()
            self._snapshot_missing = ()
            self._transition(
                NavigationTransitionAction.RESET_READY,
                "pending_goal_restarting",
            )
            self.start_goal(
                pending.goal_id,
                pending.goal_revision,
                pending.goal_state,
                frame,
                damage_budget=pending.damage_budget,
                task_id=self._retry_ledger.task_id,
                reach_policy=self._goal_requests.reach_policy,
                planning_policy=self._goal_requests.planning_policy,
            )
            return
        goal_node, goal_missing = self._surface_for_goal(
            frame, pending.goal_state,
        )
        start_node, start_missing = self._surface_for_body(frame)
        missing = tuple(sorted(set(goal_missing) | set(start_missing)))
        planning_permit: PlanningAttemptPermit | None = None
        if self._handoff.stop_request is not None:
            handoff = self._supervisor.last_handoff
            resolution = self._handoff.resolve_goal(
                frame, handoff,
                goal_ready=goal_node is not None,
                start_ready=start_node is not None,
                missing_cells=missing,
                budget=self._retry_ledger,
                unavailable_reason=(
                    "goal_surface_unavailable" if goal_node is None
                    else "current_surface_unavailable"
                ),
            )
            if resolution is None:
                return
            if self._state is NavigationSessionState.STOPPING:
                self._transition(
                    NavigationTransitionAction.REPLAN_AFTER_HANDOFF,
                    "goal_revision_body_released",
                    handoff=resolution.handoff,
                )
            assert resolution.pending_goal is not None
            pending = resolution.pending_goal
            if resolution.destination is HandoffDestination.WAIT_FOR_INFORMATION:
                self._snapshot_missing = resolution.missing_cells
                self._transition(
                    NavigationTransitionAction.WAIT_FOR_INFORMATION,
                    resolution.reason,
                )
                return
            if resolution.destination is HandoffDestination.FAIL:
                self._snapshot_missing = ()
                self._transition(
                    NavigationTransitionAction.MARK_FAILED,
                    resolution.reason,
                )
                return
            if resolution.destination is not HandoffDestination.REPLAN:
                raise ContractViolation(
                    "goal handoff produced an unsupported destination"
                )
            planning_permit = PlanningAttemptPermit(
                f"{resolution.request_id}/permit",
                self._retry_ledger.task_id,
                pending.goal_revision,
                resolution.request_id,
                resolution.planning_permit_kind,
            )
        if goal_node is None or start_node is None:
            self._snapshot_missing = missing
            if missing:
                self._transition(
                    NavigationTransitionAction.WAIT_FOR_INFORMATION,
                    ("goal_surface_requires_information"
                     if goal_node is None else
                     "current_surface_requires_information"),
                )
                return
            self._handoff.clear_goal()
            self._transition(
                NavigationTransitionAction.MARK_FAILED,
                ("goal_surface_unavailable" if goal_node is None else
                 "current_surface_unavailable"),
            )
            return
        self._handoff.clear_goal()
        request = replace(
            self._request,
            start=start_node,
            goal=goal_node,
            goal_id=pending.goal_id,
            goal_revision=pending.goal_revision,
            goal_state=pending.goal_state,
            damage_budget=pending.damage_budget,
        )
        self._snapshot_missing = ()
        self._accept_goal_request(
            request, frame, "pending_goal_committed",
            planning_permit=planning_permit,
        )

    def _check_recovery_wait(self, frame: NavigationFrame) -> None:
        if self._retry_ledger is None or self._recovery_wait_capacity_exhausted:
            return
        movement_tick = (frame.body.movement_tick_id
                         if frame.body.movement_tick_id is not None
                         else frame.body.sequence_id)
        try:
            self._retry_ledger.begin_wait(
                "recovery", self._probe_wait_owner_id(),
                WaitPolicy(_INFORMATION_WAIT_LIMIT_FRAMES,
                           _INFORMATION_WAIT_LIMIT_NS),
                movement_tick, self._clock(),
            )
        except RetryLedgerCapacityExceeded:
            self._recovery_wait_capacity_exhausted = True
            return
        self._recovery_wait_status = self._retry_ledger.check_wait(
            "recovery", movement_tick, self._clock(),
        )

    def _probe_movement(self, frame: NavigationFrame) -> MovementV1:
        probe = self._edge_probe
        assert probe is not None
        ledger = self._retry_ledger
        if ledger is None:
            return probe.movement(frame)
        try:
            expired = self._information.probe_wait_expired(
                probe, frame, ledger,
                WaitPolicy(DIRECT_DROP_EDGE_PROBE_MAX_FRAMES,
                           _INFORMATION_WAIT_LIMIT_NS), self._clock(),
            )
        except RetryLedgerCapacityExceeded:
            expired = True
            self._request_probe_stop(
                StopCause.ACQUISITION_TIMED_OUT,
                terminal=NavigationSessionState.FAILED,
                terminal_reason="acquisition_wait_capacity_exhausted",
            )
        return probe.movement(frame, acquisition_expired=expired)

    def _retire_route(self) -> None:
        if self._executor is not None:
            self._supervisor.route.request_stop(StopCause.CANCELLED)
        self._clear_active_execution()

    def _wait_for_active_terminal(
        self, missing: tuple[BlockPos, ...], reason: str,
    ) -> bool:
        if self._executor is None:
            return False
        self._supervisor.route.request_stop(StopCause.CANCELLED)
        self._stage_reanchor_request(StopCause.CANCELLED, "route_handoff_reanchored")
        self._snapshot_missing = missing
        self._continue_execution(reason)
        return True

    def _stage_reanchor_request(self, cause: StopCause, reason: str) -> None:
        request = self._request
        assert request is not None
        self._handoff.stage_reanchor(
            request_id=f"{request.request_id}/body-release", cause=cause, reason=reason,
        )

    def _reissue_request_from_current(
        self,
        frame: NavigationFrame,
        reason: str,
        *,
        computation_invalidation: ComputationInvalidationCause | None,
        source_event_id: str | None = None,
        retry_trigger: PlanningRetryTrigger | None = None,
        retry_cause: RetryCause = RetryCause.PLANNING,
        planning_permit: PlanningAttemptPermit | None = None,
    ) -> None:
        request = self._request
        if request is None:
            return
        planning = self._ensure_planning_coordinator()
        assert self._retry_ledger is not None
        if ((retry_trigger is PlanningRetryTrigger.RECOVERY_REANCHOR)
                != (planning_permit is not None)):
            raise ContractViolation(
                "recovery planning retry requires its typed handoff permit"
            )
        if computation_invalidation is not None:
            self._goal_requests.invalidate_computation(computation_invalidation)
        source = source_event_id or (
            f"{reason}/{request.request_id}/{frame.body.sequence_id}"
        )
        if retry_trigger is not None:
            update = planning.retry_from_current(
                frame,
                trigger=retry_trigger,
                cause=retry_cause,
                failure_id=source,
                remaining_damage_budget=self._remaining_damage_budget(),
                permit=planning_permit,
            )
        else:
            update = planning.restart_from_current(
                frame,
                permit=PlanningAttemptPermit(
                    f"{source}/permit",
                    self._retry_ledger.task_id,
                    request.goal_revision,
                    source,
                    PlanningAttemptPermitKind.PROGRESS,
                ),
                remaining_damage_budget=self._remaining_damage_budget(),
            )
        self._request = update.request
        request = update.request
        if (type(request) is SurfacePlanningRequest
                and surface_search_need(request)
                    is SurfaceSearchNeed.SAME_SUPPORT_LOCAL_GOAL):
            self._accept_goal_request(
                request,
                frame,
                reason,
                preserve_active_route=self._executor is not None,
            )
            return
        if update.kind is PlanningUpdateKind.FAILED:
            assert update.failure is not None
            if self._wait_for_active_terminal(
                (), "restart_waiting_for_safe_terminal",
            ):
                return
            self._fail_planning_or_preserve_incumbent(update.failure)
        elif self._state is NavigationSessionState.STOPPING:
            handoff = self._supervisor.last_handoff
            if (handoff is not None
                    and handoff.disposition is HandoffDisposition.QUIESCENT
                    and handoff.world_session == frame.session
                    and handoff.observation_sequence_id
                        == frame.body.sequence_id):
                self._transition(
                    NavigationTransitionAction.REPLAN_AFTER_HANDOFF,
                    reason,
                    handoff=handoff,
                )
            else:
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    "planning_restart_waits_for_handoff_evidence",
                )
        else:
            self._transition(
                NavigationTransitionAction.BEGIN_PLANNING,
                reason,
            )

    def _advance_planning(
        self, frame: NavigationFrame, state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None,
    ) -> PlanningUpdate | None:
        # Body handoff remains the session's responsibility. Planning may run
        # behind an incumbent, but cannot reopen the lifecycle while that owner
        # is proving a safe release.
        if self._state is NavigationSessionState.STOPPING:
            return
        request = self._request
        if request is None or self._state is NavigationSessionState.NEEDS_INFORMATION:
            return
        if self._pending_goal is not None:
            return
        if self._local_goal_request_id == request.request_id:
            return
        if self._direct_goal_request_id == request.request_id:
            return
        if (self._active_route is not None
                and self._active_route.source_request_id == request.request_id):
            return
        planning = self._ensure_planning_coordinator()
        update = planning.advance(
            frame,
            current_scope=self.current_computation_scope,
            state_anchor=state_anchor,
            edge_probe=self._edge_probe,
            remaining_damage_budget=self._remaining_damage_budget(),
        )
        self._request = update.request
        request = update.request
        if update.kind is PlanningUpdateKind.RUNNING:
            self._transition(
                NavigationTransitionAction.BEGIN_PLANNING,
                update.reason,
            )
            return update
        if update.kind is PlanningUpdateKind.NEEDS_INFORMATION:
            assert update.information_need is not None
            self._snapshot_missing = update.missing_cells
            self._transition(
                NavigationTransitionAction.WAIT_FOR_INFORMATION,
                update.reason,
            )
            if update.landing_probe_cell is not None:
                landing_cell = update.landing_probe_cell
                if (self._edge_probe is None
                        or not self._edge_probe.belongs_to(
                            request.goal_id, request.goal_revision,
                        )
                        or self._edge_probe.landing_cell != landing_cell):
                    if self._request_probe_stop(StopCause.ROUTE_REPLACED):
                        return update
                    self._edge_probe = LandingEdgeProbe(
                        request.goal_id,
                        request.goal_revision,
                        landing_cell,
                        frame.body.sequence_id,
                    )
                    if self._retry_ledger is not None:
                        self._end_information_wait()
            return update
        if update.kind is PlanningUpdateKind.REQUIRES_INTERACTION:
            assert update.interaction is not None
            self._required_interaction = update.interaction
            self._interaction_approach_pending = False
            self._transition(
                NavigationTransitionAction.REQUIRE_INTERACTION,
                update.reason,
            )
            return update
        if update.kind is PlanningUpdateKind.FAILED:
            assert update.failure is not None
            self._fail_planning_or_preserve_incumbent(update.failure)
            return update
        assert update.kind is PlanningUpdateKind.ROUTE_READY
        assert update.route is not None
        self._required_interaction = update.interaction
        self._interaction_approach_pending = update.interaction is not None
        self._activate_planning_route(
            update.route, frame, state_anchor, input_ledger,
        )
        return update

    def _activate_planning_route(
        self,
        route: ActiveRoute,
        frame: NavigationFrame,
        state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None,
        *,
        planning_result: bool = True,
        preserve_incumbent_on_offer_failure: bool = False,
    ) -> None:
        current = self._request
        if current is None:
            return
        current_damage_budget = self._remaining_damage_budget()
        if (self._route_expected_damage_points(route)
                > current_damage_budget.maximum_expected_damage_points
                + 1.0e-9):
            self._reissue_request_from_current(
                frame,
                "damage_budget_changed_before_admission",
                retry_trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
                retry_cause=RetryCause.DEPENDENCY,
                computation_invalidation=ComputationInvalidationCause.BASIS_INVALIDATED,
            )
            return
        if (self._active_route is not None
                and self._active_route.source_request_id != current.request_id):
            assert self._executor is not None
            if self._executor.requires_safe_handoff(frame):
                self._supervisor.route.request_stop(StopCause.CANCELLED)
                self._stage_reanchor_request(StopCause.CANCELLED, "route_handoff_reanchored")
                self._continue_execution(
                    "route_handoff_waiting_for_safe_terminal",
                )
                return
        if self._edge_probe is not None and not self._edge_probe.ready:
            self._edge_probe.begin_handoff("route_admitted")
        executor = ActionRouteExecutor(
            self.profiles.ground,
            self.profiles.jump_up,
            self.profiles.step,
            self.profiles.ground_modes,
            self.profiles.air,
            gap_solver_policy=self.profiles.gap_solver,
        )
        motion_coordinator = None
        if any(action_spec(action).needs_background_solving
               for action in route.action_route.actions):
            if self._motion_worker is None:
                self._motion_worker = MotionSolverWorker(max_pending=4)
                self._owns_motion_worker = True
            motion_coordinator = MotionRouteCoordinator(
                route, executor, self._motion_worker,
                computation_scope=self.current_computation_scope,
                damage_budget=current_damage_budget,
                retry_ledger=self._retry_ledger,
                result_inbox=self._motion_result_inbox,
                owner_instance_id=self._execution_instances.allocate(),
                clock_ns=self._clock,
                gap_solver_policy=self.profiles.gap_solver,
            )
            motion_coordinator.start(frame)
        else:
            executor.start(
                route.action_route, frame,
                damage_budget=current_damage_budget,
            )
        if not self._supervisor.offer_route(
            RouteControl(route, executor, motion_coordinator), frame,
            input_ledger, state_anchor,
        ):
            if preserve_incumbent_on_offer_failure:
                self._continue_execution(
                    "ground_direct_waits_for_transferable_incumbent",
                )
                return
            incumbent = self._supervisor.incumbent_route
            if incumbent is not None:
                incumbent.request_stop(StopCause.ROUTE_REPLACED)
                self._stage_reanchor_request(StopCause.ROUTE_REPLACED, "route_handoff_reanchored")
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    "route_handoff_waiting_for_safe_terminal",
                )
            else:
                self._reissue_request_from_current(
                    frame, "route_handoff_reanchored",
                    computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR,
                )
            return
        if self._planning_coordinator is not None:
            if planning_result:
                self._planning_coordinator.confirm_route_admitted(route)
            else:
                self._planning_coordinator.confirm_nonplanner_route_admitted(
                    route,
                    current_scope=self.current_computation_scope,
                )
        self._executor_reported_damage_points = 0.0
        if planning_result and self._planning_coordinator is not None:
            self._planning_coordinator.clear_changes()
        self._snapshot_missing = ()
        self._continue_execution("route_admitted")

    def _apply_decision_state(self, decision: ActionRouteDecision) -> None:
        if (decision.state is ActionRouteState.COMPLETE
                and self._goal_requests.reach_policy is GoalReachPolicy.KEEP_ACTIVE_ON_REACH):
            self._continue_execution(decision.reason_code)
            self._snapshot_missing = decision.missing_cells
            return
        mapping = {
            ActionRouteState.COMPLETE:
                NavigationTransitionAction.MARK_COMPLETE,
            ActionRouteState.CANCELLED:
                NavigationTransitionAction.MARK_CANCELLED,
            ActionRouteState.NEEDS_INFORMATION:
                NavigationTransitionAction.WAIT_FOR_INFORMATION,
            ActionRouteState.FAILED: NavigationTransitionAction.MARK_FAILED,
            ActionRouteState.BLOCKED: NavigationTransitionAction.MARK_FAILED,
            ActionRouteState.UNSUPPORTED: NavigationTransitionAction.MARK_FAILED,
            ActionRouteState.INPUT_LOST: NavigationTransitionAction.MARK_FAILED,
            ActionRouteState.NEEDS_REPLAN:
                NavigationTransitionAction.BEGIN_PLANNING,
        }
        action = mapping.get(decision.state)
        if action is None:
            self._continue_execution(decision.reason_code)
        else:
            self._transition(action, decision.reason_code)
        self._snapshot_missing = decision.missing_cells

    def _proposal(
        self,
        movement: MovementV1,
        look: LookV1 | None,
        lease_ticks: int,
        deadline_ns: int,
        *,
        route_decision: ActionRouteDecision | None = None,
        conditioned_look_intent_id: str | None = None,
        information_look: LookV1 | None = None,
        safety_guard: bool = False,
        route_control: RouteControl | None = None,
        risk_action_id: str | None = None,
        retain_body_input: bool = False,
        movement_required: bool = True,
    ) -> NavigationSessionProposal:
        if self._frame is not None:
            _, newly_exhausted = self._finalize_task_activity(self._frame)
            if newly_exhausted and not (safety_guard or retain_body_input):
                movement = MovementV1()
                look = None
                information_look = None
                route_decision = None
                movement_required = False
        control = None
        if self._source is not None:
            if self._frame is None:
                raise ContractViolation("navigation intent has no current frame")
            observation_request = self.observation_request()
            decision_events = (self._session_decision_event(route_decision),)
            if route_decision is not None:
                decision_events += (self._route_decision_event(
                    route_decision, conditioned_look_intent_id,
                ),)
            if (not movement_required or (route_decision is not None and not route_decision.submit_input
                    and not retain_body_input
                    and self._edge_probe is None)):
                return NavigationSessionProposal(
                    ControlFrameProposalV1(
                        observation_request=observation_request,
                        task_events=decision_events,
                    ),
                    self.report,
                    route_decision,
                )
            now = self._clock()
            expires = min(deadline_ns, now + 250_000_000)
            if expires <= now:
                raise ContractViolation("navigation intent window expired")
            self._intent_sequence += 1
            identity = ordered_intent_id(self._source, self._intent_sequence)
            current_action = None
            route_executor = (route_control.executor if route_control is not None
                              else self._executor)
            executor_route = (
                getattr(route_executor, "route", None)
                if route_executor is not None else None
            )
            if (route_decision is not None and executor_route is not None
                    and 0 <= route_decision.action_index
                        < len(executor_route.actions)):
                current_action = executor_route.actions[
                    route_decision.action_index
                ]
            ordinary_walk = (
                type(current_action) is WalkSegment
                and (
                    current_action.transition is None
                    or current_action.transition.mode is MovementMode.WALK
                )
            )
            look_conditioned = (
                movement != MovementV1()
                and ordinary_walk
                and look is None
                and conditioned_look_intent_id is not None
            )
            observed_yaw_bound = (
                movement != MovementV1()
                and ordinary_walk
                and not look_conditioned
            )
            intent = ActionIntentV1(
                identity,
                self._source.source_id,
                self._source.episode_id,
                self._frame.body.sequence_id,
                (ActionPriorityV0.SAFETY if safety_guard
                 else ActionPriorityV0.TASK),
                now,
                expires,
                movement=(None if safety_guard and movement == MovementV1()
                          else movement),
                look=look,
                valid_for_ticks=(
                    1
                    if (observed_yaw_bound or look_conditioned)
                    else max(1, min(20, lease_ticks))
                ),
                movement_requires_look=(look is not None and movement != MovementV1()),
                movement_look_tolerance_degrees=(
                    5.0
                    if (
                        look is not None
                        and movement != MovementV1()
                        and ordinary_walk
                    )
                    else 0.0
                ),
                movement_observed_yaw_limit_degrees=(
                    0.0 if (safety_guard and
                            (movement.forward or movement.strafe)) else
                    (5.0 if observed_yaw_bound else None)
                ),
                movement_conditioned_look_intent_id=(
                    conditioned_look_intent_id if look_conditioned else None
                ),
                movement_tick_window=(
                    MovementTickWindowV1(
                        route_decision.expected_movement_tick,
                        route_decision.latest_movement_tick,
                    )
                    if (
                        route_decision is not None
                        and route_decision.expected_movement_tick is not None
                        and route_decision.latest_movement_tick is not None
                    )
                    else None
                ),
            )
            intents = [
                OrderedIntentV1(self._source, self._intent_sequence, intent),
            ]
            if information_look is not None:
                self._intent_sequence += 1
                information_identity = ordered_intent_id(
                    self._source, self._intent_sequence,
                )
                intents.append(OrderedIntentV1(
                    self._source,
                    self._intent_sequence,
                    ActionIntentV1(
                        information_identity,
                        self._source.source_id,
                        self._source.episode_id,
                        self._frame.body.sequence_id,
                        ActionPriorityV0.BEHAVIOR,
                        now,
                        expires,
                        look=information_look,
                        valid_for_ticks=1,
                    ),
                ))
            control = ControlFrameProposalV1(
                tuple(intents),
                observation_request,
                decision_events,
            )
        # A route owner may be attached only when this proposal is the route's
        # actual submitted command.  Probe and recovery guards can reuse the
        # same report decision while replacing its body movement; registering
        # that guard as a route command would corrupt the verified command
        # ledger and can turn a safe goal revision into a contract failure.
        route_command_selected = (
            route_decision is not None
            and route_decision.submit_input
            and movement == route_decision.movement
        )
        route_owner = (
            (route_control if route_control is not None else self._supervisor.route)
            if route_command_selected else None
        )
        return NavigationSessionProposal(
            control, self.report, route_decision,
            None if route_owner is None else route_owner.route.route_id,
            risk_action_id,
            (route_owner.activity(self._frame, route_decision)
             if route_owner is not None else next(iter(
                 self._supervisor.activities(self._frame, route_decision)), None)),
        )

    def _information_look(self, frame: NavigationFrame) -> LookV1 | None:
        """Route owner facts; neither classify queries nor choose a camera angle."""
        if (self._state is not NavigationSessionState.NEEDS_INFORMATION
                or not self._snapshot_missing):
            return None
        selection = self.planning_information_update
        pending_surface_wait = (
            selection is None and self._pending_goal is not None
        )
        if pending_surface_wait:
            assert self._retry_ledger is not None
            self._retry_ledger.set_blockers(tuple(
                f"cell/{x}/{y}/{z}"
                for x, y, z in self._snapshot_missing[:128]
            ))
        facts = self._information.advance(
            frame, landing_acquisition_pending=self._landing_acquisition_pending(),
            edge_probe=self._edge_probe, ledger=self._retry_ledger,
            wait_owner_id=self._information_wait_owner_id(),
            wait_policy=WaitPolicy(_INFORMATION_WAIT_LIMIT_FRAMES, _INFORMATION_WAIT_LIMIT_NS),
            now_ns=self._clock(),
            planning_wait=selection is not None or pending_surface_wait,
        )
        if facts.wait_verdict is None or facts.wait_verdict is WaitVerdict.WAITING:
            return facts.look
        if selection is not None:
            update = self._planning_coordinator.reconcile_information(
                selection, frame, edge_probe=self._edge_probe,
                outcomes=tuple((blocker.blocker_key, InformationOutcome.TIMED_OUT)
                               for blocker in selection.information_need.blockers),
                current_scope=self.current_computation_scope,
            )
            self._end_information_wait()
            if update.kind is PlanningUpdateKind.NEEDS_INFORMATION:
                self._information.select_missing(update.missing_cells)
                self._reason = update.reason
                return None
            if update.kind is PlanningUpdateKind.FAILED:
                # Planner frontier truncation and sensor classification keep
                # their existing diagnostic precedence; capacity applies only
                # to a wait without a planner-owned selection.
                failure_facts = replace(facts, capacity_exhausted=False)
                self._fail_planning_or_preserve_incumbent(replace(
                    update.failure, reason=failure_facts.timeout_reason(
                        frontier_truncated=selection.information_need.truncated)))
                return None
        failure = facts.timeout_reason()
        if not self._request_probe_stop(
                StopCause.INFORMATION_TIMED_OUT,
                terminal=NavigationSessionState.FAILED, terminal_reason=failure):
            self._transition(NavigationTransitionAction.MARK_FAILED, failure)
        return facts.look

    def _session_decision_event(
        self,
        decision: ActionRouteDecision | None,
    ) -> ControlFrameEventV1:
        if self._frame is None:
            raise ContractViolation("navigation session event has no current frame")
        movement = MovementV1() if decision is None else decision.movement
        return ControlFrameEventV1(
            "navigation_session_decision",
            {
                "schema_version": "mc2p.navigation-session-decision.v1",
                "episode_id": self._source.episode_id,
                "session_id": self.session_id,
                "request_id": self.report.request_id,
                "goal_id": self.report.goal_id,
                "goal_revision": self.report.goal_revision,
                "route_id": self.report.route_id,
                "observation_sequence_id": self._frame.body.sequence_id,
                "state": self.report.state.value,
                "reason_code": self.report.reason,
                "active_route": self._active_route is not None,
                "route_decision_present": decision is not None,
                "submit_input": (
                    decision is not None and decision.submit_input
                ),
                "movement": {
                    "forward": movement.forward,
                    "strafe": movement.strafe,
                    "jump": movement.jump,
                    "sneak": movement.sneak,
                    "sprint": movement.sprint,
                },
            },
        )

    def _route_decision_event(
        self,
        decision: ActionRouteDecision,
        conditioned_look_intent_id: str | None,
    ) -> ControlFrameEventV1:
        if self._frame is None:
            raise ContractViolation("navigation decision event has no current frame")
        movement = decision.movement
        return ControlFrameEventV1(
            "navigation_route_decision",
            {
                "schema_version": "mc2p.navigation-route-decision.v1",
                "episode_id": self._source.episode_id,
                "session_id": self.session_id,
                "request_id": self.report.request_id,
                "goal_id": self.report.goal_id,
                "goal_revision": self.report.goal_revision,
                "route_id": self.report.route_id,
                "observation_sequence_id": self._frame.body.sequence_id,
                "state": decision.state.value,
                "reason_code": decision.reason_code,
                "submit_input": decision.submit_input,
                "action_index": decision.action_index,
                "input_lease_ticks": decision.input_lease_ticks,
                "movement": {
                    "forward": movement.forward,
                    "strafe": movement.strafe,
                    "jump": movement.jump,
                    "sneak": movement.sneak,
                    "sprint": movement.sprint,
                },
                "verified_command_index": decision.verified_command_index,
                "expected_movement_tick": decision.expected_movement_tick,
                "latest_movement_tick": decision.latest_movement_tick,
                "conditioned_look_intent_id": conditioned_look_intent_id,
            },
        )

    @staticmethod
    def _surface_for_body(
        frame: NavigationFrame,
    ) -> tuple[SurfaceNodeId | None, tuple[BlockPos, ...]]:
        x, y, z = frame.body.position
        body = frame.body.body_box
        max_x = math.floor(math.nextafter(body.max_x, -math.inf))
        max_z = math.floor(math.nextafter(body.max_z, -math.inf))
        candidates = []
        missing: set[BlockPos] = set()
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
                math.hypot(surface.position[0] - x, surface.position[2] - z),
                surface.node_id,
            ),
        ).node_id, tuple(sorted(missing))

    @staticmethod
    def _surface_for_goal(
        frame: NavigationFrame,
        goal: GoalState,
    ) -> tuple[SurfaceNodeId | None, tuple[BlockPos, ...]]:
        center = (
            (goal.region.min_x + goal.region.max_x) / 2.0,
            (goal.region.min_y + goal.region.max_y) / 2.0,
            (goal.region.min_z + goal.region.max_z) / 2.0,
        )
        cache = WorldQueryCache(frame.world)
        missing: set[BlockPos] = set()
        max_x = math.floor(math.nextafter(goal.region.max_x, -math.inf))
        max_z = math.floor(math.nextafter(goal.region.max_z, -math.inf))
        columns = []
        for x in range(math.floor(goal.region.min_x), max_x + 1):
            for z in range(math.floor(goal.region.min_z), max_z + 1):
                nearest_x = min(max(center[0], max(goal.region.min_x, x)),
                                min(goal.region.max_x, x + 1))
                nearest_z = min(max(center[2], max(goal.region.min_z, z)),
                                min(goal.region.max_z, z + 1))
                columns.append((math.hypot(nearest_x-center[0], nearest_z-center[2]), x, z))
        columns.sort()
        best = None
        best_rank = (math.inf, (math.inf, math.inf, math.inf))
        point_missing: set[BlockPos] = set()
        region_only = []
        for column_lower_bound, x, z in columns:
            if column_lower_bound > best_rank[0] + 1.0e-12:
                break
            result = query_support_surfaces(
                frame.world, x, z,
                goal.region.min_y, goal.region.max_y,
                collect_complete_missing=True, query_cache=cache,
            )
            missing.update(result.missing_cells)
            for index, surface in enumerate(result.surfaces):
                # Preserve the F2-R point preference and original scan tie
                # order. Geometric lower bounds skip only farther columns.
                point = standable_point_in_region(
                    frame.world, surface, goal.region, query_cache=cache,
                )
                point_missing.update(point.missing_cells)
                if point.status is not QueryStatus.FEASIBLE:
                    region_only.append(surface)
                    continue
                rank = (math.dist(point.position, center), (x, z, index))
                if rank >= best_rank:
                    continue
                # The point only ranks a face; the signed region query is
                # the final feasibility authority shared with admission.
                target = standable_region_in_goal(
                    frame.world, surface, goal.region, query_cache=cache,
                )
                if target.status is QueryStatus.FEASIBLE:
                    best, best_rank = surface.node_id, rank
                else:
                    missing.update(target.missing_cells)
        if missing:
            return None, tuple(sorted(missing))
        if best is not None:
            return best, ()
        # A positive region may exist even when none of the historical point
        # samples is usable. Such faces cannot displace an old feasible choice.
        for surface in region_only:
            target = standable_region_in_goal(
                frame.world, surface, goal.region, query_cache=cache,
            )
            if target.status is QueryStatus.FEASIBLE:
                return surface.node_id, ()
            missing.update(target.missing_cells)
        return None, tuple(sorted(missing | point_missing))
