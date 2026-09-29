"""Thin body-controller adapter for bounded landing-edge acquisition."""
from __future__ import annotations

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.body_control import (
    BodyControlDecision, HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.landing_edge_probe import (
    LandingEdgeProbe, LandingEdgeProbeState,
)
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputResponsibilityDisposition, StateAnchor,
    assess_input_responsibility,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame


class ProbeBodyController:
    def __init__(self, probe: LandingEdgeProbe) -> None:
        if type(probe) is not LandingEdgeProbe:
            raise ContractViolation("probe controller requires landing-edge probe")
        self.probe = probe

    @property
    def owner_id(self) -> str:
        return self.probe.owner_id

    def request_stop(self, cause: StopCause) -> None:
        self.probe.request_stop(cause)

    def decide(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *,
        movement: MovementV1 | None = None,
        look: LookV1 | None = None,
        reason: str | None = None,
    ) -> BodyControlDecision:
        physics_state = (
            anchor.physics_state
            if anchor is not None
            and anchor.observation_sequence_id == frame.body.sequence_id
            else None
        )
        movement = self.probe.movement(frame, physics_state)
        disposition = HandoffDisposition.RETAIN
        reason = self.probe.state.value
        if self.probe.state is LandingEdgeProbeState.STOPPING:
            responsibility = assess_input_responsibility(ledger, anchor)
            if (ledger is not None and anchor is not None
                    and self.probe.stop_ready(frame, physics_state)
                    and responsibility.disposition in {
                        InputResponsibilityDisposition.CLEAR,
                        InputResponsibilityDisposition.TRANSFERABLE_FROM_CURRENT_ANCHOR,
                    }):
                disposition = HandoffDisposition.QUIESCENT
                reason = "safe_stance_and_input_resolved"
                movement = MovementV1()
                self.probe.end(reason)
            elif responsibility.disposition is \
                    InputResponsibilityDisposition.AMBIGUOUS_WAITING:
                reason = "input_application_ambiguous"
        handoff = HandoffEvidence(
            self.owner_id, frame.session, disposition,
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
        return self.decide(frame, ledger, anchor).handoff
