"""Restricted playground-only views: legal state in, no protocol truth or diagnostic motion out."""
from dataclasses import dataclass
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_types import FollowView


@dataclass(frozen=True, slots=True)
class PlaygroundView:
    base: FollowView
    pose: str | None
    food_points: int | None
    game_mode: str | None
    status_effect_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrackedTarget:
    status: str
    track_id: str | None
    position: Vec3V0 | None
    size: Vec3V0 | None
    last_seen_ns: int | None
    reason: str | None = None
