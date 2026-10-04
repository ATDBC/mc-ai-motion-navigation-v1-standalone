"""Thin body-controller adapter for bounded landing-edge acquisition."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
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
from mc2p.motion_nav.action_preconditions import AcquisitionGrant
from mc2p.motion_nav.world_model import WorldSessionId


class ProbeOutcomeKind(StrEnum):
    READY = "ready"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """Acquisition facts; none of these facts grant body release."""

    kind: ProbeOutcomeKind
    owner_id: str
    world_session: WorldSessionId
    observation_sequence_id: int
    stop_cause: StopCause | None
    route_id: str | None
    route_revision: int | None
    action_index: int | None
    grant: AcquisitionGrant | None = None

    def __post_init__(self) -> None:
        require_identifier(self.owner_id, "probe outcome owner")
        if self.route_id is not None:
            require_identifier(self.route_id, "probe outcome route")
        if ((self.route_id is None) != (self.route_revision is None)
                or (self.route_id is None) != (self.action_index is None)):
            raise ContractViolation("probe outcome action identity must be complete")
        for value in (self.route_revision, self.action_index):
            if value is not None and (type(value) is not int or value < 0):
                raise ContractViolation("probe outcome action identity must be nonnegative")
        if (type(self.kind) is not ProbeOutcomeKind
                or type(self.world_session) is not WorldSessionId
                or type(self.observation_sequence_id) is not int
                or self.observation_sequence_id < 0
                or (self.stop_cause is not None and type(self.stop_cause) is not StopCause)
                or (self.grant is not None and type(self.grant) is not AcquisitionGrant)):
            raise ContractViolation("probe outcome requires typed current facts")
        if self.grant is not None and (self.kind is not ProbeOutcomeKind.READY
                or (self.grant.route_id, self.grant.route_revision, self.grant.action_index)
                    != (self.route_id, self.route_revision, self.action_index)):
            raise ContractViolation("probe grant must belong to its completed action")


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

    def outcome(self, frame: NavigationFrame) -> ProbeOutcome | None:
        probe = self.probe
        if probe.world_session is not None and probe.world_session != frame.session.value:
            return None
        if probe.ready:
            kind = ProbeOutcomeKind.READY
        elif probe.state in {LandingEdgeProbeState.STOPPING, LandingEdgeProbeState.ENDED}:
            kind = (ProbeOutcomeKind.TIMED_OUT
                    if probe.stop_cause is StopCause.ACQUISITION_TIMED_OUT
                    else ProbeOutcomeKind.STOPPED)
        else:
            return None
        grant = None
        if (kind is ProbeOutcomeKind.READY and probe.acquisition_id is not None
                and probe.route_id is not None and probe.route_revision is not None
                and probe.action_index is not None and probe.evidence_sequence_id is not None):
            grant = AcquisitionGrant(probe.acquisition_id, probe.route_id,
                probe.route_revision, probe.action_index, probe.landing_cell,
                probe.evidence_sequence_id, probe.dependencies)
        return ProbeOutcome(kind, probe.owner_id, frame.session,
            frame.body.sequence_id, probe.stop_cause, probe.route_id,
            probe.route_revision, probe.action_index, grant)

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
            and anchor.session == frame.session
            and anchor.observation_sequence_id == frame.body.sequence_id
            else None
        )
        movement = self.probe.movement(frame, physics_state)
        disposition = HandoffDisposition.RETAIN
        reason = self.probe.state.value
        if self.probe.state is LandingEdgeProbeState.STOPPING:
            responsibility = assess_input_responsibility(ledger, anchor)
            if (ledger is not None and anchor is not None
                    and anchor.session == frame.session
                    and anchor.observation_sequence_id == frame.body.sequence_id
                    and (self.probe.world_session is None
                         or self.probe.world_session == frame.session.value)
                    and self.probe.stop_ready(frame, physics_state)
                    and responsibility.disposition in {
                        InputResponsibilityDisposition.CLEAR,
                        InputResponsibilityDisposition.TRANSFERABLE_FROM_CURRENT_ANCHOR,
                    }):
                disposition = HandoffDisposition.QUIESCENT
                reason = "safe_stance_and_input_resolved"
                movement = MovementV1()
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
