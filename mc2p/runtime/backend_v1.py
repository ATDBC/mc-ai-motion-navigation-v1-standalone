"""Formal backend boundary: Action V1 plus immutable, correlated execution evidence."""
from dataclasses import dataclass
from typing import Protocol

from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.action_receipt import (
    ClientBehaviorReceipt, ClientBehaviorReceiptV2, ClientBehaviorReceiptV3, ClientBehaviorReceiptV4,
)
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.contracts.report import FailureCodeV0, FailureV0
from mc2p.runtime.backend import BackendStepResultV0
from mc2p.runtime.transport_lifecycle import (
    TransportDispatchStage, TransportLifecycleKind, unresolved_transport_fact,
)


class BackendIOFailure(OSError):
    """A backend that knows recoverability can preserve its typed I/O failure."""

    def __init__(self, failure: FailureV0) -> None:
        if type(failure) is not FailureV0 or failure.code is not FailureCodeV0.BACKEND_IO:
            raise ContractViolation("backend I/O failure requires typed BACKEND_IO")
        super().__init__(failure.message)
        self.failure = failure


class _UnresolvedBackendTransport:
    lifecycle_kind: TransportLifecycleKind

    def __init__(self, message: str, *, dispatch_stage=None) -> None:
        super().__init__(message)
        if dispatch_stage is None:
            dispatch_stage = (
                TransportDispatchStage.SENT_NO_RECEIPT
                if self.lifecycle_kind is TransportLifecycleKind.DELIVERY_UNKNOWN
                else TransportDispatchStage.CHANNEL_SEALED
            )
        self.dispatch_stage = dispatch_stage

    def lifecycle_fact(self, action, generation_id, causal_movement_tick):
        return unresolved_transport_fact(
            self.lifecycle_kind, action, generation_id, causal_movement_tick,
            str(self) or self.lifecycle_kind.value,
            dispatch_stage=self.dispatch_stage,
        )


class BackendDeliveryUnknown(_UnresolvedBackendTransport, TimeoutError):
    """A request entered transport without a correlated remote result."""

    lifecycle_kind = TransportLifecycleKind.DELIVERY_UNKNOWN


class BackendChannelLost(_UnresolvedBackendTransport, OSError):
    """The sole authenticated client channel is sealed or disconnected."""

    lifecycle_kind = TransportLifecycleKind.CHANNEL_LOST


@dataclass(frozen=True, slots=True)
class BackendStepResultV1(BackendStepResultV0):
    receipt: ClientBehaviorReceipt

    def __post_init__(self) -> None:
        BackendStepResultV0.__post_init__(self)
        if type(self.receipt) not in (ClientBehaviorReceiptV2, ClientBehaviorReceiptV3, ClientBehaviorReceiptV4):
            raise ContractViolation("formal backend result requires immutable client receipt")


class PlayerBackendV1(Protocol):
    action_schema_version: str
    observation_schema_version: str

    def reset(self, request: ResetRequestV0) -> ResetResultV0: ...
    def step(self, action: ActionSnapshotV1, deadline_monotonic_ns: int, *,
             observation_request: ObservationRequestV3 | None = None) -> BackendStepResultV1: ...
    def close(self) -> None: ...
