"""World-session, time and three-state voxel knowledge for the new motion core."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Mapping

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int


BlockPos = tuple[int, int, int]
_EPSILON = 1.0e-9
MIN_BLOCK_COLLISION_Y = 0.0
MAX_BLOCK_COLLISION_Y = 2.0
COLLISION_OWNER_BELOW_REACH_CELLS = 1


def _position(value: BlockPos) -> None:
    if type(value) is not tuple or len(value) != 3 or any(type(part) is not int for part in value):
        raise ContractViolation("block position must be an integer triple")


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


@dataclass(frozen=True, slots=True)
class WorldView:
    session: WorldSessionId
    geometry_revision: int
    evidence_revision: int
    _owner: WorldKnowledge | None
    _snapshot_facts: dict[BlockPos, CellFact] | None = None

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
            if (self._owner._geometry_revision != self.geometry_revision
                    or self._owner._evidence_revision != self.evidence_revision):
                raise ContractViolation("world view expired after an update")
            return self._owner._facts.get(position, UNKNOWN_CELL)
        assert self._snapshot_facts is not None
        return self._snapshot_facts.get(position, UNKNOWN_CELL)


class WorldKnowledge:
    """Single owner of semantic cell facts for one world session."""

    def __init__(self, session: WorldSessionId) -> None:
        if type(session) is not WorldSessionId:
            raise ContractViolation("world knowledge requires a session")
        self.session = session
        self._facts: dict[BlockPos, CellFact] = {}
        self._invalidated_at: dict[BlockPos, ObservationStamp] = {}
        self._geometry_revision = 0
        self._evidence_revision = 0

    def _stamp(self, value: ObservationStamp) -> None:
        if type(value) is not ObservationStamp or value.session != self.session:
            raise ContractViolation("world fact belongs to another session")

    def observe_blocks(self, stamp: ObservationStamp,
                       blocks: Mapping[BlockPos, BlockGeometry]) -> None:
        self._stamp(stamp)
        if not isinstance(blocks, Mapping):
            raise ContractViolation("visible blocks must be a mapping")
        for position in sorted(blocks):
            _position(position)
            geometry = blocks[position]
            if type(geometry) is not BlockGeometry:
                raise ContractViolation("visible block requires geometry")
            self._apply(position, CellFact(CellKnowledge.BLOCK, stamp, geometry))

    def confirm_air(self, stamp: ObservationStamp, positions: tuple[BlockPos, ...]) -> None:
        self._stamp(stamp)
        if type(positions) is not tuple:
            raise ContractViolation("confirmed air must be an immutable position tuple")
        for position in sorted(set(positions)):
            _position(position)
            self._apply(position, CellFact(CellKnowledge.AIR, stamp))

    def _apply(self, position: BlockPos, incoming: CellFact) -> None:
        assert incoming.stamp is not None
        invalidated = self._invalidated_at.get(position)
        if invalidated is not None:
            if incoming.stamp.world_order <= invalidated.world_order:
                return
            del self._invalidated_at[position]
        existing = self._facts.get(position)
        if existing is not None:
            assert existing.stamp is not None
            if incoming.stamp.world_order < existing.stamp.world_order:
                return
            if (incoming.stamp.world_order == existing.stamp.world_order
                    and existing.knowledge is CellKnowledge.BLOCK
                    and incoming.knowledge is CellKnowledge.AIR):
                return
            if incoming.stamp.world_order == existing.stamp.world_order and existing == incoming:
                return
        semantic_changed = existing is None or (existing.knowledge, existing.block) != (
            incoming.knowledge, incoming.block)
        self._facts[position] = incoming
        self._evidence_revision += 1
        if semantic_changed:
            self._geometry_revision += 1

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
            existing = self._facts.get(position)
            if (existing is not None and existing.stamp is not None
                    and existing.stamp.world_order > stamp.world_order):
                continue
            self._invalidated_at[position] = stamp
            if existing is not None:
                del self._facts[position]
                self._geometry_revision += 1
            self._evidence_revision += 1

    def view(self) -> WorldView:
        return WorldView(self.session, self._geometry_revision, self._evidence_revision, self)
