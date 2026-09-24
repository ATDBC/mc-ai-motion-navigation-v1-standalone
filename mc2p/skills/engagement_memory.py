"""Pure, bounded permission for using one moving combat target position."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from mc2p.contracts.common import (
    ContractViolation, FieldStatusV0, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import VisibleEntityV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3, TrackedEntityStateV3
from mc2p.skills.fixed_melee import CombatTargetV1


POSITION_FRESHNESS_NS = 500_000_000
RETALIATION_GRANT = "retaliation_after_confirmed_hit"


class TargetPositionSource(StrEnum):
    VISION = "vision"
    ENGAGEMENT = "engagement"


class EngagementEventKind(StrEnum):
    OBSERVED = "observed"
    CONFIRMED_HIT = "confirmed_hit"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class EngagementEventV1:
    kind: EngagementEventKind
    task_id: str
    goal_id: str
    target_revision: int
    target_episode_id: str
    observation_episode_id: str
    track_id: str
    observation_sequence_id: int
    client_completed_at_monotonic_ns: int
    received_at_monotonic_ns: int
    visible_position: Vec3V0 | None
    target_available: bool
    target_dead: bool
    schema_version: str = field(default="mc2p.engagement-event.v1", init=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not EngagementEventKind:
            raise ContractViolation("engagement event kind is invalid")
        for name in ("task_id", "goal_id", "target_episode_id",
                     "observation_episode_id", "track_id"):
            require_identifier(getattr(self, name), name)
        for name in ("target_revision", "observation_sequence_id",
                     "client_completed_at_monotonic_ns", "received_at_monotonic_ns"):
            require_nonnegative_int(getattr(self, name), name)
        if self.visible_position is not None and type(self.visible_position) is not Vec3V0:
            raise ContractViolation("visible position must be a typed vector")
        if type(self.target_available) is not bool or type(self.target_dead) is not bool:
            raise ContractViolation("engagement availability must be boolean")


@dataclass(frozen=True, slots=True)
class EngagementStateV1:
    task_id: str
    goal_id: str
    target_revision: int
    episode_id: str
    track_id: str
    active: bool = True
    seen_directly: bool = False
    engagement_granted: bool = False
    grant_basis: str | None = None
    last_observation_sequence_id: int | None = None
    last_client_completed_at_monotonic_ns: int | None = None
    last_received_at_monotonic_ns: int | None = None
    last_visible_position: Vec3V0 | None = None
    revocation_reason: str | None = None
    schema_version: str = field(default="mc2p.engagement-state.v1", init=False)

    def __post_init__(self) -> None:
        for name in ("task_id", "goal_id", "episode_id", "track_id"):
            require_identifier(getattr(self, name), name)
        require_nonnegative_int(self.target_revision, "target revision")
        if type(self.active) is not bool or type(self.seen_directly) is not bool \
                or type(self.engagement_granted) is not bool:
            raise ContractViolation("engagement flags must be boolean")

    @classmethod
    def for_target(cls, target: CombatTargetV1) -> "EngagementStateV1":
        if type(target) is not CombatTargetV1:
            raise ContractViolation("engagement state requires CombatTargetV1")
        return cls(target.task_id, target.goal_id, target.revision,
                   target.episode_id, target.track_id)


@dataclass(frozen=True, slots=True)
class TargetPositionFactV1:
    track_id: str
    source: TargetPositionSource
    relative_position: Vec3V0
    relative_velocity: Vec3V0
    observation_sequence_id: int
    received_at_monotonic_ns: int
    health_points: float | None
    max_health_points: float | None
    is_dead: bool | None
    schema_version: str = field(default="mc2p.target-position-fact.v1", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.track_id, "track id")
        if type(self.source) is not TargetPositionSource:
            raise ContractViolation("target position source is invalid")
        if type(self.relative_position) is not Vec3V0 or type(self.relative_velocity) is not Vec3V0:
            raise ContractViolation("target position fact requires typed vectors")
        require_nonnegative_int(self.observation_sequence_id, "observation sequence")
        require_nonnegative_int(self.received_at_monotonic_ns, "observation receive time")


def _visible_target(observation: ObservationSnapshotV3,
                    track_id: str) -> VisibleEntityV2 | None:
    if (observation.perception.status is not FieldStatusV0.VALID
            or observation.perception.value is None):
        return None
    return next((entity for entity in observation.perception.value.visible_entities
                 if entity.track_id == track_id), None)


def _tracked_target(observation: ObservationSnapshotV3,
                    track_id: str) -> TrackedEntityStateV3 | None:
    value = observation.tracked_entity.value
    return value if value is not None and value.track_id == track_id else None


def event_from_observation(
    target: CombatTargetV1,
    observation: ObservationSnapshotV3,
    *,
    kind: EngagementEventKind = EngagementEventKind.OBSERVED,
) -> EngagementEventV1:
    if type(target) is not CombatTargetV1 or type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("engagement event requires typed target and observation")
    if type(kind) is not EngagementEventKind:
        raise ContractViolation("engagement event kind is invalid")
    visible = _visible_target(observation, target.track_id)
    tracked = _tracked_target(observation, target.track_id)
    unavailable = (
        visible is None
        and tracked is None
        and observation.tracked_entity.reason_code in {
            "entity_unavailable", "not_living_entity", "player_or_world_missing",
        }
    )
    return EngagementEventV1(
        kind, target.task_id, target.goal_id, target.revision, target.episode_id,
        observation.episode_id, target.track_id, observation.sequence_id,
        observation.client_sample.completed_at_monotonic_ns,
        observation.received_at_monotonic_ns,
        None if visible is None else visible.relative_position,
        not unavailable, False if tracked is None else tracked.is_dead,
    )


def _revoke(state: EngagementStateV1, reason: str) -> EngagementStateV1:
    return replace(state, active=False, engagement_granted=False,
                   grant_basis=None, revocation_reason=reason)


def advance_engagement(state: EngagementStateV1,
                       event: EngagementEventV1) -> EngagementStateV1:
    if type(state) is not EngagementStateV1 or type(event) is not EngagementEventV1:
        raise ContractViolation("engagement reducer requires typed state and event")
    if not state.active:
        return state
    if (event.task_id != state.task_id or event.goal_id != state.goal_id
            or event.target_revision != state.target_revision
            or event.target_episode_id != state.episode_id
            or event.track_id != state.track_id):
        return _revoke(state, "target_identity_changed")
    if event.observation_episode_id != state.episode_id:
        return _revoke(state, "world_session_changed")
    if event.kind is EngagementEventKind.CANCELLED:
        return _revoke(state, "task_cancelled")
    if not event.target_available:
        return _revoke(state, "target_unavailable")
    if event.target_dead:
        return _revoke(state, "target_dead")

    previous_sequence = state.last_observation_sequence_id
    if previous_sequence is not None:
        if event.observation_sequence_id < previous_sequence:
            return _revoke(state, "observation_sequence_regressed")
        if event.observation_sequence_id > previous_sequence + 1:
            return _revoke(state, "observation_gap")
        if (event.observation_sequence_id == previous_sequence
                and event.kind is not EngagementEventKind.CONFIRMED_HIT):
            return _revoke(state, "observation_sequence_regressed")
    if (state.last_client_completed_at_monotonic_ns is not None
            and event.client_completed_at_monotonic_ns
                < state.last_client_completed_at_monotonic_ns):
        return _revoke(state, "client_sample_time_regressed")
    if (state.last_received_at_monotonic_ns is not None
            and event.received_at_monotonic_ns < state.last_received_at_monotonic_ns):
        return _revoke(state, "controller_receive_time_regressed")

    seen = state.seen_directly or event.visible_position is not None
    grant = state.engagement_granted
    basis = state.grant_basis
    if event.kind is EngagementEventKind.CONFIRMED_HIT:
        if not seen:
            return _revoke(state, "confirmed_hit_without_direct_sighting")
        grant, basis = True, RETALIATION_GRANT
    return replace(
        state,
        seen_directly=seen,
        engagement_granted=grant,
        grant_basis=basis,
        last_observation_sequence_id=event.observation_sequence_id,
        last_client_completed_at_monotonic_ns=event.client_completed_at_monotonic_ns,
        last_received_at_monotonic_ns=event.received_at_monotonic_ns,
        last_visible_position=(event.visible_position
                               if event.visible_position is not None
                               else state.last_visible_position),
    )


def resolve_target_position(
    state: EngagementStateV1,
    observation: ObservationSnapshotV3,
    target: CombatTargetV1,
    now_ns: int,
    freshness_ns: int = POSITION_FRESHNESS_NS,
) -> TargetPositionFactV1 | None:
    if (type(state) is not EngagementStateV1
            or type(observation) is not ObservationSnapshotV3
            or type(target) is not CombatTargetV1):
        raise ContractViolation("target position resolution requires typed values")
    require_nonnegative_int(now_ns, "target position decision time")
    require_nonnegative_int(freshness_ns, "target position freshness")
    if (not state.active
            or (target.task_id, target.goal_id, target.revision, target.episode_id, target.track_id)
                != (state.task_id, state.goal_id, state.target_revision,
                    state.episode_id, state.track_id)
            or observation.episode_id != state.episode_id
            or observation.sequence_id != state.last_observation_sequence_id
            or observation.client_sample.completed_at_monotonic_ns
                != state.last_client_completed_at_monotonic_ns
            or observation.received_at_monotonic_ns != state.last_received_at_monotonic_ns
            or now_ns < observation.received_at_monotonic_ns
            or now_ns - observation.received_at_monotonic_ns > freshness_ns):
        return None
    visible = _visible_target(observation, target.track_id)
    tracked = _tracked_target(observation, target.track_id)
    health = None if tracked is None else tracked.health_points
    maximum = None if tracked is None else tracked.max_health_points
    dead = None if tracked is None else tracked.is_dead
    if visible is not None:
        return TargetPositionFactV1(
            target.track_id, TargetPositionSource.VISION,
            visible.relative_position, visible.relative_velocity,
            observation.sequence_id, observation.received_at_monotonic_ns,
            health, maximum, dead,
        )
    if (not state.engagement_granted or state.grant_basis != RETALIATION_GRANT
            or tracked is None or tracked.is_dead or not tracked.is_loaded):
        return None
    return TargetPositionFactV1(
        target.track_id, TargetPositionSource.ENGAGEMENT,
        tracked.relative_position, tracked.relative_velocity,
        observation.sequence_id, observation.received_at_monotonic_ns,
        tracked.health_points, tracked.max_health_points, tracked.is_dead,
    )
