"""Backend protocol consumed exclusively by PlayerRuntimeV0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mc2p.contracts.action import ActionSnapshotV0
from mc2p.contracts.common import ContractViolation, require_finite
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0


@dataclass(frozen=True, slots=True)
class BackendStepResultV0:
    observation: ObservationSnapshotV2 | ObservationSnapshotV3
    reward: float
    terminated: bool
    truncated: bool

    def __post_init__(self) -> None:
        if type(self.observation) not in (ObservationSnapshotV2, ObservationSnapshotV3):
            raise ContractViolation("backend result requires ObservationSnapshotV2 or V3")
        require_finite(self.reward, "backend reward")
        if type(self.terminated) is not bool or type(self.truncated) is not bool:
            raise ContractViolation("backend termination values must be bool")


class PlayerBackendV0(Protocol):
    def reset(self, request: ResetRequestV0) -> ResetResultV0: ...

    def step(
        self,
        action: ActionSnapshotV0,
        deadline_monotonic_ns: int,
    ) -> BackendStepResultV0: ...

    def close(self) -> None: ...
