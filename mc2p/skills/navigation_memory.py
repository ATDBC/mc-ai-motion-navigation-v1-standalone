"""Bounded session-local summaries, not occupancy truth or movement permission.

Only observe(V3) writes evidence. Reads may evict but never renew measurement
times. No world/backend handle, entity prediction, rebinding, or action output.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from functools import cached_property
from heapq import merge
import math

from mc2p.contracts.common import (
    ContractViolation, require_finite, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import ObservedBlockV3, ObservationSnapshotV3
from mc2p.skills.follow_types import FollowEntity
from mc2p.skills.navigation_evidence import (
    EvidenceStamp, NavigationEvidence, ObservationCoverage, ObservationPose,
    project_navigation_evidence,
)
from mc2p.skills.terrain_spatial_index import TerrainIndex, SPHERICAL_TERRAIN_BOUND, TILE_SIZE


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    max_terrain: int = SPHERICAL_TERRAIN_BOUND
    max_entities: int = 64
    terrain_retention_ns: int = 60_000_000_000
    entity_retention_ns: int = 15_000_000_000
    freshness_ns: int = 500_000_000
    radius_blocks: float = 32.0
    max_position_step_blocks: float = 8.0

    def __post_init__(self) -> None:
        for name in ("max_terrain", "max_entities", "terrain_retention_ns", "entity_retention_ns", "freshness_ns"):
            value = getattr(self, name)
            require_nonnegative_int(value, name)
            if value == 0:
                raise ContractViolation(name + " must be positive")
        for name in ("radius_blocks", "max_position_step_blocks"):
            value = getattr(self, name)
            require_finite(value, name)
            if value <= 0:
                raise ContractViolation(name + " must be positive")


@dataclass(frozen=True, slots=True)
class TerrainHistory:
    block: ObservedBlockV3
    last_seen: EvidenceStamp


@dataclass(frozen=True, slots=True)
class EntityHistory:
    entity: FollowEntity
    last_seen: EvidenceStamp
    observer_pose: ObservationPose
    coverage: ObservationCoverage


@dataclass(frozen=True)
class MemorySnapshot:
    scope_id: str
    invalid_reason: str | None
    latest: NavigationEvidence | None
    terrain: tuple[TerrainHistory, ...]
    entities: tuple[EntityHistory, ...]
    recent_entities: tuple[FollowEntity, ...]  # Latest sample only; still historical.

    @cached_property
    def terrain_index(self):
        return TerrainIndex.from_records(self.terrain)


class NavigationMemory:
    """Single ordered consumer. Lifecycle owner supplies a unique scope token.

    reset() is needed after dimension/world/reconnect events and invalidation;
    the owner must flush old observations before accepting the new scope.
    """

    def __init__(self, scope_id: str, limits: MemoryLimits | None = None) -> None:
        require_identifier(scope_id, "navigation scope")
        if limits is not None and type(limits) is not MemoryLimits:
            raise ContractViolation("navigation requires MemoryLimits")
        self.limits = limits or MemoryLimits()
        self._scope_id = scope_id
        self._terrain = OrderedDict()
        self._terrain_index = TerrainIndex()
        self._terrain_changes = set()
        self._terrain_order = None
        self._terrain_order_dirty = False
        self._terrain_added = set()
        self._radius_position = None
        self._radius_cache = {}
        self._entities: dict[str, EntityHistory] = {}
        self._latest: NavigationEvidence | None = None
        self._last_stamp: EvidenceStamp | None = None
        self._last_observation: ObservationSnapshotV3 | None = None
        self._position: Vec3V0 | None = None
        self._clock_id: str | None = None
        self._now_ns: int | None = None
        self._invalid_reason: str | None = None

    def reset(self, scope_id: str) -> None:
        require_identifier(scope_id, "navigation scope")
        if scope_id == self._scope_id:
            raise ContractViolation("reset requires a new unique scope token")
        self.__init__(scope_id, self.limits)

    def _invalidate(self, reason: str) -> None:
        self._terrain.clear()
        self._terrain_index = TerrainIndex()
        self._terrain_changes.clear()
        self._terrain_order = None
        self._terrain_order_dirty = False
        self._terrain_added.clear()
        self._radius_position = None
        self._radius_cache.clear()
        self._entities.clear()
        self._latest = None
        self._last_stamp = None
        self._last_observation = None
        self._position = None
        self._invalid_reason = reason

    def _check_time(self, now_ns: int, controller_clock_id: str) -> None:
        require_nonnegative_int(now_ns, "memory controller now")
        require_identifier(controller_clock_id, "memory controller clock")
        if self._clock_id is not None and self._clock_id != controller_clock_id:
            raise ContractViolation("controller_clock_changed")
        if self._now_ns is not None and now_ns < self._now_ns:
            raise ContractViolation("controller_time_regression")
        self._clock_id, self._now_ns = controller_clock_id, now_ns

    def observe(self, observation: ObservationSnapshotV3, *, now_ns: int,
                controller_clock_id: str, scope_id: str) -> MemorySnapshot:
        # A delayed old task is rejected without destroying the current task's map.
        if scope_id != self._scope_id:
            raise ContractViolation("old_scope_rejected")
        if self._invalid_reason is not None:
            raise ContractViolation("memory_requires_reset: " + self._invalid_reason)
        try:
            self._check_time(now_ns, controller_clock_id)
            if observation is self._last_observation:
                # Runtime drivers hand the exact immutable post-action sample
                # back on their next eligible tick.  It was already validated
                # and projected; only time-based pruning must run again.
                return self.snapshot(
                    now_ns=now_ns,
                    controller_clock_id=controller_clock_id,
                )
            evidence = project_navigation_evidence(observation, now_ns=now_ns,
                                                    controller_clock_id=controller_clock_id)
            if self._last_stamp is not None:
                before, after = self._last_stamp, evidence.stamp
                if before.scope != after.scope or before.source_backend != after.source_backend:
                    raise ContractViolation("session_or_clock_changed")
                if after.sequence_id <= before.sequence_id:
                    if (after.sequence_id == before.sequence_id
                            and observation == self._last_observation):
                        return self.snapshot(now_ns=now_ns, controller_clock_id=controller_clock_id)
                    raise ContractViolation("sequence_regressed_or_rewritten")
                if (after.received_at_ns < before.received_at_ns
                        or after.request_start_ns < before.request_start_ns
                        or after.client_sample.started_at_monotonic_ns < before.client_sample.completed_at_monotonic_ns):
                    raise ContractViolation("sample_time_regression")
            if evidence.available:
                assert evidence.pose is not None
                position = evidence.pose.position
                if self._position is not None and self._distance(position, self._position) > self.limits.max_position_step_blocks:
                    raise ContractViolation("position_discontinuity")
                self._position = position
                self._update_terrain(evidence)
                for entity in evidence.entities:
                    existing = self._entities.get(entity.track_id)
                    if existing is not None and existing.entity.entity_type != entity.entity_type:
                        raise ContractViolation("track_type_changed")
                    self._entities[entity.track_id] = EntityHistory(entity, evidence.stamp,
                                                                    evidence.pose, evidence.coverage)
            self._latest = evidence
            self._last_stamp, self._last_observation = evidence.stamp, observation
        except ContractViolation as error:
            self._invalidate(str(error))
            raise
        return self.snapshot(now_ns=now_ns, controller_clock_id=controller_clock_id)

    @staticmethod
    def _distance(a: Vec3V0, b: Vec3V0) -> float:
        return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))

    def _update_terrain(self, evidence: NavigationEvidence) -> None:
        for block in evidence.blocks:
            if self._terrain_distance_sq(block.position) > self.limits.radius_blocks ** 2:
                continue
            if block.position not in self._terrain:
                self._terrain_order_dirty = True
                if self._terrain_index.get(block.position) is None:
                    self._terrain_added.add(block.position)
            self._terrain[block.position] = TerrainHistory(block, evidence.stamp)
            self._terrain.move_to_end(block.position)
            self._terrain_changes.add(block.position)

    def _terrain_distance_sq(self, block):
        p = self._position
        return (block[0]+.5-p.x)**2 + (block[1]+.5-p.y)**2 + (block[2]+.5-p.z)**2

    def _drop_terrain(self, block):
        if block in self._terrain:
            del self._terrain[block]
            self._terrain_changes.add(block)
            self._terrain_order_dirty = True
            self._terrain_added.discard(block)

    def _prune(self, now_ns: int) -> None:
        # Expire the projected payload while retaining one immutable input frame
        # for exact replay/rewrite detection. Reads never restore that payload.
        if self._latest and now_ns - self._latest.stamp.request_start_ns >= min(
                self.limits.terrain_retention_ns, self.limits.entity_retention_ns):
            self._latest = None
        if self._position is None:
            return

        while self._terrain:
            oldest = next(iter(self._terrain))
            if now_ns-self._terrain[oldest].last_seen.request_start_ns < self.limits.terrain_retention_ns:
                break
            self._drop_terrain(oldest)
        if self._radius_position != self._position:
            eye = (self._position.x,self._position.y,self._position.z)
            radius_sq = self.limits.radius_blocks ** 2
            for key, bucket in self._terrain_index.owners.items():
                cached = self._radius_cache.get(key)
                if cached is not None and math.dist(eye,cached[0]) <= cached[1]:
                    continue
                furthest = 0.
                for block in bucket:
                    if block not in self._terrain:
                        continue
                    distance_sq = self._terrain_distance_sq(block)
                    if distance_sq > radius_sq:
                        self._drop_terrain(block)
                    else:
                        furthest = max(furthest,distance_sq)
                # Triangle inequality proves the whole retained tile remains inside
                # until the body travels this far. The margin makes reuse conservative.
                self._radius_cache[key] = (eye,max(0.,self.limits.radius_blocks-math.sqrt(furthest)-1e-10))
            self._radius_position = self._position
        if len(self._terrain) > self.limits.max_terrain:
            # Explicit smaller legacy/test budgets retain their deterministic order.
            eligible = sorted(self._terrain, key=lambda block: (
                -self._terrain[block].last_seen.request_start_ns,
                self._terrain_distance_sq(block), block))
            for block in eligible[self.limits.max_terrain:]:
                self._drop_terrain(block)
        eligible_entities = [key for key, record in self._entities.items()
                    if now_ns - record.last_seen.request_start_ns < self.limits.entity_retention_ns
                    and self._distance(record.entity.position, self._position) <= self.limits.radius_blocks]
        eligible_entities.sort(key=lambda key: (-self._entities[key].last_seen.request_start_ns,
                              self._distance(self._entities[key].entity.position, self._position), key))
        self._entities = {key: self._entities[key] for key in eligible_entities[:self.limits.max_entities]}

    def snapshot(self, *, now_ns: int, controller_clock_id: str) -> MemorySnapshot:
        try:
            self._check_time(now_ns, controller_clock_id)
        except ContractViolation as error:
            self._invalidate(str(error))
            raise
        self._prune(now_ns)
        recent = ()
        if self._latest and self._latest.available and self._latest.stamp.recent(now_ns, self.limits.freshness_ns):
            recent = tuple(entity for entity in self._latest.entities if entity.track_id in self._entities)
        if self._terrain_changes:
            changed = [self._terrain[p] for p in self._terrain_changes if p in self._terrain]
            removed = [p for p in self._terrain_changes if p not in self._terrain]
            scope = None if self._last_stamp is None else self._last_stamp.scope
            self._terrain_index = self._terrain_index.updated(changed, removed, scope)
            for position in self._terrain_added:
                self._radius_cache.pop(tuple(v//TILE_SIZE for v in position),None)
            for position in removed:
                key = tuple(v//TILE_SIZE for v in position)
                if key not in self._terrain_index.owners:
                    self._radius_cache.pop(key,None)
            self._terrain_changes.clear()
        if self._terrain_order is None:
            self._terrain_order = tuple(sorted(self._terrain))
        elif self._terrain_order_dirty:
            self._terrain_order = tuple(merge(
                (p for p in self._terrain_order if p in self._terrain),sorted(self._terrain_added)))
        self._terrain_order_dirty = False
        self._terrain_added.clear()
        result = MemorySnapshot(self._scope_id, self._invalid_reason, self._latest,
                   tuple(self._terrain[key] for key in self._terrain_order),
                   tuple(self._entities[key] for key in sorted(self._entities)), recent)
        object.__setattr__(result, 'terrain_index', self._terrain_index)
        return result
