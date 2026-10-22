"""Typed facts at the Runtime/backend transport boundary."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.action_receipt import (
    ClientBehaviorReceiptV4, ClientTimingStatusV1,
)
from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)


class TransportLifecycleKind(StrEnum):
    LOCAL_NOT_DISPATCHED = "local_not_dispatched"
    REMOTE_REJECTED_BEFORE_SAMPLING = "remote_rejected_before_sampling"
    DELIVERY_UNKNOWN = "delivery_unknown"
    CHANNEL_LOST = "channel_lost"


class TransportDispatchStage(StrEnum):
    NOT_OUTBOUND = "not_outbound"
    PARTIAL_SEND = "partial_send"
    SENT_NO_RECEIPT = "sent_no_receipt"
    REMOTE_REJECTED = "remote_rejected"
    CHANNEL_SEALED = "channel_sealed"


@dataclass(frozen=True, slots=True)
class TransportLifecycleFactV1:
    kind: TransportLifecycleKind
    episode_id: str
    request_sequence_id: int
    generation_id: int
    earliest_effect_tick: int
    latest_effect_tick: int
    causal_movement_tick: int
    input_applications: int | None
    channel_healthy: bool | None
    reason: str
    dispatch_stage: TransportDispatchStage

    def __post_init__(self) -> None:
        if type(self.kind) is not TransportLifecycleKind:
            raise ContractViolation("transport lifecycle kind must be typed")
        require_identifier(self.episode_id, "transport lifecycle episode")
        if type(self.reason) is not str or not self.reason.strip():
            raise ContractViolation("transport lifecycle reason is required")
        for name in (
            "request_sequence_id", "generation_id", "earliest_effect_tick",
            "latest_effect_tick", "causal_movement_tick",
        ):
            require_nonnegative_int(getattr(self, name), name)
        if (self.input_applications is not None
                and type(self.input_applications) is not int):
            raise ContractViolation("transport input applications must be exact or unknown")
        if self.input_applications is not None:
            require_nonnegative_int(self.input_applications, "input_applications")
        if self.latest_effect_tick < self.earliest_effect_tick:
            raise ContractViolation("transport lifecycle effect window is reversed")
        if self.channel_healthy is not None and type(self.channel_healthy) is not bool:
            raise ContractViolation("transport lifecycle channel health must be typed")
        if type(self.dispatch_stage) is not TransportDispatchStage:
            raise ContractViolation("transport dispatch stage must be typed")
        expected = {
            TransportLifecycleKind.LOCAL_NOT_DISPATCHED:
                ((TransportDispatchStage.NOT_OUTBOUND,), 0, True),
            TransportLifecycleKind.REMOTE_REJECTED_BEFORE_SAMPLING:
                ((TransportDispatchStage.REMOTE_REJECTED,), 0, True),
            TransportLifecycleKind.DELIVERY_UNKNOWN:
                ((TransportDispatchStage.PARTIAL_SEND,
                  TransportDispatchStage.SENT_NO_RECEIPT), None, None),
            TransportLifecycleKind.CHANNEL_LOST:
                ((TransportDispatchStage.CHANNEL_SEALED,), None, False),
        }[self.kind]
        stages, applications, healthy = expected
        if (self.dispatch_stage not in stages
                or self.input_applications != applications
                or self.channel_healthy is not healthy):
            raise ContractViolation(
                "transport lifecycle kind has incompatible evidence fields"
            )

    @property
    def unresolved(self) -> bool:
        return self.kind in {
            TransportLifecycleKind.DELIVERY_UNKNOWN,
            TransportLifecycleKind.CHANNEL_LOST,
        }


def _identity(action: ActionSnapshotV1) -> tuple[int, int]:
    if type(action) is not ActionSnapshotV1 or action.first_effect_window is None:
        raise ContractViolation("not-applied proof requires a windowed action")
    return (
        action.first_effect_window.earliest_tick,
        action.first_effect_window.latest_tick,
    )


def prove_remote_rejected_before_sampling(
    action: ActionSnapshotV1,
    receipt: ClientBehaviorReceiptV4,
    *,
    expected_generation_id: int | None = None,
) -> TransportLifecycleFactV1:
    """Prove a windowed full request had no sampled input or accepted effects."""
    earliest, latest = _identity(action)
    if type(receipt) is not ClientBehaviorReceiptV4:
        raise ContractViolation("remote not-applied proof requires a V4 receipt")
    if receipt.status == "operation_rejected":
        raise ContractViolation(
            "operation_rejected can retain movement/look and is not full-request proof"
        )
    if receipt.status != "rejected":
        raise ContractViolation("remote not-applied proof requires full-request rejected")
    expected_generation = (
        action.observation_sequence_id + 1
        if expected_generation_id is None else expected_generation_id
    )
    require_nonnegative_int(expected_generation, "expected receipt generation")
    if (
        receipt.episode_id != action.episode_id
        or receipt.request_sequence_id != action.request_sequence_id
        or receipt.generation_id != expected_generation
    ):
        raise ContractViolation("remote rejection identity is detached from the request")
    if receipt.dropped_input_samples != 0:
        raise ContractViolation("dropped samples prevent a zero-application proof")
    applications = tuple(
        item for item in receipt.input_applications
        if item.episode_id == action.episode_id
        and item.request_sequence_id == action.request_sequence_id
    )
    if applications:
        raise ContractViolation("rejected request has a physical input application")
    rejected = tuple(
        event for event in receipt.timing_events
        if event.episode_id == action.episode_id
        and event.request_sequence_id == action.request_sequence_id
        and event.status is ClientTimingStatusV1.NOT_APPLIED
    )
    applied = tuple(
        event for event in receipt.timing_events
        if event.episode_id == action.episode_id
        and event.request_sequence_id == action.request_sequence_id
        and event.status is ClientTimingStatusV1.WINDOW_APPLIED
    )
    if len(rejected) != 1 or applied:
        raise ContractViolation("remote rejection lacks one pre-sampling causal tick")
    causal_tick = rejected[0].movement_tick_id
    if not earliest <= causal_tick <= latest:
        raise ContractViolation("remote rejection causal tick is outside the request window")
    return TransportLifecycleFactV1(
        TransportLifecycleKind.REMOTE_REJECTED_BEFORE_SAMPLING,
        action.episode_id,
        action.request_sequence_id,
        receipt.generation_id,
        earliest,
        latest,
        causal_tick,
        0,
        True,
        receipt.reason,
        TransportDispatchStage.REMOTE_REJECTED,
    )


def unresolved_transport_fact(
    kind: TransportLifecycleKind,
    action: ActionSnapshotV1,
    generation_id: int,
    causal_movement_tick: int,
    reason: str,
    *,
    dispatch_stage: TransportDispatchStage | None = None,
) -> TransportLifecycleFactV1:
    if kind not in {
        TransportLifecycleKind.DELIVERY_UNKNOWN,
        TransportLifecycleKind.CHANNEL_LOST,
    }:
        raise ContractViolation("only uncertain transport facts are unresolved")
    earliest, latest = _identity(action)
    if dispatch_stage is None:
        dispatch_stage = (
            TransportDispatchStage.SENT_NO_RECEIPT
            if kind is TransportLifecycleKind.DELIVERY_UNKNOWN
            else TransportDispatchStage.CHANNEL_SEALED
        )
    return TransportLifecycleFactV1(
        kind, action.episode_id, action.request_sequence_id, generation_id,
        earliest, latest, causal_movement_tick, None,
        None if kind is TransportLifecycleKind.DELIVERY_UNKNOWN else False,
        reason, dispatch_stage,
    )


def local_not_dispatched_fact(
    action: ActionSnapshotV1,
    generation_id: int,
    causal_movement_tick: int,
    reason: str,
) -> TransportLifecycleFactV1:
    earliest, latest = _identity(action)
    return TransportLifecycleFactV1(
        TransportLifecycleKind.LOCAL_NOT_DISPATCHED,
        action.episode_id, action.request_sequence_id, generation_id,
        earliest, latest, causal_movement_tick, 0, True, reason,
        TransportDispatchStage.NOT_OUTBOUND,
    )


def classify_transport_failure(
    *,
    channel_sealed: bool,
    outbound_started: bool,
    outbound_completed: bool,
    response_correlated: bool,
) -> tuple[TransportLifecycleKind, TransportDispatchStage]:
    """Classify known transport state, with channel sealing taking priority."""
    if any(type(value) is not bool for value in (
            channel_sealed, outbound_started, outbound_completed,
            response_correlated)):
        raise ContractViolation("transport classification requires boolean facts")
    if outbound_completed and not outbound_started:
        raise ContractViolation("completed outbound transport never precedes its start")
    if response_correlated:
        raise ContractViolation("a correlated response is not a transport failure")
    if channel_sealed:
        return (TransportLifecycleKind.CHANNEL_LOST,
                TransportDispatchStage.CHANNEL_SEALED)
    if not outbound_started:
        return (TransportLifecycleKind.LOCAL_NOT_DISPATCHED,
                TransportDispatchStage.NOT_OUTBOUND)
    return (
        TransportLifecycleKind.DELIVERY_UNKNOWN,
        (TransportDispatchStage.SENT_NO_RECEIPT if outbound_completed
         else TransportDispatchStage.PARTIAL_SEND),
    )
