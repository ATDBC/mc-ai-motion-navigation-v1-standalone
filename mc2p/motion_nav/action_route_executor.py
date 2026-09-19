"""Sequential Walk/JumpUp execution without merging their state machines."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import time

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.action_v1 import LookV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import ActionRoute, JumpUpSegment, WalkSegment
from mc2p.motion_nav.fixed_route import (
    FixedRouteConfig, FixedRouteController, FixedRouteState,
)
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.jump_up import JumpUpController, JumpUpProfile, JumpUpState
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
                 jump_profile: JumpUpProfile) -> None:
        if type(ground_profile) is not GroundMotionProfile or type(jump_profile) is not JumpUpProfile:
            raise ContractViolation("action route executor requires calibrated profiles")
        self.ground_profile = ground_profile
        self.jump_profile = jump_profile
        self.route: ActionRoute | None = None
        self.state = ActionRouteState.IDLE
        self.action_index = 0
        self._controller: FixedRouteController | JumpUpController | None = None
        self._cancel_requested = False

    def _activate(self, frame: NavigationFrame) -> None:
        assert self.route is not None
        action = self.route.actions[self.action_index]
        if type(action) is WalkSegment:
            config = FixedRouteConfig()
            if (self.action_index + 1 < len(self.route.actions)
                    and type(self.route.actions[self.action_index + 1]) is JumpUpSegment):
                config = replace(
                    config,
                    endpoint_tolerance_blocks=min(
                        config.endpoint_tolerance_blocks,
                        self.jump_profile.entry_center_tolerance_blocks,
                    ),
                    stopped_speed_blocks_per_second=min(
                        config.stopped_speed_blocks_per_second,
                        self.jump_profile.maximum_entry_speed_blocks_per_second,
                    ),
                )
            controller = FixedRouteController(self.ground_profile, config)
            controller.start(action.fixed_route, frame)
        else:
            assert type(action) is JumpUpSegment
            if action.edge.profile_id != self.jump_profile.profile_id:
                raise ContractViolation("JumpUp segment uses another calibrated profile")
            controller = JumpUpController(self.jump_profile)
            controller.start(action.edge.start, action.edge.end, frame)
        self._controller = controller

    def start(self, route: ActionRoute, frame: NavigationFrame) -> None:
        if type(route) is not ActionRoute or type(frame) is not NavigationFrame:
            raise ContractViolation("action route start requires a route and frame")
        self.route = route
        self.state = ActionRouteState.RUNNING
        self.action_index = 0
        self._cancel_requested = False
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
            self.state = ActionRouteState.COMPLETE
            return self._result(started, MovementV1(), 1, "action_route_complete")
        self._activate(frame)
        self.state = ActionRouteState.RUNNING
        # Run the new controller immediately so a hand-off does not introduce
        # an artificial neutral-input frame.
        return self.decide(frame)
