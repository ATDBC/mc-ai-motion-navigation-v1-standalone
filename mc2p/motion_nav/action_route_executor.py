"""Sequential Walk/JumpUp execution without merging their state machines."""
from __future__ import annotations

from mc2p.motion_nav.actions.registry import action_spec
from mc2p.motion_nav.actions.contracts import BodyCommitment, ActionRouteState, ActionControllerResult

from dataclasses import dataclass, replace
from enum import StrEnum
import math
import time

from mc2p.contracts.action_v1 import MovementTickWindowV1, MovementV1, SafeDefaultV1
from mc2p.contracts.action_v1 import LookV1
from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.body_control import StopCause, BodyControlPhase
from mc2p.motion_nav.action_route import (
    ActionRoute, JumpGapSegment, JumpUpSegment,
    StepSegment, WalkSegment,
)
from mc2p.motion_nav.air_motion import AirMotionProfile
from mc2p.motion_nav.fixed_route import (
    FixedRouteConfig, FixedRouteController, FixedRouteDecision, FixedRouteState,
    GroundHandoffTarget,
    terminal_route_config,
    GroundHandoffDisposition,
)
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_modes import GroundModeProfiles, observed_ground_mode
from mc2p.motion_nav.jump_up import JumpUpController, JumpUpProfile, JumpUpState
from mc2p.motion_nav.step_transition import StepController, StepProfile, StepState
from mc2p.motion_nav.goal_observation import (
    ObservedGoalStatus, evaluate_observed_goal,
)
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.motion_candidate import (
    AdmittedMotionCandidate, VerifiedMotionExecutor,
    VerifiedMotionExecutorState, verified_candidate_can_start,
    verified_start_state_matches,
)
from mc2p.motion_nav.motion_risk import (
    TaskDamageBudget,
)
from mc2p.motion_nav.motion_solver import (
    DEFAULT_AIR_TRANSITION_POLICIES, DEFAULT_GAP_SOLVER_POLICY,
    AirTransitionSolverPolicy, GapSolverPolicy, MotionSolveKind,
)
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputResponsibilityDisposition, StateAnchor,
    assess_input_responsibility,
)
from mc2p.motion_nav.physics_types import PhysicsState
from mc2p.motion_nav.prepared_motion import (
    PreparedMotionHandle, ProofConsumerIdentity, ProofReleaseEvidence, ProofUse,
    PreparedProofUsePort, ReferenceRejectReason, ReferenceResult, ReferenceStatus,
    PreparedActionHandoff, PreparedHandoffApproval, PreparedHandoffQueryPort,
    PreparedHandoffStatus, PreparedQueryStatus, PreparedActivationRegistryPort,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.route_validation import RouteProgressEvidence
from mc2p.motion_nav.segment_entry import body_fits_segment_entry
from mc2p.motion_nav.safe_ground_control import (
    verified_ground_recovery_movement, verified_ground_rollout,
    verified_ground_route_candidate,
)
from mc2p.motion_nav.world_model import BlockPos, WorldQueryCache
from mc2p.motion_nav.world_snapshot import DependencyStatus
from mc2p.motion_nav.geometry import QueryStatus

@dataclass(frozen=True, slots=True)
class ActionRouteDecision:
    state: ActionRouteState
    movement: MovementV1
    look: LookV1 | None
    input_lease_ticks: int
    action_index: int
    reason_code: str
    missing_cells: tuple[BlockPos, ...]
    control_time_ns: int
    submit_input: bool = True
    verified_command_index: int | None = None
    expected_movement_tick: int | None = None
    latest_movement_tick: int | None = None
    requires_verified_motion: bool = False
    body_phase: BodyControlPhase | None = None
    ground_handoff_disposition: GroundHandoffDisposition = GroundHandoffDisposition.NOT_REQUESTED
    route_progress_evidence: RouteProgressEvidence | None = None
    failure_cause: StopCause | None = None
    safe_default: SafeDefaultV1 | None = None


@dataclass(frozen=True, slots=True)
class ActionCompletionPlan:
    """Single-use local commit plan for one exact executor action."""

    executor_token: object
    executor_epoch: int
    route: ActionRoute
    action_index: int
    next_action_index: int
    controller: object
    pending_action_boundary_index: int | None
    frame: NavigationFrame
    observation_sequence_id: int
    damage_before: float
    damage_after: float
    committed_damage_before: frozenset[int]
    committed_damage_after: frozenset[int]
    verified_motion_identity: tuple[tuple[int, int], ...]


def decision_input_window(decision: ActionRouteDecision) -> MovementTickWindowV1 | None:
    """Carry an action's proved start window without inventing timing slack."""
    if type(decision) is not ActionRouteDecision:
        raise ContractViolation("input window requires an action route decision")
    earliest, latest = decision.expected_movement_tick, decision.latest_movement_tick
    if earliest is None and latest is None:
        if decision.verified_command_index is not None:
            raise ContractViolation("verified command requires both movement window endpoints")
        return None
    if earliest is None or latest is None:
        raise ContractViolation("movement window requires both endpoints")
    window = MovementTickWindowV1(earliest, latest)
    if window.latest_tick - window.earliest_tick > 1:
        raise ContractViolation("action movement window exceeds one tick of start slack")
    return window


def _ordinary_walk_start_window(
    action: WalkSegment,
    decision: FixedRouteDecision,
    frame: NavigationFrame,
    *, physics_state: PhysicsState | None = None,
    profile: GroundMotionProfile | None = None,
    query_cache: WorldQueryCache | None = None,
    first_command: bool = False,
    minimum_support: float = .15,
) -> tuple[int, int] | None:
    """Reuse a safe ordinary candidate only after a proved stationary prefix.

    The candidate already covers its complete lease and released-input tail.
    An equivalent one-tick neutral entry makes that same spatial proof valid
    one tick later. The submitted lease remains unchanged.
    """
    movement_tick = frame.body.movement_tick_id
    if (
        not first_command
        or action.traversal_plan is not None
        or (
            action.transition is not None
            and action.transition.mode is not MovementMode.WALK
        )
        or decision.state is not FixedRouteState.RUNNING
        or decision.movement == MovementV1()
        or decision.input_lease_ticks < 2
        or not frame.body.is_on_ground
        or frame.body.pose != "standing"
        or movement_tick is None
        or math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        ) > 1.0e-6
        or physics_state is None
        or profile is None
        or query_cache is None
        or physics_state.status_effects
        or physics_state.jumping_cooldown_ticks
    ):
        return None
    prefix = verified_ground_route_candidate(
        frame, physics_state, MovementV1(), control_ticks=1, tail_ticks=0,
        minimum_support=minimum_support, profile=profile, query_cache=query_cache,
    )
    delayed = prefix.tracking_end
    if prefix.status is not QueryStatus.FEASIBLE or delayed is None:
        return None
    if (math.dist(delayed.position, physics_state.position) > 1.e-9
            or any(abs(a-b) > 1.e-9 for a,b in zip(
                delayed.velocity_blocks_per_tick,physics_state.velocity_blocks_per_tick))
            or replace(delayed,movement_tick_id=physics_state.movement_tick_id,
                       position=physics_state.position,
                       velocity_blocks_per_tick=physics_state.velocity_blocks_per_tick)
                != physics_state):
        return None
    return movement_tick + 1, movement_tick + 2


