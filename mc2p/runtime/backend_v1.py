"""Formal backend boundary: Action V1 plus immutable, correlated execution evidence."""
from dataclasses import dataclass
from typing import Protocol

from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.action_receipt import (
    ClientBehaviorReceipt, ClientBehaviorReceiptV2, ClientBehaviorReceiptV3,
)
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.runtime.backend import BackendStepResultV0


@dataclass(frozen=True, slots=True)
class BackendStepResultV1(BackendStepResultV0):
    receipt: ClientBehaviorReceipt

    def __post_init__(self) -> None:
        BackendStepResultV0.__post_init__(self)
        if type(self.receipt) not in (ClientBehaviorReceiptV2, ClientBehaviorReceiptV3):
            raise ContractViolation("formal backend result requires immutable client receipt")


class PlayerBackendV1(Protocol):
    action_schema_version: str
    observation_schema_version: str

    def reset(self, request: ResetRequestV0) -> ResetResultV0: ...
    def step(self, action: ActionSnapshotV1, deadline_monotonic_ns: int, *,
             observation_request: ObservationRequestV3 | None = None) -> BackendStepResultV1: ...
    def close(self) -> None: ...
