"""Bounded derived terrain beliefs over immutable legal navigation history.

This module never samples the world and never rewrites evidence stamps.  A
belief can only refer to a block still retained by ``NavigationMemory``.
"""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation_v3 import ObservedBlockV3
from mc2p.skills.block_geometry import is_supported_floor
from mc2p.skills.navigation_memory import MemorySnapshot, TerrainHistory
from mc2p.skills.terrain_spatial_index import SPHERICAL_TERRAIN_BOUND


MAX_TERRAIN_BELIEFS = SPHERICAL_TERRAIN_BOUND
FRESH_SUPPORT_NS = 500_000_000
HISTORICAL_SUPPORT_NS = 60_000_000_000


@dataclass(frozen=True, slots=True)
class TerrainBelief:
    history: TerrainHistory
    contradicted: bool = False
    review_after_ns: int | None = None

    def __post_init__(self) -> None:
        if type(self.history) is not TerrainHistory:
            raise ContractViolation("terrain belief requires exact TerrainHistory")
        if type(self.contradicted) is not bool:
            raise ContractViolation("terrain contradiction must be boolean")
        if self.review_after_ns is not None:
            require_nonnegative_int(self.review_after_ns,'terrain review time')


def support_admission(
    record: TerrainHistory | None,
    now_ns: int,
    *,
    historical: bool,
    high_consequence: bool,
    contradicted: bool,
) -> str | None:
    """Classify one required support record without renewing its evidence.

    Neighborhood completeness and body-envelope consequence are properties of
    the caller's whole segment.  This function only admits the supplied block;
    a caller may use the 60 second limit only after proving that the segment is
    low consequence and its required one-block neighborhood is complete.
    """
    require_nonnegative_int(now_ns, "support admission time")
    for value, name in (
        (historical, "historical admission"),
        (high_consequence, "high consequence"),
        (contradicted, "support contradiction"),
    ):
        if type(value) is not bool:
            raise ContractViolation(name + " must be boolean")
    if record is None:
        return "missing_support"
    if type(record) is not TerrainHistory:
        raise ContractViolation("support admission requires exact TerrainHistory")

    block = record.block
    if type(block) is not ObservedBlockV3:
        raise ContractViolation("terrain history carries invalid block")
    if block.collision.kind == "unsupported":
        return "missing_field"
    if block.collision.kind != "empty" and not is_supported_floor(block):
        return "unsupported_motion"
    if contradicted:
        return "contradiction"
    if block.collision.kind == "empty":
        return "missing_support"

    age_ns = now_ns - record.last_seen.request_start_ns
    if age_ns < 0:
        return "control_unavailable"
    limit_ns = (
        HISTORICAL_SUPPORT_NS
        if historical and not high_consequence
        else FRESH_SUPPORT_NS
    )
    if age_ns > limit_ns:
        return "uncertain_history"
    return None


def _geometry_key(block: ObservedBlockV3) -> tuple[object, ...]:
    """Decision geometry excludes discovery source and measurement time."""
    return block.collision.kind, block.collision.boxes, block.fluid_id


def _grid(block: tuple[int, int, int]) -> None:
    if (
        type(block) is not tuple
        or len(block) != 3
        or any(type(value) is not int for value in block)
    ):
        raise ContractViolation("belief block must be an integer grid triple")


