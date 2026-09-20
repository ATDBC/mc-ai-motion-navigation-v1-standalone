"""Sequential Walk/JumpUp execution without merging their state machines."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math
import time

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.action_v1 import LookV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import (
    ActionRoute, ControlledDropSegment, JumpGapSegment, JumpUpSegment,
    StepSegment, WalkSegment,
)
from mc2p.motion_nav.air_motion import AirMotionController, AirMotionProfile, AirMotionState
from mc2p.motion_nav.fixed_route import (
    FixedRouteConfig, FixedRouteController, FixedRouteState,
)
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_modes import GroundModeProfiles, observed_ground_mode
from mc2p.motion_nav.jump_up import JumpUpController, JumpUpProfile, JumpUpState
from mc2p.motion_nav.step_transition import StepController, StepProfile, StepState
from mc2p.motion_nav.geometry import QueryStatus, query_support
from mc2p.motion_nav.movement_transition import GoalSupport, MovementMode
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


class ActionRouteExecutor:
    """Own only segment order; each movement ability keeps its own controller."""

    def __init__(self, ground_profile: GroundMotionProfile,
                 jump_profile: JumpUpProfile,
                 step_profile: StepProfile | None = None,
                 ground_modes: GroundModeProfiles | None = None,
                 air_profiles: tuple[AirMotionProfile, ...] = ()) -> None:
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
        self.route: ActionRoute | None = None
        self.state = ActionRouteState.IDLE
        self.action_index = 0
        self._controller: (
            FixedRouteController | JumpUpController | StepController
            | AirMotionController | None
        ) = None
        self._cancel_requested = False
        self._actions_finished = False
        self._applied_risk_policy_id = "no_expected_damage"
        self._session = None

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
            if (self.action_index + 1 < len(self.route.actions)
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
                config = replace(
                    config,
                    endpoint_tolerance_blocks=min(
                        config.endpoint_tolerance_blocks,
                        entry_tolerance,
                    ),
                    stopped_speed_blocks_per_second=min(
                        config.stopped_speed_blocks_per_second,
                        entry_speed,
                    ),
                )
            if (self.action_index + 1 == len(self.route.actions)
                    and self.route.goal_state is not None):
                goal = self.route.goal_state
                endpoint = action.fixed_route.points[-1]
                horizontal_margin = min(
                    endpoint.x - goal.region.min_x,
                    goal.region.max_x - endpoint.x,
                    endpoint.z - goal.region.min_z,
                    goal.region.max_z - endpoint.z,
                )
                if horizontal_margin >= 0:
                    config = replace(
                        config,
                        endpoint_tolerance_blocks=min(
                            config.endpoint_tolerance_blocks,
                            max(1.0e-4, horizontal_margin),
                        ),
                        stopped_speed_blocks_per_second=min(
                            config.stopped_speed_blocks_per_second,
                            goal.maximum_terminal_speed_blocks_per_second,
                        ),
                    )
            controller = FixedRouteController(
                motion_profile, config, mode_profile=mode_profile,
            )
            controller.start(action.fixed_route, frame)
        elif type(action) is JumpUpSegment:
            if action.edge.profile_id != self.jump_profile.profile_id:
                raise ContractViolation("JumpUp segment uses another calibrated profile")
            controller = JumpUpController(self.jump_profile)
            controller.start(action.edge.start, action.edge.end, frame)
        elif type(action) in (JumpGapSegment, ControlledDropSegment):
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

    def start(self, route: ActionRoute, frame: NavigationFrame, *,
              applied_risk_policy_id: str = "no_expected_damage") -> None:
        if type(route) is not ActionRoute or type(frame) is not NavigationFrame:
            raise ContractViolation("action route start requires a route and frame")
        if type(applied_risk_policy_id) is not str or not applied_risk_policy_id:
            raise ContractViolation("action route risk policy id is required")
        if self.state in {ActionRouteState.RUNNING, ActionRouteState.CANCELLING}:
            raise ContractViolation("action route executor is already active")
        self.route = route
        self.state = ActionRouteState.RUNNING
        self.action_index = 0
        self._cancel_requested = False
        self._actions_finished = False
        self._applied_risk_policy_id = applied_risk_policy_id
        self._session = frame.session
        self._activate(frame)

    def cancel(self) -> None:
        if self.state is ActionRouteState.RUNNING:
            self._cancel_requested = True
            self.state = ActionRouteState.CANCELLING
            assert self._controller is not None
            self._controller.cancel()

    def _result(self, started: int, movement: MovementV1, lease: int,
                reason: str, missing: tuple[BlockPos, ...] = (),
                look: LookV1 | None = None) -> ActionRouteDecision:
        return ActionRouteDecision(
            self.state, movement, look, lease, self.action_index, reason, missing,
            time.perf_counter_ns() - started,
        )

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True) -> ActionRouteDecision:
        started = time.perf_counter_ns()
        if type(frame) is not NavigationFrame:
            raise ContractViolation("action route decision requires a navigation frame")
        if self.route is None or self._controller is None:
            self.state = ActionRouteState.IDLE
            return self._result(started, MovementV1(), 1, "not_started")
        if self.state in {
            ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
            ActionRouteState.FAILED, ActionRouteState.UNSUPPORTED,
            ActionRouteState.INPUT_LOST,
        }:
            return self._result(started, MovementV1(), 1, self.state.value)
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
            return self._finish_goal(frame, started)
        action = self.route.actions[self.action_index]
        if type(action) is WalkSegment:
            decision = self._controller.decide(frame, input_confirmed=input_confirmed)
            assert hasattr(decision, "state")
            if decision.state is FixedRouteState.SUCCEEDED:
                if self._cancel_requested:
                    self.state = ActionRouteState.CANCELLED
                    return self._result(started, MovementV1(), 1, "cancelled_on_ground")
                return self._advance(frame, started)
            mapping = {
                FixedRouteState.BLOCKED: ActionRouteState.BLOCKED,
                FixedRouteState.NEEDS_INFORMATION: ActionRouteState.NEEDS_INFORMATION,
                FixedRouteState.UNSUPPORTED: ActionRouteState.UNSUPPORTED,
                FixedRouteState.INPUT_LOST: ActionRouteState.INPUT_LOST,
                FixedRouteState.FAILED: ActionRouteState.FAILED,
                FixedRouteState.CANCELLED: ActionRouteState.CANCELLED,
            }
            if decision.state in mapping:
                self.state = mapping[decision.state]
            return self._result(
                started, decision.movement, decision.input_lease_ticks,
                decision.reason, decision.missing_cells,
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
                return self._advance(frame, started)
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
                return self._advance(frame, started)
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
            return self._advance(frame, started)
        if decision.state in terminal:
            self.state = terminal[decision.state]
        elif self._cancel_requested:
            self.state = ActionRouteState.CANCELLING
        return self._result(
            started, decision.movement, decision.input_lease_ticks,
            decision.reason_code, decision.missing_cells, decision.look,
        )

    def _advance(self, frame: NavigationFrame, started: int) -> ActionRouteDecision:
        assert self.route is not None
        self.action_index += 1
        if self.action_index >= len(self.route.actions):
            self.action_index = len(self.route.actions) - 1
            self._actions_finished = True
            return self._finish_goal(frame, started)
        self._activate(frame)
        self.state = ActionRouteState.RUNNING
        # Run the new controller immediately so a hand-off does not introduce
        # an artificial neutral-input frame.
        return self.decide(frame)

    def _finish_goal(self, frame: NavigationFrame, started: int) -> ActionRouteDecision:
        assert self.route is not None
        goal = self.route.goal_state
        if goal is None:
            self.state = ActionRouteState.COMPLETE
            return self._result(started, MovementV1(), 1, "action_route_complete")
        support_query = query_support(frame.body.body_box, frame.world)
        if support_query.status is QueryStatus.NEEDS_INFORMATION:
            self.state = ActionRouteState.NEEDS_INFORMATION
            return self._result(
                started, MovementV1(), 1, "goal_support_requires_information",
                support_query.missing_cells,
            )
        observed_support = (
            GoalSupport.SOLID
            if (support_query.status is QueryStatus.FEASIBLE
                and frame.body.is_on_ground)
            else None
        )
        # Minecraft keeps a small downward velocity while an entity is resting
        # on a block.  Ground movement completion therefore uses planar speed,
        # matching FixedRouteController, rather than treating gravity bookkeeping
        # as a real fall.
        speed = math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        )
        actual_mode = observed_ground_mode(frame.body)
        common = dict(
            position=frame.body.position,
            support=observed_support,
            mode=actual_mode,
            pose=frame.body.pose,
            speed_blocks_per_second=speed,
            resources=self.route.final_resources,
            applied_risk_policy_id=self._applied_risk_policy_id,
        )
        if observed_support is not None and goal.accepts(
                **common, yaw_radians=frame.body.yaw_radians):
            self.state = ActionRouteState.COMPLETE
            return self._result(started, MovementV1(), 1, "goal_state_satisfied")
        if (observed_support is not None and goal.required_yaw_radians is not None
                and goal.accepts(**common, yaw_radians=goal.required_yaw_radians)):
            delta = math.atan2(
                math.sin(goal.required_yaw_radians - frame.body.yaw_radians),
                math.cos(goal.required_yaw_radians - frame.body.yaw_radians),
            )
            self.state = ActionRouteState.RUNNING
            return self._result(
                started, MovementV1(), 1, "aligning_goal_heading",
                look=LookV1(math.degrees(delta), 0.0),
            )
        self.state = ActionRouteState.FAILED
        return self._result(started, MovementV1(), 1, "goal_state_not_satisfied")
