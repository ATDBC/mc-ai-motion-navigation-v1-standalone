"""Geometry of authorized block snapshots; never queries or infers neighboring cells."""
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v3 import AabbV3, ObservedBlockV3

# Unchanged movement capability policy. Knowing glass/wool does not open traversal.
SUPPORTED_FLOORS = frozenset({"minecraft:stone", "minecraft:dirt", "minecraft:grass_block",
                            "minecraft:cobblestone", "minecraft:oak_planks", "minecraft:bedrock"})


def world_boxes(block: ObservedBlockV3) -> tuple[AabbV3, ...]:
    if type(block) is not ObservedBlockV3:
        raise ContractViolation("block geometry requires exact ObservedBlockV3")
    kind = block.collision.kind
    if kind == "unsupported":
        raise ContractViolation("unsupported_block_collision")
    if kind == "empty":
        return ()
    x,y,z = block.position
    boxes = (AabbV3(0,0,0,1,1,1),) if kind == "full_cube" else block.collision.boxes
    return tuple(AabbV3(b.min_x+x,b.min_y+y,b.min_z+z,b.max_x+x,b.max_y+y,b.max_z+z) for b in boxes)


def is_supported_floor(block: ObservedBlockV3) -> bool:
    if type(block) is not ObservedBlockV3:
        raise ContractViolation("floor policy requires exact ObservedBlockV3")
    return block.block_id in SUPPORTED_FLOORS and block.collision.kind == "full_cube" and block.fluid_id is None


def overlaps(a: AabbV3, b: AabbV3) -> bool:
    return (a.min_x < b.max_x-1e-7 and a.max_x > b.min_x+1e-7
            and a.min_y < b.max_y-1e-7 and a.max_y > b.min_y+1e-7
            and a.min_z < b.max_z-1e-7 and a.max_z > b.min_z+1e-7)
