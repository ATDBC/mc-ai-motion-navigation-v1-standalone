"""Restricted, image-free internal values; these are not new actor wire contracts."""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, require_finite, require_identifier, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import ObservedBlockV3

TARGET_TTL_NS = 2_000_000_000
MOVEMENT_FRESHNESS_NS = 500_000_000
MAX_SKILL_NS = 120_000_000_000
MAX_DECISIONS = 2400


@dataclass(frozen=True, slots=True)
class FollowRequest:
    skill_id: str
    episode_id: str
    target_track_id: str
    started_at_ns: int
    deadline_ns: int
    controller_clock_id: str
    retreat_distance_blocks: float = 1.5
    hold_distance_blocks: float = 2.5
    approach_distance_blocks: float = 3.5

    def __post_init__(self) -> None:
        for name in ("skill_id", "episode_id", "target_track_id", "controller_clock_id"):
            require_identifier(getattr(self, name), name)
        if len(self.skill_id) > 80:
            raise ContractViolation("follow skill id exceeds intent namespace budget")
        require_nonnegative_int(self.started_at_ns, "follow start")
        require_nonnegative_int(self.deadline_ns, "follow deadline")
        if not 0 < self.deadline_ns - self.started_at_ns <= MAX_SKILL_NS:
            raise ContractViolation("follow duration must be positive and at most 120 seconds")
        for name in ("retreat_distance_blocks", "hold_distance_blocks", "approach_distance_blocks"):
            require_finite(getattr(self,name),name)
        if not 1 <= self.retreat_distance_blocks < self.hold_distance_blocks < self.approach_distance_blocks <= 6:
            raise ContractViolation("follow distances must satisfy 1 <= retreat < hold < approach <= 6 blocks")


@dataclass(frozen=True, slots=True)
class FollowSelf:
    position: Vec3V0
    velocity: Vec3V0
    yaw: float
    pitch: float
    on_ground: bool
    dead: bool
    horizontal_collision: bool
    unsupported_motion: bool


@dataclass(frozen=True, slots=True)
class FollowEntity:
    track_id: str
    entity_type: str
    position: Vec3V0
    size: Vec3V0


@dataclass(frozen=True, slots=True)
class FollowView:
    episode_id: str
    sequence_id: int
    controller_clock_id: str
    client_clock_id: str
    client_sample_start_ns: int
    client_sample_end_ns: int
    request_start_ns: int
    received_at_ns: int
    available: bool
    reason: str | None
    own: FollowSelf | None = None
    gui_open: bool = False
    observed_blocks: tuple[ObservedBlockV3, ...] = ()
    entities: tuple[FollowEntity, ...] = ()
    entities_truncated: bool = False


@dataclass(frozen=True, slots=True)
class TargetState:
    status: str
    position: Vec3V0 | None
    last_visible_at_ns: int | None
    reason: str | None = None
