"""Same search as kinodynamic_search.py, plus an exact primitive cache (prototype of the 'lattice').

On flat, open ground the outcome of a primitive depends only on the start velocity, yaw and flags,
not on the absolute position.  The cache stores each primitive's per-tick relative trajectory once,
computed by the real calculator on a reference flat floor.  At a new position the cached result is
reused only if the actual world matches the reference along that trajectory:
  * every body box along the path is in air cells, and
  * every tick that was on the ground in the reference has floor under the body at the same height.
Otherwise (step, wall, gap edge under a ground tick) the real calculator is run as before.
Keys use the exact start velocity (no binning), so a hit reproduces the calculator to rounding.

    PYTHONPATH=. python -B primitive_cache.py
"""
from __future__ import annotations

import math
import time

import kinodynamic_search as ks
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from tests.sim.backend import CalculatorBackend, Scene
from dataclasses import replace

REF = Scene({(x, 63, z): ks.STONE for x in range(-40, 41) for z in range(-40, 41)},
            ((-42, 42), (60, 70), (-42, 42)))
REF_BACKEND = CalculatorBackend([0], REF, (0.5, 64.0, 0.5))
CACHE: dict = {}
STATS = {"hit": 0, "miss_new": 0, "miss_terrain": 0}
SOLIDS: dict = {}


def _cells(box):
    for x in range(math.floor(box[0]), math.floor(box[3] - 1e-9) + 1):
        for y in range(math.floor(box[1]), math.floor(box[4] - 1e-9) + 1):
            for z in range(math.floor(box[2]), math.floor(box[5] - 1e-9) + 1):
                yield (x, y, z)


def _terrain_matches(origin, offsets):
    ox, oy, oz = origin
    for dx, dy, dz, ground in offsets:
        x, y, z = ox + dx, oy + dy, oz + dz
        body = (x - .3, y, z - .3, x + .3, y + 1.8, z + .3)
        if any(c in SOLIDS for c in _cells(body)):
            return False
        if ground:
            floor = (x - .3, y - 1.0, z - .3, x + .3, y - 1e-6, z + .3)
            if not any(SOLIDS.get(c) for c in _cells(floor)):
                return False
    return True


_original = ks.run_primitive


def cached_primitive(state, world, kind, yaw, *, late_jump=False):
    if abs(state.position[1] - round(state.position[1])) > 1e-9 or not state.on_ground:
        return _original(state, world, kind, yaw, late_jump=late_jump)
    k = (kind, round(yaw, 9), late_jump, state.sprinting, state.jumping_cooldown_ticks,
         tuple(round(v, 12) for v in state.velocity_blocks_per_tick))
    entry = CACHE.get(k)
    if entry is None:
        STATS["miss_new"] += 1
        ref_start = replace(REF_BACKEND.state, velocity_blocks_per_tick=state.velocity_blocks_per_tick,
                            sprinting=state.sprinting, jumping_cooldown_ticks=state.jumping_cooldown_ticks,
                            yaw_radians=state.yaw_radians)
        positions = []
        s = ref_start
        # Re-run the primitive on the reference floor, recording every tick.
        seq_end, inputs, alive = _original(ref_start, REF_BACKEND.world, kind, yaw, late_jump=late_jump)
        s = ref_start
        for inp in inputs:
            s = ks.tick(s, inp, REF_BACKEND.world)
            positions.append((s.position[0] - ref_start.position[0], s.position[1] - ref_start.position[1],
                              s.position[2] - ref_start.position[2], s.on_ground))
        entry = (seq_end, ref_start.position, inputs, alive, positions)
        CACHE[k] = entry
    seq_end, ref_origin, inputs, alive, positions = entry
    if not _terrain_matches(state.position, positions):
        STATS["miss_terrain"] += 1
        return _original(state, world, kind, yaw, late_jump=late_jump)
    STATS["hit"] += 1
    if seq_end is None:
        return None, inputs, False
    offset = tuple(state.position[i] - ref_origin[i] for i in range(3))
    end = replace(seq_end, position=tuple(seq_end.position[i] + offset[i] for i in range(3)),
                  session=state.session, movement_tick_id=state.movement_tick_id + len(inputs))
    alive = end.on_ground and end.position[1] >= ks.FLOOR_Y - 0.01 and end.fall_distance_blocks <= 3.0
    return end, inputs, alive


ks.run_primitive = cached_primitive


if __name__ == "__main__":
    print("exact primitive cache on top of the same weighted A* (cache is warm across courses, in order)\n")
    for course in ks.courses():
        SOLIDS.clear()
        SOLIDS.update(course.solids)
        for robust in (False, True):
            before = dict(STATS)
            r = ks.search(course, robust=robust)
            used = {k: STATS[k] - before[k] for k in STATS}
            label = f"{course.name:<26}{'robust' if robust else 'nominal':<9}"
            if not r["found"]:
                print(f"{label} NOT FOUND ({r['reason']}) expanded={r['expanded']} steps={r['steps']} ms={r['ms']:.0f} cache={used}")
                continue
            ks.run_primitive = _original          # verify the found plan with the real calculator only
            ok = ks.replay(r["world"], r["start"], r["plan"], course)
            ks.run_primitive = cached_primitive
            print(f"{label} ticks={r['ticks']:<4} expanded={r['expanded']:<6} steps={r['steps']:<7} ms={r['ms']:<7.0f} "
                  f"real-calculator replay={'ok' if ok else 'FAIL'} cache={used}")
    print(f"\ncache entries: {len(CACHE)}")
