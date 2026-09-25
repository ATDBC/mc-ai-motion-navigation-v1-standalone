"""World-session, time and three-state voxel knowledge for the new motion core."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Iterable, Mapping

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int


BlockPos = tuple[int, int, int]
SectionPos = tuple[int, int, int]
WORLD_SECTION_SIZE = 16
DEFAULT_MAX_KNOWN_CELLS = 262_144
DEFAULT_PROTECTION_RADIUS_BLOCKS = 32.0
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
    def world_order(self) -> tuple[int, int]:
        return self.world_tick, self.sequence_id

    def elapsed_seconds(self, earlier: ObservationStamp) -> float:
        return elapsed_seconds(self, earlier)


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

    def __post_init__(self) -> None:
        if type(self.knowledge) is not CellKnowledge:
            raise ContractViolation("invalid cell knowledge")
        if self.knowledge is CellKnowledge.UNKNOWN:
            if self.stamp is not None or self.block is not None:
                raise ContractViolation("unknown cell cannot carry a fact")
        elif type(self.stamp) is not ObservationStamp:
            raise ContractViolation("known cell requires an observation stamp")
        elif self.knowledge is CellKnowledge.AIR and self.block is not None:
            raise ContractViolation("known air cannot carry block geometry")
        elif self.knowledge is CellKnowledge.BLOCK and type(self.block) is not BlockGeometry:
            raise ContractViolation("known block requires block geometry")


UNKNOWN_CELL = CellFact(CellKnowledge.UNKNOWN)


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

    def __post_init__(self) -> None:
        if (self._owner is None) == (self._snapshot_facts is None):
            raise ContractViolation("world view requires exactly one fact source")

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
        if self._owner is not None:
            section = block_section(position)
            geometry = 0 if self._section_geometry_revisions is None \
                else self._section_geometry_revisions.get(section, 0)
            evidence = 0 if self._section_evidence_revisions is None \
                else self._section_evidence_revisions.get(section, 0)
            if (self._owner.section_geometry_revision(section) != geometry
                    or self._owner.section_evidence_revision(section) != evidence):
                raise ContractViolation("world view expired after section changed")
            return self._owner._cell(position)
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
        fact = self.world.cell(position)
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


class WorldKnowledge:
    """Single owner of semantic cell facts for one world session."""

    def __init__(
        self,
        session: WorldSessionId,
        *,
        max_known_cells: int = DEFAULT_MAX_KNOWN_CELLS,
        protection_radius_blocks: float = DEFAULT_PROTECTION_RADIUS_BLOCKS,
    ) -> None:
        if type(session) is not WorldSessionId:
            raise ContractViolation("world knowledge requires a session")
        if type(max_known_cells) is not int or max_known_cells < 1:
            raise ContractViolation("world knowledge capacity must be positive")
        _finite(protection_radius_blocks, "world protection radius")
        if protection_radius_blocks < 0:
            raise ContractViolation("world protection radius must be nonnegative")
        self.session = session
        self._max_known_cells = max_known_cells
        self._max_tombstones = max_known_cells
        self._protection_radius_blocks = float(protection_radius_blocks)
        self._sections: dict[SectionPos, dict[BlockPos, CellFact]] = {}
        self._known_cell_count = 0
        self._invalidated_at: dict[BlockPos, ObservationStamp] = {}
        self._geometry_revision = 0
        self._evidence_revision = 0
        self._section_geometry_revisions: dict[SectionPos, int] = {}
        self._section_evidence_revisions: dict[SectionPos, int] = {}
        self._section_last_order: dict[SectionPos, tuple[int, int]] = {}
        self._protection_center: tuple[float, float, float] | None = None
        self._protected_dependency_sections: frozenset[SectionPos] = frozenset()

    @property
    def known_cell_count(self) -> int:
        return self._known_cell_count

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
        self._stamp(stamp)
        if not isinstance(blocks, Mapping):
            raise ContractViolation("visible blocks must be a mapping")
        results = []
        evicted: set[BlockPos] = set()
        for position in sorted(blocks):
            _position(position)
            geometry = blocks[position]
            if type(geometry) is not BlockGeometry:
                raise ContractViolation("visible block requires geometry")
            outcome, removed = self._apply(
                position, CellFact(CellKnowledge.BLOCK, stamp, geometry),
            )
            results.append((position, outcome))
            evicted.update(removed)
        return self._update_result(results, evicted)

    def confirm_air(
        self, stamp: ObservationStamp, positions: tuple[BlockPos, ...],
    ) -> WorldUpdateResult:
        self._stamp(stamp)
        if type(positions) is not tuple:
            raise ContractViolation("confirmed air must be an immutable position tuple")
        results = []
        evicted: set[BlockPos] = set()
        for position in sorted(set(positions)):
            _position(position)
            outcome, removed = self._apply(
                position, CellFact(CellKnowledge.AIR, stamp),
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
        self, position: BlockPos, incoming: CellFact,
    ) -> tuple[str, tuple[BlockPos, ...]]:
        assert incoming.stamp is not None
        invalidated = self._invalidated_at.get(position)
        if invalidated is not None:
            if incoming.stamp.world_order <= invalidated.world_order:
                return "unchanged", ()
            del self._invalidated_at[position]
        existing = self._cell(position)
        if existing is UNKNOWN_CELL:
            existing = None
        if existing is not None:
            assert existing.stamp is not None
            if incoming.stamp.world_order < existing.stamp.world_order:
                return "unchanged", ()
            if (incoming.stamp.world_order == existing.stamp.world_order
                    and existing.knowledge is CellKnowledge.BLOCK
                    and incoming.knowledge is CellKnowledge.AIR):
                return "unchanged", ()
            if incoming.stamp.world_order == existing.stamp.world_order and existing == incoming:
                return "unchanged", ()
        evicted: tuple[BlockPos, ...] = ()
        if existing is None and self._known_cell_count >= self._max_known_cells:
            evicted = self._evict_one_section()
            if not evicted:
                return "rejected", ()
        semantic_changed = existing is None or (existing.knowledge, existing.block) != (
            incoming.knowledge, incoming.block)
        section_position = block_section(position)
        section = self._sections.setdefault(section_position, {})
        section[position] = incoming
        if existing is None:
            self._known_cell_count += 1
        previous_order = self._section_last_order.get(section_position)
        if previous_order is None or incoming.stamp.world_order > previous_order:
            self._section_last_order[section_position] = incoming.stamp.world_order
        self._evidence_revision += 1
        self._section_evidence_revisions[section_position] = self._evidence_revision
        if semantic_changed:
            self._geometry_revision += 1
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
        self._evidence_revision += 1
        # A removed section returns to revision zero.  A future observation
        # receives the newer global revision, so old live views cannot become
        # accidentally valid again while per-section metadata remains bounded.
        self._section_geometry_revisions.pop(selected, None)
        self._section_evidence_revisions.pop(selected, None)
        return removed

    def invalidate(self, stamp: ObservationStamp, positions: tuple[BlockPos, ...]) -> None:
        """Apply a world-change tombstone so delayed facts cannot restore old geometry."""
        self._stamp(stamp)
        if type(positions) is not tuple:
            raise ContractViolation("world invalidation must be an immutable position tuple")
        for position in sorted(set(positions)):
            _position(position)
            tombstone = self._invalidated_at.get(position)
            if tombstone is not None and tombstone.world_order >= stamp.world_order:
                continue
            existing = self._cell(position)
            if existing is UNKNOWN_CELL:
                existing = None
            if (existing is not None and existing.stamp is not None
                    and existing.stamp.world_order > stamp.world_order):
                continue
            self._invalidated_at[position] = stamp
            section_position = block_section(position)
            previous_order = self._section_last_order.get(section_position)
            if previous_order is None or stamp.world_order > previous_order:
                self._section_last_order[section_position] = stamp.world_order
            if existing is not None:
                section = self._sections[section_position]
                del section[position]
                self._known_cell_count -= 1
                if not section:
                    del self._sections[section_position]
                    self._section_last_order.pop(section_position, None)
                self._geometry_revision += 1
                self._section_geometry_revisions[section_position] = \
                    self._geometry_revision
            self._evidence_revision += 1
            self._section_evidence_revisions[section_position] = \
                self._evidence_revision
        self._prune_tombstones()

    def _prune_tombstones(self) -> None:
        while len(self._invalidated_at) > self._max_tombstones:
            candidates = sorted(
                self._invalidated_at,
                key=lambda position: (
                    self._section_is_protected(block_section(position)),
                    self._invalidated_at[position].world_order,
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
