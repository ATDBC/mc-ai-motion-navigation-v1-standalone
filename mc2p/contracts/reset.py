"""Player Runtime V0 reset request and result."""

from __future__ import annotations

from dataclasses import dataclass, field

from mc2p.contracts.common import (
    ContractViolation,
    parse_json_object,
    require_identifier,
    require_nonnegative_int,
)
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.report import FailureV0


_FORBIDDEN_RESET_KEYS = frozenset(
    {
        "command",
        "commands",
        "initial_extra_commands",
        "initialextracommands",
        "give",
        "tp",
    }
)


@dataclass(frozen=True, slots=True)
class ResetRequestV0:
    request_id: str
    episode_id: str
    scenario_id: str
    seed: int
    deadline_monotonic_ns: int
    backend_options_json: str = "{}"
    schema_version: str = field(default="mc2p.reset-request.v0", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.episode_id, "episode_id")
        require_identifier(self.scenario_id, "scenario_id")
        if type(self.seed) is not int:
            raise ContractViolation("seed must be an integer")
        require_nonnegative_int(
            self.deadline_monotonic_ns,
            "deadline_monotonic_ns",
        )
        if self.deadline_monotonic_ns == 0:
            raise ContractViolation("deadline_monotonic_ns must be positive")
        parse_json_object(
            self.backend_options_json,
            "backend options",
            forbidden_keys=_FORBIDDEN_RESET_KEYS,
        )


@dataclass(frozen=True, slots=True)
class ResetResultV0:
    request_id: str
    actual_episode_id: str
    succeeded: bool
    observation: ObservationSnapshotV2 | ObservationSnapshotV3 | None = None
    failure: FailureV0 | None = None
    schema_version: str = field(default="mc2p.reset-result.v0", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.actual_episode_id, "actual_episode_id")
        if type(self.succeeded) is not bool:
            raise ContractViolation("succeeded must be bool")
        if self.succeeded:
            if self.observation is None or self.failure is not None:
                raise ContractViolation(
                    "successful reset requires observation and no failure"
                )
            if type(self.observation) not in (ObservationSnapshotV2, ObservationSnapshotV3):
                raise ContractViolation(
                    "successful reset requires ObservationSnapshotV2 or V3"
                )
        elif self.observation is not None or self.failure is None:
            raise ContractViolation("failed reset requires failure and no observation")
