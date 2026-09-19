"""Adapter from legal pre-floating V3 visible blocks into the new world owner."""
from __future__ import annotations

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v3 import ObservedBlockV3
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp, WorldKnowledge


_AIR_IDS = frozenset({"minecraft:air", "minecraft:cave_air", "minecraft:void_air"})


def _geometry(block: ObservedBlockV3) -> BlockGeometry:
    collision = block.collision
    fluid = block.fluid_id is not None
    if collision.kind == "full_cube":
        return BlockGeometry.full_cube(block.block_id, fluid=fluid)
    if collision.kind == "empty":
        return BlockGeometry.empty(block.block_id, fluid=fluid)
    if collision.kind == "unsupported":
        return BlockGeometry.unsupported(block.block_id, fluid=fluid)
    boxes = tuple(Aabb(box.min_x, box.min_y, box.min_z,
                       box.max_x, box.max_y, box.max_z) for box in collision.boxes)
    return BlockGeometry(block.block_id, "boxes", boxes, fluid)


def apply_visible_blocks(world: WorldKnowledge, stamp: ObservationStamp,
                         blocks: tuple[ObservedBlockV3, ...]) -> None:
    if type(world) is not WorldKnowledge or type(stamp) is not ObservationStamp:
        raise ContractViolation("visible block adapter requires world knowledge and a stamp")
    if type(blocks) is not tuple or any(type(block) is not ObservedBlockV3 for block in blocks):
        raise ContractViolation("visible block adapter requires exact V3 blocks")
    air = tuple(block.position for block in blocks if block.block_id in _AIR_IDS)
    solids = {block.position: _geometry(block) for block in blocks if block.block_id not in _AIR_IDS}
    if solids:
        world.observe_blocks(stamp, solids)
    if air:
        world.confirm_air(stamp, air)
