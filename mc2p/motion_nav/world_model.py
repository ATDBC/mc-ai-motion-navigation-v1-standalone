"""World-session, time and three-state voxel knowledge for the new motion core."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from enum import StrEnum
from functools import wraps
import math
import threading
from typing import TYPE_CHECKING, Iterable, Mapping
import uuid
from weakref import WeakValueDictionary

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int

if TYPE_CHECKING:
    from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
    from mc2p.motion_nav.physics_types import PhysicsRuleset
    from mc2p.motion_nav.prepared_motion import (
        DependencyVersions, SnapshotLease, SnapshotLeaseResult, WorldSnapshotVersion,
    )
    from mc2p.motion_nav.world_snapshot import DependencyCheck, SealedSnapshot


BlockPos = tuple[int, int, int]
SectionPos = tuple[int, int, int]
WORLD_SECTION_SIZE = 16
DEFAULT_MAX_KNOWN_CELLS = 262_144
DEFAULT_PROTECTION_RADIUS_BLOCKS = 32.0
DEFAULT_MAX_SNAPSHOT_LEASES = 8
DEFAULT_MAX_SNAPSHOT_CELLS = 16_384
DEFAULT_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_EPSILON = 1.0e-9
MIN_BLOCK_COLLISION_Y = 0.0
MAX_BLOCK_COLLISION_Y = 2.0
COLLISION_OWNER_BELOW_REACH_CELLS = 1


def _position(value: BlockPos) -> None:
    if type(value) is not tuple or len(value) != 3 or any(type(part) is not int for part in value):
        raise ContractViolation("block position must be an integer triple")


def block_section(position: BlockPos) -> SectionPos:
    """Return the 16x16x16 section that owns one block position."""
    _position(position)
    return _block_section_from_valid_position(position)


def _block_section_from_valid_position(position: BlockPos) -> SectionPos:
    """Return a section after the caller has validated the block position."""
    return tuple(part // WORLD_SECTION_SIZE for part in position)  # type: ignore[return-value]


def _section(value: SectionPos) -> None:
    if type(value) is not tuple or len(value) != 3 \
            or any(type(part) is not int for part in value):
        raise ContractViolation("section position must be an integer triple")


def _finite(value: float, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class WorldSessionId:
    value: str

    def __post_init__(self) -> None:
        require_identifier(self.value, "world session id")


@dataclass(frozen=True, slots=True)
class ObservationStamp:
    session: WorldSessionId
    sequence_id: int
    world_tick: int
    controller_clock_id: str
    received_monotonic_ns: int

    def __post_init__(self) -> None:
        if type(self.session) is not WorldSessionId:
            raise ContractViolation("observation stamp requires a world session")
        require_nonnegative_int(self.sequence_id, "observation sequence")
        require_nonnegative_int(self.world_tick, "world tick")
        require_identifier(self.controller_clock_id, "controller clock id")
        require_nonnegative_int(self.received_monotonic_ns, "receipt time")

    @property
    def causal_order(self) -> tuple[int, int]:
        """Order facts inside one world session by controller observation order.

        Minecraft world time can move backward after sleep, server correction or
        an administrator command.  The observation sequence remains monotonic
        for the lifetime of the world session, so it must lead this ordering.
        """
        return self.sequence_id, self.world_tick

    @property
    def world_order(self) -> tuple[int, int]:
        """Compatibility name for the causal observation order."""
        return self.causal_order

    def elapsed_seconds(self, earlier: ObservationStamp) -> float:
        return elapsed_seconds(self, earlier)


@dataclass(frozen=True, slots=True)
class VisualAirEvidence:
    """What part of one visually empty cell was actually exposed."""

    stamp: ObservationStamp
    observer_distance_blocks: float
    lower_region_visible: bool

    def __post_init__(self) -> None:
        if type(self.stamp) is not ObservationStamp:
            raise ContractViolation("visual-air evidence requires an observation stamp")
        _finite(self.observer_distance_blocks, "visual-air observer distance")
        if self.observer_distance_blocks < 0:
            raise ContractViolation("visual-air observer distance must be nonnegative")
        if type(self.lower_region_visible) is not bool:
            raise ContractViolation("visual-air lower-region evidence must be explicit")


def elapsed_seconds(later: ObservationStamp, earlier: ObservationStamp) -> float:
    if type(later) is not ObservationStamp or type(earlier) is not ObservationStamp:
        raise ContractViolation("elapsed time requires observation stamps")
    if later.session != earlier.session:
        raise ContractViolation("cannot subtract time across world sessions")
    if later.controller_clock_id != earlier.controller_clock_id:
        raise ContractViolation("cannot subtract different monotonic clocks")
    if later.received_monotonic_ns < earlier.received_monotonic_ns:
        raise ContractViolation("later observation precedes earlier observation")
    return (later.received_monotonic_ns - earlier.received_monotonic_ns) / 1_000_000_000.0


@dataclass(frozen=True, slots=True)
class Aabb:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    def __post_init__(self) -> None:
        for value in self.as_tuple():
            _finite(value, "AABB coordinate")
        if not (self.min_x < self.max_x and self.min_y < self.max_y and self.min_z < self.max_z):
            raise ContractViolation("AABB must have positive volume")

    def as_tuple(self) -> tuple[float, ...]:
        return self.min_x, self.min_y, self.min_z, self.max_x, self.max_y, self.max_z

    def moved(self, dx: float, dy: float, dz: float) -> Aabb:
        for value in (dx, dy, dz):
            _finite(value, "AABB displacement")
        return Aabb(self.min_x + dx, self.min_y + dy, self.min_z + dz,
                    self.max_x + dx, self.max_y + dy, self.max_z + dz)


@dataclass(frozen=True, slots=True)
class BlockGeometry:
    material_key: str
    collision_kind: str
    boxes: tuple[Aabb, ...] = ()
    fluid: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.material_key, "block material key")
        if self.collision_kind not in {"empty", "full_cube", "boxes", "unsupported"}:
            raise ContractViolation("invalid collision kind")
        if type(self.boxes) is not tuple or any(type(box) is not Aabb for box in self.boxes):
            raise ContractViolation("block boxes must be an immutable AABB tuple")
        if self.collision_kind == "boxes":
            if not self.boxes or tuple(box.as_tuple() for box in self.boxes) != tuple(
                    sorted(set(box.as_tuple() for box in self.boxes))):
                raise ContractViolation("explicit collision boxes must be sorted and unique")
            if any(
                box.min_x < -_EPSILON or box.max_x > 1.0 + _EPSILON
                or box.min_z < -_EPSILON or box.max_z > 1.0 + _EPSILON
                or box.min_y < MIN_BLOCK_COLLISION_Y - _EPSILON
                or box.max_y > MAX_BLOCK_COLLISION_Y + _EPSILON
                for box in self.boxes
            ):
                raise ContractViolation(
                    "block collision boxes exceed the supported vertical neighbor extent"
                )
        elif self.boxes:
            raise ContractViolation("compact collision kinds cannot carry boxes")
        if type(self.fluid) is not bool:
            raise ContractViolation("fluid flag must be boolean")

    @classmethod
    def full_cube(cls, material_key: str, *, fluid: bool = False) -> BlockGeometry:
        return cls(material_key, "full_cube", fluid=fluid)

    @classmethod
    def empty(cls, material_key: str, *, fluid: bool = False) -> BlockGeometry:
        return cls(material_key, "empty", fluid=fluid)

    @classmethod
    def unsupported(cls, material_key: str, *, fluid: bool = False) -> BlockGeometry:
        return cls(material_key, "unsupported", fluid=fluid)

    def world_boxes(self, position: BlockPos) -> tuple[Aabb, ...]:
        _position(position)
        x, y, z = position
        if self.collision_kind == "empty":
            return ()
        if self.collision_kind == "unsupported":
            raise ContractViolation("unsupported_block_collision")
        local = (Aabb(0, 0, 0, 1, 1, 1),) if self.collision_kind == "full_cube" else self.boxes
        return tuple(box.moved(x, y, z) for box in local)


class CellKnowledge(StrEnum):
    UNKNOWN = "unknown"
    AIR = "air"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class CellFact:
    knowledge: CellKnowledge
    stamp: ObservationStamp | None = None
    block: BlockGeometry | None = None
    visual_air_evidence: VisualAirEvidence | None = None

    def __post_init__(self) -> None:
        if type(self.knowledge) is not CellKnowledge:
            raise ContractViolation("invalid cell knowledge")
        if self.knowledge is CellKnowledge.UNKNOWN:
            if (self.stamp is not None or self.block is not None
                    or self.visual_air_evidence is not None):
                raise ContractViolation("unknown cell cannot carry a fact")
        elif type(self.stamp) is not ObservationStamp:
            raise ContractViolation("known cell requires an observation stamp")
        elif self.knowledge is CellKnowledge.AIR and self.block is not None:
            raise ContractViolation("known air cannot carry block geometry")
        elif self.knowledge is CellKnowledge.BLOCK and type(self.block) is not BlockGeometry:
            raise ContractViolation("known block requires block geometry")
        if (self.visual_air_evidence is not None
                and self.knowledge is not CellKnowledge.AIR):
            raise ContractViolation("only known air can carry visual evidence")
        if (self.visual_air_evidence is not None
                and self.visual_air_evidence.stamp.session != self.stamp.session):
            raise ContractViolation("visual-air evidence belongs to another session")


UNKNOWN_CELL = CellFact(CellKnowledge.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ImmutableWorldSection:
    """One observed section's persistent storage, shared by borrowed versions."""

    position: SectionPos
    facts: tuple[tuple[BlockPos, CellFact], ...]
    dependency_versions: tuple[tuple[BlockPos, int], ...]

    def __post_init__(self) -> None:
        from mc2p.contracts.motion_activation import validate_frozen_value
        validate_frozen_value(self)
        self._validate_structure()

    @classmethod
    def _from_validated_observation(
        cls,
        position: SectionPos,
        facts: tuple[tuple[BlockPos, CellFact], ...],
        dependency_versions: tuple[tuple[BlockPos, int], ...],
    ) -> ImmutableWorldSection:
        """Build from owner-created facts that already ran their constructors.

        The public constructor and the process boundary still validate the
        complete immutable graph.  The world owner only needs to validate the
        section index because every CellFact entered through _apply after its
        own dataclass constructor had checked the nested values.
        """
        value = object.__new__(cls)
        object.__setattr__(value, "position", position)
        object.__setattr__(value, "facts", facts)
        object.__setattr__(value, "dependency_versions", dependency_versions)
        value._validate_structure()
        return value

    def _validate_structure(self) -> None:
        _section(self.position)
        if type(self.facts) is not tuple or type(self.dependency_versions) is not tuple:
            raise ContractViolation("section facts and versions must be immutable")
        positions = tuple(position for position, _ in self.facts)
        if positions != tuple(sorted(set(positions))):
            raise ContractViolation("section facts must be sorted and unique")
        if positions != tuple(position for position, _ in self.dependency_versions):
            raise ContractViolation("section versions must cover its facts")
        for position, fact in self.facts:
            if block_section(position) != self.position or type(fact) is not CellFact:
                raise ContractViolation("snapshot cell does not belong to its declared section")
        for _, number in self.dependency_versions:
            require_nonnegative_int(number, "semantic cell version")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class SnapshotSourceProvenance:
    """Owner-issued witness to the original metadata and section references.

    The owner's weak issuance index authenticates object identity, not a token
    supplied by the caller. It does not extend the lifetime of these facts.
    """

    token: str
    metadata: tuple[object, ...]
    section_references: tuple[ImmutableWorldSection, ...]


