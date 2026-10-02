"""Online owner for planning, route admission and route execution.

The session never advances a backend. It turns the newest navigation frame
into an ordered control-frame proposal and keeps asynchronous results bound to
the request and goal revision that produced them.
"""
from __future__ import annotations

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
    ActionRoute, ControlledDropSegment, JumpGapSegment, JumpUpSegment, WalkSegment,
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
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor, RouteControl
from mc2p.motion_nav.body_control import BodyControlActivity
from mc2p.motion_nav.body_control import (
    BodyControlProgress, HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.fixed_route import (
    FixedRoute, RoutePoint,
)
from mc2p.motion_nav.goal_observation import (
    ObservedGoalStatus, evaluate_observed_goal,
)
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_modes import GroundModeProfiles
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
    TaskRiskLedger, conservative_plain_fall_damage_points,
)
from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence, ProgressKind, RetryCause, RetryLedger,
    RetryLedgerCapacityExceeded, RetryVerdict, WaitPolicy, WaitVerdict,
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
    HandoffDestination, NavigationHandoffCoordinator,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.planner_worker import PlannerWorker, PlannerWorkerPort
from mc2p.motion_nav.planning_coordinator import (
    InformationOutcome,
    PlanningAttemptPermit,
    PlanningAttemptPermitKind,
    PlanningCapabilities,
    PlanningCoordinator,
    PlanningUpdateKind,
)
from mc2p.motion_nav.route_admission import (
    ActiveRoute, ExecutableCorridor,
    direct_drop_visual_evidence_sufficient,
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
from mc2p.motion_nav.support_surfaces import SurfaceNodeId, query_support_surfaces, standable_point_in_region
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge


_FORMAL_HALF_FOV_DEGREES = 60.0
_INFORMATION_LOOK_MAX_DELTA_DEGREES = 36.0
_INFORMATION_WAIT_LIMIT_FRAMES = 40
_INFORMATION_WAIT_LIMIT_NS = 2_000_000_000


def information_look_for_missing_cells(
        frame: NavigationFrame,
        positions: tuple[BlockPos, ...],
        statuses: dict[BlockPos, str],
        *,
        lower_region_positions: frozenset[BlockPos] = frozenset(),
) -> LookV1 | None:
    """Choose the same bounded information look used by formal navigation."""
    eye_x = frame.body.position[0]
    eye_y = frame.body.body_box.max_y - 0.18
    eye_z = frame.body.position[2]
    current_yaw = math.degrees(frame.body.yaw_radians)
    current_pitch = math.degrees(frame.body.pitch_radians)
    candidates: list[tuple[float, float, float]] = []
    for x, y, z in positions:
        status = statuses.get((x, y, z))
        if status in {"out_of_range", "occluded", "unavailable"}:
            continue
        dx = x + 0.5 - eye_x
        target_y = y + (0.05 if (x, y, z) in lower_region_positions else 0.5)
        dy = target_y - eye_y
        dz = z + 0.5 - eye_z
        horizontal = math.hypot(dx, dz)
        desired_yaw = (
            current_yaw
            if horizontal <= 1.0e-9
            else math.degrees(math.atan2(-dx, dz))
        )
        desired_pitch = -math.degrees(
            math.atan2(dy, max(horizontal, 1.0e-9))
        )
        yaw_error = (desired_yaw - current_yaw + 180.0) % 360.0 - 180.0
        pitch_error = desired_pitch - current_pitch
        if status is None and (
                abs(yaw_error) <= _FORMAL_HALF_FOV_DEGREES
                and abs(pitch_error) <= _FORMAL_HALF_FOV_DEGREES):
            continue
        candidates.append((
            yaw_error * yaw_error + pitch_error * pitch_error,
            yaw_error,
            pitch_error,
        ))
    if not candidates:
        return None
    _, yaw_error, pitch_error = min(candidates)
    limit = _INFORMATION_LOOK_MAX_DELTA_DEGREES
    yaw_delta = max(-limit, min(limit, yaw_error))
    pitch_delta = max(-limit, min(limit, pitch_error))
    if abs(yaw_delta) <= 1.0e-6 and abs(pitch_delta) <= 1.0e-6:
        return None
    return LookV1(yaw_delta, pitch_delta)


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


@dataclass(frozen=True, slots=True)
class NavigationSessionProposal:
    control_frame: ControlFrameProposalV1 | None
    report: NavigationSessionReport
    route_decision: ActionRouteDecision | None = None
    route_owner_id: str | None = None
    risk_action_id: str | None = None
    body_activity: BodyControlActivity | None = None


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
    ) -> None: ...
    def update_goal(
        self, goal_id: str, goal_revision: int, goal_state: GoalState, *,
        damage_budget: TaskDamageBudget | None = None,
        task_id: str | None = None,
    ) -> None: ...
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
    def spawn_successor(self, session_id: str) -> "NavigationSessionPort": ...
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
        self._pending_probe_stop_cause: StopCause | None = None
        self._pending_probe_terminal: NavigationSessionState | None = None
        self._pending_probe_terminal_reason: str | None = None
        self._required_interaction: BridgeInteractionPlan | None = None
        self._interaction_approach_pending = False
        self._local_goal_request_id: str | None = None
        self._local_input_floor: int | None = None
        self._local_mode_wait_frames = 0
        self._probe_mode_exit_pending = False
        self._last_decision: ActionRouteDecision | None = None
        self._task_damage_budget = TaskDamageBudget()
        self._movement_damage_spent_points = 0.0
        self._executor_reported_damage_points = 0.0
        self._motion_residual = MotionResidualTracker()
        self._restart_after_active_terminal = False
        self._retry_ledger: RetryLedger | None = retry_ledger
        self._risk_ledger: TaskRiskLedger | None = risk_ledger
        self._risk_action_id: str | None = None
        self._risk_action_key: tuple[str, int] | None = None
        self._risk_failure_reason: str | None = None
        self._risk_submission_capacity_exhausted = False
        self._recovery_wait_status: WaitVerdict | None = None
        self._recovery_wait_capacity_exhausted = False
        self._pending_retry: tuple[
            str, RetryVerdict, str, tuple[BlockPos, ...],
        ] | None = None
        self._last_retry_route_id: str | None = None
        self._last_retry_action_index: int | None = None
        self._last_retry_body_cell: tuple[int, int, int] | None = None
        self._cancel_reason: str | None = None
        self._closed = False
        self._close_requested = False

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
        if self._edge_probe is not None:
            owners.add(self._edge_probe.owner_id)
        for owner_id in owners:
            ledger.end_owner_waits(owner_id)
        self._recovery_wait_status = None

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
        if self._edge_probe is not None and self._edge_probe.owned:
            self._request_probe_stop(
                StopCause.CLOSED,
                terminal=NavigationSessionState.FAILED,
                terminal_reason=reason,
            )
            return
        if self._executor is not None:
            self._risk_failure_reason = reason
            self._supervisor.route.request_stop(StopCause.CANCELLED)
            self._transition(
                NavigationTransitionAction.BEGIN_STOPPING,
                reason,
            )
            return
        self._transition(NavigationTransitionAction.MARK_FAILED, reason)

    def _admit_command_event(self, event: NavigationSessionEvent) -> None:
        """Reject an invalid synchronous command before it mutates the task."""
        policy = self._lifecycle.admit_event(event)
        if policy is not SessionEventPolicy.HANDLE:
            raise ContractViolation(
                "navigation lifecycle event "
                f"{event.value} is {policy.value} in {self._state.value}"
            )

    def _admit_async_event(self, event: NavigationSessionEvent) -> bool:
        """Drop stale async work; turn an impossible active event into failure."""
        policy = self._lifecycle.admit_event(event)
        if policy is SessionEventPolicy.HANDLE:
            return True
        if policy is SessionEventPolicy.IGNORE:
            return False
        if self._state not in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            self._transition(
                NavigationTransitionAction.MARK_FAILED,
                f"rejected_async_event_{event.value}",
            )
            return False
        raise ContractViolation(
            "navigation lifecycle event "
            f"{event.value} is rejected in {self._state.value}"
        )

    @property
    def _request(self) -> PlanningRequest | SurfacePlanningRequest | None:
        return self._goal_requests.request

    @_request.setter
    def _request(self, value: PlanningRequest | SurfacePlanningRequest | None) -> None:
        self._goal_requests.accept(value)

    @property
    def _pending_goal(self) -> PendingGoalRevision | None:
        return self._handoff.pending_goal

    @_pending_goal.setter
    def _pending_goal(self, value: PendingGoalRevision | None) -> None:
        if value is None:
            self._handoff.clear_goal()
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
    def _information_statuses(self) -> dict[BlockPos, str]:
        return self._information.statuses

    @_information_statuses.setter
    def _information_statuses(self, value: dict[BlockPos, str]) -> None:
        self._information.statuses = value

    @property
    def _information_lower_required(self) -> set[BlockPos]:
        return self._information.lower_required

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
        )

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
             self._retry_ledger.count_for(RetryCause.EXECUTION)),
            self._information_wait_frames, self._reason,
            self._supervisor.last_handoff,
            (None if self._supervisor.incumbent_route is None else
             self._supervisor.incumbent_route.route.route_id),
            (None if not self._supervisor.has_pending_route else
             self._supervisor.route.route.route_id),
            (0 if self._retry_ledger is None else
             self._retry_ledger.round_failures),
            (0 if self._retry_ledger is None else
             self._retry_ledger.total_failures),
            (None if self._pending_retry is None else self._pending_retry[0]),
            (False if self._retry_ledger is None else
             self._retry_ledger.progress_capacity_exhausted),
            (() if self._risk_ledger is None else
             self._risk_ledger.snapshot_actions()),
            (0.0 if self._risk_ledger is None else
             self._risk_ledger.available_points),
            (0 if self._risk_ledger is None else
             self._risk_ledger.policy_revision),
            self._risk_submission_capacity_exhausted,
            (None if self._retry_ledger is None else
             self._retry_ledger.last_failure_attempt_id),
            (() if self._retry_ledger is None else
             self._retry_ledger.cause_counts),
            (0 if self._retry_ledger is None else
             self._retry_ledger.progress_version),
            (None if self._retry_ledger is None else
             self._retry_ledger.last_progress_evidence),
            self._recovery_wait_status,
            self._recovery_wait_capacity_exhausted,
            (0 if self._retry_ledger is None else
             self._retry_ledger.approved_round_retries),
            (0 if self._retry_ledger is None else
             self._retry_ledger.approved_total_retries),
            (() if self._retry_ledger is None else
             self._retry_ledger.approved_cause_counts),
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
        """Check the currently observed body and input history before release."""
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
        return self._supervisor.evaluate_quiescence(frame, ledger, anchor)

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
        residual_missing = tuple(sorted(set(self._residual_missing)))
        planning_missing = tuple(sorted(
            set(self._snapshot_missing) - set(residual_missing)
        ))
        missing = residual_missing + planning_missing
        route_dependencies = (
            () if self._active_route is None
            else self._active_route.action_route.dependencies
        )
        departure_dependencies: tuple[BlockPos, ...] = ()
        if (self._frame is not None and self._frame.body.is_on_ground
                and self._executor is not None):
            route = getattr(self._executor, "route", None)
            action_index = getattr(self._executor, "action_index", None)
            if (route is not None and type(action_index) is int
                    and 0 <= action_index < len(route.actions)):
                action = route.actions[action_index]
                if type(action) is ControlledDropSegment:
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
        if type(request) is SurfacePlanningRequest:
            self._task_damage_budget = request.damage_budget
            self._movement_damage_spent_points = 0.0
            self._executor_reported_damage_points = 0.0
        if self._retry_ledger is None:
            self._retry_ledger = RetryLedger(request.goal_id)
        elif self._retry_ledger.task_id != request.goal_id:
            raise ContractViolation("navigation request changed task ledger identity")
        if self._risk_ledger is None:
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
    ) -> None:
        self._admit_command_event(NavigationSessionEvent.START_GOAL)
        self._start_goal_after_admission(
            goal_id, goal_revision, goal_state, frame,
            maximum_expansions=maximum_expansions,
            maximum_planning_seconds=maximum_planning_seconds,
            damage_budget=damage_budget,
            task_id=task_id,
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
    ) -> None:
        """Resolve current and goal support without creating point-goal state."""
        if self._request is not None:
            raise ContractViolation("navigation session already has a request")
        require_identifier(goal_id, "navigation goal id")
        if type(goal_revision) is not int or goal_revision < 0:
            raise ContractViolation("navigation goal revision is invalid")
        if type(goal_state) is not GoalState or type(frame) is not NavigationFrame:
            raise ContractViolation("navigation goal requires typed state and frame")
        if damage_budget is None:
            damage_budget = (TaskDamageBudget() if self._risk_ledger is None
                             else self._risk_ledger.budget)
        if type(damage_budget) is not TaskDamageBudget:
            raise ContractViolation("navigation goal damage budget must be typed")
        identity = goal_id if task_id is None else task_id
        require_identifier(identity, "navigation task id")
        if self._retry_ledger is None:
            self._retry_ledger = RetryLedger(identity, initial_support=(
                math.floor(frame.body.position[0]),
                math.floor(frame.body.position[1] + 1.0e-9),
                math.floor(frame.body.position[2]),
            ))
        elif self._retry_ledger.task_id != identity:
            raise ContractViolation("navigation goal changed task ledger identity")
        if self._risk_ledger is None:
            self._risk_ledger = TaskRiskLedger(identity, damage_budget)
        elif self._risk_ledger.task_id != identity:
            raise ContractViolation("navigation goal changed risk task identity")
        elif self._risk_ledger.budget != damage_budget:
            self._risk_ledger.update_policy(
                damage_budget,
                revision=max(goal_revision, self._risk_ledger.policy_revision + 1),
            )
        self._task_damage_budget = damage_budget
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
    ) -> None:
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
            return
        if type(self._request) is not SurfacePlanningRequest:
            raise ContractViolation("surface goal update requires an active request")
        if (goal_id != self._request.goal_id
                or type(goal_revision) is not int
                or goal_revision <= self._request.goal_revision
                or type(goal_state) is not GoalState):
            raise ContractViolation("navigation goal identity or revision is invalid")
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
            return
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
                self._planning_coordinator.consume_replacement_failure()
            self._stage_goal_revision_for_body_release(
                pending, (),
                "goal_revision_waits_for_risk_action_terminal",
            )
            return
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
            return
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
            return
        self._pending_goal = None
        self._accept_goal_request(
            request, self._frame, "goal_revised",
            preserve_active_route=self._executor is not None,
        )

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
        return sum(
            conservative_plain_fall_damage_points(max(
                0.0,
                action.start_surface.position[1]
                - action.end_surface.position[1],
            ))
            for action in route.action_route.actions
            if type(action) is ControlledDropSegment
        )

    @staticmethod
    def _cell_fact_id(position: BlockPos) -> str:
        return f"cell/{position[0]}/{position[1]}/{position[2]}"

    def _record_retry_route_progress(
        self, frame: NavigationFrame, decision: ActionRouteDecision,
    ) -> None:
        ledger = self._retry_ledger
        route = self._active_route
        if ledger is None or route is None:
            return
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
                ledger.record_progress(ProgressEvidence(
                    ProgressKind.ACTION_COMPLETED,
                    frame.body.sequence_id, action_id=action_id,
                ))
        self._last_retry_route_id = route.route_id
        self._last_retry_action_index = decision.action_index
        if not frame.body.is_on_ground:
            return
        position = frame.body.position
        cell = (math.floor(position[0]),
                math.floor(position[1] + 1.0e-9),
                math.floor(position[2]))
        if cell == self._last_retry_body_cell:
            return
        self._last_retry_body_cell = cell
        node, _ = self._surface_for_body(frame)
        if node is None:
            return
        corridor = tuple(
            (item.column_x, item.vertical_band, item.column_z)
            for item in route.corridor.node_ids
            if type(item) is SurfaceNodeId
        )
        support = (node.column_x, node.vertical_band, node.column_z)
        ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, frame.body.sequence_id,
            support=support, valid_corridor=corridor,
        ))

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
        if type(action) is not ControlledDropSegment:
            return None, None
        key = (f"{route.source_request_id}/{route.route_id}",
               decision.action_index)
        record = (None if self._risk_action_id is None else
                  ledger.action(self._risk_action_id))
        if (self._risk_action_key != key or record is None
                or record.state is RiskActionState.RELEASED):
            self._risk_action_key = key
            self._risk_action_id = ledger.next_action_id()
        expected = conservative_plain_fall_damage_points(max(
            0.0, action.start_surface.position[1]
            - action.end_surface.position[1],
        ))
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
        self, route_control: RouteControl, decision: ActionRouteDecision,
        frame: NavigationFrame, state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None,
    ) -> tuple[ActionRouteDecision, str | None]:
        status, action_id = self._reserve_route_risk(route_control, decision)
        if status in {None, RiskReservationStatus.RESERVED,
                      RiskReservationStatus.EXISTING}:
            return decision, action_id
        self._risk_failure_reason = f"risk_{status.value}"
        route_control.request_stop(StopCause.CANCELLED)
        self._transition(NavigationTransitionAction.BEGIN_STOPPING, self._risk_failure_reason)
        # The incumbent still owns the body. Its cancel path must provide the
        # protective input until Runtime verifies that it can be retired.
        return route_control.executor.decide(
            frame, state_anchor=state_anchor, input_ledger=input_ledger,
        ), None

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
        self, frame: NavigationFrame, *, through_grounded_departure: bool = False,
    ) -> ActionPreconditionResult | None:
        route = self._active_route
        executor = self._executor
        if (route is None or executor is None
                or self._state is NavigationSessionState.STOPPING):
            return None
        action_index = getattr(executor, "action_index", None)
        if (type(action_index) is not int
                or not 0 <= action_index < len(route.action_route.actions)):
            return None
        action = route.action_route.actions[action_index]
        if (hasattr(executor, "current_verified_action_started")
                and executor.current_verified_action_started()):
            # Entry evidence is checked once, before the first verified
            # command is submitted.  Once airborne, the executor owns landing
            # and handles late or missing receipts without restarting
            # acquisition.  A controlled drop that is still grounded is the
            # exception: a changed landing support can still be answered by
            # stopping before departure, so that dependency remains live.
            still_on_departure_support = (
                type(action) is ControlledDropSegment
                and frame.body.is_on_ground
                and abs(
                    frame.body.position[1]
                    - action.start_surface.position[1]
                ) <= .25
            )
            if (not through_grounded_departure
                    or not still_on_departure_support):
                return None
        task_id = (
            self._retry_ledger.task_id
            if self._retry_ledger is not None else route.goal_id
        )
        return check_action_precondition(
            route, action_index, frame,
            task_id=task_id, edge_probe=self._edge_probe,
            acquisition_grant=self._completed_acquisition,
        )

    def _enter_pending_action_boundary(self, frame: NavigationFrame) -> None:
        executor = self._executor
        route = None if executor is None else getattr(executor, "route", None)
        if executor is None or route is None:
            return
        index = executor.action_index
        if not 0 <= index + 1 < len(route.actions):
            return
        upcoming = route.actions[index + 1]
        if (type(upcoming) is ControlledDropSegment
                and upcoming.start_surface.position[1]
                    - upcoming.end_surface.position[1] > 1.0 + 1.0e-6):
            executor.enter_upcoming_action_boundary(index + 1, frame)

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
        )
        self._snapshot_missing = result.missing_cells
        self._transition(NavigationTransitionAction.WAIT_FOR_INFORMATION, result.reason)
        if self._retry_ledger is not None:
            self._retry_ledger.end_wait("information")
        return True

    def _resolve_pending_retry(self, frame: NavigationFrame) -> None:
        pending = self._pending_retry
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
        self._pending_retry = None
        attempt_id, verdict, reason, missing = pending
        if verdict is RetryVerdict.RETRY:
            self._reissue_request_from_current(
                frame,
                reason,
                permit_kind=PlanningAttemptPermitKind.RETRY,
                source_event_id=attempt_id,
            )
        else:
            self._transition(NavigationTransitionAction.MARK_FAILED, 'replan_retry_exhausted')
            self._snapshot_missing = missing

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
        route = self._active_route
        frame.world.set_protection(
            frame.body.position,
            () if route is None else route.action_route.dependencies,
        )
        if self._planning_coordinator is not None:
            self._planning_coordinator.observe_changes(changed_cells)
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
                if planning.request is not None:
                    self._request = planning.request
                if update.kind is PlanningUpdateKind.FAILED:
                    assert update.failure is not None
                    self._fail_planning_or_preserve_incumbent(
                        update.failure.reason,
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
                    retry_cause=RetryCause.DEPENDENCY,
                )
            return
        visible_information_results = tuple(
            result for result in frame.air_query_results
            if result.status == "visible_air"
            and result.position in set(self._snapshot_missing)
        )
        probe_acquired_evidence = False
        landing_acquisition_pending = self._landing_acquisition_pending()
        if (landing_acquisition_pending
                and self._edge_probe is not None
                and self._edge_probe.state
                    is LandingEdgeProbeState.HOLDING_EDGE):
            for result in visible_information_results:
                if self._edge_probe.has_observed_evidence(
                        frame, result.position):
                    self._edge_probe.begin_entry_alignment(frame)
                    probe_acquired_evidence = True
                    break
        if landing_acquisition_pending:
            visible_information_results = tuple(
                result for result in visible_information_results
                if direct_drop_visual_evidence_sufficient(
                    frame, result.position, edge_probe=self._edge_probe,
                )
            )
            if probe_acquired_evidence:
                # Move sideways from the view corner to the controlled-drop
                # entry line before this evidence can authorize a route.
                visible_information_results = ()
        changed_information = set(changed_cells).intersection(
            self._snapshot_missing
        )
        if landing_acquisition_pending:
            # Learning that the landing cell is air does not by itself make a
            # direct drop executable.  The edge probe must first own fresh
            # lower-region evidence, return to the stable entry line and
            # release sneak.  A solid block is different: it invalidates the
            # candidate immediately and should be replanned without waiting
            # for the probe lifecycle.
            changed_information = {
                position for position in changed_information
                if (frame.world.cell(position).knowledge
                    is CellKnowledge.BLOCK)
                or direct_drop_visual_evidence_sufficient(
                    frame, position, edge_probe=self._edge_probe,
                )
            }
        planning_need = (
            None if self._planning_coordinator is None
            else self._planning_coordinator.current_information_need
        )
        acquired_blocker = None
        if planning_need is not None:
            assert self._planning_coordinator is not None
            acquired_blocker = next((
                blocker for blocker in planning_need.blockers
                if self._planning_coordinator.information_fact_is_acquired(
                    blocker,
                    frame,
                    edge_probe=self._edge_probe,
                )
            ), None)
        information_updated = bool(
            changed_information
            or visible_information_results
            or acquired_blocker is not None
        )
        coordinator_progressed = False
        if planning_need is not None:
            assert self._planning_coordinator is not None
            outcome = self._planning_coordinator.reconcile_information(
                self._planning_coordinator.current_information_update, frame,
                edge_probe=self._edge_probe,
            )
            coordinator_progressed = outcome.kind is PlanningUpdateKind.INFORMATION_ACQUIRED
            if outcome.kind is PlanningUpdateKind.FAILED:
                self._retry_ledger.end_wait("information")
                self._fail_planning_or_preserve_incumbent(outcome.failure.reason)
                return
            # Sensor activity alone is not a planning progress permit.
            information_updated = coordinator_progressed
        elif information_updated and self._retry_ledger is not None:
            progressed = False
            for position in sorted(changed_information | {
                result.position for result in visible_information_results
            }):
                progressed |= self._retry_ledger.record_progress(ProgressEvidence(
                    ProgressKind.BLOCKING_FACT, frame.body.sequence_id,
                    fact_id=self._cell_fact_id(position),
                ))
            if progressed:
                self._retry_ledger.end_wait("information")
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
                    _, goal_missing = self._surface_for_goal(
                        frame, pending.goal_state,
                    )
                    _, start_missing = self._surface_for_body(frame)
                    self._snapshot_missing = tuple(sorted(
                        set(goal_missing) | set(start_missing)
                    ))
                else:
                    self._resume_pending_goal(frame)
            elif self._request is not None:
                if coordinator_progressed:
                    update = self._planning_coordinator.resume_after_information(
                        frame,
                        remaining_damage_budget=self._remaining_damage_budget(),
                    )
                    assert self._planning_coordinator.request is not None
                    self._request = self._planning_coordinator.request
                    self._snapshot_missing = ()
                    if self._retry_ledger is not None:
                        self._retry_ledger.end_wait("information")
                    self._transition(
                        NavigationTransitionAction.BEGIN_PLANNING,
                        update.reason,
                    )
                else:
                    self._reissue_request_from_current(
                        frame, "planning_information_updated",
                    )
        route = self._active_route
        if route is not None and set(route.action_route.dependencies).intersection(changed_cells):
            departure = self._current_action_precondition(
                frame, through_grounded_departure=True,
            )
            if (departure is not None
                    and departure.status is ActionPreconditionStatus.REJECTED):
                self._risk_failure_reason = departure.reason.value
                route_control = self._supervisor.route
                assert route_control is not None
                route_control.request_stop(StopCause.DEPENDENCY_CHANGED)
                if self._edge_probe is not None and self._edge_probe.owned:
                    self._request_probe_stop(
                        StopCause.DEPENDENCY_CHANGED,
                        terminal=NavigationSessionState.FAILED,
                        terminal_reason=departure.reason.value,
                    )
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    departure.reason.value,
                )
                return
            self._reissue_request_from_current(
                self._frame,
                "active_route_dependency_changed",
                retry_cause=RetryCause.DEPENDENCY,
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
        self.observe(frame, frame.changed_cells)
        self._motion_result_poll_sequence += 1
        self._control_ledger = input_ledger
        self._control_anchor = state_anchor
        self._commit_applied_risk(frame, input_ledger)
        if self._executor is None:
            self._close_finished_risk(frame, None)
        if (self._edge_probe is not None
                and self._edge_probe.state is LandingEdgeProbeState.STOPPING):
            decision = self._supervisor.decide_probe(
                frame, input_ledger, state_anchor,
            )
            if decision.handoff.disposition is HandoffDisposition.QUIESCENT:
                if self._retry_ledger is not None:
                    self._retry_ledger.end_wait("recovery")
                self._recovery_wait_status = None
                stop_cause = self._edge_probe.stop_cause
                retired_probe = self._edge_probe
                self._supervisor.retire_quiescent_probe(frame)
                self._end_probe_waits(retired_probe)
                terminal = self._pending_probe_terminal
                terminal_reason = self._pending_probe_terminal_reason
                self._pending_probe_stop_cause = None
                if terminal is not None:
                    # S3 can pause an admitted route at the drop boundary
                    # while the probe owns the body.  Once a cancellation or
                    # close finishes the probe's safe retreat, that suspended
                    # route must also receive the terminal stop.  Leaving it
                    # RUNNING prevents the driver from ever proving a
                    # quiescent release even though the body is safely back on
                    # support.
                    self._supervisor.discard_pending_route()
                    route_control = self._supervisor.incumbent_route
                    if route_control is not None:
                        route_control.request_stop(
                            stop_cause or StopCause.ACQUISITION_TIMED_OUT,
                        )
                        self._transition(
                            NavigationTransitionAction.BEGIN_STOPPING,
                            terminal_reason or terminal.value,
                        )
                    else:
                        self._finish_pending_probe_terminal(
                            terminal_reason or terminal.value,
                        )
                elif stop_cause is StopCause.ACQUISITION_TIMED_OUT:
                    route_control = self._supervisor.route
                    if route_control is not None:
                        route_control.request_stop(StopCause.CANCELLED)
                    self._transition(
                        NavigationTransitionAction.MARK_FAILED,
                        "edge_probe_acquisition_timeout",
                    )
                elif self._pending_retry is not None:
                    # The probe and its suspended route share one body
                    # responsibility. Consume the registered retry only
                    # after the probe has safely retired.
                    self._resolve_pending_retry(frame)
                elif (self._active_route is not None
                      and self._request is not None
                      and self._active_route.source_request_id
                          != self._request.request_id):
                    # A replacement has already failed or is waiting for the
                    # incumbent route to release. Retiring the probe is only
                    # the first half of that handoff; the route branch below
                    # owns the final failure/replan decision.
                    self._reason = "replacement_route_waiting_for_release"
                else:
                    self._probe_mode_exit_pending = True
                    if self._supervisor.has_pending_route:
                        self._reason = "successor_route_waiting_for_motion"
                    else:
                        self._reissue_request_from_current(
                            frame, "edge_probe_stopped_for_replan",
                        )
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
        if self._probe_mode_exit_pending:
            if frame.body.is_sneaking or frame.body.pose == "crouching":
                self._reason = (
                    "successor_route_waiting_for_motion"
                    if self._supervisor.has_pending_route else
                    "edge_probe_mode_exit_pending"
                )
                return self._proposal(MovementV1(), None, 1, deadline_ns)
            self._probe_mode_exit_pending = False
        if (self._state is NavigationSessionState.STOPPING
                and self._pending_goal is not None
                and not self._has_body_owner()):
            handoff = self._supervisor.evaluate_quiescence(
                frame, input_ledger, state_anchor,
            )
            if handoff.disposition is HandoffDisposition.QUIESCENT:
                self._transition(
                    NavigationTransitionAction.REPLAN_AFTER_HANDOFF,
                    "goal_revision_body_released",
                    handoff=handoff,
                )
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
            if not self._edge_probe.finish_release(frame):
                return self._proposal(
                    release_movement,
                    self._edge_probe.release_look(frame) or LookV1(0.0, 0.0),
                    1, deadline_ns, safety_guard=True,
                )
            if (self._active_route is not None
                    and self._executor is not None
                    and self._edge_probe.belongs_to_action(
                        self._active_route.route_id,
                        self._active_route.route_revision,
                        self._executor.action_index,
                    )):
                probe = self._edge_probe
                if (probe.acquisition_id is not None
                        and probe.route_id is not None
                        and probe.route_revision is not None
                        and probe.action_index is not None
                        and probe.evidence_sequence_id is not None):
                    self._completed_acquisition = AcquisitionGrant(
                        probe.acquisition_id,
                        probe.route_id,
                        probe.route_revision,
                        probe.action_index,
                        probe.landing_cell,
                        probe.evidence_sequence_id,
                        probe.dependencies,
                    )
                if self._state is NavigationSessionState.STOPPING:
                    # The probe has finished positioning, but the route does
                    # not own the body until one of its commands actually wins
                    # Runtime arbitration.  Keep STOPPING until that receipt
                    # creates transferable handoff evidence.
                    self._reason = "action_acquisition_waiting_for_selected_command"
                else:
                    self._transition(
                        NavigationTransitionAction.BEGIN_EXECUTION,
                        "action_acquisition_complete",
                    )
                if self._retry_ledger is not None:
                    wait_id = self._edge_probe.acquisition_id
                    if wait_id is not None:
                        self._retry_ledger.end_wait(wait_id)
            else:
                self._reissue_request_from_current(
                    frame, "landing_edge_probe_complete",
                )
        if (self._local_goal_request_id is not None
                and self._executor is None
                and not self._task_stop_requested()):
            self._activate_local_route(frame, state_anchor, input_ledger)
        if not self._task_stop_requested():
            self._advance_planning(frame, state_anchor, input_ledger)
        if self._active_route is None:
            if (self._state is NavigationSessionState.STOPPING
                    and self._task_stop_requested()):
                self._transition(
                    NavigationTransitionAction.MARK_CANCELLED,
                    self._cancel_reason or "cancelled",
                )
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

        assert self._executor is not None
        active_route = self._active_route
        route_is_superseded = (
            active_route is not None
            and self._request is not None
            and active_route.goal_revision != self._request.goal_revision
        )
        if not route_is_superseded:
            self._enter_pending_action_boundary(frame)
        executor_route = getattr(self._executor, "route", None)
        current_executor_action = (
            None if executor_route is None
            or not 0 <= self._executor.action_index < len(executor_route.actions)
            else executor_route.actions[self._executor.action_index]
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
            )
        )
        if precondition is not None:
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
                self._transition(NavigationTransitionAction.WAIT_FOR_INFORMATION, precondition.reason)
                self._snapshot_missing = precondition.missing_cells
                return self._proposal(
                    (MovementV1(sneak=True)
                     if frame.body.is_on_ground
                     and type(current_executor_action) is ControlledDropSegment
                     else MovementV1()),
                    None, 1, deadline_ns,
                    information_look=self._information_look(frame),
                    safety_guard=(frame.body.is_on_ground
                                  and type(current_executor_action)
                                      is ControlledDropSegment),
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
                self._supervisor.route.request_stop(StopCause.CANCELLED)
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    precondition.reason.value,
                )
                return self._proposal(MovementV1(), None, 1, deadline_ns)
        current_action = None
        executor_route = getattr(self._executor, "route", None)
        if (executor_route is not None
                and 0 <= self._executor.action_index < len(executor_route.actions)):
            current_action = executor_route.actions[self._executor.action_index]
        conditioned_ordinary_walk = (
            conditioned_yaw_delta_degrees is not None
            and type(current_action) is WalkSegment
            and (
                current_action.transition is None
                or current_action.transition.mode is MovementMode.WALK
            )
        )
        movement_yaw_radians = None
        if conditioned_ordinary_walk:
            yaw = frame.body.yaw_radians + math.radians(
                float(conditioned_yaw_delta_degrees)
            )
            movement_yaw_radians = math.atan2(math.sin(yaw), math.cos(yaw))
        if (self._coordinator is not None and state_anchor is not None
                and input_ledger is not None):
            decision = self._coordinator.decide(
                frame, state_anchor, input_ledger,
                PhysicsWorldView(frame.world, JAVA_1_21_RULESET),
                changed_cells=frame.changed_cells,
                movement_yaw_radians=movement_yaw_radians,
                allow_grounded_reprepare=(
                    self._state is not NavigationSessionState.STOPPING
                    and not self._supervisor.has_pending_route
                    and not self._task_stop_requested()
                    and not self._restart_after_active_terminal
                    and self._active_route is not None
                    and self._request is not None
                    and self._active_route.source_request_id
                        == self._request.request_id
                ),
                result_poll_sequence=self._motion_result_poll_sequence,
            )
        else:
            decision = self._executor.decide(
                frame, state_anchor=state_anchor, input_ledger=input_ledger,
                movement_yaw_radians=movement_yaw_radians,
            )
        if self._supervisor.has_pending_route and not decision.submit_input:
            successor_decision = decision
            incumbent = self._supervisor.incumbent_route
            assert incumbent is not None
            if decision.state in {
                ActionRouteState.FAILED, ActionRouteState.BLOCKED,
                ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
                ActionRouteState.NEEDS_REPLAN,
            }:
                self._supervisor.discard_pending_route()
                self._reissue_request_from_current(
                    frame,
                    "successor_route_entry_changed",
                    retry_cause=RetryCause.EXECUTION,
                )
            if incumbent.coordinator is not None and state_anchor is not None \
                    and input_ledger is not None:
                incumbent_decision = incumbent.coordinator.decide(
                    frame, state_anchor, input_ledger,
                    PhysicsWorldView(frame.world, JAVA_1_21_RULESET),
                    changed_cells=frame.changed_cells,
                    allow_grounded_reprepare=False,
                    result_poll_sequence=self._motion_result_poll_sequence,
                )
            else:
                incumbent_decision = incumbent.executor.decide(
                    frame, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                )
            incumbent_decision, risk_action_id = self._risk_checked_decision(
                incumbent, incumbent_decision, frame, state_anchor,
                input_ledger,
            )
            if incumbent_decision.state in {
                ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
                ActionRouteState.FAILED, ActionRouteState.BLOCKED,
                ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
                ActionRouteState.NEEDS_REPLAN,
            }:
                if self._edge_probe is not None and self._edge_probe.owned:
                    self._last_decision = successor_decision
                    self._close_finished_risk(frame, successor_decision)
                    self._request_probe_stop(StopCause.ROUTE_REPLACED)
                    return self._proposal(
                        self._probe_movement(frame), LookV1(0.0, 0.0),
                        1, deadline_ns, safety_guard=True,
                        retain_body_input=True,
                    )
                release = self._supervisor.incumbent_release_evidence(
                    frame, input_ledger, state_anchor,
                )
                if release.disposition is HandoffDisposition.QUIESCENT:
                    self._last_decision = successor_decision
                    self._close_finished_risk(frame, successor_decision)
                    self._transition(
                        NavigationTransitionAction.BEGIN_STOPPING,
                        "successor_route_waiting_for_motion",
                    )
                    return self._proposal(
                        MovementV1(),
                        successor_decision.look or LookV1(0.0, 0.0),
                        successor_decision.input_lease_ticks, deadline_ns,
                        safety_guard=True, retain_body_input=True,
                    )
            self._last_decision = incumbent_decision
            self._close_finished_risk(frame, incumbent_decision)
            if self._risk_failure_reason is None:
                self._continue_execution(
                    "executing_safe_prefix_during_handoff",
                )
            return self._proposal(
                incumbent_decision.movement if incumbent_decision.submit_input
                else MovementV1(), incumbent_decision.look,
                incumbent_decision.input_lease_ticks, deadline_ns,
                route_decision=incumbent_decision,
                route_control=incumbent,
                risk_action_id=risk_action_id,
            )
        route_control = self._supervisor.route
        risk_action_id = None
        if route_control is not None:
            decision, risk_action_id = self._risk_checked_decision(
                route_control, decision, frame, state_anchor, input_ledger,
            )
        self._last_decision = decision
        self._close_finished_risk(frame, decision)
        self._record_completed_movement_damage()
        self._record_retry_route_progress(frame, decision)
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
            if decision.state in {
                ActionRouteState.CANCELLED, ActionRouteState.COMPLETE,
            }:
                if self._clear_active_execution():
                    if self._pending_probe_terminal is not None:
                        self._finish_pending_probe_terminal(
                            decision.reason_code,
                        )
                    elif self._close_requested:
                        self._transition(
                            NavigationTransitionAction.MARK_CLOSED,
                            "closed",
                        )
                    else:
                        self._transition(
                            (NavigationTransitionAction.MARK_FAILED
                             if self._risk_failure_reason is not None else
                             NavigationTransitionAction.MARK_CANCELLED),
                            (self._risk_failure_reason or self._cancel_reason
                             or decision.reason_code),
                        )
                    self._risk_failure_reason = None
                else:
                    self._reason = "route_release_waiting_for_evidence"
            elif decision.state in {
                ActionRouteState.FAILED, ActionRouteState.BLOCKED,
                ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
            }:
                if self._clear_active_execution():
                    if self._pending_probe_terminal is not None:
                        self._finish_pending_probe_terminal(
                            decision.reason_code,
                        )
                    else:
                        self._transition(
                            (NavigationTransitionAction.MARK_CLOSED
                             if self._close_requested else
                             NavigationTransitionAction.MARK_FAILED
                             if (self._risk_failure_reason is not None
                                 or self._cancel_reason is None) else
                             NavigationTransitionAction.MARK_CANCELLED),
                            ("closed" if self._close_requested else
                             self._risk_failure_reason or self._cancel_reason
                             or decision.reason_code),
                        )
                    self._risk_failure_reason = None
                else:
                    self._reason = "route_release_waiting_for_evidence"
            else:
                self._transition(NavigationTransitionAction.BEGIN_STOPPING, decision.reason_code)
        elif decision.state is ActionRouteState.INPUT_LOST:
            if self._clear_active_execution():
                self._transition(NavigationTransitionAction.MARK_FAILED, decision.reason_code)
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
        elif (self._restart_after_active_terminal
                and decision.state in terminal_decisions):
            if self._clear_active_execution():
                self._restart_after_active_terminal = False
                if self._pending_goal is not None:
                    handoff = self._supervisor.last_handoff
                    if self._state is NavigationSessionState.STOPPING:
                        self._transition(
                            NavigationTransitionAction.REPLAN_AFTER_HANDOFF,
                            "goal_revision_body_released",
                            handoff=handoff,
                        )
                    self._resume_pending_goal(frame)
                else:
                    self._reissue_request_from_current(
                        frame, "route_handoff_reanchored",
                    )
            else:
                if (self._edge_probe is not None
                        and self._edge_probe.owned
                        and self._request_probe_stop(
                            StopCause.ROUTE_REPLACED,
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
        elif active_request_id != current_request_id:
            if decision.state in terminal_decisions:
                if self._clear_active_execution():
                    replacement_failure = (
                        None if self._planning_coordinator is None else
                        self._planning_coordinator.consume_replacement_failure()
                    )
                    if replacement_failure is not None:
                        self._transition(
                            NavigationTransitionAction.MARK_FAILED,
                            replacement_failure,
                        )
                    else:
                        # The replacement candidate may have been consumed
                        # while the incumbent still owned the body.  Start a
                        # fresh request from the observed release position;
                        # merely changing the lifecycle state would leave no
                        # snapshot, job, or result capable of making progress.
                        self._reissue_request_from_current(
                            frame, "replacement_route_reanchored",
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
            registration = self._retry_ledger.record_failure(
                attempt_id, RetryCause.EXECUTION,
            )
            if registration.first_seen:
                self._pending_retry = (
                    attempt_id, registration.verdict,
                    decision.reason_code, decision.missing_cells,
                )
            if self._pending_retry is None:
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
        if (self._state is NavigationSessionState.STOPPING
                and (self._task_stop_requested()
                     or self._restart_after_active_terminal)
                and self._executor is not None
                and self._edge_probe is None):
            self._check_recovery_wait(frame)
            if (self._recovery_wait_capacity_exhausted
                    or self._recovery_wait_status in {
                        WaitVerdict.EXHAUSTED_TICKS,
                        WaitVerdict.EXHAUSTED_CLOCK,
                    }):
                self._reason = "recovery_unresolved"
        elif (self._state in {
                NavigationSessionState.FAILED,
                NavigationSessionState.CANCELLED,
                NavigationSessionState.COMPLETE,
                NavigationSessionState.CLOSED,
              } and self._retry_ledger is not None):
            self._retry_ledger.end_wait("recovery")
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
                route_control.request_stop(StopCause.CANCELLED)
                self._transition(NavigationTransitionAction.BEGIN_STOPPING, self._risk_failure_reason)
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
                and self._active_route is not None and self._frame is not None
                and proposal.report.route_id == self._active_route.route_id
                and proposal.report.goal_revision
                    == self._active_route.goal_revision
                and decision.submit_input
                and decision.movement != MovementV1()):
            transferred_probe = self._edge_probe
            handoff = self._supervisor.transfer_probe_to_route(
                self._frame, route_id=self._active_route.route_id,
                route_revision=self._active_route.route_revision,
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
            )

    def cancel(self, reason: str) -> None:
        if type(reason) is not str or not reason.strip():
            raise ContractViolation("navigation cancellation reason is required")
        if self._closed:
            raise ContractViolation("navigation session is closed")
        self._admit_command_event(NavigationSessionEvent.CANCEL)
        self._supervisor.request_route_stop(StopCause.CANCELLED)
        if self._planning_coordinator is not None:
            self._planning_coordinator.cancel_work("session_cancelled")
        if self._request_probe_stop(
            StopCause.CANCELLED, terminal=NavigationSessionState.CANCELLED,
            terminal_reason=reason.strip(),
        ):
            self._cancel_reason = reason.strip()
            return
        if self._executor is not None:
            self._supervisor.route.request_stop(StopCause.CANCELLED)
            self._cancel_reason = reason.strip()
            self._transition(
                NavigationTransitionAction.BEGIN_STOPPING,
                "cancellation_requested",
            )
            if self._planning_coordinator is not None:
                self._planning_coordinator.cancel_work()
            return
        self._cancel_reason = reason.strip()
        self._transition(
            NavigationTransitionAction.MARK_CANCELLED,
            self._cancel_reason,
        )
        self._required_interaction = None
        self._interaction_approach_pending = False

    def close(self) -> None:
        if self._closed:
            return
        self._admit_command_event(NavigationSessionEvent.CLOSE)
        self._supervisor.request_route_stop(StopCause.CLOSED)
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
        if self._request_probe_stop(
            StopCause.CLOSED, terminal=NavigationSessionState.CLOSED,
            terminal_reason="closed",
        ):
            return
        if self._executor is not None:
            self._supervisor.route.request_stop(StopCause.CANCELLED)
            self._transition(NavigationTransitionAction.BEGIN_STOPPING, 'closing_after_body_control')
            return
        self._finalize_close()

    def spawn_successor(self, session_id: str) -> "NavigationSession":
        """Move reusable workers into a fresh, independent goal lifecycle."""
        require_identifier(session_id, "successor navigation session id")
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
            retry_ledger=self._retry_ledger,
            risk_ledger=self._risk_ledger,
        )
        successor._owns_motion_worker = self._owns_motion_worker
        successor._execution_instances = self._execution_instances
        successor._motion_result_inbox = self._motion_result_inbox
        successor._motion_result_poll_sequence = self._motion_result_poll_sequence
        successor._bridge_remaining = self._bridge_remaining
        self._owns_planner_worker = False
        self._owns_motion_worker = False
        self._finalize_close()
        return successor

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
    ) -> None:
        if (type(request) is SurfacePlanningRequest
                and surface_search_need(request)
                    is SurfaceSearchNeed.SAME_SUPPORT_LOCAL_GOAL):
            self._accept_goal_request(
                request, frame, reason,
                preserve_active_route=preserve_active_route,
            )
            return
        self._local_goal_request_id = None
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
        self._information_statuses.clear()
        self._information_lower_required.clear()
        self._required_interaction = None
        self._interaction_approach_pending = False
        coordinator = self._ensure_planning_coordinator()
        coordinator.begin(
            request,
            frame,
            permit=PlanningAttemptPermit(
                f"{request.request_id}/task-update",
                self._retry_ledger.task_id,
                request.goal_revision,
                f"request/{request.request_id}",
                PlanningAttemptPermitKind.TASK_UPDATE,
            ),
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

    def _fail_planning_or_preserve_incumbent(self, reason: str) -> None:
        """Keep an older body owner until its safe terminal is observed."""
        if self._planning_coordinator is not None:
            self._planning_coordinator.cancel_work(
                "planning_failure_or_incumbent_preserved",
            )
        current = self._request
        active = self._active_route
        if (current is not None and active is not None
                and active.source_request_id != current.request_id):
            assert self._planning_coordinator is not None
            self._planning_coordinator.preserve_replacement_failure(reason)
            route_control = self._supervisor.incumbent_route
            if route_control is not None:
                route_control.request_stop(StopCause.MOTION_UNSOLVABLE)
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    "replacement_failure_waits_for_body_release",
                )
            return
        self._transition(NavigationTransitionAction.MARK_FAILED, reason)

    def _accept_goal_request(
        self, request: SurfacePlanningRequest, frame: NavigationFrame,
        reason: str, *, preserve_active_route: bool = False,
    ) -> None:
        if surface_search_need(request) is SurfaceSearchNeed.GRAPH_SEARCH:
            self._replace_request(
                request, frame, reason,
                preserve_active_route=preserve_active_route,
            )
            return
        # The support graph has no movement edge to execute here.  Keep any
        # previous owner alive until its cancellation reaches a safe terminal.
        self._request = request
        self._frame = frame
        self._pending_goal = None
        self._local_goal_request_id = request.request_id
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
        center = (
            (goal.region.min_x + goal.region.max_x) / 2,
            (goal.region.min_y + goal.region.max_y) / 2,
            (goal.region.min_z + goal.region.max_z) / 2,
        )
        dx = center[0] - frame.body.position[0]
        dz = center[2] - frame.body.position[2]
        movement = sweep(frame.body.body_box, (dx, 0.0, dz), frame.world)
        landing = query_support(
            frame.body.body_box.moved(dx, 0.0, dz), frame.world,
        )
        dependencies = tuple(sorted(set(movement.dependencies) | set(landing.dependencies)))
        if (movement.status is QueryStatus.NEEDS_INFORMATION
                or landing.status is QueryStatus.NEEDS_INFORMATION):
            self._transition(
                NavigationTransitionAction.WAIT_FOR_INFORMATION,
                "same_support_local_requires_information",
            )
            self._snapshot_missing = tuple(sorted(
                set(movement.missing_cells) | set(landing.missing_cells)
            ))
            return
        if (movement.status is not QueryStatus.FEASIBLE
                or landing.status is not QueryStatus.FEASIBLE):
            self._transition(NavigationTransitionAction.MARK_FAILED, 'same_support_local_path_unavailable')
            return
        route_id = f"{request.request_id}-local"
        fixed = FixedRoute(route_id, (
            RoutePoint(*frame.body.position), RoutePoint(*center),
        ))
        action_route = ActionRoute(
            route_id, (WalkSegment(fixed, (request.start,), dependencies),),
            goal, ResourceState((("food_points", float(frame.body.food_points)),)),
        )
        active_route = ActiveRoute(
            route_id, 1, request.request_id, request.goal_id,
            request.goal_revision, request.world_session, fixed,
            math.hypot(dx, dz), 0.0, dependencies,
            ExecutableCorridor(
                (request.start,), dependencies, math.hypot(dx, dz), request.goal,
            ), action_route, goal, request.sequence,
        )
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
        self._continue_execution('same_support_local_route_started')
        self._snapshot_missing = ()
    def _clear_active_execution(self) -> bool:
        if self._frame is None and self._supervisor.route is not None:
            return False
        if (self._frame is not None and not self._supervisor.retire_route(
                self._frame, self._control_ledger, self._control_anchor,
        )):
            return False
        self._last_decision = None
        self._executor_reported_damage_points = 0.0
        return True

    def _request_probe_stop(
        self, cause: StopCause, *,
        terminal: NavigationSessionState | None = None,
        terminal_reason: str | None = None,
    ) -> bool:
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
        self._pending_probe_stop_cause = cause
        if terminal is not None:
            self._pending_probe_terminal = terminal
            self._pending_probe_terminal_reason = terminal_reason
        self._transition(NavigationTransitionAction.BEGIN_STOPPING, 'edge_probe_stopping')
        return True

    def _has_body_owner(self) -> bool:
        return (
            (self._edge_probe is not None and self._edge_probe.owned)
            or self._supervisor.incumbent_route is not None
            or self._supervisor.has_pending_route
        )

    def _task_stop_requested(self) -> bool:
        return (
            self._cancel_reason is not None
            or self._close_requested
            or self._risk_failure_reason is not None
            or self._pending_probe_terminal is not None
        )

    def _finish_pending_probe_terminal(self, fallback_reason: str) -> None:
        """Publish a deferred terminal only after every body owner has left."""
        terminal = self._pending_probe_terminal
        reason = self._pending_probe_terminal_reason or fallback_reason
        terminal_action = {
            NavigationSessionState.COMPLETE:
                NavigationTransitionAction.MARK_COMPLETE,
            NavigationSessionState.CANCELLED:
                NavigationTransitionAction.MARK_CANCELLED,
            NavigationSessionState.FAILED:
                NavigationTransitionAction.MARK_FAILED,
            NavigationSessionState.CLOSED:
                NavigationTransitionAction.MARK_CLOSED,
        }.get(terminal)
        if terminal_action is None:
            raise ContractViolation("pending probe terminal state must be terminal")
        self._pending_probe_terminal = None
        self._pending_probe_terminal_reason = None
        self._transition(terminal_action, reason)
        if terminal is NavigationSessionState.CLOSED:
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
            self._restart_after_active_terminal = True
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
            )
            return
        goal_node, goal_missing = self._surface_for_goal(
            frame, pending.goal_state,
        )
        start_node, start_missing = self._surface_for_body(frame)
        missing = tuple(sorted(set(goal_missing) | set(start_missing)))
        if self._handoff.stop_request is not None:
            handoff = self._supervisor.last_handoff
            resolution = self._handoff.resolve_goal(
                handoff,
                goal_ready=goal_node is not None,
                start_ready=start_node is not None,
                missing_cells=missing,
                unavailable_reason=(
                    "goal_surface_unavailable" if goal_node is None
                    else "current_surface_unavailable"
                ),
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
        else:
            self._handoff.clear_goal()
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
            self._transition(
                NavigationTransitionAction.MARK_FAILED,
                ("goal_surface_unavailable" if goal_node is None else
                 "current_surface_unavailable"),
            )
            return
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
        self._accept_goal_request(request, frame, "pending_goal_committed")

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
        movement_tick = (frame.body.movement_tick_id
                         if frame.body.movement_tick_id is not None
                         else frame.body.sequence_id)
        cell = probe.landing_cell
        wait_id = (
            probe.acquisition_id
            or f"acquisition/{cell[0]}/{cell[1]}/{cell[2]}"
        )
        now_ns = self._clock()
        try:
            ledger.begin_wait(
                wait_id, probe.owner_id,
                WaitPolicy(DIRECT_DROP_EDGE_PROBE_MAX_FRAMES,
                           _INFORMATION_WAIT_LIMIT_NS),
                movement_tick, now_ns,
            )
            expired = ledger.check_wait(
                wait_id, movement_tick, now_ns,
            ) is not WaitVerdict.WAITING
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
        self._restart_after_active_terminal = True
        self._snapshot_missing = missing
        self._continue_execution(reason)
        return True

    def _reissue_request_from_current(
        self,
        frame: NavigationFrame,
        reason: str,
        *,
        permit_kind: PlanningAttemptPermitKind = (
            PlanningAttemptPermitKind.PROGRESS
        ),
        source_event_id: str | None = None,
        retry_cause: RetryCause | None = None,
    ) -> None:
        request = self._request
        if request is None:
            return
        planning = self._ensure_planning_coordinator()
        assert self._retry_ledger is not None
        source = source_event_id or (
            f"{reason}/{request.request_id}/{frame.body.sequence_id}"
        )
        if retry_cause is not None:
            update = planning.retry_from_current(
                frame,
                cause=retry_cause,
                failure_id=source,
                remaining_damage_budget=self._remaining_damage_budget(),
            )
        else:
            update = planning.restart_from_current(
                frame,
                permit=PlanningAttemptPermit(
                    f"{source}/permit",
                    self._retry_ledger.task_id,
                    request.goal_revision,
                    source,
                    permit_kind,
                ),
                remaining_damage_budget=self._remaining_damage_budget(),
            )
        if planning.request is not None:
            self._request = planning.request
            request = planning.request
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
            self._fail_planning_or_preserve_incumbent(update.failure.reason)
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
    ) -> None:
        # Body handoff remains the session's responsibility. Planning may run
        # behind an incumbent, but cannot reopen the lifecycle while that owner
        # is proving a safe release.
        if self._state is NavigationSessionState.STOPPING:
            return
        request = self._request
        if request is None or self._state is NavigationSessionState.NEEDS_INFORMATION:
            return
        if self._local_goal_request_id == request.request_id:
            return
        if (self._active_route is not None
                and self._active_route.source_request_id == request.request_id):
            return
        planning = self._ensure_planning_coordinator()
        update = planning.advance(
            frame,
            state_anchor=state_anchor,
            edge_probe=self._edge_probe,
            remaining_damage_budget=self._remaining_damage_budget(),
        )
        if planning.request is not None and planning.request is not self._request:
            self._request = planning.request
            request = planning.request
        if update.kind is PlanningUpdateKind.RUNNING:
            self._transition(
                NavigationTransitionAction.BEGIN_PLANNING,
                update.reason,
            )
            return
        if update.kind is PlanningUpdateKind.NEEDS_INFORMATION:
            assert update.information_need is not None
            self._snapshot_missing = tuple(dict.fromkeys(
                blocker.position
                for blocker in update.information_need.blockers
            ))[:64]
            self._transition(
                NavigationTransitionAction.WAIT_FOR_INFORMATION,
                update.reason,
            )
            if (update.reason == "landing_visual_evidence_missing"
                    and len(self._snapshot_missing) == 1):
                landing_cell = self._snapshot_missing[0]
                if (self._edge_probe is None
                        or not self._edge_probe.belongs_to(
                            request.goal_id, request.goal_revision,
                        )
                        or self._edge_probe.landing_cell != landing_cell):
                    if self._request_probe_stop(StopCause.ROUTE_REPLACED):
                        return
                    self._edge_probe = LandingEdgeProbe(
                        request.goal_id,
                        request.goal_revision,
                        landing_cell,
                        frame.body.sequence_id,
                    )
                    if self._retry_ledger is not None:
                        self._retry_ledger.end_wait("information")
            return
        if update.kind is PlanningUpdateKind.REQUIRES_INTERACTION:
            assert update.interaction is not None
            self._required_interaction = update.interaction
            self._interaction_approach_pending = False
            self._transition(
                NavigationTransitionAction.REQUIRE_INTERACTION,
                update.reason,
            )
            return
        if update.kind is PlanningUpdateKind.FAILED:
            assert update.failure is not None
            self._fail_planning_or_preserve_incumbent(update.failure.reason)
            return
        assert update.kind is PlanningUpdateKind.ROUTE_READY
        assert update.route is not None
        self._required_interaction = update.interaction
        self._interaction_approach_pending = update.interaction is not None
        self._activate_planning_route(
            update.route, frame, state_anchor, input_ledger,
        )

    def _activate_planning_route(
        self,
        route: ActiveRoute,
        frame: NavigationFrame,
        state_anchor: StateAnchor | None,
        input_ledger: InputApplicationLedger | None,
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
                retry_cause=RetryCause.DEPENDENCY,
            )
            return
        if (self._active_route is not None
                and self._active_route.source_request_id != current.request_id):
            assert self._executor is not None
            if self._executor.requires_safe_handoff(frame):
                self._supervisor.route.request_stop(StopCause.CANCELLED)
                self._restart_after_active_terminal = True
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
        if any(type(action) in {
                   JumpGapSegment, JumpUpSegment, ControlledDropSegment}
               for action in route.action_route.actions):
            if self._motion_worker is None:
                self._motion_worker = MotionSolverWorker(max_pending=4)
                self._owns_motion_worker = True
            motion_coordinator = MotionRouteCoordinator(
                route, executor, self._motion_worker,
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
            incumbent = self._supervisor.incumbent_route
            if incumbent is not None:
                incumbent.request_stop(StopCause.ROUTE_REPLACED)
                self._restart_after_active_terminal = True
                self._transition(
                    NavigationTransitionAction.BEGIN_STOPPING,
                    "route_handoff_waiting_for_safe_terminal",
                )
            else:
                self._reissue_request_from_current(
                    frame, "route_handoff_reanchored",
                )
            return
        self._executor_reported_damage_points = 0.0
        if self._planning_coordinator is not None:
            self._planning_coordinator.clear_changes()
        self._snapshot_missing = ()
        self._continue_execution("route_admitted")

    def _apply_decision_state(self, decision: ActionRouteDecision) -> None:
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
    ) -> NavigationSessionProposal:
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
            if (route_decision is not None and not route_decision.submit_input
                    and not retain_body_input
                    and self._edge_probe is None):
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
                        and route_decision.verified_command_index is not None
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
        """Aim only at cells the formal surface sensor classified outside view."""
        if (self._state is not NavigationSessionState.NEEDS_INFORMATION
                or not self._snapshot_missing):
            return None
        missing = set(self._snapshot_missing)
        self._information_statuses = {
            position: status
            for position, status in self._information_statuses.items()
            if position in missing
        }
        self._information_lower_required.intersection_update(missing)
        for result in frame.air_query_results:
            if result.position not in missing:
                continue
            status = result.status
            if status == "visible_air":
                if not direct_drop_visual_evidence_sufficient(
                        frame, result.position, edge_probe=self._edge_probe):
                    self._information_lower_required.add(result.position)
                else:
                    self._information_lower_required.discard(result.position)
            elif status == "occluded" and self._landing_acquisition_pending():
                # A lower landing cell can be hidden by the current support
                # even while the player is already facing it.  Turning cannot
                # reveal that volume; a bounded sneak-to-edge probe can.
                self._information_lower_required.add(result.position)
            else:
                self._information_lower_required.discard(result.position)
            self._information_statuses[result.position] = status
        statuses = self._information_statuses
        information_look = information_look_for_missing_cells(
            frame, self._snapshot_missing, statuses,
            lower_region_positions=frozenset(self._information_lower_required),
        )
        unresolved = tuple(sorted(
            (position, statuses.get(position, "unknown"))
            for position in self._snapshot_missing
        ))
        if (unresolved and self._retry_ledger is not None
                and self._edge_probe is None):
            movement_tick = (frame.body.movement_tick_id
                             if frame.body.movement_tick_id is not None
                             else frame.body.sequence_id)
            now_ns = self._clock()
            capacity_exhausted = False
            try:
                planning_need = (
                    None if self._planning_coordinator is None
                    else self._planning_coordinator.current_information_need
                )
                if planning_need is None:
                    self._retry_ledger.set_blockers(tuple(
                        self._cell_fact_id(position)
                        for position, _ in unresolved
                    ))
                token = self._retry_ledger.begin_wait(
                    "information", self._information_wait_owner_id(),
                    WaitPolicy(_INFORMATION_WAIT_LIMIT_FRAMES,
                               _INFORMATION_WAIT_LIMIT_NS),
                    movement_tick, now_ns,
                )
                self._information_wait_frames = (
                    movement_tick - token.started_movement_tick
                )
                wait_result = self._retry_ledger.check_wait(
                    "information", movement_tick, now_ns,
                )
            except RetryLedgerCapacityExceeded:
                wait_result = WaitVerdict.EXHAUSTED_TICKS
                capacity_exhausted = True
            if wait_result is not WaitVerdict.WAITING:
                if (planning_need is not None
                        and self._planning_coordinator is not None):
                    outcome_update = self._planning_coordinator.reconcile_information(
                        self._planning_coordinator.current_information_update, frame, edge_probe=self._edge_probe,
                        outcomes=tuple((blocker.blocker_key, InformationOutcome.TIMED_OUT)
                                       for blocker in planning_need.blockers),
                    )
                    if (outcome_update is not None
                            and outcome_update.kind
                                is PlanningUpdateKind.NEEDS_INFORMATION):
                        assert outcome_update.information_need is not None
                        self._snapshot_missing = tuple(dict.fromkeys(
                            blocker.position
                            for blocker in outcome_update.information_need.blockers
                        ))[:64]
                        self._information_statuses.clear()
                        self._information_lower_required.clear()
                        self._retry_ledger.end_wait("information")
                        self._reason = outcome_update.reason
                        return None
                    if (outcome_update is not None
                            and outcome_update.kind is PlanningUpdateKind.FAILED):
                        assert outcome_update.failure is not None
                        self._retry_ledger.end_wait("information")
                        failure = (
                            "information_frontier_truncated" if planning_need.truncated
                            else "information_occluded_requires_observation_position"
                            if all(status == "occluded" for _, status in unresolved)
                            else "information_out_of_range"
                            if all(status == "out_of_range" for _, status in unresolved)
                            else "information_unavailable_timeout"
                        )
                        self._fail_planning_or_preserve_incumbent(failure)
                        return None
                failure = (
                    "information_occluded_requires_observation_position"
                    if all(status == "occluded" for _, status in unresolved)
                    else "information_out_of_range"
                    if all(status == "out_of_range" for _, status in unresolved)
                    else "information_unavailable_timeout"
                )
                if capacity_exhausted:
                    failure = "information_capacity_exhausted"
                if not self._request_probe_stop(
                        StopCause.INFORMATION_TIMED_OUT,
                        terminal=NavigationSessionState.FAILED,
                        terminal_reason=failure):
                    self._transition(NavigationTransitionAction.MARK_FAILED, failure)
        return information_look

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
        candidates = []
        missing: set[BlockPos] = set()
        max_x = math.floor(math.nextafter(goal.region.max_x, -math.inf))
        max_z = math.floor(math.nextafter(goal.region.max_z, -math.inf))
        for x in range(math.floor(goal.region.min_x), max_x + 1):
            for z in range(math.floor(goal.region.min_z), max_z + 1):
                result = query_support_surfaces(
                    frame.world, x, z,
                    goal.region.min_y, goal.region.max_y,
                )
                missing.update(result.missing_cells)
                for surface in result.surfaces:
                    target = standable_point_in_region(frame.world, surface, goal.region)
                    missing.update(target.missing_cells)
                    if target.status is QueryStatus.FEASIBLE:
                        candidates.append((surface, target.position))
        if not candidates:
            return None, tuple(sorted(missing))
        center = (
            (goal.region.min_x + goal.region.max_x) / 2.0,
            (goal.region.min_y + goal.region.max_y) / 2.0,
            (goal.region.min_z + goal.region.max_z) / 2.0,
        )
        return min(
            candidates,
            key=lambda item: math.dist(item[1], center),
        )[0].node_id, tuple(sorted(missing))
