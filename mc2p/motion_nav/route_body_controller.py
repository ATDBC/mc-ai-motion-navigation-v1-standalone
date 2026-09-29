"""Thin body-controller adapter for one admitted action route."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route_executor import (
    ActionRouteExecutor, ActionRouteState,
)
from mc2p.motion_nav.body_control import (
    BodyControlDecision, HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputResponsibilityStatus, StateAnchor,
    input_responsibility_status,
)
from mc2p.motion_nav.route_admission import ActiveRoute
from mc2p.motion_nav.runtime_adapter import NavigationFrame
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

    def request_stop(self, cause: StopCause) -> None:
        if type(cause) is not StopCause:
            raise ContractViolation("route stop cause must be typed")
        self.executor.request_stop(cause)

    def requires_safe_handoff(self, frame: NavigationFrame) -> bool:
        return self.executor.requires_safe_handoff(frame)

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
        elif input_responsibility_status(
            ledger, anchor, previous_sequence_floor=input_floor,
        ) is not InputResponsibilityStatus.CLEAR:
            reason = "input_responsibility_unresolved"
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