class ActionRouteExecutor:
    """Own only segment order; each movement ability keeps its own controller."""

    def __init__(self, ground_profile: GroundMotionProfile,
                 jump_profile: JumpUpProfile,
                 step_profile: StepProfile | None = None,
                 ground_modes: GroundModeProfiles | None = None,
                 air_profiles: tuple[AirMotionProfile, ...] = (), *,
                 gap_solver_policy: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY,
                 air_transition_policies: dict[
                     MotionSolveKind, AirTransitionSolverPolicy
                 ] = DEFAULT_AIR_TRANSITION_POLICIES) -> None:
        if type(ground_profile) is not GroundMotionProfile or type(jump_profile) is not JumpUpProfile:
            raise ContractViolation("action route executor requires calibrated profiles")
        self.ground_profile = ground_profile
        self.jump_profile = jump_profile
        if step_profile is not None and type(step_profile) is not StepProfile:
            raise ContractViolation("action route executor Step profile must be typed")
        self.step_profile = step_profile
        if ground_modes is not None and type(ground_modes) is not GroundModeProfiles:
            raise ContractViolation("action route ground modes must be typed")
        self.ground_modes = ground_modes
        if (type(air_profiles) is not tuple
                or any(type(profile) is not AirMotionProfile for profile in air_profiles)
                or len({profile.profile_id for profile in air_profiles}) != len(air_profiles)):
            raise ContractViolation("action route air profiles must be typed and unique")
        self.air_profiles = {profile.profile_id: profile for profile in air_profiles}
        if type(gap_solver_policy) is not GapSolverPolicy:
            raise ContractViolation("action route gap solver policy must be typed")
        self.gap_solver_policy = gap_solver_policy
        if (type(air_transition_policies) is not dict
                or any(type(kind) is not MotionSolveKind
                       or type(policy) is not AirTransitionSolverPolicy
                       or policy.kind is not kind
                       for kind, policy in air_transition_policies.items())):
            raise ContractViolation("action route air policies must be typed")
        self.air_transition_policies = dict(air_transition_policies)
        self.route: ActionRoute | None = None
        self.state = ActionRouteState.IDLE
        self.action_index = 0
        self._controller: object | None = None
        self._cancel_requested = False
        self._stop_cause: StopCause | None = None
        self._actions_finished = False
        self._final_action_failure_cause: StopCause | None = None
        self._damage_budget = TaskDamageBudget()
        self._session = None
        self._verified_motion: dict[int, AdmittedMotionCandidate] = {}
        self._require_verified_gap_motion = False
        self._required_verified_motion: frozenset[int] = frozenset()
        self._completed_movement_damage_points = 0.0
        self._committed_damage_actions: set[int] = set()
        self._input_scope_floor: int | None = None
        self._pending_action_boundary_index: int | None = None
        self._prepared_result_inbox: PreparedProofUsePort | None = None
        self._prepared_body_owner_id: str | None = None
        self._prepared_action_indices: frozenset[int] = frozenset()
        self._prepared_route_revision: int | None = None
        self._prepared_generation_for = None
        self._prepared_handoff_supervisor = None
        self._prepared_ledger_id: str | None = None
        self._last_prepared_frame: NavigationFrame | None = None
        self._last_prepared_anchor: StateAnchor | None = None
        self._last_prepared_ledger: InputApplicationLedger | None = None
        self._prepared_delivery_controller: VerifiedMotionExecutor | None = None
        self._completion_token = object()
        self._completion_epoch = 0
        self._pending_completion_candidate_cleanup: set[int] = set()

    @property
    def activation_registry(self) -> PreparedActivationRegistryPort | None:
        """Expose the existing activation service without granting replacement."""
        registry = getattr(self, "_prepared_result_inbox", None)
        return registry if isinstance(registry, PreparedActivationRegistryPort) else None

    @property
    def prepared_controller(self) -> VerifiedMotionExecutor | None:
        controller = self._controller
        return (controller if type(controller) is VerifiedMotionExecutor
                and controller.prepared_handle is not None else None)

    def deliver_prepared_activation(self, handle, transaction, accepted):
        """Stage only the successor executor; current action and owner stay intact."""
        from mc2p.contracts.motion_activation import ActivationResult, ActivationStatus, ActivationRejectReason
        if (self._cancel_requested or self._pending_action_boundary_index != handle.action_index
                or handle.action_index != self.action_index + 1):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        controller = VerifiedMotionExecutor(prepared_handle=handle)
        delivered = controller.prepare_activation(transaction, accepted)
        if not isinstance(delivered, ActivationResult):
            self._prepared_delivery_controller = controller
        return delivered

    def abort_prepared_activation(self) -> None:
        self._prepared_delivery_controller = None

    def can_commit_prepared_activation(
            self, delivered, completion_plan: ActionCompletionPlan) -> bool:
        controller = self._prepared_delivery_controller
        return (
            controller is not None
            and delivered is controller.activation_delivery
            and not self._cancel_requested
            and self.route is not None
            and self._pending_action_boundary_index == self.action_index + 1
            and controller.prepared_handle.action_index == self.action_index + 1
            and controller.prepared_handle.route_id == self.route.route_id
            and controller.state is VerifiedMotionExecutorState.IDLE
            and controller.stream_tracker is not None
            and self.can_commit_action_completion(completion_plan)
        )

    def commit_prepared_activation(
            self, delivered, completion_plan: ActionCompletionPlan):
        """Called within the registry's short commit; no queries, sequences or send."""
        from mc2p.contracts.motion_activation import ActivationResult, ActivationStatus, ActivationRejectReason
        controller = self._prepared_delivery_controller
        if not self.can_commit_prepared_activation(delivered, completion_plan):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        return self._commit_prepared_activation_prechecked(
            delivered, completion_plan,
        )

    def _commit_prepared_activation_prechecked(
            self, delivered, completion_plan: ActionCompletionPlan):
        """Commit local fields after RouteControl checked both participants."""
        from mc2p.contracts.motion_activation import ActivationResult, ActivationStatus
        controller = self._prepared_delivery_controller
        assert controller is not None
        controller.commit_activation(delivered)
        self._commit_action_completion_prechecked(completion_plan)
        self._controller = controller
        self._prepared_delivery_controller = None
        self.state = ActionRouteState.RUNNING
        return ActivationResult._from_native_references(ActivationStatus.ACTIVATED)

    def prepare_action_completion(
            self, frame: NavigationFrame, *, observation_sequence_id: int,
            expected_next_action_index: int | None = None,
    ) -> ActionCompletionPlan:
        """Describe completion without changing damage, candidates or indices."""
        if (type(frame) is not NavigationFrame
                or type(observation_sequence_id) is not int
                or observation_sequence_id < 0):
            raise ContractViolation("action completion requires an observation")
        if frame.body.sequence_id != observation_sequence_id:
            raise ContractViolation("action completion frame is stale")
        route = self.route
        if (route is None or not 0 <= self.action_index < len(route.actions)):
            raise ContractViolation("action completion requires an active action")
        next_index = self.action_index + 1
        if (expected_next_action_index is not None
                and expected_next_action_index != next_index):
            raise ContractViolation("action completion successor changed")
        completed = route.actions[self.action_index]
        damage_after = self._completed_movement_damage_points
        committed_after = set(self._committed_damage_actions)
        spec = action_spec(completed)
        if (spec.tracks_damage
                and self.action_index not in committed_after
                and spec.damage_committed(completed, frame, force=True)):
            damage_after += spec.expected_damage_points(completed)
            committed_after.add(self.action_index)
        return ActionCompletionPlan(
            self._completion_token,
            self._completion_epoch,
            route,
            self.action_index,
            next_index,
            self._controller,
            self._pending_action_boundary_index,
            frame,
            observation_sequence_id,
            self._completed_movement_damage_points,
            damage_after,
            frozenset(self._committed_damage_actions),
            frozenset(committed_after),
            tuple(sorted(
                (index, id(candidate))
                for index, candidate in self._verified_motion.items()
            )),
        )

    def can_commit_action_completion(self, plan: ActionCompletionPlan) -> bool:
        return (
            type(plan) is ActionCompletionPlan
            and plan.executor_token is self._completion_token
            and plan.executor_epoch == self._completion_epoch
            and self.route is plan.route
            and self.action_index == plan.action_index
            and self._controller is plan.controller
            and self._pending_action_boundary_index
                == plan.pending_action_boundary_index
            and plan.frame.body.sequence_id == plan.observation_sequence_id
            and plan.frame.session == self._session
            and self._completed_movement_damage_points == plan.damage_before
            and frozenset(self._committed_damage_actions)
                == plan.committed_damage_before
            and tuple(sorted(
                (index, id(candidate))
                for index, candidate in self._verified_motion.items()
            )) == plan.verified_motion_identity
        )

    def commit_action_completion(self, plan: ActionCompletionPlan) -> bool:
        """Apply the local part of one prepared completion exactly once."""
        if not self.can_commit_action_completion(plan):
            return False
        self._commit_action_completion_prechecked(plan)
        return True

    def _commit_action_completion_prechecked(
            self, plan: ActionCompletionPlan) -> None:
        """Mutate only local fields after the caller checked every owner."""
        self._completed_movement_damage_points = plan.damage_after
        self._committed_damage_actions = set(plan.committed_damage_after)
        self._pending_action_boundary_index = None
        self.action_index = plan.next_action_index
        self._pending_completion_candidate_cleanup.add(plan.action_index)
        self._completion_epoch += 1

    def finalize_action_completion(self, plan: ActionCompletionPlan) -> int:
        """Release registry candidates after an enclosing registry lock exits."""
        if (type(plan) is not ActionCompletionPlan
                or plan.executor_token is not self._completion_token
                or plan.action_index
                    not in self._pending_completion_candidate_cleanup):
            return 0
        self._pending_completion_candidate_cleanup.remove(plan.action_index)
        return self.discard_prepared_candidates(
            action_index=plan.action_index,
        )

    def bind_prepared_result_inbox(self, inbox: PreparedProofUsePort, *, owner_id: str) -> None:
        """Borrow the existing use registry; do not store a second handle map."""
        if not isinstance(inbox, PreparedProofUsePort):
            raise ContractViolation("prepared proof uses require a registry use port")
        require_identifier(owner_id, "prepared body consumer id")
        current = getattr(self, "_prepared_result_inbox", None)
        if current is not None and (current is not inbox or self._prepared_body_owner_id != owner_id):
            raise ContractViolation("an executor cannot change its prepared service or body consumer")
        self._prepared_result_inbox, self._prepared_body_owner_id = inbox, owner_id

    def bind_prepared_handoff_context(self, *, route_revision: int, generation_for) -> None:
        """Borrow the coordinator's generation lookup; never produce a stream."""
        require_nonnegative_int(route_revision, "prepared route revision")
        if not callable(generation_for):
            raise ContractViolation("prepared handoff needs its coordinator lookup")
        self._prepared_route_revision = route_revision
        self._prepared_generation_for = generation_for

    def require_prepared_action(self, action_index: int, *, ledger_identity: str) -> None:
        require_nonnegative_int(action_index, "prepared action index")
        require_identifier(ledger_identity, "prepared ledger identity")
        if self.route is None or not self.action_index <= action_index < min(len(self.route.actions), self.action_index + 2):
            raise ContractViolation("preparation is limited to the current action and its successor")
        self._prepared_action_indices = self._prepared_action_indices | {action_index}
        self._prepared_ledger_id = ledger_identity

    def revoke_unactivated_prepared_successor(
            self, action_index: int, *, expected_generation_id: str) -> bool:
        """Remove one speculative successor without touching body ownership."""
        require_nonnegative_int(action_index, "revoked prepared action index")
        require_identifier(expected_generation_id, "revoked prepared generation")
        if (self.route is None or action_index != self.action_index + 1
                or self.prepared_controller is not None
                or self._prepared_delivery_controller is not None):
            return False
        stream = (None if self._prepared_generation_for is None
                  else self._prepared_generation_for(action_index))
        if (stream is None
                or stream.generation_id != expected_generation_id):
            return False
        self._prepared_action_indices = frozenset(
            index for index in self._prepared_action_indices
            if index != action_index
        )
        if self._pending_action_boundary_index == action_index:
            self._pending_action_boundary_index = None
        if not self._prepared_action_indices:
            self._prepared_ledger_id = None
        return True

    @property
    def preparation_ledger(self) -> InputApplicationLedger | None:
        return self._last_prepared_ledger

    @property
    def preparation_anchor(self) -> StateAnchor | None:
        return self._last_prepared_anchor

    @property
    def prepared_successor_required(self) -> bool:
        return self.action_index + 1 in self._prepared_action_indices

    def selected_motion(self, action_index: int):
        """An existing SOLVE choice is preparation input, never a handle."""
        candidate = self._verified_motion.get(action_index)
        return None if candidate is None else candidate.proof

    def _prepared_handle(self, action_index: int) -> PreparedMotionHandle | None:
        inbox = self._prepared_result_inbox
        if not isinstance(inbox, PreparedHandoffQueryPort) or self._prepared_generation_for is None:
            return None
        stream = self._prepared_generation_for(action_index)
        return next((handle for handle in inbox.registered_handles
            if stream is not None and handle.hot_view.stream == stream
            and handle.owner_id == self._prepared_body_owner_id
            and self.route is not None and handle.route_id == self.route.route_id
            and handle.action_index == action_index and inbox.usage_active(handle, ProofUse.CANDIDATE)), None)

    def install_prepared_motion(self, handle: PreparedMotionHandle) -> None:
        """Verify an existing candidate reference without starting or retiring it."""
        inbox = self._prepared_result_inbox
        if (type(handle) is not PreparedMotionHandle or not isinstance(inbox, PreparedHandoffQueryPort)
                or not inbox.has_registered_handle(handle)
                or not inbox.usage_active(handle, ProofUse.CANDIDATE)
                or self.route is None or handle.route_id != self.route.route_id
                or handle.owner_id != self._prepared_body_owner_id
                or handle.hot_view.route_revision != self._prepared_route_revision
                or self._prepared_generation_for is None
                or handle.hot_view.stream != self._prepared_generation_for(handle.action_index)
                or handle.action_index not in self._prepared_action_indices):
            raise ContractViolation("prepared installation requires this owner's exact registered candidate")
        # The registry already retains the handle. Installation needs no map,
        # use conversion, action advance, or speculative sequence allocation.

    def stage_prepared_action(self, frame: NavigationFrame, anchor: StateAnchor) -> PreparedActionHandoff:
        if (type(frame) is not NavigationFrame or type(anchor) is not StateAnchor
                or self.route is None or self._prepared_body_owner_id is None
                or self._prepared_ledger_id is None or self._prepared_route_revision is None
                or self.action_index + 1 >= len(self.route.actions)):
            raise ContractViolation("prepared action staging requires a current route and real body anchor")
        index = self.action_index + 1
        handle = self._prepared_handle(index)
        hot = None if handle is None else handle.hot_view
        status = PreparedQueryStatus.NOT_READY
        if hot is not None:
            if (getattr(self._controller, "committed_execution", False)
                    and not self._controller.matches_successor_handle(handle)):
                status = PreparedQueryStatus.ENTRY_MISMATCH
            elif anchor.movement_tick_id > hot.deadline_tick:
                status = PreparedQueryStatus.OVERDUE
            elif (frame.session != anchor.session or frame.body.sequence_id != anchor.observation_sequence_id
                    or frame.body.movement_tick_id != anchor.movement_tick_id
                    or frame.body.position != anchor.physics_state.position
                    or frame.body.velocity_blocks_per_second != tuple(v * 20. for v in anchor.physics_state.velocity_blocks_per_tick)
                    or frame.body.yaw_radians != anchor.physics_state.yaw_radians
                    or frame.body.pose != anchor.physics_state.pose
                    or frame.body.is_on_ground != anchor.physics_state.on_ground
                    or not any(verified_start_state_matches(
                        branch.trajectory[0], anchor.physics_state,
                    ) for branch in hot.branches)):
                status = PreparedQueryStatus.ENTRY_MISMATCH
            elif not hot.requested_window.allows_start(anchor.movement_tick_id + 1):
                status = PreparedQueryStatus.TIMING_MISMATCH
            else:
                source = frame.world._owner
                changes = (None if source is None
                           else source.dependency_changes(hot.dependency_versions))
                status = (PreparedQueryStatus.READY_MATCH
                          if changes is not None
                          and changes.status is DependencyStatus.UNCHANGED
                          else PreparedQueryStatus.DEPENDENCY_CHANGED)
        ledger = self._last_prepared_ledger
        records = () if ledger is None else ledger.snapshot()
        window = None if hot is None else hot.requested_window
        # An elapsed authorization window never proves client revocation.
        # Keep the bounded ledger facts, including unresolved older sequences.
        conflicts = tuple(record for record in records if record.session == frame.session)
        latest = max((record for record in records if record.applied_ticks),
                     key=lambda record: (record.applied_ticks[-1], record.control_sequence), default=None)
        inbox = self._prepared_result_inbox
        old = (None if not isinstance(inbox, PreparedHandoffQueryPort) else next((item
            for item in inbox.registered_handles if item.owner_id == self._prepared_body_owner_id
            and item.route_id == self.route.route_id and item.action_index == self.action_index
            and (inbox.usage_active(item, ProofUse.EXECUTING) or inbox.usage_active(item, ProofUse.SAFE_TAIL))), None))
        command = None if hot is None else hot.commands[0]
        return PreparedActionHandoff(
            f"handoff/{index}/{anchor.observation_sequence_id}/{('pending' if handle is None else handle.proof_token)}",
            self._prepared_body_owner_id, self.route.route_id, self._prepared_route_revision,
            self.action_index, index, None if old is None else old.proof_token,
            None if old is None else old.generation_id, None if handle is None else handle.proof_token,
            None if handle is None else handle.generation_id, self._prepared_ledger_id,
            frame.session.value, anchor.observation_sequence_id, anchor.physics_state, window, status,
            self._pending_action_boundary_index == index and index in self._prepared_action_indices
                and not self._cancel_requested,
            conflicts, None if latest is None else latest.action,
            None if command is None else command.movement,
            None if command is None else command.required_movement_yaw_radians)

    def prepared_action_proposal(self, approval: PreparedHandoffApproval | None, *, control=None):
        from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
        supervisor = self._prepared_handoff_supervisor
        if (type(approval) is not PreparedHandoffApproval or approval.status is not PreparedHandoffStatus.APPROVED
                or type(supervisor) is not ExecutionSupervisor
                or not ExecutionSupervisor.prepared_approval_is_current(supervisor, approval, control=control)
                or control.executor is not self
                or self._last_prepared_frame is None or self._last_prepared_anchor is None):
            return None
        fresh = self.stage_prepared_action(self._last_prepared_frame, self._last_prepared_anchor)
        if fresh != approval.proposal or fresh.entry_result is not PreparedQueryStatus.READY_MATCH:
            return None
        handle = self._prepared_handle(fresh.new_action_index)
        return None if handle is None else VerifiedMotionExecutor.prepared_action_proposal(
            handle, approval, supervisor=supervisor, control=control)

    def discard_prepared_candidates(self, *, action_index: int | None = None) -> int:
        """Publish unused-action retirement; body uses require explicit facts."""
        if action_index is not None:
            require_nonnegative_int(action_index, "discarded prepared action index")
        inbox = getattr(self, "_prepared_result_inbox", None)
        if inbox is None or self.route is None:
            return 0
        released = 0
        for handle in inbox.registered_handles:  # At most eight registry records.
            if (handle.owner_id != self._prepared_body_owner_id or handle.route_id != self.route.route_id
                    or (action_index is not None and handle.action_index != action_index)):
                continue
            release_candidate = getattr(inbox, "release_candidate", None)
            if callable(release_candidate):
                released += bool(release_candidate(handle))
                continue
            consumer = ProofConsumerIdentity.from_handle(handle, ProofUse.CANDIDATE)
            result = inbox.release_usage(handle, use=ProofUse.CANDIDATE,
                                         evidence=ProofReleaseEvidence(consumer, None, None, None))
            released += result.status is ReferenceStatus.RELEASED
        return released

    def release_prepared_usage(self, handle: PreparedMotionHandle, *, use: ProofUse,
                              evidence: ProofReleaseEvidence) -> ReferenceResult:
        """Forward ledger/owner/supervisor facts without inventing an ending."""
        inbox = getattr(self, "_prepared_result_inbox", None)
        if (inbox is None or type(handle) is not PreparedMotionHandle
                or handle.owner_id != self._prepared_body_owner_id):
            return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.CONSUMER)
        return inbox.release_usage(handle, use=use, evidence=evidence)

    @property
    def completed_movement_damage_points(self) -> float:
        return self._completed_movement_damage_points

    @property
    def pending_action_boundary_index(self) -> int | None:
        return self._pending_action_boundary_index

    def clear_pending_action_boundary(self) -> None:
        self._pending_action_boundary_index = None

    def enter_upcoming_action_boundary(
        self, next_index: int, frame: NavigationFrame,
    ) -> bool:
        """Stage a proved next-action boundary until Walk progress is observed."""
        self.clear_pending_action_boundary()
        if (self.route is None
                or type(next_index) is not int
                or next_index != self.action_index + 1
                or next_index >= len(self.route.actions)
                or type(self.route.actions[self.action_index]) is not WalkSegment
                or action_spec(self.route.actions[next_index]).entry_observation(self.route.actions[next_index], frame) is None
                or not frame.body.is_on_ground):
            return False
        upcoming = self.route.actions[next_index]
        mode = observed_ground_mode(frame.body)
        if (upcoming.entry_window is None
                or mode is None
                or not body_fits_segment_entry(
                    upcoming.entry_window, frame.body, mode,
                )):
            return False
        self._pending_action_boundary_index = next_index
        return True

    def _activate(self, frame: NavigationFrame) -> None:
        assert self.route is not None
        action = self.route.actions[self.action_index]
        if type(action) is WalkSegment:
            mode = (action.transition.mode if action.transition is not None
                    else MovementMode.WALK)
            mode_profile = None
            motion_profile = self.ground_profile
            if action.transition is not None and self.ground_modes is not None:
                mode_profile = self.ground_modes.require(mode)
                motion_profile = mode_profile.motion
                if action.transition.trajectory_profile_id != motion_profile.profile_id:
                    raise ContractViolation("ground segment uses another calibrated profile")
            elif mode is not MovementMode.WALK or (
                    action.transition is not None
                    and action.transition.trajectory_profile_id != self.ground_profile.profile_id):
                if self.ground_modes is None:
                    raise ContractViolation("walk segment requires configured B08 ground modes")
                mode_profile = self.ground_modes.require(mode)
                motion_profile = mode_profile.motion
                if (action.transition is not None
                        and action.transition.trajectory_profile_id != motion_profile.profile_id):
                    raise ContractViolation("ground segment uses another calibrated profile")
            config = FixedRouteConfig()
            next_index = self.action_index + 1
            if (next_index < len(self.route.actions)
                    and type(self.route.actions[next_index]) is WalkSegment
                    and mode is MovementMode.WALK
                    and self.route.actions[next_index].transition is not None
                    and self.route.actions[next_index].transition.mode
                       is MovementMode.SPRINT):
                # Walking speed is already a valid Sprint entry.  Let the old
                # segment finish at the shared point without braking; _advance
                # asks the Sprint controller for its first input in this frame.
                config = replace(
                    config,
                    handoff_speed_blocks_per_second=(
                        motion_profile.maximum_speed_blocks_per_second
                        + config.speed_model_tolerance_blocks_per_second
                    ),
                )
            elif (next_index < len(self.route.actions)
                    and action_spec(self.route.actions[self.action_index + 1]).body_commitment
                    is BodyCommitment.TRANSITION):
                next_action = self.route.actions[self.action_index + 1]
                if type(next_action) is JumpUpSegment:
                    entry_tolerance = self.jump_profile.entry_center_tolerance_blocks
                    entry_speed = self.jump_profile.maximum_entry_speed_blocks_per_second
                elif action_spec(next_action).controller_adapter is not None:
                    entry_tolerance, entry_speed = action_spec(next_action).controller_adapter.entry_limits(
                        next_action, self.air_profiles)
                else:
                    if self.step_profile is None:
                        raise ContractViolation("Step segment requires a calibrated profile")
                    entry_tolerance = self.step_profile.target_horizontal_radius_blocks
                    entry_speed = self.step_profile.maximum_entry_speed_blocks_per_second
                common = dict(
                    endpoint_tolerance_blocks=min(
                        config.endpoint_tolerance_blocks,
                        entry_tolerance,
                    ),
                    handoff_entry_window=next_action.entry_window,
                )
                verified_kind = action_spec(next_action).solve_kind
                if (next_index in self._required_verified_motion
                        and verified_kind is not None):
                    # The R4 solver validates the continuous entry state.  The
                    # approach controller therefore preserves eligible motion
                    # and leaves only one tick of input ownership at the edge.
                    policy = self.air_transition_policies[verified_kind]
                    effective_window = (
                        replace(
                            next_action.entry_window,
                            minimum_speed_blocks_per_second=0.0,
                            maximum_speed_blocks_per_second=(
                                policy.maximum_entry_speed_blocks_per_second
                            ),
                            maximum_velocity_direction_error_radians=math.radians(
                                policy.maximum_velocity_heading_error_degrees
                            ),
                            maximum_yaw_error_radians=(
                                math.radians(
                                    policy.maximum_velocity_heading_error_degrees
                                )
                                if next_action.entry_window.required_yaw_radians
                                   is not None else None
                            ),
                        )
                        if next_action.entry_window is not None else None
                    )
                    config = replace(
                        config, **{
                            **common,
                            "handoff_entry_window": effective_window,
                        },
                        handoff_speed_blocks_per_second=(
                            policy.maximum_entry_speed_blocks_per_second
                        ),
                        input_lease_ticks=1,
                    )
                else:
                    config = replace(
                        config, **common,
                        stopped_speed_blocks_per_second=min(
                            config.stopped_speed_blocks_per_second,
                            entry_speed,
                        ),
                    )
            if (self.action_index + 1 == len(self.route.actions)
                    and self.route.goal_state is not None):
                config = terminal_route_config(config, motion_profile,
                    self.route.goal_state, action.fixed_route.points[-1],
                    action.fixed_route.execution_contract)
            controller = FixedRouteController(
                motion_profile, config, mode_profile=mode_profile,
            )
            tracking_route = action.fixed_route
            previous = self._verified_motion.get(self.action_index - 1)
            previous_continuation = (previous.proof.continuation if previous is not None else None)
            if previous_continuation is None and self.action_index > 0:
                previous_action = self.route.actions[self.action_index - 1]
                if type(previous_action) is WalkSegment and previous_action.traversal_plan is not None:
                    previous_continuation = previous_action.traversal_plan.continuation
            if (previous_continuation is not None
                    and previous_continuation.following_route_id == tracking_route.route_id
                    and action.traversal_plan is None):
                # The completed proof established this actual entry on the
                # first straight leg. Do not route back through its old centre
                # or let projection clip progress to the discarded point.
                from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
                progress = previous_continuation.progress(frame.body.position)
                remaining = tracking_route.points[1:]
                window = previous_continuation.entry_window
                dx, dz = window.horizontal_approach_direction
                for point_index, point in enumerate(remaining):
                    lateral = (-(point.x - window.reference_point[0]) * dz
                               + (point.z - window.reference_point[2]) * dx)
                    if (abs(lateral) > 1.0e-7 or abs(point.y - window.reference_point[1]) > 1.0e-7
                            or previous_continuation.progress((point.x, point.y, point.z)) > progress + 1.0e-7):
                        remaining = remaining[point_index:]
                        break
                tracking_route = FixedRoute(tracking_route.route_id,
                    (RoutePoint(*frame.body.position), *remaining),
                    tracking_route.execution_contract)
            controller.start(
                tracking_route, frame,
                traversal_plan=action.traversal_plan,
                continuation=(None if action.traversal_plan is None
                              else action.traversal_plan.continuation),
            )
        elif type(action) is JumpUpSegment:
            admitted = self._verified_motion.get(self.action_index)
            if admitted is not None:
                controller = admitted.committed_executor or VerifiedMotionExecutor()
                controller.start(admitted)
                self._controller = controller
                return
            if self.action_index in self._required_verified_motion:
                self._controller = None
                return
            if action.edge.profile_id != self.jump_profile.profile_id:
                raise ContractViolation("JumpUp segment uses another calibrated profile")
            controller = JumpUpController(self.jump_profile)
            controller.start(action.edge.start, action.edge.end, frame)
        elif action_spec(action).controller_adapter is not None:
            admitted = self._verified_motion.get(self.action_index)
            if admitted is not None:
                controller = admitted.committed_executor or VerifiedMotionExecutor()
                controller.start(admitted)
                self._controller = controller
                return
            if self.action_index in self._required_verified_motion:
                self._controller = None
                return
            controller = action_spec(action).controller_adapter.create(action, frame, self.air_profiles)
        else:
            assert type(action) is StepSegment
            if self.step_profile is None:
                raise ContractViolation("Step segment requires a calibrated profile")
            if action.edge.profile_id != self.step_profile.profile_id:
                raise ContractViolation("Step segment uses another calibrated profile")
            controller = StepController(self.step_profile)
            controller.start(action.start_surface, action.end_surface, frame)
        self._controller = controller

    @staticmethod
    def _landed_on_current_action_destination(
        action,
        frame: NavigationFrame,
        continuation=None,
    ) -> bool:
        """Accept a lost proof once the observed body is on its end support.

        A delayed command can change the exact landing point without changing
        the topological result of an air action.  Replaying the old entry from
        the new support would try to perform the same height transition twice.
        The following ground segment is responsible for tracking from the
        observed landing position, so only support height and footprint overlap
        are required here.
        """
        if not frame.body.is_on_ground:
            return False
        if continuation is not None:
            from mc2p.motion_nav.segment_entry import body_fits_segment_entry
            from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
            window = continuation.recovery_entry_window or continuation.entry_window
            if not body_fits_segment_entry(window, frame.body, continuation.mode):
                return False
            support = query_support(frame.body.body_box, frame.world)
            clearance = sweep(frame.body.body_box, (0., 0., 0.), frame.world)
            return (support.status is QueryStatus.FEASIBLE
                    and clearance.status is QueryStatus.FEASIBLE
                    and support.support_fraction + 1.0e-9
                        >= continuation.minimum_recovery_support_fraction)
        return action_spec(action).completed(action, frame)

    def start(self, route: ActionRoute, frame: NavigationFrame, *,
              damage_budget: TaskDamageBudget = TaskDamageBudget(),
              verified_motion: tuple[AdmittedMotionCandidate, ...] = (),
              require_verified_gap_motion: bool = True,
              require_verified_motion_actions: frozenset[int] = frozenset(),
              ) -> None:
        """Start one route under the current B10 execution contract.

        Setting ``require_verified_gap_motion`` to ``False`` is a bounded B09
        regression hook. Formal runtime callers must keep the default.
        """
        if type(route) is not ActionRoute or type(frame) is not NavigationFrame:
            raise ContractViolation("action route start requires a route and frame")
        if type(damage_budget) is not TaskDamageBudget:
            raise ContractViolation("action route damage budget must be typed")
        if (type(verified_motion) is not tuple
                or any(type(candidate) is not AdmittedMotionCandidate
                       for candidate in verified_motion)):
            raise ContractViolation("verified route motion must be immutable and admitted")
        if type(require_verified_gap_motion) is not bool:
            raise ContractViolation("verified gap requirement must be boolean")
        if (type(require_verified_motion_actions) is not frozenset
                or any(type(index) is not int
                       or not 0 <= index < len(route.actions)
                       or not action_spec(route.actions[index]).requires_verified_motion
                       for index in require_verified_motion_actions)):
            raise ContractViolation("required verified motion indices are invalid")
        if self.state in {ActionRouteState.RUNNING, ActionRouteState.CANCELLING}:
            raise ContractViolation("action route executor is already active")
        self.discard_prepared_candidates()
        self.route = route
        self.state = ActionRouteState.RUNNING
        self.action_index = 0
        self._cancel_requested = False
        self._stop_cause = None
        self._actions_finished = False
        self._final_action_failure_cause: StopCause | None = None
        self._damage_budget = damage_budget
        self._session = frame.session
        self._verified_motion = {}
        self._prepared_action_indices = frozenset()
        self._last_prepared_frame = None
        self._last_prepared_anchor = None
        self._last_prepared_ledger = None
        self._require_verified_gap_motion = require_verified_gap_motion
        self._required_verified_motion = frozenset({
            *require_verified_motion_actions,
            *(index for index, action in enumerate(route.actions)
              if require_verified_gap_motion
              and type(action) is JumpGapSegment),
        })
        self._completed_movement_damage_points = 0.0
        self._committed_damage_actions.clear()
        self._completion_epoch += 1
        self._pending_completion_candidate_cleanup.clear()
        self._input_scope_floor = None
        self.clear_pending_action_boundary()
        for candidate in verified_motion:
            self._validate_verified_motion(route, candidate, damage_budget)
            index = candidate.context.action_index
            if index in self._verified_motion:
                raise ContractViolation("duplicate verified motion for one route action")
            self._verified_motion[index] = candidate
        self._activate(frame)

    @staticmethod
    def _validate_verified_motion(
            route: ActionRoute, candidate: AdmittedMotionCandidate,
            damage_budget: TaskDamageBudget) -> None:
        context = candidate.context
        if context.route_id != route.route_id:
            raise ContractViolation("verified motion belongs to another route")
        if not 0 <= context.action_index < len(route.actions):
            raise ContractViolation("verified motion action is outside the route")
        action = route.actions[context.action_index]
        expected_kind = action_spec(action).solve_kind
        if expected_kind is None or candidate.proof.kind is not expected_kind:
            raise ContractViolation(
                "verified motion kind does not match its route action"
            )
        if context.damage_budget != damage_budget:
            raise ContractViolation("verified motion uses another risk policy")

    def install_verified_motion(self, candidate: AdmittedMotionCandidate) -> None:
        if type(candidate) is not AdmittedMotionCandidate or self.route is None:
            raise ContractViolation("installing verified motion requires an active route")
        self._validate_verified_motion(
            self.route, candidate, self._damage_budget,
        )
        index = candidate.context.action_index
        activating_current = (
            index == self.action_index
            and self._controller is None
            and index in self._required_verified_motion
        )
        if index < self.action_index or (index == self.action_index
                                         and not activating_current):
            raise ContractViolation("verified motion arrived after its action activated")
        current = self._verified_motion.get(index)
        if (current is not None
                and current.context.candidate_revision
                    >= candidate.context.candidate_revision):
            raise ContractViolation("verified motion revision did not advance")
        self.discard_prepared_candidates(action_index=index)
        self._verified_motion[index] = candidate
        if activating_current:
            controller = candidate.committed_executor or VerifiedMotionExecutor()
            controller.start(candidate)
            self._controller = controller

    def has_verified_motion(self, action_index: int) -> bool:
        if type(action_index) is not int or action_index < 0:
            raise ContractViolation("verified motion lookup requires an action index")
        return action_index in self._verified_motion

    def active_verified_exit_state(self) -> PhysicsState | None:
        """Expose one already-selected proof exit for bounded look-ahead."""
        if type(self._controller) is not VerifiedMotionExecutor:
            return None
        return self._controller.predicted_exit_state()

    def active_verified_entry_state(self) -> PhysicsState | None:
        """Expose the stale proof entry while a grounded retry still owns it."""
        if type(self._controller) is not VerifiedMotionExecutor:
            return None
        return self._controller.entry_state()

    def active_verified_exit_hold_input(self):
        controller = self._controller
        return None if type(controller) is not VerifiedMotionExecutor else controller.predicted_exit_hold_input()

    def current_verified_action_started(self) -> bool:
        """Return whether the current verified action owns the body already."""
        return (
            type(getattr(self, "_controller", None)) is VerifiedMotionExecutor
            and self._controller.has_started()
        )

    def current_verified_motion_can_start(self, anchor: StateAnchor) -> bool | None:
        """Check the installed proof before its first command is submitted."""
        if type(anchor) is not StateAnchor:
            raise ContractViolation("verified entry check requires an anchor")
        if type(getattr(self, "_controller", None)) is not VerifiedMotionExecutor:
            return None
        if self._controller.has_started():
            return None
        return self._controller.can_start_from(anchor)

    def discard_unstarted_verified_motion(self) -> bool:
        """Return the current action to its bounded solve boundary."""
        controller = getattr(self, "_controller", None)
        if type(controller) is not VerifiedMotionExecutor:
            return False
        if controller.has_started():
            raise ContractViolation(
                "started verified motion cannot return to entry solving"
            )
        self.discard_prepared_candidates(action_index=self.action_index)
        self._verified_motion.pop(self.action_index, None)
        self._controller = None
        return True

    def reprepare_grounded_verified_motion(
        self,
        frame: NavigationFrame,
    ) -> bool:
        """Return a failed air action to solving while it is still supported.

        A verified action may already have submitted one or more approach
        inputs without leaving the start surface.  If a later command is lost,
        the old proof no longer applies, but the body is still available for a
        fresh proof from the observed state.  Keep the route and body owner;
        only discard the stale candidate and controller.
        """
        if type(frame) is not NavigationFrame:
            raise ContractViolation(
                "grounded verified reprepare requires a navigation frame"
            )
        controller = getattr(self, "_controller", None)
        if (type(controller) is not VerifiedMotionExecutor
                or controller.state is not VerifiedMotionExecutorState.INPUT_LOST
                or not frame.body.is_on_ground
                or self.action_index not in self._required_verified_motion):
            return False
        self.discard_prepared_candidates(action_index=self.action_index)
        self._verified_motion.pop(self.action_index, None)
        self._controller = None
        self.state = ActionRouteState.RUNNING
        return True

    def retain_landing_after_verified_input_loss(
        self,
        anchor: StateAnchor,
    ) -> bool:
        """Keep a lost verified action until its residual motion lands."""
        controller = getattr(self, "_controller", None)
        if (type(controller) is not VerifiedMotionExecutor
                or not controller.retain_landing_after_input_loss(anchor)):
            return False
        self.state = ActionRouteState.CANCELLING
        return True

    def requires_safe_handoff(self, frame: NavigationFrame) -> bool:
        """Return whether another route must wait for this action to finish."""
        if type(frame) is not NavigationFrame:
            raise ContractViolation("route handoff requires a navigation frame")
        if self.route is None or self._controller is None or self.state in {
            ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
            ActionRouteState.FAILED, ActionRouteState.UNSUPPORTED,
            ActionRouteState.INPUT_LOST,
        }:
            return False
        if not frame.body.is_on_ground:
            return True
        if type(self._controller) is VerifiedMotionExecutor:
            return self._controller.state in {
                VerifiedMotionExecutorState.RUNNING,
                VerifiedMotionExecutorState.RECOVERING,
            }
        return action_spec(self.route.actions[self.action_index]).body_commitment is BodyCommitment.TRANSITION

    def register_verified_submission(
            self, command_index: int, *, control_sequence: int,
            requested_movement_tick: int,
            requested_latest_movement_tick: int | None = None) -> None:
        if type(self._controller) is not VerifiedMotionExecutor:
            raise ContractViolation("current route action is not verified motion")
        self._controller.register_submission(
            command_index, control_sequence=control_sequence,
            requested_movement_tick=requested_movement_tick,
            requested_latest_movement_tick=requested_latest_movement_tick,
        )

    @property
    def has_committed_execution(self) -> bool:
        return getattr(self._controller, "committed_execution", False)

    def exact_verified_release_ready(self, frame, anchor, ledger) -> bool:
        if (not self.has_committed_execution or anchor is None or ledger is None
                or anchor.observation_sequence_id != frame.body.sequence_id
                or anchor.session != frame.session or self.route is None
                or frame.world._owner is None
                or self._controller._candidate.context.route_id != self.route.route_id
                or self._controller._candidate.context.action_index != self.action_index):
            return False
        source = frame.world._owner
        versions = self._controller._candidate.proof.world_dependency_versions
        return (source.dependency_changes(versions).status is DependencyStatus.UNCHANGED
                and self._controller.exact_release_ready(anchor, ledger,
                    changed_cells=frame.changed_cells))

    def request_stop(self, cause: StopCause) -> None:
        if type(cause) is not StopCause:
            raise ContractViolation("action route stop cause must be typed")
        self._stop_cause = cause
        self.cancel()

    def cancel(self) -> None:
        self.clear_pending_action_boundary()
        if self.state in {
            ActionRouteState.RUNNING,
            ActionRouteState.NEEDS_INFORMATION,
            ActionRouteState.BLOCKED,
            ActionRouteState.CANCELLING,
        }:
            self._cancel_requested = True
            self.state = ActionRouteState.CANCELLING
            if self._controller is None:
                self.state = ActionRouteState.CANCELLED
                return
            if type(self._controller) is not VerifiedMotionExecutor:
                self._controller.cancel()

    def stop_protection(
        self, previous: ActionRouteDecision, frame: NavigationFrame, *,
        state_anchor: StateAnchor | None,
    ) -> ActionRouteDecision:
        """Apply an accepted stop to this frame's result without advancing it.

        The ordinary decide already consumed this observation.  Only start
        cancellation and replace an unsubmitted departure; do not poll input,
        increment the action/command index or decide a controller again.
        """
        if (type(previous) is not ActionRouteDecision
                or previous.action_index != self.action_index
                or self._stop_cause is None):
            raise ContractViolation("stop protection requires the current stopped action")
        started = time.perf_counter_ns()
        if self.state in {
            ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
            ActionRouteState.FAILED, ActionRouteState.UNSUPPORTED,
            ActionRouteState.INPUT_LOST,
        }:
            return self._terminal_decision(frame, started, state_anchor)
        controller = self._controller
        if (self._stop_cause is StopCause.DEPENDENCY_CHANGED
                and action_spec(self.route.actions[self.action_index]).stop_hold.dependency_movement is not None
                and frame.body.is_on_ground
                and math.hypot(frame.body.velocity_blocks_per_second[0],
                               frame.body.velocity_blocks_per_second[2]) > .10):
            return self._result(started, action_spec(self.route.actions[self.action_index]).stop_hold.dependency_movement, 1,
                                "grounded_dependency_stop")
        if type(controller) is VerifiedMotionExecutor:
            if (state_anchor is None or state_anchor.session != frame.session
                    or state_anchor.observation_sequence_id != frame.body.sequence_id):
                recovery = controller.recover_without_anchor()
                self.state = ActionRouteState.CANCELLING
                return self._result(started, recovery.movement or MovementV1(), 1,
                                    recovery.reason, submit_input=recovery.movement is not None)
            was_running = controller.state is VerifiedMotionExecutorState.RUNNING
            controller.cancel(state_anchor,
                preserve_verified_remainder=self._stop_cause is not StopCause.DEPENDENCY_CHANGED)
            if controller.committed_execution:
                return replace(previous, state=self.state, body_phase=None)
            if not was_running:
                return replace(previous, state=self.state, body_phase=None)
            self.state = ActionRouteState.CANCELLING
            if not state_anchor.physics_state.on_ground:
                # decide already checked this exact command and its window.
                # Keep that legal remainder; cancel has recorded this tick.
                if self._stop_cause is StopCause.DEPENDENCY_CHANGED:
                    return self._result(started, MovementV1(), 1,
                                        "retain_landing_responsibility")
                return replace(previous, state=self.state, body_phase=None,
                    reason_code="complete_verified_landing_after_cancel")
            speed = math.hypot(state_anchor.physics_state.velocity_blocks_per_tick[0],
                               state_anchor.physics_state.velocity_blocks_per_tick[2])
            return self._result(started, MovementV1(), 1,
                "awaiting_cancel_observation" if speed <= .01
                else "retain_landing_responsibility")
        adapter = action_spec(self.route.actions[self.action_index]).controller_adapter
        if adapter is not None and adapter.stop_protection is not None:
            outcome = adapter.stop_protection(controller, frame)
            if outcome is None:
                return replace(previous, state=self.state, body_phase=None)
            if type(outcome) is not ActionControllerResult:
                raise ContractViolation('action stop protection requires a typed result')
            if outcome.state is not None:
                self.state = outcome.state
            return self._result(started, outcome.movement, outcome.input_lease_ticks,
                                outcome.reason, outcome.missing_cells, outcome.look)
        raise ContractViolation("same-frame stop protection requires the current air action")

    def _terminal_decision(
        self, frame: NavigationFrame, started: int,
        state_anchor: StateAnchor | None,
        movement_yaw_radians: float | None = None,
    ) -> ActionRouteDecision:
        if self.has_committed_execution:
            return self._result(started, MovementV1(), 1, self.state.value, submit_input=False)
        # A business terminal does not release a body perched on an edge.
        if (state_anchor is not None
                and state_anchor.observation_sequence_id == frame.body.sequence_id
                and frame.body.is_on_ground
                and verified_ground_rollout(
                    frame, state_anchor.physics_state, MovementV1(),
                    control_ticks=0, tail_ticks=8, minimum_support=.01,
                ) is None):
            recovery = verified_ground_recovery_movement(
                frame, (state_anchor.physics_state
                        if movement_yaw_radians is None else
                        replace(state_anchor.physics_state,
                                yaw_radians=movement_yaw_radians)),
            )
            if recovery is not None:
                return replace(self._result(
                    started, recovery, 1, "recovering_terminal_ground_support",
                ), body_phase=BodyControlPhase.STOPPING)
        return self._result(started, MovementV1(), 1, self.state.value,
                            submit_input=False)

    def _result(self, started: int, movement: MovementV1, lease: int,
                reason: str, missing: tuple[BlockPos, ...] = (),
                look: LookV1 | None = None, *,
                submit_input: bool = True,
                verified_command_index: int | None = None,
                expected_movement_tick: int | None = None,
                latest_movement_tick: int | None = None,
                requires_verified_motion: bool = False,
                ground_handoff_disposition=GroundHandoffDisposition.NOT_REQUESTED,
                route_progress_evidence: RouteProgressEvidence | None = None,
                safe_default: SafeDefaultV1 | None = None,
                ) -> ActionRouteDecision:
        return ActionRouteDecision(
            self.state, movement, look, lease, self.action_index, reason, missing,
            time.perf_counter_ns() - started, submit_input,
            verified_command_index, expected_movement_tick,
            latest_movement_tick, requires_verified_motion,
            ground_handoff_disposition=ground_handoff_disposition,
            route_progress_evidence=route_progress_evidence,
            failure_cause=(self._final_action_failure_cause
                           if self.state in {ActionRouteState.FAILED, ActionRouteState.NEEDS_REPLAN} else None),
            safe_default=safe_default,
        )

    def prepare_ground_handoff(self, target: GroundHandoffTarget | None) -> None:
        """Provide a tracking target without changing route or body ownership."""
        if type(self._controller) is FixedRouteController:
            self._controller.set_handoff_target(target)

    def _upcoming_entry_look(
        self,
        frame: NavigationFrame,
        conditioned_yaw_radians: float | None,
    ) -> tuple[float, LookV1] | None:
        """Return the generic next-entry yaw while the current Walk owns approach.

        The next segment declares the required yaw in its entry window.  This
        executor only consumes that contract; it does not inspect the next
        action type.  Starting within roughly one block gives the ordinary
        ground controller room to keep tracking the same world-space route
        while the view turns.
        """
        if (self.route is None
                or self.action_index + 1 >= len(self.route.actions)):
            return None
        window = getattr(
            self.route.actions[self.action_index + 1], "entry_window", None,
        )
        if window is None or window.required_yaw_radians is None:
            return None
        x, _, z = frame.body.position
        rx, _, rz = window.reference_point
        assert type(self._controller) is FixedRouteController
        if (math.hypot(x - rx, z - rz)
                > self._controller.config.lookahead_max_blocks):
            return None
        required = window.required_yaw_radians
        observed_delta = math.atan2(
            math.sin(required - frame.body.yaw_radians),
            math.cos(required - frame.body.yaw_radians),
        )
        tolerance = window.maximum_yaw_error_radians or 0.0
        if conditioned_yaw_radians is not None:
            # The Runtime caller has already selected this frame's external
            # combat/safety view.  Let ordinary Walk use that final yaw.  If
            # it does not satisfy the next entry, the strict successor stays
            # closed and the existing ground progress bound ends the wait.
            return None
        if abs(observed_delta) <= tolerance + 1.0e-9:
            return None
        return required, LookV1(math.degrees(observed_delta), 0.0)

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True,
               state_anchor: StateAnchor | None = None,
               input_ledger: InputApplicationLedger | None = None,
               movement_yaw_radians: float | None = None) -> ActionRouteDecision:
        started = time.perf_counter_ns()
        if type(frame) is not NavigationFrame:
            raise ContractViolation("action route decision requires a navigation frame")
        self._last_prepared_frame = frame
        self._last_prepared_anchor = state_anchor
        self._last_prepared_ledger = input_ledger
        if (movement_yaw_radians is not None
                and (type(movement_yaw_radians) not in (int, float)
                     or not math.isfinite(movement_yaw_radians))):
            raise ContractViolation("movement yaw must be finite")
        if self.route is None:
            self.state = ActionRouteState.IDLE
            return self._result(started, MovementV1(), 1, "not_started")
        if input_ledger is not None and self._input_scope_floor is None:
            self._input_scope_floor = max(
                (record.control_sequence for record in input_ledger.snapshot()),
                default=0,
            )
        if self.state in {
            ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
            ActionRouteState.FAILED, ActionRouteState.UNSUPPORTED,
            ActionRouteState.INPUT_LOST,
        }:
            return self._terminal_decision(
                frame, started, state_anchor, movement_yaw_radians,
            )
        if self._controller is None:
            if self.action_index in self._required_verified_motion:
                self.state = ActionRouteState.RUNNING
                return self._result(
                    started, MovementV1(), 1, "awaiting_verified_motion",
                    submit_input=False, requires_verified_motion=True,
                )
            self.state = ActionRouteState.IDLE
            return self._result(started, MovementV1(), 1, "not_started")
        if (frame.session != self._session or frame.body.session != self._session
                or frame.world.session != self._session):
            self.state = ActionRouteState.FAILED
            return self._result(started, MovementV1(), 1, "world_session_changed")
        if self._actions_finished:
            if self._cancel_requested:
                self.state = ActionRouteState.CANCELLED
                return self._result(started, MovementV1(), 1,
                                    "cancelled_after_actions")
            if not input_confirmed:
                self.state = ActionRouteState.INPUT_LOST
                return self._result(started, MovementV1(), 1,
                                    "input_application_unconfirmed")
            return self._finish_goal(frame, started, input_ledger, state_anchor)
        action = self.route.actions[self.action_index]
        self._commit_action_damage_if_started(action, frame)
        if (self._cancel_requested
                and self._stop_cause is StopCause.DEPENDENCY_CHANGED
                and action_spec(action).stop_hold.dependency_movement is not None
                and frame.body.is_on_ground
                and math.hypot(
                    frame.body.velocity_blocks_per_second[0],
                    frame.body.velocity_blocks_per_second[2],
                ) > .10):
            # The landing changed while departure was still preventable.
            # Neutral input only brakes; it does not keep a moving player on
            # the edge.  Hold sneak until the observed body is stationary,
            # then finish cancellation without replaying the stale proof.
            self.state = ActionRouteState.CANCELLING
            return self._result(
                started, action_spec(action).stop_hold.dependency_movement, 1,
                "grounded_dependency_stop",
            )
        if type(self._controller) is VerifiedMotionExecutor:
            if (type(state_anchor) is not StateAnchor
                    or type(input_ledger) is not InputApplicationLedger):
                verified = self._controller.recover_without_anchor()
            elif self._cancel_requested:
                self._controller.cancel(
                    state_anchor,
                    preserve_verified_remainder=(
                        self._stop_cause is not StopCause.DEPENDENCY_CHANGED
                    ),
                )
                verified = self._controller.decide(
                    state_anchor, input_ledger, changed_cells=frame.changed_cells,
                )
            else:
                verified = self._controller.decide(
                    state_anchor, input_ledger, changed_cells=frame.changed_cells,
                )
            if (not self._cancel_requested
                    and verified.state is VerifiedMotionExecutorState.INPUT_LOST
                    and type(state_anchor) is StateAnchor
                    and (
                        self._controller.confirm_observed_exit_after_input_loss(
                            state_anchor,
                        )
                        or (
                            self.action_index in self._verified_motion
                            and self._landed_on_current_action_destination(
                                action, frame,
                                self._verified_motion[
                                    self.action_index
                                ].proof.continuation,
                            )
                        )
                    )):
                return self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                    movement_yaw_radians=movement_yaw_radians,
                    completed_after_input_loss=True,
                )
            terminal = {
                VerifiedMotionExecutorState.CANCELLED: ActionRouteState.CANCELLED,
                VerifiedMotionExecutorState.FAILED: ActionRouteState.FAILED,
                VerifiedMotionExecutorState.INPUT_LOST: ActionRouteState.INPUT_LOST,
            }
            if verified.state is VerifiedMotionExecutorState.COMPLETE:
                return self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                    movement_yaw_radians=movement_yaw_radians,
                )
            if verified.state in terminal:
                self.state = terminal[verified.state]
            elif verified.state is VerifiedMotionExecutorState.RECOVERING:
                self.state = ActionRouteState.CANCELLING
            else:
                self.state = ActionRouteState.RUNNING
            look = None
            if verified.movement_yaw_radians is not None:
                delta = math.atan2(
                    math.sin(verified.movement_yaw_radians - frame.body.yaw_radians),
                    math.cos(verified.movement_yaw_radians - frame.body.yaw_radians),
                )
                # Even an unchanged route yaw must win arbitration. Otherwise
                # a simultaneous combat/safety look can invalidate this input.
                look = LookV1(math.degrees(delta), 0.0)
            if (self._controller.committed_exit_choice
                    and self.prepared_successor_required):
                self._pending_action_boundary_index = self.action_index + 1
                verified = replace(verified, reason="prepared_action_boundary")
            safe_default = (
                self._controller.prearmed_exit_default(verified)
                if (self.prepared_successor_required
                    and input_ledger is not None
                    and input_ledger.action_timing_v2) else None
            )
            return self._result(
                started, verified.movement or MovementV1(),
                max(1, verified.input_lease_ticks), verified.reason,
                look=look, submit_input=verified.movement is not None,
                verified_command_index=(
                    verified.command_index
                    if verified.submittable_as_verified_command else None
                ),
                expected_movement_tick=(
                    verified.expected_movement_tick
                    if verified.submittable_as_verified_command else None
                ),
                latest_movement_tick=(
                    verified.latest_movement_tick
                    if verified.submittable_as_verified_command else None
                ),
                safe_default=safe_default,
            )
        if type(action) is WalkSegment:
            entry_look = self._upcoming_entry_look(
                frame, movement_yaw_radians,
            )
            if entry_look is not None:
                movement_yaw_radians = entry_look[0]
            movement_frame = frame
            if movement_yaw_radians is not None:
                movement_frame = replace(
                    frame,
                    body=replace(
                        frame.body,
                        yaw_radians=float(movement_yaw_radians),
                    ),
                )
            first_command = self._controller._last_sequence is None
            ground_state = (None if state_anchor is None else replace(
                state_anchor.physics_state,
                yaw_radians=movement_frame.body.yaw_radians,
                pitch_radians=movement_frame.body.pitch_radians,
            ))
            ground_cache = WorldQueryCache(movement_frame.world)
            decision = self._controller.decide(
                movement_frame, input_confirmed=input_confirmed,
                physics_state=ground_state,query_cache=ground_cache,
            )
            if type(decision) is not FixedRouteDecision:
                raise ContractViolation(
                    "ordinary Walk requires a typed fixed-route decision"
                )
            progress_evidence = RouteProgressEvidence(
                self.action_index,
                action.fixed_route.route_id,
                decision.progress_blocks,
                frame.body.sequence_id,
            )
            successor = (
                None if self._cancel_requested or not input_confirmed
                or state_anchor is None or input_ledger is None
                else self._verified_motion.get(self.action_index + 1)
            )
            verified_successor_ready = (
                successor is not None
                and verified_candidate_can_start(successor, state_anchor)
            )
            boundary_ready = (
                self._pending_action_boundary_index == self.action_index + 1
            )
            if ((verified_successor_ready or boundary_ready)
                    and decision.state in {
                        FixedRouteState.RUNNING,
                        FixedRouteState.BRAKING,
                        FixedRouteState.SUCCEEDED,
                    }
                    and decision.handoff_disposition
                        is not GroundHandoffDisposition.REJECTED):
                # The verified successor may start before the graph reference
                # point, but retirement still uses this frame's real Walk
                # decision. Only the successor command is returned.
                advanced = self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                    movement_yaw_radians=movement_yaw_radians,
                )
                return replace(
                    advanced,
                    route_progress_evidence=progress_evidence,
                )
            if decision.state is FixedRouteState.SUCCEEDED:
                if self._cancel_requested:
                    self.state = ActionRouteState.CANCELLED
                    return self._result(started, MovementV1(), 1, "cancelled_on_ground")
                advanced = self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                    movement_yaw_radians=movement_yaw_radians,
                )
                return replace(
                    advanced,
                    route_progress_evidence=progress_evidence,
                )
            mapping = {
                FixedRouteState.BLOCKED: ActionRouteState.BLOCKED,
                FixedRouteState.NEEDS_INFORMATION: ActionRouteState.NEEDS_INFORMATION,
                FixedRouteState.UNSUPPORTED: ActionRouteState.UNSUPPORTED,
                FixedRouteState.INPUT_LOST: ActionRouteState.INPUT_LOST,
                FixedRouteState.FAILED: ActionRouteState.FAILED,
                FixedRouteState.CANCELLED: ActionRouteState.CANCELLED,
                FixedRouteState.NEEDS_REPLAN: ActionRouteState.NEEDS_REPLAN,
            }
            if decision.state in mapping:
                self.state = mapping[decision.state]
            ordinary_start_window = _ordinary_walk_start_window(
                action, decision, movement_frame,physics_state=ground_state,
                profile=self.ground_profile,query_cache=ground_cache,
                first_command=first_command,
                minimum_support=self._controller.config.minimum_support_fraction,
            )
            return self._result(
                started, decision.movement, decision.input_lease_ticks,
                decision.reason, decision.missing_cells,
                (entry_look[1] if entry_look is not None else None),
                expected_movement_tick=(
                    None if ordinary_start_window is None
                    else ordinary_start_window[0]
                ),
                latest_movement_tick=(
                    None if ordinary_start_window is None
                    else ordinary_start_window[1]
                ),
                ground_handoff_disposition=decision.handoff_disposition,
                route_progress_evidence=progress_evidence,
            )

        decision = self._controller.decide(frame, input_confirmed=input_confirmed)
        if type(action) is StepSegment:
            terminal = {
                StepState.BLOCKED: ActionRouteState.BLOCKED,
                StepState.NEEDS_INFORMATION: ActionRouteState.NEEDS_INFORMATION,
                StepState.UNSUPPORTED: ActionRouteState.UNSUPPORTED,
                StepState.FAILED: ActionRouteState.FAILED,
                StepState.CANCELLED: ActionRouteState.CANCELLED,
                StepState.INPUT_LOST: ActionRouteState.INPUT_LOST,
            }
            if decision.state is StepState.COMPLETE:
                return self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                )
            if decision.state in terminal:
                self.state = terminal[decision.state]
            elif self._cancel_requested:
                self.state = ActionRouteState.CANCELLING
            return self._result(
                started, decision.movement, decision.input_lease_ticks,
                decision.reason_code, decision.missing_cells,
            )
        adapter = action_spec(action).controller_adapter
        if adapter is not None:
            outcome = adapter.interpret(decision)
            if type(outcome) is not ActionControllerResult:
                raise ContractViolation('action adapter requires a typed result')
            if outcome.state is ActionRouteState.COMPLETE:
                return self._advance(frame, started, state_anchor=state_anchor,
                                     input_ledger=input_ledger)
            if outcome.state is not None:
                self.state = outcome.state
            elif self._cancel_requested:
                self.state = ActionRouteState.CANCELLING
            return self._result(started, outcome.movement, outcome.input_lease_ticks,
                                outcome.reason, outcome.missing_cells, outcome.look)
        terminal = {
            JumpUpState.BLOCKED: ActionRouteState.BLOCKED,
            JumpUpState.NEEDS_INFORMATION: ActionRouteState.NEEDS_INFORMATION,
            JumpUpState.UNSUPPORTED: ActionRouteState.UNSUPPORTED,
            JumpUpState.FAILED: ActionRouteState.FAILED,
            JumpUpState.CANCELLED: ActionRouteState.CANCELLED,
            JumpUpState.INPUT_LOST: ActionRouteState.INPUT_LOST,
        }
        if decision.state is JumpUpState.COMPLETE:
            return self._advance(
                frame, started, state_anchor=state_anchor,
                input_ledger=input_ledger,
            )
        if decision.state in terminal:
            self.state = terminal[decision.state]
        elif self._cancel_requested:
            self.state = ActionRouteState.CANCELLING
        return self._result(
            started, decision.movement, decision.input_lease_ticks,
            decision.reason_code, decision.missing_cells, decision.look,
        )

    def _advance(self, frame: NavigationFrame, started: int, *,
                 state_anchor: StateAnchor | None = None,
                 input_ledger: InputApplicationLedger | None = None,
                 movement_yaw_radians: float | None = None,
                 completed_after_input_loss: bool = False) -> ActionRouteDecision:
        assert self.route is not None
        next_index = self.action_index + 1
        if next_index in self._prepared_action_indices and not self._cancel_requested:
            # This is only the owner's boundary fact. Task 8 must obtain registry
            # acceptance and delivery before it can commit an action advance.
            self._pending_action_boundary_index = next_index
            return self._result(started, MovementV1(), 1, "prepared_action_boundary",
                                submit_input=False)
        completed = self.route.actions[self.action_index]
        completion = self.prepare_action_completion(
            frame,
            observation_sequence_id=frame.body.sequence_id,
            expected_next_action_index=next_index,
        )
        if not self.commit_action_completion(completion):
            raise ContractViolation("ordinary action completion changed after preflight")
        self.finalize_action_completion(completion)
        if self.action_index >= len(self.route.actions):
            self.action_index = len(self.route.actions) - 1
            self._actions_finished = True
            self._final_action_failure_cause = (
                StopCause.INPUT_LOST if completed_after_input_loss else None
            )
            return self._finish_goal(frame, started, input_ledger, state_anchor)
        installed = self._verified_motion.get(self.action_index)
        if (installed is not None
                and self.action_index in self._required_verified_motion
                and state_anchor is not None):
            if verified_candidate_can_start(installed, state_anchor):
                # A proof may contain a one-tick delayed start variant.  Bind
                # the executor to the variant matching the observed boundary,
                # rather than retaining the earlier predicted start tick.
                self._verified_motion[self.action_index] = replace(
                    installed,
                    intended_start_tick=state_anchor.movement_tick_id + 1,
                )
            else:
                # The preceding action ended outside every proved start
                # variant.  No proof command has been submitted, so return to
                # solving rather than misclassifying drift as input loss.
                self.discard_prepared_candidates(action_index=self.action_index)
                self._verified_motion.pop(self.action_index, None)
        self._activate(frame)
        self.state = ActionRouteState.RUNNING
        # Run the new controller immediately so a hand-off does not introduce
        # an artificial neutral-input frame.
        return self.decide(
            frame, state_anchor=state_anchor, input_ledger=input_ledger,
            movement_yaw_radians=movement_yaw_radians,
        )

    def _commit_action_damage_if_started(
        self,
        action,
        frame: NavigationFrame,
        *,
        force: bool = False,
    ) -> None:
        """Book a fall once leaving support makes its risk unavoidable."""
        spec = action_spec(action)
        if (not spec.tracks_damage
                or self.action_index in self._committed_damage_actions
                or not spec.damage_committed(action, frame, force=force)):
            return
        self._completed_movement_damage_points += spec.expected_damage_points(action)
        self._committed_damage_actions.add(self.action_index)

    def _finish_goal(
        self, frame: NavigationFrame, started: int,
        input_ledger: InputApplicationLedger | None = None,
        state_anchor: StateAnchor | None = None,
    ) -> ActionRouteDecision:
        assert self.route is not None
        responsibility = assess_input_responsibility(
            input_ledger, state_anchor,
            previous_sequence_floor=self._input_scope_floor or 0,
        ).disposition
        if responsibility is InputResponsibilityDisposition.IN_FLIGHT:
            self.state = ActionRouteState.RUNNING
            return self._result(
                started, MovementV1(), 1,
                "waiting_for_previous_input_application",
            )
        if responsibility is InputResponsibilityDisposition.AMBIGUOUS_WAITING:
            self.state = ActionRouteState.INPUT_LOST
            return self._result(
                started, MovementV1(), 1,
                "previous_input_application_ambiguous",
            )
        goal = self.route.goal_state
        if goal is None:
            self.state = ActionRouteState.COMPLETE
            return self._result(started, MovementV1(), 1, "action_route_complete")
        observed = evaluate_observed_goal(
            frame, goal, self._damage_budget.risk_policy_id,
        )
        if observed.status is ObservedGoalStatus.NEEDS_INFORMATION:
            self.state = ActionRouteState.NEEDS_INFORMATION
            return self._result(
                started, MovementV1(), 1, "goal_support_requires_information",
                observed.missing_cells,
            )
        if observed.status is ObservedGoalStatus.SATISFIED:
            self.state = ActionRouteState.COMPLETE
            return self._result(started, MovementV1(), 1, "goal_state_satisfied")
        if observed.status is ObservedGoalStatus.HEADING_ONLY:
            self.state = ActionRouteState.RUNNING
            return self._result(
                started, MovementV1(), 1, "aligning_goal_heading",
                look=LookV1(observed.heading_delta_degrees, 0.0),
            )
        if (self._final_action_failure_cause is StopCause.INPUT_LOST
                and observed.status is ObservedGoalStatus.NOT_SATISFIED
                and frame.body.is_on_ground):
            self.state = ActionRouteState.NEEDS_REPLAN
            return self._result(
                started, MovementV1(), 1,
                "input_lost_landing_requires_replan",
            )
        if (self._final_action_failure_cause is StopCause.INPUT_LOST
                and observed.status is ObservedGoalStatus.NOT_SATISFIED):
            self.state = ActionRouteState.FAILED
            return self._result(
                started, MovementV1(), 1,
                "input_lost_after_safe_landing_before_goal",
            )
        self._final_action_failure_cause = None
        self.state = ActionRouteState.FAILED
        return self._result(
            started, MovementV1(), 1,
            ("goal_resource_unobservable" if observed.status is
             ObservedGoalStatus.RESOURCE_UNOBSERVABLE else
             "goal_state_not_satisfied"),
        )
