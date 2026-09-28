"""Typed body ownership and stopping evidence for one navigation control frame."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.world_model import WorldSessionId


class StopCause(StrEnum):
    GOAL_REVISED = "goal_revised"
    CANCELLED = "cancelled"
    ACQUISITION_TIMED_OUT = "acquisition_timed_out"
    INFORMATION_TIMED_OUT = "information_timed_out"
    DEPENDENCY_CHANGED = "dependency_changed"
    INPUT_LOST = "input_lost"
    CLOSED = "closed"
    ROUTE_REPLACED = "route_replaced"


class HandoffDisposition(StrEnum):
    RETAIN = "retain"
    QUIESCENT = "quiescent"
    TRANSFERABLE = "transferable"


@dataclass(frozen=True, slots=True)
class HandoffEvidence:
    owner_id: str
    world_session: WorldSessionId
    disposition: HandoffDisposition
    observation_sequence_id: int
    movement_tick_id: int | None
    movement: MovementV1
    reason: str
    successor_id: str | None = None
    control_sequence: int | None = None
    successor_route_revision: int | None = None
    successor_action_index: int | None = None

    def __post_init__(self) -> None:
        require_identifier(self.owner_id, "body owner id")
        if type(self.world_session) is not WorldSessionId:
            raise ContractViolation("handoff world session must be typed")
        if type(self.disposition) is not HandoffDisposition:
            raise ContractViolation("handoff disposition must be typed")
        if type(self.observation_sequence_id) is not int or self.observation_sequence_id < 0:
            raise ContractViolation("handoff observation must be nonnegative")
        if (self.movement_tick_id is not None
                and (type(self.movement_tick_id) is not int
                     or self.movement_tick_id < 0)):
            raise ContractViolation("handoff movement tick must be nonnegative")
        if type(self.movement) is not MovementV1:
            raise ContractViolation("handoff movement must be typed")
        require_identifier(self.reason, "handoff reason")
        if self.successor_id is not None:
            require_identifier(self.successor_id, "handoff successor id")
        if (self.disposition is HandoffDisposition.TRANSFERABLE
                and self.successor_id is None):
            raise ContractViolation("transfer requires a named successor")
        for value, name in (
            (self.control_sequence, "handoff control sequence"),
            (self.successor_route_revision, "successor route revision"),
            (self.successor_action_index, "successor action index"),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ContractViolation(f"{name} must be nonnegative")
        if (self.disposition is HandoffDisposition.TRANSFERABLE
                and (self.control_sequence is None
                     or self.successor_route_revision is None
                     or self.successor_action_index is None)):
            raise ContractViolation("transfer requires selected successor action")


@dataclass(frozen=True, slots=True)
class BodyControlDecision:
    movement: MovementV1
    look: LookV1 | None
    handoff: HandoffEvidence
