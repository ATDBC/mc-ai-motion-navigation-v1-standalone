"""Online owner for planning, route admission and route execution.

The session never advances a backend. It turns the newest navigation frame
into an ordered control-frame proposal and keeps asynchronous results bound to
the request and goal revision that produced them.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
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
from mc2p.motion_nav.action_route import JumpGapSegment, WalkSegment
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision,
    ActionRouteExecutor,
    ActionRouteState,
)
from mc2p.motion_nav.air_motion import AirMotionProfile
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_modes import GroundModeProfiles
from mc2p.motion_nav.ground_modes import load_ground_mode_profiles
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.jump_up import JumpUpProfile, load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshot,
    KnownMapSnapshotBuilder,
    PlanningRequest,
    PlanningStatus,
    SnapshotBuildStatus,
    SurfacePlanningRequest,
    SurfacePlanningStatus,
    SurfaceRouteCandidate,
)
from mc2p.motion_nav.bridge_planner import (
    BridgeInteractionPlan, BridgePlacementPolicy,
    plan_next_bridge_interaction,
)
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.motion_solver import (
    DEFAULT_GAP_SOLVER_POLICY, GapSolverPolicy, load_gap_solver_policy,
)
from mc2p.motion_nav.motion_worker import MotionSolverWorker
from mc2p.motion_nav.movement_transition import (
    GoalState, MovementMode, ResourceState,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger, StateAnchor
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult, MotionResidualStatus, MotionResidualTracker,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.route_admission import (
    ActiveRoute, AdmissionReason,
    AdmissionStatus,
    RouteAdmitter,
)
from mc2p.motion_nav.runtime_adapter import (
    NavigationFrame,
    NavigationObservationAdapter,
    world_session_from_observation,
)
from mc2p.motion_nav.step_transition import StepProfile, load_step_profile
from mc2p.motion_nav.support_surfaces import SurfaceNodeId, query_support_surfaces
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge


class NavigationSessionState(StrEnum):
    READY = "ready"
    NEEDS_INFORMATION = "needs_information"
    SNAPSHOTTING = "snapshotting"
    PLANNING = "planning"
    REQUIRES_INTERACTION = "requires_interaction"
    EXECUTING = "executing"
    CANCELLING = "cancelling"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"


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
class NavigationSessionProposal:
    control_frame: ControlFrameProposalV1 | None
    report: NavigationSessionReport
    route_decision: ActionRouteDecision | None = None


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
    ) -> None: ...
    def update_goal(
        self, goal_id: str, goal_revision: int, goal_state: GoalState,
    ) -> None: ...
    def propose(
        self, frame: NavigationFrame, state_anchor: StateAnchor | None,
        deadline_ns: int, *, input_ledger: InputApplicationLedger | None = None,
        conditioned_yaw_delta_degrees: float | None = None,
        conditioned_look_intent_id: str | None = None,
    ) -> NavigationSessionProposal: ...
    def register_verified_submission(
        self, proposal: NavigationSessionProposal, *, control_sequence: int,
    ) -> None: ...
    def cancel(self, reason: str) -> None: ...


class NavigationSession:
    """Own one goal/request/route lifecycle while reading one world owner."""

    def __init__(
        self,
        session_id: str,
        profiles: NavigationSessionProfiles,
        *,
        planner_worker=None,
        owns_planner_worker: bool = True,
        motion_worker: MotionSolverWorker | None = None,
        observation_adapter: NavigationObservationAdapter | None = None,
        route_admitter: RouteAdmitter | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        snapshot_cells_per_step: int = 4096,
        planning_margin_cells: int = 1,
        bridge_policy: BridgePlacementPolicy | None = None,
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
        if type(snapshot_cells_per_step) is not int or snapshot_cells_per_step < 1:
            raise ContractViolation("snapshot step budget must be positive")
        if type(planning_margin_cells) is not int or not 0 <= planning_margin_cells <= 16:
            raise ContractViolation("planning margin must be within 0..16 cells")
        if bridge_policy is not None and type(bridge_policy) is not BridgePlacementPolicy:
            raise ContractViolation("navigation bridge policy must be typed")
        self.session_id = session_id
        self.profiles = profiles
        self._planner = planner_worker or PlannerWorker()
        self._owns_planner_worker = owns_planner_worker
        self._motion_worker = motion_worker
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
        self._state = NavigationSessionState.READY
        self._reason = "not_started"
        self._request: PlanningRequest | SurfacePlanningRequest | None = None
        self._frame: NavigationFrame | None = None
        self._snapshot_builder: KnownMapSnapshotBuilder | None = None
        self._planning_snapshot: KnownMapSnapshot | None = None
        self._planning_snapshot_request_id: str | None = None
        self._snapshot_missing: tuple[BlockPos, ...] = ()
        self._residual_missing: tuple[BlockPos, ...] = ()
        self._planning_changes: set[BlockPos] = set()
        self._required_interaction: BridgeInteractionPlan | None = None
        self._interaction_approach_pending = False
        self._active_route: ActiveRoute | None = None
        self._executor: ActionRouteExecutor | None = None
        self._coordinator: MotionRouteCoordinator | None = None
        self._last_decision: ActionRouteDecision | None = None
        self._pending_goal: tuple[str, int, GoalState] | None = None
        self._motion_residual = MotionResidualTracker()
        self._restart_after_active_terminal = False
        self._cancel_reason: str | None = None
        self._closed = False

    @property
    def required_interaction(self) -> BridgeInteractionPlan | None:
        return self._required_interaction

    @property
    def bridge_remaining(self) -> int:
        return self._bridge_remaining

    def confirm_required_interaction(self, interaction_id: str) -> None:
        """Consume one authorized placement after its transaction confirms it."""
        require_identifier(interaction_id, "confirmed interaction id")
        if (self._required_interaction is None
                or self._required_interaction.requirement.interaction_id != interaction_id
                or self._state is not NavigationSessionState.REQUIRES_INTERACTION):
            raise ContractViolation("navigation has no matching required interaction")
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
            (pending[0] if request is None and pending is not None
             else None if request is None else request.goal_id),
            (pending[1] if request is None and pending is not None
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
        if not missing:
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
        planning_request = ObservationRequestV3("navigation_v1")
        if planning_missing and remaining:
            planning_request, _ = self._adapter.air_request(
                planning_missing, max_positions=remaining,
            )
        return merge_observation_requests((
            residual_request, planning_request,
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
    ) -> None:
        """Resolve current and goal support without creating point-goal state."""
        if self._request is not None:
            raise ContractViolation("navigation session already has a request")
        require_identifier(goal_id, "navigation goal id")
        if type(goal_revision) is not int or goal_revision < 0:
            raise ContractViolation("navigation goal revision is invalid")
        if type(goal_state) is not GoalState or type(frame) is not NavigationFrame:
            raise ContractViolation("navigation goal requires typed state and frame")
        goal_node, missing = self._surface_for_goal(frame, goal_state)
        if goal_node is None:
            self._frame = frame
            self._pending_goal = (goal_id, goal_revision, goal_state)
            self._snapshot_missing = missing
            self._state = (
                NavigationSessionState.NEEDS_INFORMATION
                if missing else NavigationSessionState.FAILED
            )
            self._reason = (
                "goal_surface_requires_information"
                if missing else "goal_surface_unavailable"
            )
            return
        start_node, start_missing = self._surface_for_body(frame)
        if start_node is None:
            self._frame = frame
            self._pending_goal = (goal_id, goal_revision, goal_state)
            self._snapshot_missing = start_missing
            self._state = (
                NavigationSessionState.NEEDS_INFORMATION
                if start_missing else NavigationSessionState.FAILED
            )
            self._reason = (
                "current_surface_requires_information"
                if start_missing else "current_surface_unavailable"
            )
            return
        request = self._goal_request(
            goal_id, goal_revision, goal_state, start_node, goal_node, frame,
            maximum_expansions=maximum_expansions,
            maximum_planning_seconds=maximum_planning_seconds,
        )
        self._replace_request(request, frame, "goal_started")

    def update_goal(
        self,
        goal_id: str,
        goal_revision: int,
        goal_state: GoalState,
    ) -> None:
        if self._frame is None:
            raise ContractViolation("surface goal update requires an active request")
        if self._request is None:
            if self._pending_goal is None:
                raise ContractViolation(
                    "surface goal update requires an active request"
                )
            pending_id, pending_revision, _ = self._pending_goal
            if (goal_id != pending_id or type(goal_revision) is not int
                    or goal_revision <= pending_revision
                    or type(goal_state) is not GoalState):
                raise ContractViolation(
                    "navigation goal identity or revision is invalid"
                )
            self._pending_goal = None
            self._snapshot_missing = ()
            self.start_goal(goal_id, goal_revision, goal_state, self._frame)
            return
        if type(self._request) is not SurfacePlanningRequest:
            raise ContractViolation("surface goal update requires an active request")
        if (goal_id != self._request.goal_id
                or type(goal_revision) is not int
                or goal_revision <= self._request.goal_revision
                or type(goal_state) is not GoalState):
            raise ContractViolation("navigation goal identity or revision is invalid")
        goal_node, missing = self._surface_for_goal(self._frame, goal_state)
        if goal_node is None:
            self._pending_goal = (goal_id, goal_revision, goal_state)
            self._snapshot_missing = missing
            self._state = (
                NavigationSessionState.NEEDS_INFORMATION
                if missing else NavigationSessionState.FAILED
            )
            self._reason = (
                "goal_surface_requires_information"
                if missing else "goal_surface_unavailable"
            )
            self._request = replace(
                self._request,
                sequence=self._request.sequence + 1,
                request_id=f"{self.session_id}-request-{self._request.sequence + 1}",
                goal_id=goal_id,
                goal_revision=goal_revision,
                goal_state=goal_state,
            )
            return
        start_node, start_missing = self._surface_for_body(self._frame)
        if start_node is None:
            self._pending_goal = (goal_id, goal_revision, goal_state)
            self._snapshot_missing = start_missing
            self._state = (
                NavigationSessionState.NEEDS_INFORMATION
                if start_missing else NavigationSessionState.FAILED
            )
            self._reason = (
                "current_surface_requires_information"
                if start_missing else "current_surface_unavailable"
            )
            self._request = replace(
                self._request,
                sequence=self._request.sequence + 1,
                request_id=f"{self.session_id}-request-{self._request.sequence + 1}",
                goal_id=goal_id,
                goal_revision=goal_revision,
                goal_state=goal_state,
            )
            return
        request = replace(
            self._request,
            sequence=self._request.sequence + 1,
            request_id=f"{self.session_id}-request-{self._request.sequence + 1}",
            start=start_node,
            goal=goal_node,
            goal_id=goal_id,
            goal_revision=goal_revision,
            goal_state=goal_state,
        )
        self._pending_goal = None
        self._replace_request(
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
    ) -> SurfacePlanningRequest:
        return SurfacePlanningRequest(
            1, f"{self.session_id}-request-1", goal_id, goal_revision,
            frame.session.value, start_node, goal_node,
            maximum_expansions=maximum_expansions,
            initial_resources=ResourceState((
                ("food_points", float(frame.body.food_points)),
            )),
            goal_state=goal_state,
            maximum_planning_seconds=maximum_planning_seconds,
        )

    def observe(
        self,
        frame: NavigationFrame,
        changed_cells: tuple[BlockPos, ...],
    ) -> None:
        if type(frame) is not NavigationFrame or type(changed_cells) is not tuple:
            raise ContractViolation("navigation observation requires typed current state")
        if self._closed:
            raise ContractViolation("navigation session is closed")
        if self._frame is not None:
            if frame.session != self._frame.session:
                self._state = NavigationSessionState.FAILED
                self._reason = "world_session_changed"
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
        route = self._active_route
        frame.world.set_protection(
            frame.body.position,
            () if route is None else route.action_route.dependencies,
        )
        self._planning_changes.update(changed_cells)
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
            self._required_interaction = None
            self._restart_request_from_current(
                frame, "world_interaction_dependency_changed",
            )
            return
        if ((self._state is NavigationSessionState.NEEDS_INFORMATION
             or self._pending_goal is not None)
                and set(changed_cells).intersection(self._snapshot_missing)):
            if self._pending_goal is not None:
                goal_id, revision, goal_state = self._pending_goal
                if self._request is None:
                    self._state = NavigationSessionState.READY
                    self._snapshot_missing = ()
                    self.start_goal(goal_id, revision, goal_state, frame)
                else:
                    goal_node, missing = self._surface_for_goal(frame, goal_state)
                    start_node, start_missing = self._surface_for_body(frame)
                    if goal_node is not None and start_node is not None:
                        request = replace(
                            self._request,
                            sequence=self._request.sequence + 1,
                            request_id=(
                                f"{self.session_id}-request-"
                                f"{self._request.sequence + 1}"
                            ),
                            start=start_node,
                            goal=goal_node,
                        )
                        self._pending_goal = None
                        self._replace_request(
                            request, frame, "goal_information_updated",
                            preserve_active_route=self._executor is not None,
                        )
                    else:
                        combined = tuple(sorted(set(missing) | set(start_missing)))
                        self._snapshot_missing = combined
                        if not combined:
                            self._state = NavigationSessionState.FAILED
                            self._reason = (
                                "goal_surface_unavailable"
                                if goal_node is None else "current_surface_unavailable"
                            )
            elif self._request is not None:
                self._restart_request_from_current(
                    frame, "planning_information_updated",
                )
        route = self._active_route
        if route is not None and set(route.action_route.dependencies).intersection(changed_cells):
            self._replan_from_current("active_route_dependency_changed")

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
        if self._state in {
            NavigationSessionState.CLOSED,
            NavigationSessionState.FAILED,
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
        }:
            return self._proposal(MovementV1(), None, 1, deadline_ns)
        if self._state is not NavigationSessionState.CANCELLING:
            self._advance_planning(frame)
        if self._active_route is None:
            if self._state is NavigationSessionState.CANCELLING:
                self._state = NavigationSessionState.CANCELLED
                self._reason = self._cancel_reason or "cancelled"
            return self._proposal(MovementV1(), None, 1, deadline_ns)

        assert self._executor is not None
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
            )
        else:
            decision = self._executor.decide(
                frame, state_anchor=state_anchor, input_ledger=input_ledger,
                movement_yaw_radians=movement_yaw_radians,
            )
        self._last_decision = decision
        active_request_id = self._active_route.source_request_id
        current_request_id = None if self._request is None else self._request.request_id
        terminal_decisions = {
            ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
            ActionRouteState.FAILED, ActionRouteState.BLOCKED,
            ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
        }
        if self._state is NavigationSessionState.CANCELLING:
            if decision.state in {
                ActionRouteState.CANCELLED, ActionRouteState.COMPLETE,
            }:
                self._clear_active_execution()
                self._state = NavigationSessionState.CANCELLED
                self._reason = self._cancel_reason or decision.reason_code
            elif decision.state in {
                ActionRouteState.FAILED, ActionRouteState.BLOCKED,
                ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
            }:
                self._clear_active_execution()
                self._state = NavigationSessionState.FAILED
                self._reason = decision.reason_code
            else:
                self._state = NavigationSessionState.CANCELLING
                self._reason = decision.reason_code
        elif (self._restart_after_active_terminal
                and decision.state in terminal_decisions):
            self._restart_after_active_terminal = False
            self._clear_active_execution()
            self._restart_request_from_current(
                frame, "route_handoff_reanchored",
            )
        elif active_request_id != current_request_id:
            if decision.state in terminal_decisions:
                self._clear_active_execution()
                self._state = (
                    NavigationSessionState.SNAPSHOTTING
                    if self._snapshot_builder is not None
                    else NavigationSessionState.PLANNING
                )
                self._reason = "replacement_route_pending"
            else:
                self._state = NavigationSessionState.EXECUTING
                self._reason = "executing_safe_prefix_during_replan"
                self._snapshot_missing = decision.missing_cells
        elif (self._interaction_approach_pending
                and decision.state is ActionRouteState.COMPLETE):
            self._clear_active_execution()
            self._interaction_approach_pending = False
            self._state = NavigationSessionState.REQUIRES_INTERACTION
            self._reason = "interaction_work_position_reached"
        else:
            self._apply_decision_state(decision)
        movement = decision.movement if decision.submit_input else MovementV1()
        return self._proposal(
            movement, decision.look, decision.input_lease_ticks,
            deadline_ns, route_decision=decision,
            conditioned_look_intent_id=(
                conditioned_look_intent_id
                if conditioned_ordinary_walk else None
            ),
        )

    def register_verified_submission(
        self,
        proposal: NavigationSessionProposal,
        *,
        control_sequence: int,
    ) -> None:
        decision = proposal.route_decision
        if (decision is None or decision.verified_command_index is None
                or decision.expected_movement_tick is None
                or self._executor is None):
            return
        self._executor.register_verified_submission(
            decision.verified_command_index,
            control_sequence=control_sequence,
            requested_movement_tick=decision.expected_movement_tick,
            requested_latest_movement_tick=decision.latest_movement_tick,
        )

    def cancel(self, reason: str) -> None:
        if type(reason) is not str or not reason.strip():
            raise ContractViolation("navigation cancellation reason is required")
        if self._closed:
            raise ContractViolation("navigation session is closed")
        if self._executor is not None:
            self._executor.cancel()
            self._state = NavigationSessionState.CANCELLING
            self._cancel_reason = reason.strip()
            self._reason = "cancellation_requested"
            self._snapshot_builder = None
            return
        self._state = NavigationSessionState.CANCELLED
        self._cancel_reason = reason.strip()
        self._reason = self._cancel_reason
        self._required_interaction = None
        self._interaction_approach_pending = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_planner_worker and hasattr(self._planner, "close"):
            self._planner.close()
        if self._motion_worker is not None and self._owns_motion_worker:
            self._motion_worker.close()
        self._state = NavigationSessionState.CLOSED
        self._reason = "closed"

    def _replace_request(
        self,
        request: PlanningRequest | SurfacePlanningRequest,
        frame: NavigationFrame,
        reason: str,
        *,
        preserve_active_route: bool = False,
    ) -> None:
        # Replacing a planning request never revokes an active body's owner.
        # The admitted-route handoff below decides when that owner is safe to
        # replace.  Callers may still state the preservation intent explicitly
        # to make the reason visible, but an existing executor is authoritative.
        preserve_active_route = preserve_active_route or self._executor is not None
        if not preserve_active_route:
            self._retire_route()
        self._request = request
        self._frame = frame
        self._planning_changes.clear()
        self._snapshot_missing = ()
        self._planning_snapshot = None
        self._planning_snapshot_request_id = None
        self._required_interaction = None
        self._interaction_approach_pending = False
        self._snapshot_builder = KnownMapSnapshotBuilder(
            frame.world, self._bounds(request),
        )
        self._state = NavigationSessionState.SNAPSHOTTING
        self._reason = reason

    def _clear_active_execution(self) -> None:
        self._active_route = None
        self._executor = None
        self._coordinator = None
        self._last_decision = None

    def _retire_route(self) -> None:
        if self._executor is not None:
            self._executor.cancel()
        self._clear_active_execution()

    def _wait_for_active_terminal(
        self, missing: tuple[BlockPos, ...], reason: str,
    ) -> bool:
        if self._executor is None:
            return False
        self._executor.cancel()
        self._restart_after_active_terminal = True
        self._snapshot_missing = missing
        self._state = NavigationSessionState.EXECUTING
        self._reason = reason
        return True

    def _replan_from_current(self, reason: str) -> None:
        if self._request is None or self._frame is None:
            return
        request = self._request
        sequence = request.sequence + 1
        if type(request) is SurfacePlanningRequest:
            start_node, start_missing = self._surface_for_body(self._frame)
            if start_node is None:
                if self._wait_for_active_terminal(
                    start_missing, "replan_waiting_for_safe_terminal",
                ):
                    return
                self._retire_route()
                self._snapshot_missing = start_missing
                self._state = (
                    NavigationSessionState.NEEDS_INFORMATION
                    if start_missing else NavigationSessionState.FAILED
                )
                self._reason = (
                    "current_surface_requires_information"
                    if start_missing else "current_surface_unavailable"
                )
                return
            request = replace(
                request,
                sequence=sequence,
                request_id=f"{self.session_id}-request-{sequence}",
                start=start_node,
            )
        else:
            x, y, z = self._frame.body.position
            request = replace(
                request,
                sequence=sequence,
                request_id=f"{self.session_id}-request-{sequence}",
                start=(math.floor(x), math.floor(y), math.floor(z)),
            )
        self._replace_request(
            request, self._frame, reason,
            preserve_active_route=self._executor is not None,
        )

    def _restart_request_from_current(
        self,
        frame: NavigationFrame,
        reason: str,
    ) -> None:
        request = self._request
        if request is None:
            return
        sequence = request.sequence + 1
        if type(request) is SurfacePlanningRequest:
            start_node, missing = self._surface_for_body(frame)
            if start_node is None:
                if self._wait_for_active_terminal(
                    missing, "restart_waiting_for_safe_terminal",
                ):
                    return
                self._retire_route()
                self._snapshot_missing = missing
                self._state = (
                    NavigationSessionState.NEEDS_INFORMATION
                    if missing else NavigationSessionState.FAILED
                )
                self._reason = (
                    "current_surface_requires_information"
                    if missing else "current_surface_unavailable"
                )
                return
            request = replace(
                request,
                sequence=sequence,
                request_id=f"{self.session_id}-request-{sequence}",
                start=start_node,
            )
        else:
            x, y, z = frame.body.position
            request = replace(
                request,
                sequence=sequence,
                request_id=f"{self.session_id}-request-{sequence}",
                start=(math.floor(x), math.floor(y), math.floor(z)),
            )
        self._replace_request(
            request, frame, reason,
            preserve_active_route=self._executor is not None,
        )

    def _advance_planning(self, frame: NavigationFrame) -> None:
        request = self._request
        if request is None or self._state is NavigationSessionState.NEEDS_INFORMATION:
            return
        if (self._active_route is not None
                and self._active_route.source_request_id == request.request_id):
            return
        if self._snapshot_builder is not None:
            progress = self._snapshot_builder.advance(
                frame.world, self._snapshot_cells_per_step,
            )
            if progress.status is SnapshotBuildStatus.STALE:
                self._snapshot_builder = KnownMapSnapshotBuilder(
                    frame.world, self._bounds(request),
                )
                self._state = NavigationSessionState.SNAPSHOTTING
                self._reason = "snapshot_restarted_after_world_change"
                return
            if progress.status is SnapshotBuildStatus.BUILDING:
                self._state = NavigationSessionState.SNAPSHOTTING
                self._reason = "snapshot_building"
                return
            assert progress.snapshot is not None
            # Unknown cells remain blocked inside the detached snapshot.  The
            # planner can therefore safely use an incomplete scope when the
            # known facts already contain a route.  Missing facts only become
            # a blocker after the planner proves that no known route exists.
            self._snapshot_missing = progress.missing_cells
            self._planning_snapshot = progress.snapshot
            self._planning_snapshot_request_id = request.request_id
            if type(request) is SurfacePlanningRequest:
                planning_mode = (
                    None if self.profiles.ground_modes is None
                    else self.profiles.ground_modes.require(MovementMode.WALK)
                )
                self._planner.submit_surface_snapshot(
                    progress.snapshot,
                    self.profiles.ground,
                    self.profiles.step,
                    request,
                    self.profiles.jump_up,
                    air_profiles=self.profiles.air,
                    ground_mode_profile=planning_mode,
                )
            else:
                self._planner.submit_snapshot(
                    progress.snapshot,
                    self.profiles.ground,
                    request,
                    self.profiles.jump_up,
                )
            self._snapshot_builder = None
            self._state = NavigationSessionState.PLANNING
            self._reason = "planning_submitted"

        candidate = self._planner.poll_latest()
        if candidate is None:
            if hasattr(self._planner, "is_alive") and not self._planner.is_alive():
                self._state = NavigationSessionState.FAILED
                self._reason = "planner_worker_died"
            return
        current = self._request
        if current is None:
            return
        if (candidate.request_id != current.request_id
                or candidate.goal_id != current.goal_id
                or candidate.goal_revision != current.goal_revision):
            return
        status = candidate.status
        no_known_route = (
            status is SurfacePlanningStatus.NO_KNOWN_ROUTE
            if type(candidate) is SurfaceRouteCandidate
            else status is PlanningStatus.NO_KNOWN_ROUTE
        )
        no_route_within_scope = (
            status is SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
            if type(candidate) is SurfaceRouteCandidate
            else status is PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
        )
        if no_known_route:
            if self._snapshot_missing:
                if set(self._snapshot_missing).intersection(self._planning_changes):
                    self._replace_request(
                        current, frame, "planning_information_updated",
                    )
                else:
                    self._state = NavigationSessionState.NEEDS_INFORMATION
                    self._reason = "no_known_route_requires_information"
                return
        if no_known_route or no_route_within_scope:
            if self._interaction_approach_pending:
                self._state = NavigationSessionState.FAILED
                self._reason = "interaction_work_route_unavailable"
                return
            if (self._bridge_policy is not None
                    and self._bridge_remaining > 0
                    and type(current) is SurfacePlanningRequest
                    and type(candidate) is SurfaceRouteCandidate
                    and self._planning_snapshot is not None
                    and self._planning_snapshot_request_id == current.request_id):
                interaction = plan_next_bridge_interaction(
                    self._planning_snapshot,
                    current,
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
                    if interaction.work_node != current.start:
                        approach = replace(
                            current,
                            goal=interaction.work_node,
                            goal_state=None,
                        )
                        planning_mode = (
                            None if self.profiles.ground_modes is None
                            else self.profiles.ground_modes.require(MovementMode.WALK)
                        )
                        self._planner.submit_surface_snapshot(
                            self._planning_snapshot,
                            self.profiles.ground,
                            self.profiles.step,
                            approach,
                            self.profiles.jump_up,
                            air_profiles=self.profiles.air,
                            ground_mode_profile=planning_mode,
                        )
                        self._interaction_approach_pending = True
                        self._state = NavigationSessionState.PLANNING
                        self._reason = "interaction_work_route_submitted"
                        return
                    self._state = NavigationSessionState.REQUIRES_INTERACTION
                    self._reason = "world_interaction_required"
                    return
            self._state = NavigationSessionState.FAILED
            self._reason = (
                "no_known_route_without_missing_cells"
                if no_known_route else "no_route_within_complete_scope"
            )
            return
        complete = (
            status is SurfacePlanningStatus.COMPLETE
            if type(candidate) is SurfaceRouteCandidate
            else status is PlanningStatus.COMPLETE
        )
        if not complete:
            self._state = NavigationSessionState.FAILED
            self._reason = f"planning_{status.value}"
            return
        if type(candidate) is SurfaceRouteCandidate:
            admitted = self._admitter.admit_surface(
                candidate, frame,
                expected_request_id=current.request_id,
                goal_id=current.goal_id,
                goal_revision=current.goal_revision,
                changed_cells=tuple(sorted(self._planning_changes)),
            )
        else:
            admitted = self._admitter.admit(
                candidate, frame,
                expected_request_id=current.request_id,
                goal_id=current.goal_id,
                goal_revision=current.goal_revision,
                changed_cells=tuple(sorted(self._planning_changes)),
            )
        if admitted.status is not AdmissionStatus.ACCEPTED or admitted.route is None:
            if admitted.reason in {
                AdmissionReason.PLANNING_REQUEST_REPLACED,
                AdmissionReason.GOAL_REVISION_CHANGED,
                AdmissionReason.WORLD_SESSION_CHANGED,
                AdmissionReason.ROUTE_DEPENDENCIES_CHANGED,
            }:
                self._state = (
                    NavigationSessionState.SNAPSHOTTING
                    if self._snapshot_builder is not None
                    else NavigationSessionState.PLANNING
                )
                self._reason = admitted.reason
                return
            self._state = NavigationSessionState.FAILED
            self._reason = admitted.reason
            return
        if (self._active_route is not None
                and self._active_route.source_request_id != current.request_id):
            assert self._executor is not None
            if self._executor.requires_safe_handoff(frame):
                self._executor.cancel()
                self._restart_after_active_terminal = True
                self._state = NavigationSessionState.EXECUTING
                self._reason = "route_handoff_waiting_for_safe_terminal"
                return
            self._clear_active_execution()
        self._active_route = admitted.route
        self._executor = ActionRouteExecutor(
            self.profiles.ground,
            self.profiles.jump_up,
            self.profiles.step,
            self.profiles.ground_modes,
            self.profiles.air,
            gap_solver_policy=self.profiles.gap_solver,
        )
        if any(type(action) is JumpGapSegment
               for action in admitted.route.action_route.actions):
            if self._motion_worker is None:
                self._motion_worker = MotionSolverWorker(max_pending=4)
                self._owns_motion_worker = True
            self._coordinator = MotionRouteCoordinator(
                admitted.route, self._executor, self._motion_worker,
                gap_solver_policy=self.profiles.gap_solver,
            )
            self._coordinator.start(frame)
        else:
            self._executor.start(admitted.route.action_route, frame)
        self._planning_changes.clear()
        self._snapshot_missing = ()
        self._state = NavigationSessionState.EXECUTING
        self._reason = "route_admitted"

    def _apply_decision_state(self, decision: ActionRouteDecision) -> None:
        mapping = {
            ActionRouteState.COMPLETE: NavigationSessionState.COMPLETE,
            ActionRouteState.CANCELLED: NavigationSessionState.CANCELLED,
            ActionRouteState.NEEDS_INFORMATION: NavigationSessionState.NEEDS_INFORMATION,
            ActionRouteState.FAILED: NavigationSessionState.FAILED,
            ActionRouteState.BLOCKED: NavigationSessionState.FAILED,
            ActionRouteState.UNSUPPORTED: NavigationSessionState.FAILED,
            ActionRouteState.INPUT_LOST: NavigationSessionState.FAILED,
        }
        self._state = mapping.get(decision.state, NavigationSessionState.EXECUTING)
        self._reason = decision.reason_code
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
            if route_decision is not None and not route_decision.submit_input:
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
            executor_route = (
                getattr(self._executor, "route", None)
                if self._executor is not None else None
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
                ActionPriorityV0.TASK,
                now,
                expires,
                movement=movement,
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
                    5.0 if observed_yaw_bound else None
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
            control = ControlFrameProposalV1(
                (OrderedIntentV1(self._source, self._intent_sequence, intent),),
                observation_request,
                decision_events,
            )
        return NavigationSessionProposal(control, self.report, route_decision)

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

    def _bounds(
        self,
        request: PlanningRequest | SurfacePlanningRequest,
    ) -> KnownMapBounds:
        if type(request) is SurfacePlanningRequest:
            start_x, start_z, start_y = (
                request.start.column_x, request.start.column_z,
                request.start.vertical_band,
            )
            goal_x, goal_z, goal_y = (
                request.goal.column_x, request.goal.column_z,
                request.goal.vertical_band,
            )
        else:
            start_x, start_y, start_z = request.start
            goal_x, goal_y, goal_z = request.goal
        extra_top = max((
            math.ceil(max((point[1] for point in profile.reference_positions), default=0.0))
            for profile in self.profiles.air
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
                    px, py, pz = surface.position
                    if (goal.region.min_x <= px <= goal.region.max_x
                            and goal.region.min_y <= py <= goal.region.max_y
                            and goal.region.min_z <= pz <= goal.region.max_z):
                        candidates.append(surface)
        if not candidates:
            return None, tuple(sorted(missing))
        center = (
            (goal.region.min_x + goal.region.max_x) / 2.0,
            (goal.region.min_y + goal.region.max_y) / 2.0,
            (goal.region.min_z + goal.region.max_z) / 2.0,
        )
        return min(
            candidates,
            key=lambda surface: math.dist(surface.position, center),
        ).node_id, tuple(sorted(missing))
