"""Thin body-controller adapter for one admitted action route."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteExecutor, ActionRouteState,
)
from mc2p.motion_nav.body_control import (
    BodyControlDecision, BodyControlActivity, BodyControlPhase,
    HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.action_route import WalkSegment
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.async_work import AsyncComputationScope
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputResponsibilityDisposition,
    StateAnchor, assess_input_responsibility,
)
from mc2p.motion_nav.route_admission import ActiveRoute
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.safe_ground_control import verified_ground_rollout


_TERMINAL_ROUTE_STATES = frozenset({
    ActionRouteState.COMPLETE,
    ActionRouteState.CANCELLED,
    ActionRouteState.FAILED,
    ActionRouteState.BLOCKED,
    ActionRouteState.UNSUPPORTED,
    ActionRouteState.INPUT_LOST,
    ActionRouteState.NEEDS_REPLAN,
    ActionRouteState.IDLE,
})


@dataclass(frozen=True, slots=True)
class RouteAdvance:
    """One route's raw frame facts, before task risk and Runtime selection."""

    control: RouteControl
    decision: ActionRouteDecision
    conditioned_ordinary_walk: bool


@dataclass(frozen=True, slots=True)
class RouteControl:
    """Keep route metadata and its concrete executor behind one adapter."""

    route: ActiveRoute
    executor: ActionRouteExecutor
    coordinator: MotionRouteCoordinator | None = None

    def __post_init__(self) -> None:
        if (type(self.route) is not ActiveRoute
                or not isinstance(self.executor, ActionRouteExecutor)
                or (self.coordinator is not None
                    and type(self.coordinator) is not MotionRouteCoordinator)):
            raise ContractViolation(
                "route control requires admitted route and executor"
            )

    @property
    def owner_id(self) -> str:
        return f"route/{self.route.route_id}"

    @property
    def action_index(self) -> int:
        return self.executor.action_index

    def activity(self, frame, decision=None) -> BodyControlActivity:
        index = min(self.executor.action_index, len(self.route.action_route.actions)-1)
        action = self.route.action_route.actions[index]
        phase = (None if decision is None else decision.body_phase)
        if phase is None:
            if self.executor.state is ActionRouteState.CANCELLING or self.executor.state in _TERMINAL_ROUTE_STATES:
                phase = BodyControlPhase.STOPPING
            elif type(action) is WalkSegment:
                phase = BodyControlPhase.TRACKING
            elif frame.body.is_on_ground:
                phase = BodyControlPhase.STRICT_PREPARATION
            else:
                phase = BodyControlPhase.STRICT_EXECUTION
        return BodyControlActivity(frame.session, frame.body.sequence_id,
            self.owner_id, self.route.route_id, self.route.route_revision, index, phase)

    def request_stop(self, cause: StopCause) -> None:
        if type(cause) is not StopCause:
            raise ContractViolation("route stop cause must be typed")
        self.executor.request_stop(cause)
        if self.coordinator is not None:
            self.coordinator.cancel_work(f"route_stop_{cause.value}")

    def requires_safe_handoff(self, frame: NavigationFrame) -> bool:
        return self.executor.requires_safe_handoff(frame)

    def advance(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *,
        conditioned_yaw_delta_degrees: float | None = None,
        allow_grounded_reprepare: bool = False,
        result_poll_sequence: int | None = None,
        current_scope: AsyncComputationScope | None = None,
    ) -> RouteAdvance:
        """Advance the installed movement owner exactly once for this frame."""
        executor_route = getattr(self.executor, "route", None)
        action = (None if executor_route is None
                  or not 0 <= self.action_index < len(executor_route.actions)
                  else executor_route.actions[self.action_index])
        conditioned_walk = (
            conditioned_yaw_delta_degrees is not None
            and type(action) is WalkSegment
            and (action.transition is None
                 or action.transition.mode is MovementMode.WALK)
        )
        movement_yaw = None
        if conditioned_walk:
            yaw = frame.body.yaw_radians + math.radians(
                conditioned_yaw_delta_degrees,
            )
            movement_yaw = math.atan2(math.sin(yaw), math.cos(yaw))
        if self.coordinator is not None and anchor is not None and ledger is not None:
            decision = self.coordinator.decide(
                frame, anchor, ledger,
                PhysicsWorldView(frame.world, JAVA_1_21_RULESET),
                changed_cells=frame.changed_cells,
                current_scope=current_scope,
                movement_yaw_radians=movement_yaw,
                allow_grounded_reprepare=allow_grounded_reprepare,
                result_poll_sequence=result_poll_sequence,
            )
        else:
            decision = self.executor.decide(
                frame, state_anchor=anchor, input_ledger=ledger,
                movement_yaw_radians=movement_yaw,
            )
        return RouteAdvance(self, decision, conditioned_walk)

    def stop_protection(
        self, previous: RouteAdvance, frame: NavigationFrame,
        anchor: StateAnchor | None,
    ) -> RouteAdvance:
        if previous.control is not self:
            raise ContractViolation("stop protection belongs to another route")
        return replace(previous, decision=self.executor.stop_protection(
            previous.decision, frame, state_anchor=anchor,
        ), conditioned_ordinary_walk=False)

    def decide(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *,
        movement: MovementV1 | None = None,
        look: LookV1 | None = None,
        reason: str | None = None,
    ) -> BodyControlDecision:
        """Wrap a route decision already computed by its movement owner."""
        if movement is None or reason is None:
            raise ContractViolation("route decision requires movement and reason")
        handoff = HandoffEvidence(
            self.owner_id, frame.session, HandoffDisposition.RETAIN,
            frame.body.sequence_id,
            None if ledger is None else ledger.latest_movement_tick_id,
            movement, reason,
        )
        return BodyControlDecision(movement, look, handoff)

    def safe_to_release(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *, input_floor: int,
    ) -> HandoffEvidence:
        reason = "body_owner_still_active"
        quiescent = False
        if self.executor.state not in _TERMINAL_ROUTE_STATES:
            reason = "route_controller_retains_body"
        elif (ledger is None or anchor is None
                or anchor.session != frame.session
                or anchor.observation_sequence_id != frame.body.sequence_id):
            reason = "current_body_or_input_evidence_missing"
        elif assess_input_responsibility(
            ledger, anchor, previous_sequence_floor=input_floor,
        ).disposition not in {
            InputResponsibilityDisposition.CLEAR,
            InputResponsibilityDisposition.TRANSFERABLE_FROM_CURRENT_ANCHOR,
        }:
            reason = "input_responsibility_unresolved"
        elif math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        ) > .10:
            reason = "current_body_still_moving"
        else:
            predicted = verified_ground_rollout(
                frame, anchor.physics_state, MovementV1(),
                control_ticks=0, tail_ticks=8, minimum_support=.01,
            )
            if predicted is None:
                reason = "released_input_tail_unproven"
            elif math.hypot(
                predicted.velocity_blocks_per_tick[0],
                predicted.velocity_blocks_per_tick[2],
            ) * 20.0 > .10:
                reason = "released_input_tail_still_moving"
            else:
                quiescent = True
                reason = "supported_released_input_tail_verified"
        return HandoffEvidence(
            self.owner_id, frame.session,
            HandoffDisposition.QUIESCENT if quiescent
            else HandoffDisposition.RETAIN,
            frame.body.sequence_id,
            None if ledger is None else ledger.latest_movement_tick_id,
            MovementV1(), reason,
        )