def _snapshot_observation(method):
    """Apply facts and invalidate derived snapshot state under the owner lock.

    Observation never constructs an immutable worker section. Geometry
    changes retire the current cached reference immediately; evidence-only
    refreshes leave the old immutable reference available to old borrowers.
    A later borrow materializes only the dirty sections in its own bounds.
    """
    @wraps(method)
    def apply(self, *args, **kwargs):
        with self._snapshot_version_lock:
            try:
                return method(self, *args, **kwargs)
            finally:
                changed_sections = {
                    block_section(position)
                    for position in self._snapshot_changed_positions
                }
                self._retire_changed_snapshot_sections(changed_sections)
                self._invalidate_changed_snapshots()
    return apply


class WorldUpdateStatus(StrEnum):
    APPLIED = "applied"
    PARTIAL = "partial"
    CAPACITY_EXHAUSTED = "capacity_exhausted"


@dataclass(frozen=True, slots=True)
class WorldUpdateResult:
    status: WorldUpdateStatus
    applied_count: int
    unchanged_count: int
    rejected_positions: tuple[BlockPos, ...] = ()
    evicted_positions: tuple[BlockPos, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not WorldUpdateStatus:
            raise ContractViolation("world update status must be typed")
        require_nonnegative_int(self.applied_count, "world applied count")
        require_nonnegative_int(self.unchanged_count, "world unchanged count")
        for values, name in (
            (self.rejected_positions, "rejected positions"),
            (self.evicted_positions, "evicted positions"),
        ):
            if type(values) is not tuple:
                raise ContractViolation(f"world {name} must be immutable")
            for position in values:
                _position(position)
        if self.rejected_positions != tuple(sorted(set(self.rejected_positions))):
            raise ContractViolation("world rejected positions must be sorted and unique")
        if self.evicted_positions != tuple(sorted(set(self.evicted_positions))):
            raise ContractViolation("world evicted positions must be sorted and unique")


@dataclass(frozen=True, slots=True)
class WorldView:
    session: WorldSessionId
    geometry_revision: int
    evidence_revision: int
    _owner: WorldKnowledge | None
    _snapshot_facts: dict[BlockPos, CellFact] | None = None
    _section_geometry_revisions: dict[SectionPos, int] | None = None
    _section_evidence_revisions: dict[SectionPos, int] | None = None
    _sealed_snapshot: object | None = None

    def __post_init__(self) -> None:
        if sum(source is not None for source in (
                self._owner, self._snapshot_facts, self._sealed_snapshot)) != 1:
            raise ContractViolation("world view requires exactly one fact source")

    @classmethod
    def from_sealed_snapshot(cls, snapshot) -> WorldView:
        from mc2p.motion_nav.world_snapshot import SealedSnapshot
        if type(snapshot) is not SealedSnapshot:
            raise ContractViolation("world view requires a sealed snapshot")
        identity = snapshot.identity
        return cls(WorldSessionId(identity.world_session_id), identity.geometry_revision,
                   identity.evidence_revision, None, _sealed_snapshot=snapshot)

    @classmethod
    def detached(cls, session: WorldSessionId, geometry_revision: int,
                 evidence_revision: int,
                 facts: Mapping[BlockPos, CellFact]) -> WorldView:
        if type(session) is not WorldSessionId or not isinstance(facts, Mapping):
            raise ContractViolation("detached world view requires a session and facts")
        copied: dict[BlockPos, CellFact] = {}
        for position, fact in facts.items():
            _position(position)
            if type(fact) is not CellFact:
                raise ContractViolation("detached world fact is invalid")
            if fact.knowledge is not CellKnowledge.UNKNOWN:
                copied[position] = fact
        return cls(session, geometry_revision, evidence_revision, None, copied)

    def cell(self, position: BlockPos) -> CellFact:
        _position(position)
        return self._cell_at_valid_position(position)

    def changes_since(self, revision: int) -> tuple[BlockPos, ...] | None:
        """None means the bounded semantic change history is incomplete."""
        if revision == self.geometry_revision:
            return ()
        if self._owner is None:
            return None
        return self._owner.changes_since(revision, through=self.geometry_revision)

    def _cell_at_valid_position(self, position: BlockPos) -> CellFact:
        """Read one already validated position for a per-decision query cache."""
        if self._sealed_snapshot is not None:
            return self._sealed_snapshot.cell(position)
        if self._owner is not None:
            section = _block_section_from_valid_position(position)
            geometry = 0 if self._section_geometry_revisions is None \
                else self._section_geometry_revisions.get(section, 0)
            evidence = 0 if self._section_evidence_revisions is None \
                else self._section_evidence_revisions.get(section, 0)
            if (self._owner.section_geometry_revision(section) != geometry
                    or self._owner.section_evidence_revision(section) != evidence):
                raise ContractViolation("world view expired after section changed")
            facts = self._owner._sections.get(section)
            return UNKNOWN_CELL if facts is None else facts.get(position, UNKNOWN_CELL)
        assert self._snapshot_facts is not None
        return self._snapshot_facts.get(position, UNKNOWN_CELL)

    def geometry_revisions(
        self, sections: Iterable[SectionPos],
    ) -> tuple[tuple[SectionPos, int], ...]:
        values = []
        for section in sorted(set(sections)):
            _section(section)
            revision = 0 if self._section_geometry_revisions is None \
                else self._section_geometry_revisions.get(section, 0)
            values.append((section, revision))
        return tuple(values)

    def validate_sections(self, sections: Iterable[SectionPos]) -> None:
        """Reject a live snapshot after any requested section changes.

        Batch geometry users call this once before reusing facts derived from
        several cells. Detached snapshots are immutable and need no check.
        """
        if self._owner is None:
            return
        for section in set(sections):
            _section(section)
            geometry = 0 if self._section_geometry_revisions is None \
                else self._section_geometry_revisions.get(section, 0)
            evidence = 0 if self._section_evidence_revisions is None \
                else self._section_evidence_revisions.get(section, 0)
            if (self._owner.section_geometry_revision(section) != geometry
                    or self._owner.section_evidence_revision(section) != evidence):
                raise ContractViolation("world view expired after section changed")

    def set_protection(
        self,
        center: tuple[float, float, float],
        dependencies: tuple[BlockPos, ...],
    ) -> None:
        if self._owner is None:
            raise ContractViolation("detached world view cannot change protection")
        self._owner.set_protection(center, dependencies)


class WorldQueryCache:
    """Reuse immutable facts and collision boxes during one control decision."""

    def __init__(self, world: WorldView) -> None:
        if type(world) is not WorldView:
            raise ContractViolation("world query cache requires a world view")
        self.world = world
        self._facts: dict[BlockPos, CellFact] = {}
        self._boxes: dict[BlockPos, tuple[Aabb, ...]] = {}

    def cell(self, position: BlockPos) -> CellFact:
        cached = self._facts.get(position)
        if cached is not None:
            return cached
        _position(position)
        fact = self.world._cell_at_valid_position(position)
        self._facts[position] = fact
        return fact

    def collision_boxes(self, position: BlockPos) -> tuple[Aabb, ...]:
        cached = self._boxes.get(position)
        if cached is not None:
            return cached
        fact = self.cell(position)
        boxes = () if fact.block is None else fact.block.world_boxes(position)
        self._boxes[position] = boxes
        return boxes

    @property
    def touched_cells(self) -> tuple[BlockPos, ...]:
        return tuple(sorted(self._facts))

    def validate_for(self, world: WorldView) -> None:
        if world is not self.world:
            raise ContractViolation("query cache belongs to another world view")
        world.validate_sections({_block_section_from_valid_position(p) for p in self._facts})


class WorldKnowledge:
    """Single owner of semantic cell facts for one world session."""

    def __init__(
        self,
        session: WorldSessionId,
        *,
        max_known_cells: int = DEFAULT_MAX_KNOWN_CELLS,
        protection_radius_blocks: float = DEFAULT_PROTECTION_RADIUS_BLOCKS,
        max_snapshot_versions: int = 8,
        max_snapshot_leases: int = DEFAULT_MAX_SNAPSHOT_LEASES,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        max_snapshot_cells: int = DEFAULT_MAX_SNAPSHOT_CELLS,
    ) -> None:
        if type(session) is not WorldSessionId:
            raise ContractViolation("world knowledge requires a session")
        if type(max_known_cells) is not int or max_known_cells < 1:
            raise ContractViolation("world knowledge capacity must be positive")
        _finite(protection_radius_blocks, "world protection radius")
        if protection_radius_blocks < 0:
            raise ContractViolation("world protection radius must be nonnegative")
        for capacity in (max_snapshot_versions, max_snapshot_leases,
                         max_snapshot_bytes, max_snapshot_cells):
            if type(capacity) is not int or capacity < 1:
                raise ContractViolation("snapshot capacities must be positive integers")
        if (max_snapshot_leases > DEFAULT_MAX_SNAPSHOT_LEASES
                or max_snapshot_cells > DEFAULT_MAX_SNAPSHOT_CELLS
                or max_snapshot_bytes > DEFAULT_MAX_SNAPSHOT_BYTES):
            raise ContractViolation("snapshot capacities exceed the frozen D102 limits")
        self.session = session
        self._max_known_cells = max_known_cells
        self._max_tombstones = max_known_cells
        self._protection_radius_blocks = float(protection_radius_blocks)
        self._sections: dict[SectionPos, dict[BlockPos, CellFact]] = {}
        self._known_cell_count = 0
        self._invalidated_at: dict[BlockPos, ObservationStamp] = {}
        self._geometry_revision = 0
        self._geometry_changes: deque[tuple[int, BlockPos]] = deque()
        self._changes_complete_after = 0
        self._evidence_revision = 0
        self._section_geometry_revisions: dict[SectionPos, int] = {}
        self._section_evidence_revisions: dict[SectionPos, int] = {}
        self._section_last_order: dict[SectionPos, tuple[int, int]] = {}
        self._protection_center: tuple[float, float, float] | None = None
        self._protected_dependency_sections: frozenset[SectionPos] = frozenset()
        self._snapshot_version_lock = threading.RLock()
        self._snapshot_publication_lock = threading.Lock()
        self._snapshot_sections: dict[SectionPos, ImmutableWorldSection] = {}
        self._snapshot_dirty_sections: set[SectionPos] = set()
        self._snapshot_dirty_positions: dict[SectionPos, set[BlockPos]] = {}
        self._snapshot_changed_positions: set[BlockPos] = set()
        self._cell_semantic_versions: dict[BlockPos, int] = {}
        self._snapshot_epoch = uuid.uuid4().int << 64
        self._snapshot_local_revision = 0
        self._snapshot_lease_sequence = 0
        self._max_snapshot_versions = max_snapshot_versions
        self._max_snapshot_leases = max_snapshot_leases
        self._max_snapshot_bytes = max_snapshot_bytes
        self._max_snapshot_cells = max_snapshot_cells
        self._published_snapshots: dict[int, SealedSnapshot] = {}
        self._snapshot_leases: dict[str, SnapshotLease] = {}
        self._snapshot_reference_counts: dict[int, int] = {}
        self._ready_snapshot_revisions: set[int] = set()
        self._published_snapshot_bytes = 0
        self._snapshot_sources: WeakValueDictionary[str, SnapshotSourceProvenance] = WeakValueDictionary()

    @property
    def known_cell_count(self) -> int:
        return self._known_cell_count

    def changes_since(self, revision: int, *, through: int | None = None) -> tuple[BlockPos, ...] | None:
        require_nonnegative_int(revision, "world change cursor")
        if revision < self._changes_complete_after:
            return None
        end = self._geometry_revision if through is None else through
        return tuple(sorted({position for number, position in self._geometry_changes
                             if revision < number <= end}))

    def _record_geometry_changes(self, positions: Iterable[BlockPos]) -> None:
        for position in positions:
            if len(self._geometry_changes) >= 4096:
                revision, old_position = self._geometry_changes.popleft()
                self._changes_complete_after = max(self._changes_complete_after, revision)
                if (self._cell_semantic_versions.get(old_position) == revision
                        and self._cell(old_position) is UNKNOWN_CELL):
                    self._cell_semantic_versions.pop(old_position, None)
            self._geometry_changes.append((self._geometry_revision, position))
            self._cell_semantic_versions[position] = self._geometry_revision
            section = block_section(position)
            self._snapshot_dirty_sections.add(section)
            self._snapshot_dirty_positions.setdefault(section, set()).add(position)
            self._snapshot_changed_positions.add(position)

    @property
    def section_count(self) -> int:
        return len(self._sections)

    @property
    def tombstone_count(self) -> int:
        return len(self._invalidated_at)

    @property
    def metadata_section_count(self) -> int:
        """Number of sections retained by facts, revisions, or tombstones."""
        return len(
            set(self._sections)
            | set(self._section_last_order)
            | set(self._section_geometry_revisions)
            | set(self._section_evidence_revisions)
        )

    def section_geometry_revision(self, section: SectionPos) -> int:
        _section(section)
        return self._section_geometry_revisions.get(section, 0)

    def section_evidence_revision(self, section: SectionPos) -> int:
        _section(section)
        return self._section_evidence_revisions.get(section, 0)

    def set_protection(
        self,
        center: tuple[float, float, float],
        dependencies: tuple[BlockPos, ...],
    ) -> None:
        self.set_protection_center(center)
        self.set_protected_dependencies(dependencies)

    def set_protection_center(self, center: tuple[float, float, float]) -> None:
        if (type(center) is not tuple or len(center) != 3
                or any(type(value) not in (int, float)
                       or not math.isfinite(float(value)) for value in center)):
            raise ContractViolation("world protection center must be a finite triple")
        self._protection_center = tuple(float(value) for value in center)

    def set_protected_dependencies(
        self, dependencies: tuple[BlockPos, ...],
    ) -> None:
        if type(dependencies) is not tuple:
            raise ContractViolation("world protection dependencies must be immutable")
        for position in dependencies:
            _position(position)
        self._protected_dependency_sections = frozenset(
            block_section(position) for position in dependencies
        )

    def _stamp(self, value: ObservationStamp) -> None:
        if type(value) is not ObservationStamp or value.session != self.session:
            raise ContractViolation("world fact belongs to another session")

    def observe_blocks(self, stamp: ObservationStamp,
                       blocks: Mapping[BlockPos, BlockGeometry]) -> WorldUpdateResult:
        result, _ = self.observe_blocks_with_changes(stamp, blocks)
        return result

    @_snapshot_observation
    def observe_blocks_with_changes(
        self,
        stamp: ObservationStamp,
        blocks: Mapping[BlockPos, BlockGeometry],
    ) -> tuple[WorldUpdateResult, tuple[BlockPos, ...]]:
        """Apply one block batch and report cells whose geometry changed."""
        self._stamp(stamp)
        if not isinstance(blocks, Mapping):
            raise ContractViolation("visible blocks must be a mapping")
        results = []
        changed = []
        evicted: set[BlockPos] = set()
        for position in sorted(blocks):
            _position(position)
            section_position = block_section(position)
            geometry = blocks[position]
            if type(geometry) is not BlockGeometry:
                raise ContractViolation("visible block requires geometry")
            section = self._sections.get(section_position)
            existing = (
                UNKNOWN_CELL if section is None
                else section.get(position, UNKNOWN_CELL)
            )
            semantic_changed = (
                existing is UNKNOWN_CELL
                or existing.knowledge is not CellKnowledge.BLOCK
                or (existing.block is not geometry and existing.block != geometry)
            )
            outcome, removed = self._apply(
                position, CellFact(CellKnowledge.BLOCK, stamp, geometry),
                existing=existing, section_position=section_position,
            )
            results.append((position, outcome))
            if outcome == "applied" and semantic_changed:
                changed.append(position)
            evicted.update(removed)
        return self._update_result(results, evicted), tuple(changed)

    @_snapshot_observation
    def confirm_air(
        self, stamp: ObservationStamp, positions: tuple[BlockPos, ...],
        visual_evidence: Mapping[BlockPos, VisualAirEvidence] | None = None,
    ) -> WorldUpdateResult:
        self._stamp(stamp)
        if type(positions) is not tuple:
            raise ContractViolation("confirmed air must be an immutable position tuple")
        if visual_evidence is None:
            visual_evidence = {}
        if not isinstance(visual_evidence, Mapping):
            raise ContractViolation("visual-air evidence must be a mapping")
        for position, evidence in visual_evidence.items():
            _position(position)
            if (position not in positions
                    or type(evidence) is not VisualAirEvidence
                    or evidence.stamp.session != self.session):
                raise ContractViolation("visual-air evidence does not match confirmed air")
        results = []
        evicted: set[BlockPos] = set()
        for position in sorted(set(positions)):
            _position(position)
            evidence = visual_evidence.get(position)
            if evidence is None:
                existing = self._cell(position)
                if (existing.knowledge is CellKnowledge.AIR
                        and existing.visual_air_evidence is not None):
                    evidence = existing.visual_air_evidence
            outcome, removed = self._apply(
                position, CellFact(
                    CellKnowledge.AIR, stamp,
                    visual_air_evidence=evidence,
                ),
            )
            results.append((position, outcome))
            evicted.update(removed)
        return self._update_result(results, evicted)

    @staticmethod
    def _update_result(
        results: list[tuple[BlockPos, str]], evicted: set[BlockPos],
    ) -> WorldUpdateResult:
        applied = sum(outcome == "applied" for _, outcome in results)
        unchanged = sum(outcome == "unchanged" for _, outcome in results)
        rejected = tuple(sorted(
            position for position, outcome in results if outcome == "rejected"
        ))
        status = (
            WorldUpdateStatus.APPLIED if not rejected
            else WorldUpdateStatus.PARTIAL if applied or unchanged
            else WorldUpdateStatus.CAPACITY_EXHAUSTED
        )
        return WorldUpdateResult(
            status, applied, unchanged, rejected, tuple(sorted(evicted)),
        )

    def _cell(self, position: BlockPos) -> CellFact:
        section = self._sections.get(block_section(position))
        return UNKNOWN_CELL if section is None else section.get(position, UNKNOWN_CELL)

    def _apply(
        self,
        position: BlockPos,
        incoming: CellFact,
        *,
        existing: CellFact | None = None,
        section_position: SectionPos | None = None,
    ) -> tuple[str, tuple[BlockPos, ...]]:
        assert incoming.stamp is not None
        if section_position is None:
            section_position = block_section(position)
        if existing is None:
            existing = self._cell(position)
        invalidated = self._invalidated_at.get(position)
        if invalidated is not None:
            if incoming.stamp.causal_order <= invalidated.causal_order:
                return "unchanged", ()
            del self._invalidated_at[position]
        if existing is UNKNOWN_CELL:
            existing = None
        if existing is not None:
            assert existing.stamp is not None
            if incoming.stamp.causal_order < existing.stamp.causal_order:
                return "unchanged", ()
            if (incoming.stamp.causal_order == existing.stamp.causal_order
                    and existing.knowledge is CellKnowledge.BLOCK
                    and incoming.knowledge is CellKnowledge.AIR):
                return "unchanged", ()
            if incoming.stamp.causal_order == existing.stamp.causal_order and existing == incoming:
                return "unchanged", ()
        evicted: tuple[BlockPos, ...] = ()
        if existing is None and self._known_cell_count >= self._max_known_cells:
            evicted = self._evict_one_section()
            if not evicted:
                return "rejected", ()
        semantic_changed = (
            existing is None
            or existing.knowledge is not incoming.knowledge
            or (existing.block is not incoming.block
                and existing.block != incoming.block)
        )
        section = self._sections.setdefault(section_position, {})
        section[position] = incoming
        if existing is None:
            self._known_cell_count += 1
        previous_order = self._section_last_order.get(section_position)
        if previous_order is None or incoming.stamp.causal_order > previous_order:
            self._section_last_order[section_position] = incoming.stamp.causal_order
        self._evidence_revision += 1
        self._section_evidence_revisions[section_position] = self._evidence_revision
        self._snapshot_dirty_sections.add(section_position)
        self._snapshot_dirty_positions.setdefault(section_position, set()).add(position)
        if semantic_changed:
            self._geometry_revision += 1
            self._record_geometry_changes((position,))
            self._section_geometry_revisions[section_position] = self._geometry_revision
        return "applied", evicted

    def _section_is_protected(self, section: SectionPos) -> bool:
        if section in self._protected_dependency_sections:
            return True
        if self._protection_center is None:
            return False
        squared = 0.0
        for section_part, center_part in zip(section, self._protection_center):
            minimum = section_part * WORLD_SECTION_SIZE
            maximum = minimum + WORLD_SECTION_SIZE
            if center_part < minimum:
                squared += (minimum - center_part) ** 2
            elif center_part > maximum:
                squared += (center_part - maximum) ** 2
        return squared <= self._protection_radius_blocks ** 2

    def _evict_one_section(self) -> tuple[BlockPos, ...]:
        candidates = [
            section for section in self._sections
            if not self._section_is_protected(section)
        ]
        if not candidates:
            return ()
        selected = min(
            candidates,
            key=lambda section: (self._section_last_order.get(section, (-1, -1)), section),
        )
        removed = tuple(sorted(self._sections.pop(selected)))
        self._known_cell_count -= len(removed)
        for position in tuple(self._invalidated_at):
            if block_section(position) == selected:
                del self._invalidated_at[position]
        self._section_last_order.pop(selected, None)
        self._geometry_revision += 1
        self._record_geometry_changes(removed)
        self._evidence_revision += 1
        # A removed section returns to revision zero.  A future observation
        # receives the newer global revision, so old live views cannot become
        # accidentally valid again while per-section metadata remains bounded.
        self._section_geometry_revisions.pop(selected, None)
        self._section_evidence_revisions.pop(selected, None)
        return removed

    @_snapshot_observation
    def invalidate(self, stamp: ObservationStamp, positions: tuple[BlockPos, ...]) -> None:
        """Apply a world-change tombstone so delayed facts cannot restore old geometry."""
        self._stamp(stamp)
        if type(positions) is not tuple:
            raise ContractViolation("world invalidation must be an immutable position tuple")
        for position in sorted(set(positions)):
            _position(position)
            tombstone = self._invalidated_at.get(position)
            if tombstone is not None and tombstone.causal_order >= stamp.causal_order:
                continue
            existing = self._cell(position)
            if existing is UNKNOWN_CELL:
                existing = None
            if (existing is not None and existing.stamp is not None
                    and existing.stamp.causal_order > stamp.causal_order):
                continue
            self._invalidated_at[position] = stamp
            section_position = block_section(position)
            previous_order = self._section_last_order.get(section_position)
            if previous_order is None or stamp.causal_order > previous_order:
                self._section_last_order[section_position] = stamp.causal_order
            if existing is not None:
                section = self._sections[section_position]
                del section[position]
                self._known_cell_count -= 1
                if not section:
                    del self._sections[section_position]
                    self._section_last_order.pop(section_position, None)
                self._geometry_revision += 1
                self._record_geometry_changes((position,))
                self._section_geometry_revisions[section_position] = \
                    self._geometry_revision
            self._evidence_revision += 1
            self._section_evidence_revisions[section_position] = \
                self._evidence_revision
        self._prune_tombstones()

    def _commit_snapshot_sections(
        self, sections: set[SectionPos],
    ) -> None:
        if type(sections) is not set:
            raise ContractViolation("snapshot materialization requires an explicit section set")
        for section in sections:
            _section(section)
        selected = self._snapshot_dirty_sections.intersection(sections)
        for position in selected:
            cells = self._sections.get(position)
            if not cells:
                self._snapshot_sections.pop(position, None)
                self._snapshot_dirty_positions.pop(position, None)
                continue
            previous = self._snapshot_sections.get(position)
            dirty = self._snapshot_dirty_positions.get(position, set())
            if (previous is not None and len(previous.facts) == len(cells)
                    and all(cell in cells for cell, _ in previous.facts)):
                # Evidence refreshes preserve cell membership and semantic
                # versions. Reuse every unchanged pair and the complete
                # dependency tuple, so a one-cell stamp refresh does not
                # allocate thousands of short-lived tracked tuples.
                facts = tuple(
                    (pair[0], cells[pair[0]]) if pair[0] in dirty else pair
                    for pair in previous.facts
                )
                versions = previous.dependency_versions
            else:
                facts = tuple(sorted(cells.items()))
                versions = tuple(
                    (cell, self._snapshot_epoch + self._cell_semantic_versions[cell])
                    for cell, _ in facts
                )
            self._snapshot_sections[position] = \
                ImmutableWorldSection._from_validated_observation(position, facts, versions)
        self._snapshot_dirty_sections.difference_update(selected)
        for position in selected:
            self._snapshot_dirty_positions.pop(position, None)

    def _retire_changed_snapshot_sections(
        self, sections: set[SectionPos],
    ) -> None:
        """Retire stale cache entries without materializing replacements."""
        for section in sections:
            self._snapshot_sections.pop(section, None)
            # Once the immutable base is gone, the next borrow performs a
            # complete section build. Per-cell evidence deltas are no longer
            # useful and must not accumulate removed positions.
            self._snapshot_dirty_positions.pop(section, None)
            if self._sections.get(section):
                self._snapshot_dirty_sections.add(section)
            else:
                # Empty and evicted sections have no immutable payload to
                # build. Keeping their marker would make the pending set grow
                # independently of the live-world capacity.
                self._snapshot_dirty_sections.discard(section)

    def _invalidate_changed_snapshots(self) -> None:
        if self._snapshot_changed_positions:
            with self._snapshot_publication_lock:
                for revision in tuple(self._ready_snapshot_revisions):
                    bounds = self._published_snapshots[revision].identity.bounds
                    if any(bounds.min_x <= x < bounds.max_x and bounds.min_y <= y < bounds.max_y
                           and bounds.min_z <= z < bounds.max_z
                           for x, y, z in self._snapshot_changed_positions):
                        self._ready_snapshot_revisions.discard(revision)
            self._snapshot_changed_positions.clear()

    def dependency_versions(self, positions: tuple[BlockPos, ...]) -> DependencyVersions:
        if type(positions) is not tuple:
            raise ContractViolation("dependency positions must be immutable")
        for position in positions:
            _position(position)
        with self._snapshot_version_lock:
            return tuple((position, self._snapshot_epoch + self._cell_semantic_versions.get(
                position, self._geometry_revision)) for position in sorted(set(positions)))

    def dependency_changes(self, versions: DependencyVersions) -> DependencyCheck:
        from mc2p.motion_nav.world_snapshot import DependencyCheck, DependencyStatus
        if type(versions) is not tuple:
            raise ContractViolation("dependency versions must be immutable")
        changed = []
        incomplete = False
        with self._snapshot_version_lock:
            positions = tuple(position for position, _ in versions)
            if positions != tuple(sorted(set(positions))):
                raise ContractViolation("dependency positions must be sorted and unique")
            for position, version in versions:
                _position(position)
                require_nonnegative_int(version, "semantic cell version")
                revision = version - self._snapshot_epoch
                if not 0 <= revision <= self._geometry_revision:
                    changed.append(position)
                    continue
                current = self._cell_semantic_versions.get(position)
                if current is not None:
                    if current != revision:
                        changed.append(position)
                elif revision < self._changes_complete_after:
                    incomplete = True
                elif any(number > revision and cell == position
                         for number, cell in self._geometry_changes):
                    changed.append(position)
        status = (DependencyStatus.CHANGED if changed else
                  DependencyStatus.HISTORY_INCOMPLETE if incomplete else DependencyStatus.UNCHANGED)
        return DependencyCheck(status, tuple(changed))

    def borrow_snapshot_version(self, bounds: PhysicsWorldBounds, *,
                                ruleset: PhysicsRuleset) -> WorldSnapshotVersion | None:
        from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
        from mc2p.motion_nav.physics_types import PhysicsRuleset
        from mc2p.motion_nav.prepared_motion import WorldSnapshotVersion
        if type(bounds) is not PhysicsWorldBounds or type(ruleset) is not PhysicsRuleset:
            raise ContractViolation("snapshot borrowing requires bounds and ruleset")
        size = ((bounds.max_x - bounds.min_x + 1) * (bounds.max_y - bounds.min_y + 1)
                * (bounds.max_z - bounds.min_z + 1))
        if size > self._max_snapshot_cells or not self._snapshot_version_lock.acquire(False):
            return None
        try:
            requested_sections = {
                (x, y, z)
                for x in range(bounds.min_x // WORLD_SECTION_SIZE,
                               bounds.max_x // WORLD_SECTION_SIZE + 1)
                for y in range(bounds.min_y // WORLD_SECTION_SIZE,
                               bounds.max_y // WORLD_SECTION_SIZE + 1)
                for z in range(bounds.min_z // WORLD_SECTION_SIZE,
                               bounds.max_z // WORLD_SECTION_SIZE + 1)
            }
            # Build current immutable payloads only for this request. Dirty
            # sections elsewhere remain pending for their own future borrow.
            self._commit_snapshot_sections(requested_sections)
            references = tuple(
                self._snapshot_sections[position]
                for position in sorted(requested_sections)
                if position in self._snapshot_sections
            )
            self._snapshot_local_revision += 1
            metadata = (self.session.value, self._geometry_revision, self._evidence_revision,
                        self._snapshot_epoch + self._snapshot_local_revision,
                        Aabb(bounds.min_x, bounds.min_y, bounds.min_z,
                             bounds.max_x + 1, bounds.max_y + 1, bounds.max_z + 1), ruleset)
            source = SnapshotSourceProvenance(uuid.uuid4().hex, metadata, references)
            self._snapshot_sources[source.token] = source
            return WorldSnapshotVersion(*metadata, references, source)
        finally:
            self._snapshot_version_lock.release()

    def publish_sealed_snapshot(self, version: WorldSnapshotVersion, sealed: SealedSnapshot) -> bool:
        from mc2p.motion_nav.prepared_motion import WorldSnapshotVersion
        from mc2p.contracts.motion_activation import validate_frozen_value
        from mc2p.motion_nav.world_snapshot import (
            SealedSnapshot, snapshot_version_content, validate_sealed_snapshot,
        )
        if type(version) is not WorldSnapshotVersion or type(sealed) is not SealedSnapshot:
            raise ContractViolation("publication requires a borrowed version and sealed snapshot")
        try:
            if type(sealed.canonical_bytes) is not bytes:
                return False
            size = len(sealed.canonical_bytes)
            if size > self._max_snapshot_bytes:
                return False
            validate_frozen_value(version)
            source = version.source_provenance
            metadata = (version.world_session_id, version.geometry_revision, version.evidence_revision,
                        version.local_revision, version.bounds, version.ruleset)
            with self._snapshot_version_lock:
                if (source is None or self._snapshot_sources.get(source.token) is not source
                        or metadata != source.metadata
                        or len(version.section_references) != len(source.section_references)
                        or any(candidate is not original for candidate, original in zip(
                            version.section_references, source.section_references))
                        or version.world_session_id != self.session.value):
                    return False
            # Pickle bypasses dataclass construction. Repeat the full immutable
            # graph, canonical-byte and digest checks at this admission boundary.
            validate_sealed_snapshot(sealed)
            identity = sealed.identity
            sealed_metadata = (identity.world_session_id, identity.geometry_revision,
                               identity.evidence_revision, identity.local_revision,
                               identity.bounds, identity.ruleset)
            if (sealed_metadata != source.metadata
                    or (sealed.facts, sealed.dependency_versions) != snapshot_version_content(version)):
                return False
        except (ContractViolation, AttributeError, TypeError, ValueError):
            return False
        # The expensive content comparison above uses only immutable references.
        # Locks below protect bounded metadata and reference publication only.
        with self._snapshot_version_lock:
            changes = self.changes_since(version.geometry_revision)
            region = identity.bounds
            current = changes is not None and not any(
                region.min_x <= x < region.max_x and region.min_y <= y < region.max_y
                and region.min_z <= z < region.max_z for x, y, z in changes)
            with self._snapshot_publication_lock:
                if identity.local_revision in self._published_snapshots:
                    return self._published_snapshots[identity.local_revision].identity == identity
                # Only unleased storage may be reclaimed. Never revoke another lease.
                reclaimable = [revision for revision in self._published_snapshots
                               if self._snapshot_reference_counts.get(revision, 0) == 0]
                while (len(self._published_snapshots) >= self._max_snapshot_versions
                       or self._published_snapshot_bytes + size > self._max_snapshot_bytes):
                    if not reclaimable:
                        return False
                    old = reclaimable.pop(0)
                    removed = self._published_snapshots.pop(old)
                    self._ready_snapshot_revisions.discard(old)
                    self._published_snapshot_bytes -= len(removed.canonical_bytes)
                self._published_snapshots[identity.local_revision] = sealed
                if current:
                    self._ready_snapshot_revisions.add(identity.local_revision)
                self._published_snapshot_bytes += size
                return True

    def acquire_published_snapshot(self, bounds: PhysicsWorldBounds, *,
                                   ruleset: PhysicsRuleset) -> SnapshotLeaseResult:
        from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
        from mc2p.motion_nav.physics_types import PhysicsRuleset
        from mc2p.motion_nav.prepared_motion import SnapshotLease, SnapshotLeaseResult, SnapshotLeaseStatus
        if type(bounds) is not PhysicsWorldBounds or type(ruleset) is not PhysicsRuleset:
            raise ContractViolation("snapshot acquisition requires bounds and ruleset")
        not_ready = SnapshotLeaseResult(SnapshotLeaseStatus.NOT_READY)
        if not self._snapshot_version_lock.acquire(False):
            return not_ready
        if not self._snapshot_publication_lock.acquire(False):
            self._snapshot_version_lock.release()
            return not_ready
        try:
            for revision, snapshot in reversed(self._published_snapshots.items()):
                if revision not in self._ready_snapshot_revisions:
                    continue
                identity = snapshot.identity
                region = identity.bounds
                if (identity.ruleset != ruleset
                        or not (region.min_x <= bounds.min_x and bounds.max_x < region.max_x
                                and region.min_y <= bounds.min_y and bounds.max_y < region.max_y
                                and region.min_z <= bounds.min_z and bounds.max_z < region.max_z)):
                    continue
                if len(self._snapshot_leases) >= self._max_snapshot_leases:
                    return SnapshotLeaseResult(SnapshotLeaseStatus.CAPACITY)
                self._snapshot_lease_sequence += 1
                token = f"snapshot-{self._snapshot_epoch:x}-{self._snapshot_lease_sequence}"
                lease = SnapshotLease(token, snapshot)
                self._snapshot_leases[token] = lease
                self._snapshot_reference_counts[identity.local_revision] = \
                    self._snapshot_reference_counts.get(identity.local_revision, 0) + 1
                return SnapshotLeaseResult(SnapshotLeaseStatus.READY, lease)
            return not_ready
        finally:
            self._snapshot_publication_lock.release()
            self._snapshot_version_lock.release()

    def snapshot_lease_is_live(self, lease) -> bool:
        """Read only exact issued-lease membership; never renew the lease."""
        from mc2p.motion_nav.prepared_motion import SnapshotLease
        if type(lease) is not SnapshotLease or not self._snapshot_publication_lock.acquire(False):
            return False
        try:
            return (self._snapshot_leases.get(lease.lease_token) is lease
                    and self._published_snapshots.get(lease.identity.local_revision) is lease.snapshot)
        finally:
            self._snapshot_publication_lock.release()

    def release_snapshot(self, lease_token: str, *, expected_snapshot=None, expected_lease=None) -> bool:
        require_identifier(lease_token, "snapshot lease token")
        with self._snapshot_publication_lock:
            lease = self._snapshot_leases.get(lease_token)
            if lease is None or (expected_lease is not None and lease is not expected_lease):
                return False
            revision = lease.identity.local_revision
            if (expected_snapshot is not None
                    and self._published_snapshots.get(revision) is not expected_snapshot):
                return False
            del self._snapshot_leases[lease_token]
            remaining = self._snapshot_reference_counts[revision] - 1
            if remaining:
                self._snapshot_reference_counts[revision] = remaining
            else:
                del self._snapshot_reference_counts[revision]
            return True

    def _prune_tombstones(self) -> None:
        while len(self._invalidated_at) > self._max_tombstones:
            candidates = sorted(
                self._invalidated_at,
                key=lambda position: (
                    self._section_is_protected(block_section(position)),
                    self._invalidated_at[position].causal_order,
                    position,
                ),
            )
            selected = candidates[0]
            section = block_section(selected)
            del self._invalidated_at[selected]
            has_fact = section in self._sections
            has_tombstone = any(
                block_section(position) == section
                for position in self._invalidated_at
            )
            if not has_fact and not has_tombstone:
                self._section_last_order.pop(section, None)
                self._section_geometry_revisions.pop(section, None)
                self._section_evidence_revisions.pop(section, None)

    def view(self) -> WorldView:
        return WorldView(
            self.session, self._geometry_revision, self._evidence_revision, self,
            _section_geometry_revisions=dict(self._section_geometry_revisions),
            _section_evidence_revisions=dict(self._section_evidence_revisions),
        )
