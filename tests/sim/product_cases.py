"""Frozen product cases use the existing simulator and public goal commands."""
from __future__ import annotations

from dataclasses import replace
import random

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Scene, Perturbations
from tests.sim.continuous_height_matrix import matrix_scenario
from tests.sim.runner import Event, Scenario, _goal, lane, late_ticks
from tests.sim.scenarios import half_steps, columns, drop_ledge

STONE = "minecraft:stone"


def _platform():
    return {(x, 63, z): STONE for x in range(-3, 4) for z in range(15)}


PLAYER_CASES = ("player_wall_head", "player_wall_parallel", "player_corner",
                "player_corridor_middle", "player_corridor_end",
                "player_ledge_0", "player_ledge_1", "player_ledge_2")


def player_layout(case: str, *, tangent_offset: float = 0.):
    """Known geometry shared by simulation and the Fabric fixture builder."""
    solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
    start, goal = (.5, 64., .5), (.5, 64., 8.5)
    if case in {"player_wall_head", "player_wall_parallel", "player_corner"}:
        solids.update({(2, y, z): STONE for y in (64, 65, 66) for z in range(4, 9)})
        goal = (1.7, 64., 6.5 + tangent_offset)
        if case == "player_wall_parallel":
            start = (1.5, 64., .5)
        if case == "player_corner":
            solids.update({(x, y, 7): STONE for y in (64, 65, 66) for x in range(-1, 3)})
            goal = (1.7, 64., 6.7)
    elif case in {"player_corridor_middle", "player_corridor_end"}:
        solids.update({(x, y, z): STONE for y in (64, 65, 66) for z in range(4, 11) for x in (-1, 1)})
        goal = (.5 + tangent_offset, 64., 8.5)
        if case == "player_corridor_end":
            solids.update({(0, y, 10): STONE for y in (64, 65, 66)})
            goal = (.5 + tangent_offset, 64., 9.7)
    elif case.startswith("player_ledge_"):
        goal = (.5 + tangent_offset, 64., 10.7 + .1 * int(case[-1]))
    else:
        raise ValueError(case)
    return Scene(solids, ((-6, 6), (60, 68), (-3, 14))), start, goal


def _revision(tick, revision, position):
    def change(context):
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal("goal", revision, goal, context.clock[0],
                                    damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
        context.goal_state, context.goal_position = goal, position
    return Event(f"goal_revision_{revision}", lambda ctx: ctx.tick >= tick, change,
                 kind="goal_revision", goal_revision=revision)


def product_scenario(manifest: dict, group: dict, seed: int) -> tuple[Scenario, dict]:
    index = seed - manifest["seed_start"]
    case = group["cases"][index % len(group["cases"])]
    start, goal, events, damage = (.5, 64.0, .5), (.5, 64.0, 8.5), [], 0.0
    scene = lane([[63]] * 11, width=3)
    if case in PLAYER_CASES:
        scene, start, goal = player_layout(case, tangent_offset=random.Random(f"player:{seed}").uniform(-.08, .08))
    elif case in {"turn", "wall_detour"}:
        solids = _platform()
        if case == "wall_detour":
            solids.update({(x, y, 5): STONE for x in (-1, 0, 1) for y in (64, 65, 66)})
        scene, goal = Scene(solids, ((-5, 5), (60, 68), (-2, 16))), (2.5, 64.0, 10.5)
    elif case == "half_steps":
        scene, goal = lane(half_steps(), width=3), (.5, 64.0, 7.5)
    elif case == "path":
        scene = lane([[63], [63], [(63, "minecraft:dirt_path")], [63], [(63, "minecraft:dirt_path")], [63], [63]], width=3)
        goal = (.5, 64.0, 6.5)
    elif case == "stairs_down":
        scene = lane(columns([68, 68, 67, 66, 65, 64, 64]), width=3)
        start, goal = (.5, 68.0, .5), (.5, 64.0, 6.5)
    elif case in {"drop_2", "drop_5"}:
        height = int(case[-1])
        scene, goal, damage = drop_ledge(height), (.5, 64.0 - height, 4.5), max(0, height - 3)
    elif case.startswith("target_"):
        scene = Scene(_platform(), ((-5, 5), (60, 68), (-2, 16)))
        if case == "target_away":
            events = [_revision(8, 2, (.5, 64.0, 10.5)), _revision(16, 3, (.5, 64.0, 12.5))]
        elif case == "target_lateral":
            events = [_revision(8, 2, (2.5, 64.0, 8.5)), _revision(16, 3, (-1.5, 64.0, 10.5))]
        else:
            events = [_revision(tick, revision, (.5 + .3 * (revision % 2), 64.0, 8.5 + .1 * revision))
                      for revision, tick in enumerate(range(3, 43, 5), 2)]
    elif case != "flat":
        raise ValueError(f"unknown product case: {case}")
    base = Scenario(case, scene, start, goal, damage_points=damage, events=events,
                    max_ticks=manifest["budgets"]["finite_max_ticks"])
    continuous = manifest.get("parameter_generator") == "continuous-v2"
    # Delay conditions share geometry and entry; only their input delivery differs.
    rng = random.Random(f"{group['family']}:{seed}")
    parameters = manifest.get("continuous_parameters", {})
    if continuous and group["family"] == "point":
        limit = parameters["maximum_goal_jitter"]
        base = replace(base, goal=(base.goal[0] + rng.uniform(-limit, limit),
                                  base.goal[1], base.goal[2] + rng.uniform(-limit, limit)))
    direction_index = index // len(group["cases"]) if continuous else index
    direction = manifest["directions"][direction_index % len(manifest["directions"])]
    speeds = manifest["entry_speeds_blocks_per_second"]
    speed = (rng.uniform(0, parameters["maximum_speed"]) if continuous else
             speeds[(index // len(group["cases"])) % len(speeds)])
    # The frozen command stream is rotated together with its map.
    from tests.sim.continuous_height_matrix import _rotate_point
    rotated = matrix_scenario(base, direction=direction, speed_blocks_per_second=speed,
                              seed=seed, late_probability=group["late_probability"])
    if events:
        positions = {
            "target_away": [(8, 2, (.5, 64.0, 10.5)), (16, 3, (.5, 64.0, 12.5))],
            "target_lateral": [(8, 2, (2.5, 64.0, 8.5)), (16, 3, (-1.5, 64.0, 10.5))],
            "target_repeat": [(tick, rev, (.5 + .3 * (rev % 2), 64.0, 8.5 + .1 * rev))
                              for rev, tick in enumerate(range(3, 43, 5), 2)],
        }[case]
        rotated.events = [_revision(tick, rev, _rotate_point(pos, direction)) for tick, rev, pos in positions]
    offset = (tuple(rng.uniform(-parameters["maximum_offset"], parameters["maximum_offset"])
                    for _ in range(2)) if continuous else
              manifest["entry_offsets"][(index // 4) % len(manifest["entry_offsets"])])
    offset_x, offset_z = (_rotate_point((offset[0], 0., offset[1]), direction)[::2]
                          if continuous else (offset, -offset))
    rotated = replace(rotated, start=(rotated.start[0] + offset_x, rotated.start[1], rotated.start[2] + offset_z),
                      perturbations=Perturbations(late_ticks=late_ticks(group["late_probability"], seed, base.max_ticks + 2)))
    return rotated, {"case": case, "direction": direction, "speed": speed, "offset": offset,
                     **({"goal": rotated.goal} if continuous else {}),
                     "late_ticks": sorted(rotated.perturbations.late_ticks)}
