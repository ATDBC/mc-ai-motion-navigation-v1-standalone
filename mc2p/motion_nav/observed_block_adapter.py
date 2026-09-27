"""Convert lawfully observed V3 blocks into the navigation world model."""
from __future__ import annotations

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v3 import ObservedBlockV3
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    CellKnowledge,
    ObservationStamp,
    WorldKnowledge,
)


_AIR_IDS = frozenset({"minecraft:air", "minecraft:cave_air", "minecraft:void_air"})
_MAX_SHARED_GEOMETRIES = 4096


def _geometry(block: ObservedBlockV3) -> BlockGeometry:
    collision = block.collision
    fluid = block.fluid_id is not None
    if collision.kind == "full_cube":
        return BlockGeometry.full_cube(block.block_id, fluid=fluid)
    if collision.kind == "empty":
        return BlockGeometry.empty(block.block_id, fluid=fluid)
    if collision.kind == "unsupported":
        return BlockGeometry.unsupported(block.block_id, fluid=fluid)
    boxes = tuple(
        Aabb(
            box.min_x, box.min_y, box.min_z,
            box.max_x, box.max_y, box.max_z,
        )
        for box in collision.boxes
    )
    return BlockGeometry(block.block_id, "boxes", boxes, fluid)


def _geometry_key(block: ObservedBlockV3) -> tuple[object, ...]:
    return block.block_id, block.fluid_id is not None, block.collision


def apply_observed_blocks(
    world: WorldKnowledge,
    stamp: ObservationStamp,
    blocks: tuple[ObservedBlockV3, ...],
    shared_geometries: dict[tuple[object, ...], BlockGeometry] | None = None,
) -> tuple[tuple[int, int, int], ...]:
    """Apply explicit block or air evidence; absence never means air."""
    if type(world) is not WorldKnowledge or type(stamp) is not ObservationStamp:
        raise ContractViolation("observed block adapter requires world knowledge and a stamp")
    if (
        type(blocks) is not tuple
        or any(type(block) is not ObservedBlockV3 for block in blocks)
    ):
        raise ContractViolation("observed block adapter requires exact V3 blocks")
    air = tuple(block.position for block in blocks if block.block_id in _AIR_IDS)
    changed = set()
    solids = {}
    for block in blocks:
        if block.block_id in _AIR_IDS:
            continue
        key = _geometry_key(block)
        geometry = None if shared_geometries is None else shared_geometries.get(key)
        if geometry is None:
            geometry = _geometry(block)
            if (shared_geometries is not None
                    and len(shared_geometries) < _MAX_SHARED_GEOMETRIES):
                shared_geometries[key] = geometry
        solids[block.position] = geometry
    rejected = set()
    if solids:
        result, solid_changes = world.observe_blocks_with_changes(stamp, solids)
        rejected.update(result.rejected_positions)
        changed.update(solid_changes)
    if air:
        before = world.view()
        for position in air:
            if before.cell(position).knowledge is not CellKnowledge.AIR:
                changed.add(position)
        rejected.update(world.confirm_air(stamp, air).rejected_positions)
    return tuple(sorted(changed - rejected))
