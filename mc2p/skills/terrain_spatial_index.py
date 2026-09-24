"""Immutable spatial views of observed terrain, with copy-on-write tile updates."""
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
import math
from types import MappingProxyType

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v3 import AabbV3
from mc2p.skills.local_navigation import prepare_block_collision

TILE_SIZE = 4
# A sphere of radius 32 fits in at most 65 grid coordinates on each axis.
# This is a defensive geometric bound, not an eviction target.
SPHERICAL_TERRAIN_BOUND = 65 ** 3


def tile(position):
    return tuple(v // TILE_SIZE for v in position)


def box_tiles(box):
    ends = [(math.floor(lo/TILE_SIZE),math.floor(hi/TILE_SIZE)) for lo,hi in
            ((box.min_x,box.max_x),(box.min_y,box.max_y),(box.min_z,box.max_z))]
    if math.prod(hi-lo+1 for lo,hi in ends) > 64:
        return None
    return tuple(product(*(range(lo,hi+1) for lo,hi in ends)))


@dataclass(frozen=True, slots=True)
class _Entry:
    record: object
    boxes: object
    potential: tuple
    tiles: object


def entry(record, previous=None):
    block = record.block
    if (previous is not None and previous.record.block.collision == block.collision
            and previous.record.block.fluid_id == block.fluid_id):
        return _Entry(record,previous.boxes,previous.potential,previous.tiles)
    boxes = prepare_block_collision(block)
    x,y,z = block.position
    potential = (AabbV3(x,y,z,x+1,y+1,z+1),) if boxes is None else boxes
    covered = set()
    for box in potential:
        cells = box_tiles(box)
        if cells is None:
            return _Entry(record,boxes,potential,None)
        covered.update(cells)
        if len(covered) > 64:
            return _Entry(record,boxes,potential,None)
    return _Entry(record,boxes,potential,frozenset(covered))


class TerrainIndex(Mapping):
    """Records are indexed by their coordinates; collisions by actual world bounds.

    Very large shapes use a conservative overflow set instead of allocating an
    unbounded number of tile entries. Exact overlap checks remain in the guard.
    """
    __slots__ = ('owners','collisions','overflow','count','scope','token','base_token','changed','removed')

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError('terrain index is immutable')
        object.__setattr__(self,name,value)

    def __delattr__(self, name):
        raise AttributeError('terrain index is immutable')
    def __init__(self, owners=None, collisions=None, overflow=frozenset(), *,
                 count=0, scope=None, base_token=None, changed=(), removed=()):
        self.owners = MappingProxyType(owners or {})
        self.collisions = MappingProxyType(collisions or {})
        self.overflow = overflow
        self.count, self.scope = count, scope
        self.token, self.base_token = object(), base_token
        self.changed, self.removed = changed, removed

    @classmethod
    def from_records(cls, records):
        from mc2p.skills.navigation_memory import TerrainHistory
        if any(type(r) is not TerrainHistory for r in records):
            raise ContractViolation('terrain index requires typed history')
        positions = tuple(r.block.position for r in records)
        if positions != tuple(sorted(set(positions))):
            raise ContractViolation('terrain beliefs must be sorted and unique')
        scopes = {r.last_seen.scope for r in records}
        if len(scopes) > 1:
            raise ContractViolation('mixed_evidence_scope')
        return cls().updated(records, (), next(iter(scopes), None))

    def __len__(self):
        return self.count

    def __iter__(self):
        return iter(sorted(p for bucket in self.owners.values() for p in bucket))

    def _entry(self, position):
        if position is None:
            return None
        return self.owners.get(tile(position), {}).get(position)

    def __getitem__(self, position):
        found = self._entry(position)
        if found is None:
            raise KeyError(position)
        return found.record

    def get(self, position, default=None):
        found = self._entry(position)
        return default if found is None else found.record

    def updated(self, changed, removed, scope):
        changed, removed = tuple(changed), tuple(removed)
        owners, collisions = dict(self.owners), dict(self.collisions)
        owner_writes, collision_writes = {}, {}
        overflow = set(self.overflow)
        count = self.count

        def owner_bucket(key):
            if key not in owner_writes:
                owner_writes[key] = dict(owners.get(key, {}))
            return owner_writes[key]

        def membership(position, cells, add):
            if cells is None:
                (overflow.add if add else overflow.discard)(position)
                return
            for key in cells:
                if key not in collision_writes:
                    collision_writes[key] = set(collisions.get(key, ()))
                (collision_writes[key].add if add else collision_writes[key].discard)(position)

        for position in removed:
            previous = self._entry(position)
            if previous is not None:
                owner_bucket(tile(position)).pop(position)
                membership(position, previous.tiles, False)
                count -= 1
        for record in changed:
            position = record.block.position
            previous = self._entry(position)
            current = entry(record, previous)
            owner_bucket(tile(position))[position] = current
            if previous is None:
                count += 1
                membership(position, current.tiles, True)
            elif previous.tiles != current.tiles:
                membership(position, previous.tiles, False)
                membership(position, current.tiles, True)
        for key, bucket in owner_writes.items():
            if bucket:
                owners[key] = MappingProxyType(bucket)
            else:
                owners.pop(key, None)
        for key, bucket in collision_writes.items():
            if bucket:
                collisions[key] = frozenset(bucket)
            else:
                collisions.pop(key, None)
        return TerrainIndex(owners, collisions, frozenset(overflow), count=count,
                            scope=scope if count else None, base_token=self.token,
                            changed=changed, removed=removed)

    def collision_candidates(self, bounds):
        cells = box_tiles(bounds)
        if cells is None:
            # Large movement queries remain correct; normal short steps use tiles.
            positions = set(self)
        else:
            positions = set(self.overflow)
            for key in cells:
                positions.update(self.collisions.get(key, ()))
        for position in sorted(positions):
            found = self._entry(position)
            yield found.record, found.boxes, found.potential

    def positions_in_grid(self, minimum, maximum):
        cells = product(*(range(a//TILE_SIZE,b//TILE_SIZE+1) for a,b in zip(minimum,maximum)))
        positions = []
        for cell in cells:
            positions.extend(p for p in self.owners.get(cell, ())
                             if all(a <= v <= b for a,v,b in zip(minimum,p,maximum)))
        return tuple(sorted(positions))