class BeliefStore:
    """Session-bound derived markers over the current ``MemorySnapshot``.

    ``scope_id`` is NavigationMemory ownership. ``evidence_scope`` is the
    episode/controller/JVM clock triple.  They are checked independently and
    are deliberately never compared with one another.
    """

    def __init__(self) -> None:
        self._beliefs: dict[tuple[int, int, int], TerrainBelief] = {}
        self._scope_id: str | None = None
        self._evidence_scope: tuple[str, str, str] | None = None
        self._terrain_token = None

    @property
    def beliefs(self) -> tuple[TerrainBelief, ...]:
        return tuple(self._beliefs[position] for position in sorted(self._beliefs))

    @property
    def scope_id(self) -> str | None:
        return self._scope_id

    @property
    def evidence_scope(self) -> tuple[str, str, str] | None:
        return self._evidence_scope

    def get(self, block: tuple[int, int, int]) -> TerrainBelief | None:
        _grid(block)
        return self._beliefs.get(block)

    def observe(self, snapshot: MemorySnapshot) -> None:
        if type(snapshot) is not MemorySnapshot:
            raise ContractViolation("belief store requires exact MemorySnapshot")
        if self._scope_id is not None and snapshot.scope_id != self._scope_id:
            raise ContractViolation("old_belief_scope_rejected")
        if snapshot.invalid_reason is not None:
            self.clear()
            raise ContractViolation("invalid_navigation_memory")
        if len(snapshot.terrain) > MAX_TERRAIN_BELIEFS:
            self.clear()
            raise ContractViolation("terrain belief capacity exceeded")
        try:
            index = snapshot.terrain_index
        except ContractViolation:
            self.clear()
            raise

        scopes = set() if index.scope is None else {index.scope}
        if snapshot.latest is not None:
            scopes.add(snapshot.latest.stamp.scope)
        if len(scopes) > 1:
            self.clear()
            raise ContractViolation("mixed_evidence_scope")
        evidence_scope = next(iter(scopes), None)
        if (
            self._evidence_scope is not None
            and evidence_scope is not None
            and evidence_scope != self._evidence_scope
        ):
            raise ContractViolation("belief_evidence_scope_changed")

        current_blocks: dict[tuple[int, int, int], ObservedBlockV3] = {}
        if snapshot.latest is not None and snapshot.latest.available:
            current_blocks = {
                block.position: block for block in snapshot.latest.blocks
            }

        if self._terrain_token is index.token:
            records = ()
            updated = self._beliefs
        elif self._terrain_token is index.base_token:
            updated = self._beliefs
            for position in index.removed:
                updated.pop(position, None)
            records = index.changed
        else:
            updated = {}
            records = snapshot.terrain
        for record in records:
            position = record.block.position
            previous = self._beliefs.get(position)
            contradicted = False if previous is None else previous.contradicted
            review_after=None if previous is None else previous.review_after_ns
            if review_after is not None and record.last_seen.request_start_ns>review_after:
                review_after=None
            sampled = current_blocks.get(position)
            if (
                contradicted
                and sampled is not None
                and _geometry_key(sampled) != _geometry_key(previous.history.block)
            ):
                contradicted = False
            updated[position] = (previous if previous is not None and previous.history is record
                                 and previous.contradicted == contradicted and previous.review_after_ns==review_after
                                 else TerrainBelief(record, contradicted,review_after))

        self._beliefs = updated
        self._scope_id = snapshot.scope_id
        self._terrain_token = index.token
        if evidence_scope is not None:
            self._evidence_scope = evidence_scope

    def contradict(self, blocks: tuple[tuple[int, int, int], ...]) -> None:
        if type(blocks) is not tuple:
            raise ContractViolation("contradiction blocks must be an immutable tuple")
        for block in blocks:
            _grid(block)
        for block in set(blocks):
            belief = self._beliefs.get(block)
            if belief is not None and not belief.contradicted:
                self._beliefs[block] = TerrainBelief(belief.history, True,belief.review_after_ns)

    def require_review(self,blocks: tuple[tuple[int,int,int],...],after_ns: int) -> None:
        """Soft deferral, resolved by a later observation even of the same shape."""
        require_nonnegative_int(after_ns,'terrain review time')
        if type(blocks) is not tuple:raise ContractViolation('terrain review blocks must be tuple')
        for block in blocks:
            _grid(block)
            marker=self._beliefs.get(block)
            if marker is not None and marker.review_after_ns is None:
                self._beliefs[block]=TerrainBelief(marker.history,marker.contradicted,after_ns)

    def clear(self) -> None:
        self._beliefs.clear()
        self._scope_id = None
        self._evidence_scope = None
        self._terrain_token = None
