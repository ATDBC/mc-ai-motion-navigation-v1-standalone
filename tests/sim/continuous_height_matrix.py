"""Frozen continuous-height scenarios for the formal-path component matrix."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path

from tests.sim.backend import Perturbations, Scene
from tests.sim.runner import Scenario, lane, late_ticks


MANIFEST = Path("tests/sim/manifests/continuous-height-full-matrix.json")
STONE = "minecraft:stone"
SLAB = "minecraft:smooth_stone_slab[type=bottom]"
PATH = "minecraft:dirt_path"
CARPET = "minecraft:white_carpet"
STAIR_SOUTH = "minecraft:oak_stairs[facing=south,half=bottom,shape=straight]"

_YAW_BY_DIRECTION = {
    "east": -90.0,
    "south": 0.0,
    "west": 90.0,
    "north": 180.0,
}


def _rotate_cell(
    position: tuple[int, int, int], direction: str,
) -> tuple[int, int, int]:
    x, y, z = position
    if direction == "south":
        return x, y, z
    if direction == "east":
        return z, y, -x - 1
    if direction == "north":
        return -x - 1, y, -z - 1
    if direction == "west":
        return -z - 1, y, x
    raise ValueError(f"unsupported matrix direction: {direction}")


def _rotate_point(
    position: tuple[float, float, float], direction: str,
) -> tuple[float, float, float]:
    x, y, z = position
    if direction == "south":
        return x, y, z
    if direction == "east":
        return z, y, -x
    if direction == "north":
        return -x, y, -z
    if direction == "west":
        return -z, y, x
    raise ValueError(f"unsupported matrix direction: {direction}")


def _rotate_block_state(block_id: str, direction: str) -> str:
    if "facing=south" not in block_id:
        return block_id
    return block_id.replace("facing=south", f"facing={direction}")


def matrix_scenario(
    base: Scenario,
    *,
    direction: str,
    speed_blocks_per_second: float,
    seed: int,
    late_probability: float,
) -> Scenario:
    """Rotate one frozen scene and bind its entry speed and lateness seed."""
    if direction not in _YAW_BY_DIRECTION:
        raise ValueError(f"unsupported matrix direction: {direction}")
    if speed_blocks_per_second < 0:
        raise ValueError("matrix entry speed cannot be negative")
    solids = {
        _rotate_cell(position, direction): _rotate_block_state(block_id, direction)
        for position, block_id in base.scene.solids.items()
    }
    xs = [position[0] for position in solids]
    zs = [position[2] for position in solids]
    y_bounds = base.scene.volume[1]
    scene = Scene(
        solids,
        ((min(xs) - 3, max(xs) + 3), y_bounds,
         (min(zs) - 3, max(zs) + 3)),
    )
    blocks_per_tick = speed_blocks_per_second / 20.0
    velocity = {
        "east": (blocks_per_tick, -0.0784, 0.0),
        "south": (0.0, -0.0784, blocks_per_tick),
        "west": (-blocks_per_tick, -0.0784, 0.0),
        "north": (0.0, -0.0784, -blocks_per_tick),
    }[direction]
    perturbations = Perturbations(
        late_ticks=late_ticks(late_probability, seed)
        if late_probability > 0 else frozenset(),
    )
    return replace(
        base,
        name=f"{base.name}/{direction}/{speed_blocks_per_second:g}/{seed}",
        scene=scene,
        start=_rotate_point(base.start, direction),
        goal=_rotate_point(base.goal, direction),
        yaw_degrees=_YAW_BY_DIRECTION[direction],
        start_velocity_blocks_per_tick=velocity,
        perturbations=perturbations,
    )


@dataclass(frozen=True, slots=True)
class MatrixCase:
    family: str
    condition: str
    direction: str
    speed_band: str
    speed_blocks_per_second: float
    seed: int
    scenario: Scenario


def small_height_matrix_cases(condition: str) -> tuple[MatrixCase, ...]:
    """Expand the frozen small-height manifest without reading test results."""
    if condition not in {"normal", "late"}:
        raise ValueError(f"unsupported matrix condition: {condition}")
    manifest = load_manifest()
    directions = tuple(manifest["directions"])
    speed_bands = tuple(manifest["entry_speed_bands"])
    repetitions = int(manifest["component_repetitions"])
    late_probability = (
        float(manifest["late_probability"]) if condition == "late" else 0.0
    )
    rows = []
    for base in small_height_scenarios():
        for index in range(repetitions):
            direction = directions[index % len(directions)]
            speed = speed_bands[index % len(speed_bands)]
            seed = int(manifest["seed_start"]) + index
            rows.append(MatrixCase(
                base.name,
                condition,
                direction,
                str(speed["id"]),
                float(speed["target"]),
                seed,
                matrix_scenario(
                    base,
                    direction=direction,
                    speed_blocks_per_second=float(speed["target"]),
                    seed=seed,
                    late_probability=late_probability,
                ),
            ))
    return tuple(rows)


def height_action_matrix_cases(condition: str) -> tuple[MatrixCase, ...]:
    """Expand the frozen full-block action matrix without reading results."""
    if condition not in {"normal", "late"}:
        raise ValueError(f"unsupported matrix condition: {condition}")
    manifest = load_manifest()
    directions = tuple(manifest["directions"])
    speed_bands = tuple(manifest["entry_speed_bands"])
    repetitions = int(manifest["component_repetitions"])
    late_probability = (
        float(manifest["late_probability"]) if condition == "late" else 0.0
    )
    rows = []
    for base in height_action_scenarios():
        for index in range(repetitions):
            direction = directions[index % len(directions)]
            speed = speed_bands[index % len(speed_bands)]
            seed = int(manifest["seed_start"]) + index
            rows.append(MatrixCase(
                base.name,
                condition,
                direction,
                str(speed["id"]),
                float(speed["target"]),
                seed,
                matrix_scenario(
                    base,
                    direction=direction,
                    speed_blocks_per_second=float(speed["target"]),
                    seed=seed,
                    late_probability=late_probability,
                ),
            ))
    return tuple(rows)


def load_manifest() -> dict:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if document.get("schema_version") != "mc2p.continuous-height-full-matrix.v1":
        raise ValueError("unsupported continuous-height matrix manifest")
    if document.get("seed_end") - document.get("seed_start") + 1 != 100:
        raise ValueError("continuous-height matrix must freeze one hundred seeds")
    return document


def _columns(materials: tuple[tuple[tuple[int, str] | int, ...], ...]) -> Scene:
    return lane([list(column) for column in materials], width=3)


def _decorated_lane(material: str) -> Scene:
    return _columns((
        (63,), (63, (64, material)), (63,), (63, (64, material)), (63,),
    ))


def small_height_scenarios() -> tuple[Scenario, ...]:
    half_steps = _columns((
        (63,), (63,), (63, (64, SLAB)), (64,),
        (64, (65, SLAB)), (65,), (65,),
    ))
    stairs = _columns((
        (63,), (63,), ((64, STAIR_SOUTH),), (64,),
        ((65, STAIR_SOUTH),), (65,), (65,),
    ))
    path = _columns(tuple(
        ((63, STONE),) if index % 2 == 0 else ((63, PATH),)
        for index in range(9)
    ))
    carpet = _decorated_lane(CARPET)
    single_slab = _columns((
        (63,), (63,), (63, (64, SLAB)), (63,), (63,),
    ))
    brake = _columns((
        (63,), (63, (64, SLAB)), (64,), (64,),
    ))
    rows = [
        Scenario("slab_up_down", half_steps, (.5, 64, .5), (.5, 66, 6.5)),
        Scenario("slab_down_up", half_steps, (.5, 66, 6.5), (.5, 64, .5),
                 yaw_degrees=180.0),
        Scenario("stairs_up", stairs, (.5, 64, .5), (.5, 66, 6.5)),
        Scenario("stairs_down", stairs, (.5, 66, 6.5), (.5, 64, .5),
                 yaw_degrees=180.0),
        Scenario("dirt_path_alternating", path, (.5, 64, .5), (.5, 64, 8.5)),
        Scenario("carpet_alternating", carpet, (.5, 64, .5), (.5, 64, 4.5)),
    ]
    for layers in (2, 4, 5):
        rows.append(Scenario(
            f"snow_layers_{layers}",
            _decorated_lane(f"minecraft:snow[layers={layers}]"),
            (.5, 64, .5), (.5, 64, 4.5),
        ))
    rows.extend((
        Scenario("single_bottom_slab", single_slab,
                 (.5, 64, .5), (.5, 64, 4.5)),
        Scenario("low_height_then_brake", brake,
                 (.5, 64, .5), (.5, 65, 3.5)),
    ))
    expected = tuple(load_manifest()["small_height_families"])
    if tuple(row.name for row in rows) != expected:
        raise ValueError("small-height scenario order differs from frozen manifest")
    return tuple(rows)


def _full_block_lane(feet_heights: tuple[int, ...]) -> Scene:
    scene = _columns(tuple(((height - 1, STONE),) for height in feet_heights))
    return Scene(
        scene.solids,
        (scene.volume[0], (scene.volume[1][0], max(feet_heights) + 4),
         scene.volume[2]),
    )


def _drop_ledge(height: int) -> Scene:
    solids = {}
    for x in (-1, 0, 1):
        for z in range(3):
            solids[(x, 63, z)] = STONE
        for z in range(3, 7):
            solids[(x, 63 - height, z)] = STONE
    return Scene(solids, ((-4, 4), (52, 72), (-3, 9)))


def height_action_scenarios() -> tuple[Scenario, ...]:
    """Full-block transitions that retain distinct action and risk semantics."""
    rows = (
        Scenario(
            "jump_up_1", _full_block_lane((64, 64, 65, 65, 65, 65)),
            (.5, 64, .5), (.5, 65, 5.5),
        ),
        Scenario(
            "step_down_1", _full_block_lane((65, 65, 64, 64, 64, 64)),
            (.5, 65, .5), (.5, 64, 5.5),
        ),
        Scenario(
            "stair_descent_2", _full_block_lane((66, 66, 65, 64, 64)),
            (.5, 66, .5), (.5, 64, 4.5),
        ),
        Scenario(
            "stair_descent_4",
            _full_block_lane((68, 68, 67, 66, 65, 64, 64)),
            (.5, 68, .5), (.5, 64, 6.5),
        ),
        Scenario(
            "stair_descent_8",
            _full_block_lane((72, 72, 71, 70, 69, 68, 67, 66, 65, 64, 64)),
            (.5, 72, .5), (.5, 64, 10.5), max_ticks=600,
        ),
        Scenario(
            "direct_drop_1", _drop_ledge(1),
            (.5, 64, .5), (.5, 63, 5.5),
        ),
        Scenario(
            "direct_drop_2", _drop_ledge(2),
            (.5, 64, .5), (.5, 62, 5.5),
        ),
        Scenario(
            "direct_drop_3", _drop_ledge(3),
            (.5, 64, .5), (.5, 61, 5.5),
        ),
        Scenario(
            "direct_drop_5_budget_2", _drop_ledge(5),
            (.5, 64, .5), (.5, 59, 5.5), damage_points=2.0,
        ),
    )
    expected = tuple(load_manifest()["height_action_families"])
    if tuple(row.name for row in rows) != expected:
        raise ValueError("height-action scenario order differs from frozen manifest")
    return rows
