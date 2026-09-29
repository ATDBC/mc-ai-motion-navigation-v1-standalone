"""One-frame body ownership for navigation's route and edge acquisition."""
from __future__ import annotations

import math

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.body_control import (
    BodyControlDecision, BodyController, HandoffDisposition, HandoffEvidence,
    StopCause,
)
from mc2p.motion_nav.probe_body_controller import ProbeBodyController
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputResponsibilityDisposition,
    StateAnchor, assess_input_responsibility,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.safe_ground_control import verified_ground_rollout

class ExecutionSupervisor:
    """Own the active acquisition while an admitted route remains queued."""

    def __init__(self) -> None:
        self._probe: ProbeBodyController | None = None
        self._last_handoff: HandoffEvidence | None = None
        self._last_handoff_controller: BodyController | None = None
        self._route: RouteControl | None = None
        self._pending_route: RouteControl | None = None
        self._route_input_floor = 0
        self._pending_route_input_floor = 0
        self._support_fraction: float | None = None

    @property
    def route(self) -> RouteControl | None:
        return self._pending_route or self._route

    @property
    def incumbent_route(self) -> RouteControl | None:
        return self._route

    @property
    def has_pending_route(self) -> bool:
        return self._pending_route is not None

    def control_by_id(self, route_id: str) -> RouteControl | None:
        for control in (self._pending_route, self._route):
            if control is not None and control.route.route_id == route_id:
                return control
        return None

    def discard_pending_route(self) -> None:
        if self._pending_route is not None:
            self._pending_route.request_stop(StopCause.ROUTE_REPLACED)
        self._pending_route = None
        self._pending_route_input_floor = 0

    def offer_route(
        self, candidate: RouteControl, frame: NavigationFrame,
        ledger: InputApplicationLedger | None = None,
        anchor: StateAnchor | None = None,
    ) -> bool:
        if type(candidate) is not RouteControl:
            raise ContractViolation("route successor must be typed")
        if candidate.route.world_session != frame.session.value:
            raise ContractViolation("route successor belongs to another world")
        if self._pending_route is candidate:
            return True
        if self._pending_route is not None:
            # A pending route has not won Runtime arbitration, so none of its
            # commands can own the body.  A newer admitted route may replace
            # that candidate while the incumbent keeps moving.  Retaining the
            # older candidate would either execute a superseded target or make
            # moving-target goal revisions crash the whole Runtime.
            self.discard_pending_route()
        if self._route is None:
            self._route = candidate
            self._route_input_floor = max(
                (record.control_sequence for record in ledger.snapshot()),
                default=0,
            ) if ledger is not None else 0
            return True
        responsibility = assess_input_responsibility(
            ledger, anchor,
            previous_sequence_floor=self._route_input_floor,
        )
        if (ledger is None or anchor is None
                or responsibility.disposition not in {
                    InputResponsibilityDisposition.CLEAR,
                    InputResponsibilityDisposition.TRANSFERABLE_FROM_CURRENT_ANCHOR,
                }
                or self._route.requires_safe_handoff(frame)):
            return False
        self._pending_route = candidate
        self._pending_route_input_floor = max(
            (record.control_sequence for record in ledger.snapshot()),
            default=0,
        )
        return True

    def adopt_route_selection(
        self, frame: NavigationFrame, *, selected: bool,
        control_sequence: int | None = None,
        movement: MovementV1 | None = None,
        movement_tick_id: int | None = None,
    ) -> HandoffEvidence | None:
        successor = self._pending_route
        if successor is None:
            return None
        if not selected:
            self._pending_route = None
            return None
        if (control_sequence is None or movement is None
                or movement == MovementV1()):
            raise ContractViolation("route transfer requires selected movement")
        predecessor = self._route
        assert predecessor is not None
        handoff = HandoffEvidence(
            predecessor.owner_id, frame.session,
            HandoffDisposition.TRANSFERABLE,
            frame.body.sequence_id, movement_tick_id, movement,
            "selected_successor_route_command",
            successor.route.route_id, control_sequence,
            successor.route.route_revision,
            successor.action_index,
        )
        self._route = successor
        self._route_input_floor = self._pending_route_input_floor
        self._pending_route = None
        self._last_handoff = handoff
        return handoff

    def evaluate_quiescence(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> HandoffEvidence:
        route = self._route
        probe = self._probe
        owner = (
            probe.owner_id
            if probe is not None and probe.probe.owned else
            (route.owner_id if route is not None else "navigation-session")
        )
        reason = "body_owner_still_active"
        quiescent = False
        if probe is not None and probe.probe.owned:
            evidence = probe.safe_to_release(
                frame, ledger, anchor, input_floor=0,
            )
            self._last_handoff = evidence
            self._last_handoff_controller = probe
            return evidence
        if route is not None:
            evidence = route.safe_to_release(
                frame, ledger, anchor, input_floor=self._route_input_floor,
            )
            self._last_handoff = evidence
            self._last_handoff_controller = route
            return evidence
        if (ledger is None or anchor is None
                or anchor.session != frame.session
                or anchor.observation_sequence_id != frame.body.sequence_id):
            reason = "current_body_or_input_evidence_missing"
        elif assess_input_responsibility(
            ledger, anchor,
            previous_sequence_floor=self._route_input_floor,
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
        handoff = HandoffEvidence(
            owner, frame.session,
            HandoffDisposition.QUIESCENT if quiescent
            else HandoffDisposition.RETAIN,
            frame.body.sequence_id,
            None if ledger is None else ledger.latest_movement_tick_id,
            MovementV1(), reason,
        )
        self._last_handoff = handoff
        self._last_handoff_controller = None
        return handoff

    def incumbent_release_evidence(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> HandoffEvidence:
        """Check only the installed predecessor while a successor waits."""
        if self._route is None:
            raise ContractViolation("execution supervisor has no incumbent route")
        evidence = self._route.safe_to_release(
            frame, ledger, anchor, input_floor=self._route_input_floor,
        )
        self._last_handoff = evidence
        self._last_handoff_controller = self._route
        return evidence

    def retire_route(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> bool:
        if self._pending_route is not None:
            return False
        if self._route is None:
            return True
        evidence = self.evaluate_quiescence(frame, ledger, anchor)
        if (evidence.disposition is not HandoffDisposition.QUIESCENT
                or evidence.owner_id != self._route.owner_id):
            return False
        self._route = None
        self._route_input_floor = 0
        return True

    @property
    def probe(self):
        return None if self._probe is None else self._probe.probe

    @probe.setter
    def probe(self, value) -> None:
        if (self._probe is not None and self._probe.probe.owned
                and value is not self._probe.probe):
            raise ContractViolation("cannot discard an active body acquisition")
        self._probe = None if value is None else ProbeBodyController(value)
        self._last_handoff = None
        self._last_handoff_controller = None

    @property
    def last_handoff(self) -> HandoffEvidence | None:
        return self._last_handoff

    def request_probe_stop(self, cause: StopCause) -> bool:
        if type(cause) is not StopCause:
            raise ContractViolation("body stop cause must be typed")
        if self._probe is None or not self._probe.probe.owned:
            return False
        self._probe.request_stop(cause)
        self._last_handoff = None
        self._last_handoff_controller = None
        return True

    def decide_probe(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> BodyControlDecision:
        controller = self._probe
        if controller is None:
            raise ContractViolation("supervisor has no edge acquisition")
        decision = controller.decide(frame, ledger, anchor)
        self._last_handoff = decision.handoff
        self._last_handoff_controller = controller
        return decision

    def retire_quiescent_probe(self, frame: NavigationFrame) -> None:
        if self._probe is None:
            return
        if (self._last_handoff is None
                or self._last_handoff_controller is not self._probe
                or self._last_handoff.world_session != frame.session
                or self._last_handoff.observation_sequence_id
                    != frame.body.sequence_id
                or self._last_handoff.owner_id != self._probe.owner_id
                or self._last_handoff.disposition is not
                    HandoffDisposition.QUIESCENT):
            raise ContractViolation("edge acquisition has no release evidence")
        self._probe = None
        self._last_handoff_controller = None

    def transfer_probe_to_route(
        self, frame: NavigationFrame, *, route_id: str,
        route_revision: int, action_index: int,
        movement: MovementV1, control_sequence: int,
        movement_tick_id: int | None,
    ) -> HandoffEvidence:
        controller = self._probe
        if (controller is None or not controller.probe.ready
                or type(movement) is not MovementV1
                or movement == MovementV1()):
            raise ContractViolation("probe transfer requires a selected route movement")
        handoff = HandoffEvidence(
            controller.owner_id,
            frame.session, HandoffDisposition.TRANSFERABLE,
            frame.body.sequence_id, movement_tick_id, movement,
            "selected_route_command_owns_next_input", route_id,
            control_sequence, route_revision, action_index,
        )
        controller.probe.end("selected_route_command_handoff")
        self._probe = None
        self._last_handoff = handoff
        self._last_handoff_controller = None
        return handoff

    @property
    def support_fraction(self) -> float | None:
        return self._support_fraction

    def record_support_fraction(self, value: float | None) -> None:
        if (value is not None
                and (type(value) not in (int, float)
                     or not math.isfinite(float(value))
                     or not 0.0 <= float(value) <= 1.0)):
            raise ContractViolation("support fraction must be in [0, 1]")
        self._support_fraction = None if value is None else float(value)
