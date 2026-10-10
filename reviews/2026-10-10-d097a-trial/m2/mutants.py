"""Set B: candidates built to be rejected (or at least stress the scanner's rejection paths).

Everything is deterministic.  A mutant is (label, inputs); the equivalence harness runs the full
scanner on each and keeps whatever it decides - the harness reports how many the scanner rejected.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import random

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.physics_types import TickInput
from mc2p.motion_nav.world_model import BlockGeometry, CellFact, CellKnowledge

import common


def _is_stop(command: TickInput) -> bool:
    return not (command.forward or command.strafe or command.jump)


def input_mutants(inputs, alphabet, stop, *, seed_key: str, random_count: int = 4):
    """Deterministic edits of one verified candidate."""
    inputs = tuple(inputs)
    n = len(inputs)
    out = []
    for cut in (1, 2, 3, 5, 8, 12, 16, 20):
        if n > cut:
            out.append((f"truncate_{cut}", inputs[:-cut]))
    jumps = [i for i, c in enumerate(inputs) if c.jump]
    if jumps:
        out.append(("jump_to_walk", tuple(replace(c, jump=False) if c.jump else c for c in inputs)))
        first = jumps[0]
        if first >= 1:
            out.append(("drop_tick_before_jump", inputs[:first - 1] + inputs[first:]))
            out.append(("dup_tick_before_jump", inputs[:first] + (inputs[first - 1],) + inputs[first:]))
            swapped = list(inputs)
            swapped[first - 1], swapped[first] = swapped[first], swapped[first - 1]
            out.append(("jump_one_earlier", tuple(swapped)))
        if first + 1 < n:
            swapped = list(inputs)
            swapped[first + 1], swapped[first] = swapped[first], swapped[first + 1]
            out.append(("jump_one_later", tuple(swapped)))
        out.append(("stop_after_jump", inputs[:first + 1] + tuple(stop for _ in inputs[first + 1:])))
        out.append(("double_jump_tick", inputs[:first + 1] + (inputs[first],) + inputs[first + 1:]))
    # commit on the very first tick: the late branch cannot sit inside a risk interval
    for jump_command in tuple(dict.fromkeys(c for c in alphabet if c.jump))[:2]:
        if inputs[0] != jump_command:
            out.append((f"first_tick_{'sprint_' if jump_command.sprint else ''}jump",
                        (jump_command,) + inputs[1:]))
    moving = [c for c in inputs if not _is_stop(c) and not c.jump]
    if moving:
        out.append(("all_walk", tuple(moving[0] for _ in inputs)))
    out.append(("all_stop", tuple(stop for _ in inputs)))
    out.append(("no_final_stop", tuple(c if not _is_stop(c) else (moving[0] if moving else c)
                                       for c in inputs)))
    digest = hashlib.sha256(seed_key.encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    for index in range(random_count):
        edited = list(inputs)
        for _ in range(1 + index // 2):
            edited[rng.randrange(n)] = rng.choice(alphabet)
        out.append((f"random_{index}", tuple(edited)))
    seen, unique = set(), []
    for label, candidate in out:
        if candidate and candidate not in seen and candidate != inputs:
            seen.add(candidate)
            unique.append((label, candidate))
    return unique


def stone_like(world):
    return world.cell((0, 0, 0))


def drop_world(world, drop_from_z: int, depth: int, x_range=range(-3, 4), z_end: int = 12):
    """Flat fixture with the floor removed for z >= drop_from_z and a platform `depth` blocks lower."""
    stone = stone_like(world)
    edits = {}
    for x in x_range:
        for z in range(drop_from_z, z_end + 1):
            edits[(x, 0, z)] = CellFact(CellKnowledge.AIR, stone.stamp, None)
            edits[(x, -depth, z)] = stone
    return common.edited_world(world, edits, bounds=((-3, 3), (-50, 7), (-3, 12)))


def drop_cases(base_fixture, inputs):
    """Walk-off candidates: (label, request, world, inputs)."""
    rows = []
    walk = next(c for c in base_fixture.request.supported_inputs
                if c.forward == 1. and not c.jump and not c.sprint and not c.sneak
                and c.movement_yaw_radians == 0.)
    stop = base_fixture.request.stop_input
    candidate = (walk,) * 16 + (stop,) * 24
    for depth in (2, 3, 4, 5, 6):
        world = drop_world(base_fixture.world, 3, depth)
        for budget in (0., 1., 2.):
            request = replace(base_fixture.request,
                              task_damage_budget=TaskDamageBudget("trial", budget),
                              request_id=f"drop-{depth}-{int(budget)}")
            rows.append((f"drop_depth{depth}_budget{int(budget)}", request, world, candidate))
    return rows
