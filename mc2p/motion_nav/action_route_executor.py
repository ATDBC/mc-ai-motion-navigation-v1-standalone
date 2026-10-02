"""Sequential Walk/JumpUp execution without merging their state machines."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math
import time

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.action_v1 import LookV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.body_control import StopCause, BodyControlPhase
from mc2p.motion_nav.action_route import (
    ActionRoute, ControlledDropSegment, JumpGapSegment, JumpUpSegment,
    StepSegment, WalkSegment,
)
from mc2p.motion_nav.air_motion import AirMotionController, AirMotionProfile, AirMotionState
from mc2p.motion_nav.fixed_route import (
    FixedRouteConfig, FixedRouteController, FixedRouteState, GroundHandoffTarget,
    terminal_route_config,
    GroundHandoffDisposition,
)
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_modes import GroundModeProfiles
from mc2p.motion_nav.jump_up import JumpUpController, JumpUpProfile, JumpUpState
from mc2p.motion_nav.step_transition import StepController, StepProfile, StepState
from mc2p.motion_nav.goal_observation import (
    ObservedGoalStatus, evaluate_observed_goal,
)
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.motion_candidate import (
    AdmittedMotionCandidate, VerifiedMotionExecutor,
    VerifiedMotionExecutorState, verified_candidate_can_start,
)
from mc2p.motion_nav.motion_risk import (
    TaskDamageBudget, conservative_plain_fall_damage_points,
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
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos


class ActionRouteState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_INFORMATION = "needs_information"
    UNSUPPORTED = "unsupported"
    INPUT_LOST = "input_lost"
    NEEDS_REPLAN = "needs_replan"


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
        self._controller: (
            FixedRouteController | JumpUpController | StepController
            | AirMotionController | VerifiedMotionExecutor | None
        ) = None
        self._cancel_requested = False
        self._stop_cause: StopCause | None = None
        self._actions_finished = False
        self._damage_budget = TaskDamageBudget()
        self._session = None
        self._verified_motion: dict[int, AdmittedMotionCandidate] = {}
        self._require_verified_gap_motion = False
        self._required_verified_motion: frozenset[int] = frozenset()
        self._completed_movement_damage_points = 0.0
        self._committed_damage_actions: set[int] = set()
        self._input_scope_floor: int | None = None

    @property
    def completed_movement_damage_points(self) -> float:
        return self._completed_movement_damage_points

    def enter_upcoming_action_boundary(
        self, next_index: int, frame: NavigationFrame,
    ) -> bool:
        """Finish a Walk once the body is already on the next action support."""
        if (self.route is None
                or type(next_index) is not int
                or next_index != self.action_index + 1
                or next_index >= len(self.route.actions)
                or type(self.route.actions[self.action_index]) is not WalkSegment
                or type(self.route.actions[next_index]) is not
                    ControlledDropSegment
                or not frame.body.is_on_ground):
            return False
        upcoming = self.route.actions[next_index]
        region = upcoming.start_surface.region
        x, y, z = frame.body.position
        if (abs(y - upcoming.start_surface.position[1]) > .10
                or x < region.min_x + .05
                or x > region.max_x - .05
                or z < region.min_z + .05
                or z > region.max_z - .05):
            return False
        self.action_index = next_index
        self._controller = None
        self.state = ActionRouteState.RUNNING
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
                    and type(self.route.actions[self.action_index + 1])
                    in (JumpUpSegment, StepSegment, JumpGapSegment,
                        ControlledDropSegment)):
                next_action = self.route.actions[self.action_index + 1]
                if type(next_action) is JumpUpSegment:
                    entry_tolerance = self.jump_profile.entry_center_tolerance_blocks
                    entry_speed = self.jump_profile.maximum_entry_speed_blocks_per_second
                elif type(next_action) in (JumpGapSegment, ControlledDropSegment):
                    profile = self.air_profiles.get(next_action.edge.profile_id)
                    if profile is None:
                        raise ContractViolation("air segment requires a calibrated profile")
                    entry_tolerance = profile.entry_center_tolerance_blocks
                    entry_speed = profile.maximum_entry_speed_blocks_per_second
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
                verified_kind = {
                    JumpGapSegment: MotionSolveKind.JUMP_GAP,
                    JumpUpSegment: MotionSolveKind.JUMP_UP,
                    ControlledDropSegment: MotionSolveKind.CONTROLLED_DROP,
                }.get(type(next_action))
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
                    self.route.goal_state, action.fixed_route.points[-1])
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
                    (RoutePoint(*frame.body.position), *remaining))
            controller.start(
                tracking_route, frame,
                traversal_plan=action.traversal_plan,
                continuation=(None if action.traversal_plan is None
                              else action.traversal_plan.continuation),
            )
        elif type(action) is JumpUpSegment:
            admitted = self._verified_motion.get(self.action_index)
            if admitted is not None:
                controller = VerifiedMotionExecutor()
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
        elif type(action) in (JumpGapSegment, ControlledDropSegment):
            admitted = self._verified_motion.get(self.action_index)
            if admitted is not None:
                controller = VerifiedMotionExecutor()
                controller.start(admitted)
                self._controller = controller
                return
            if self.action_index in self._required_verified_motion:
                self._controller = None
                return
            profile = self.air_profiles.get(action.edge.profile_id)
            if profile is None:
                raise ContractViolation("air segment requires a calibrated profile")
            if (action.transition is not None
                    and action.transition.trajectory_profile_id != profile.profile_id):
                raise ContractViolation("air segment uses another trajectory profile")
            controller = AirMotionController(profile)
            controller.start(action.start_surface, action.end_surface, frame)
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
        if type(action) is JumpUpSegment:
            end_x, end_y, end_z = action.edge.end
            min_x, max_x = float(end_x), float(end_x + 1)
            min_z, max_z = float(end_z), float(end_z + 1)
            feet_y = float(end_y)
        elif type(action) in (JumpGapSegment, ControlledDropSegment):
            region = action.end_surface.region
            min_x, max_x = region.min_x, region.max_x
            min_z, max_z = region.min_z, region.max_z
            feet_y = action.end_surface.position[1]
        else:
            return False
        x, y, z = frame.body.position
        if abs(y - feet_y) > .10:
            return False
        half_width = .30
        overlap_x = max(
            0.0, min(x + half_width, max_x) - max(x - half_width, min_x),
        )
        overlap_z = max(
            0.0, min(z + half_width, max_z) - max(z - half_width, min_z),
        )
        return overlap_x * overlap_z >= .01

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
                       or type(route.actions[index]) not in {
                           JumpGapSegment, JumpUpSegment, ControlledDropSegment}
                       for index in require_verified_motion_actions)):
            raise ContractViolation("required verified motion indices are invalid")
        if self.state in {ActionRouteState.RUNNING, ActionRouteState.CANCELLING}:
            raise ContractViolation("action route executor is already active")
        self.route = route
        self.state = ActionRouteState.RUNNING
        self.action_index = 0
        self._cancel_requested = False
        self._stop_cause = None
        self._actions_finished = False
        self._damage_budget = damage_budget
        self._session = frame.session
        self._verified_motion = {}
        self._require_verified_gap_motion = require_verified_gap_motion
        self._required_verified_motion = frozenset({
            *require_verified_motion_actions,
            *(index for index, action in enumerate(route.actions)
              if require_verified_gap_motion
              and type(action) is JumpGapSegment),
        })
        self._completed_movement_damage_points = 0.0
        self._committed_damage_actions.clear()
        self._input_scope_floor = None
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
        expected_kind = {
            JumpGapSegment: MotionSolveKind.JUMP_GAP,
            JumpUpSegment: MotionSolveKind.JUMP_UP,
            ControlledDropSegment: MotionSolveKind.CONTROLLED_DROP,
        }.get(type(action))
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
        self._verified_motion[index] = candidate
        if activating_current:
            controller = VerifiedMotionExecutor()
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
        return type(self.route.actions[self.action_index]) is not WalkSegment

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

    def request_stop(self, cause: StopCause) -> None:
        if type(cause) is not StopCause:
            raise ContractViolation("action route stop cause must be typed")
        self._stop_cause = cause
        self.cancel()

    def cancel(self) -> None:
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

    def _result(self, started: int, movement: MovementV1, lease: int,
                reason: str, missing: tuple[BlockPos, ...] = (),
                look: LookV1 | None = None, *,
                submit_input: bool = True,
                verified_command_index: int | None = None,
                expected_movement_tick: int | None = None,
                latest_movement_tick: int | None = None,
                requires_verified_motion: bool = False,
                ground_handoff_disposition=GroundHandoffDisposition.NOT_REQUESTED) -> ActionRouteDecision:
        return ActionRouteDecision(
            self.state, movement, look, lease, self.action_index, reason, missing,
            time.perf_counter_ns() - started, submit_input,
            verified_command_index, expected_movement_tick,
            latest_movement_tick, requires_verified_motion,
            ground_handoff_disposition=ground_handoff_disposition,
        )

    def prepare_ground_handoff(self, target: GroundHandoffTarget | None) -> None:
        """Provide a tracking target without changing route or body ownership."""
        if type(self._controller) is FixedRouteController:
            self._controller.set_handoff_target(target)

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True,
               state_anchor: StateAnchor | None = None,
               input_ledger: InputApplicationLedger | None = None,
               movement_yaw_radians: float | None = None) -> ActionRouteDecision:
        started = time.perf_counter_ns()
        if type(frame) is not NavigationFrame:
            raise ContractViolation("action route decision requires a navigation frame")
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
            return self._result(
                started, MovementV1(), 1, self.state.value,
                submit_input=False,
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
        if (type(action) is WalkSegment and not self._cancel_requested
                and input_confirmed and state_anchor is not None
                and input_ledger is not None):
            successor = self._verified_motion.get(self.action_index + 1)
            if (successor is not None
                    and verified_candidate_can_start(successor, state_anchor)):
                # A concrete proved entry can precede the graph's reference
                # point. The route owner remains unchanged; only the winning
                # first submission establishes the strict controller's input.
                return self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                    movement_yaw_radians=movement_yaw_radians,
                )
        self._commit_drop_damage_if_started(action, frame)
        if (self._cancel_requested
                and self._stop_cause is StopCause.DEPENDENCY_CHANGED
                and type(action) is ControlledDropSegment
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
                started, MovementV1(sneak=True), 1,
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
                        or self._landed_on_current_action_destination(
                            action, frame,
                            self._verified_motion[self.action_index].proof.continuation,
                        )
                    )):
                return self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                    movement_yaw_radians=movement_yaw_radians,
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
            )
        if type(action) is WalkSegment:
            movement_frame = frame
            if movement_yaw_radians is not None:
                movement_frame = replace(
                    frame,
                    body=replace(
                        frame.body,
                        yaw_radians=float(movement_yaw_radians),
                    ),
                )
            decision = self._controller.decide(
                movement_frame, input_confirmed=input_confirmed,
                physics_state=(
                    None if state_anchor is None else replace(
                        state_anchor.physics_state,
                        yaw_radians=movement_frame.body.yaw_radians,
                        pitch_radians=movement_frame.body.pitch_radians,
                    )
                ),
            )
            assert hasattr(decision, "state")
            if decision.state is FixedRouteState.SUCCEEDED:
                if self._cancel_requested:
                    self.state = ActionRouteState.CANCELLED
                    return self._result(started, MovementV1(), 1, "cancelled_on_ground")
                return self._advance(
                    frame, started, state_anchor=state_anchor,
                    input_ledger=input_ledger,
                    movement_yaw_radians=movement_yaw_radians,
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
            return self._result(
                started, decision.movement, decision.input_lease_ticks,
                decision.reason, decision.missing_cells,
                ground_handoff_disposition=decision.handoff_disposition,
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
        if type(action) in (JumpGapSegment, ControlledDropSegment):
            terminal = {
                AirMotionState.BLOCKED: ActionRouteState.BLOCKED,
                AirMotionState.NEEDS_INFORMATION: ActionRouteState.NEEDS_INFORMATION,
                AirMotionState.UNSUPPORTED: ActionRouteState.UNSUPPORTED,
                AirMotionState.FAILED: ActionRouteState.FAILED,
                AirMotionState.CANCELLED: ActionRouteState.CANCELLED,
                AirMotionState.INPUT_LOST: ActionRouteState.INPUT_LOST,
            }
            if decision.state is AirMotionState.COMPLETE:
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
                 movement_yaw_radians: float | None = None) -> ActionRouteDecision:
        assert self.route is not None
        completed = self.route.actions[self.action_index]
        self._commit_drop_damage_if_started(completed, frame, force=True)
        self.action_index += 1
        if self.action_index >= len(self.route.actions):
            self.action_index = len(self.route.actions) - 1
            self._actions_finished = True
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
                self._verified_motion.pop(self.action_index, None)
        self._activate(frame)
        self.state = ActionRouteState.RUNNING
        # Run the new controller immediately so a hand-off does not introduce
        # an artificial neutral-input frame.
        return self.decide(
            frame, state_anchor=state_anchor, input_ledger=input_ledger,
            movement_yaw_radians=movement_yaw_radians,
        )

    def _commit_drop_damage_if_started(
        self,
        action,
        frame: NavigationFrame,
        *,
        force: bool = False,
    ) -> None:
        """Book a fall once leaving support makes its risk unavoidable."""
        if (type(action) is not ControlledDropSegment
                or self.action_index in self._committed_damage_actions
                or (not force and frame.body.is_on_ground)):
            return
        self._completed_movement_damage_points += (
            conservative_plain_fall_damage_points(max(
                0.0,
                action.start_surface.position[1]
                - action.end_surface.position[1],
            ))
        )
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
        self.state = ActionRouteState.FAILED
        return self._result(
            started, MovementV1(), 1,
            ("goal_resource_unobservable" if observed.status is
             ObservedGoalStatus.RESOURCE_UNOBSERVABLE else
             "goal_state_not_satisfied"),
        )
