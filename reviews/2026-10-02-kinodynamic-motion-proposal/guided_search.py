"""Same primitives and calculator, but guided by a coarse route (as the existing surface planner would give).

Differences from kinodynamic_search.py:
  * heuristic = remaining length along the route polyline (from the state's projection) / 0.45;
  * yaw choices = route direction 1.5 blocks ahead of the projection, +/-45 degrees, and current yaw;
  * the budget is a number of expansions (deterministic), not wall-clock time.

    PYTHONPATH=. python -B guided_search.py
"""
from __future__ import annotations

import math

import kinodynamic_search as ks

ROUTES = {
    "flat 20": [(0.5, 0.5), (0.5, 20.5)],
    "gap 1": [(0.5, 0.5), (0.5, 14.5)],
    "gap 2": [(0.5, 0.5), (0.5, 15.5)],
    "gap 3": [(0.5, 0.5), (0.5, 16.5)],
    "gap 4": [(0.5, 0.5), (0.5, 17.5)],
    "gap 3 + 4 floor + gap 3": [(0.5, 0.5), (0.5, 22.5)],
    "step up 1 at speed": [(0.5, 0.5), (0.5, 16.5)],
    "turn then gap 3": [(0.5, 0.5), (0.5, 6.5), (17.5, 6.5)],
}
ROUTE = []
MAX_EXPANSIONS = 3000


def _project(x, z):
    best = None
    walked = 0.0
    for (ax, az), (bx, bz) in zip(ROUTE, ROUTE[1:]):
        dx, dz = bx - ax, bz - az
        length = math.hypot(dx, dz)
        t = max(0.0, min(1.0, ((x - ax) * dx + (z - az) * dz) / (length * length)))
        px, pz = ax + dx * t, az + dz * t
        d = math.hypot(x - px, z - pz)
        if best is None or d < best[0] - 1e-9:
            best = (d, walked + t * length)
        walked += length
    return best[1], walked


def _point_at(s_along):
    walked = 0.0
    for (ax, az), (bx, bz) in zip(ROUTE, ROUTE[1:]):
        length = math.hypot(bx - ax, bz - az)
        if s_along <= walked + length:
            t = (s_along - walked) / length
            return ax + (bx - ax) * t, az + (bz - az) * t
        walked += length
    return ROUTE[-1]


def heuristic(s, box):
    along, total = _project(s.position[0], s.position[2])
    return max(0.0, total - along) / ks.H_SPEED


def yaw_choices(s, box):
    along, _ = _project(s.position[0], s.position[2])
    tx, tz = _point_at(along + 1.5)
    x, _, z = s.position
    toward = math.atan2(-(tx - x), tz - z)
    current = s.yaw_radians
    return sorted({round(v, 6) for v in (toward, toward + math.pi / 4, toward - math.pi / 4, current)})


ks.heuristic = heuristic
ks.yaw_choices = yaw_choices


def run(course, robust):
    """search() with an expansion budget instead of wall-clock."""
    import time as _time
    original = _time.perf_counter
    counter = {"n": 0}

    def fake_clock():
        counter["n"] += 1
        return original()
    result = ks.search(course, robust=robust, budget_s=1e9) if MAX_EXPANSIONS is None else _bounded(course, robust)
    return result


def _bounded(course, robust):
    # Wrap the frontier loop budget by monkeypatching heapq.heappop to stop after MAX_EXPANSIONS pops.
    import heapq
    pops = {"n": 0}
    original_pop = heapq.heappop

    def limited_pop(heap):
        pops["n"] += 1
        if pops["n"] > MAX_EXPANSIONS:
            heap.clear()
            raise IndexError
        return original_pop(heap)
    ks.heapq.heappop = limited_pop
    try:
        return ks.search(course, robust=robust, budget_s=1e9)
    except IndexError:
        return dict(found=False, reason=f"{MAX_EXPANSIONS} expansions", expanded=MAX_EXPANSIONS,
                    steps=ks.Counter.steps, ms=float("nan"), rejected_late=-1)
    finally:
        ks.heapq.heappop = original_pop


if __name__ == "__main__":
    import sys
    if "--walk-only" in sys.argv:
        ks.KINDS = ks.WALK_KINDS
        ks.H_SPEED = 0.22          # walking top horizontal speed ~0.216 block/tick (walk-jump is slightly faster)
    print(f"route-guided weighted A* (w={ks.WEIGHT}), budget {MAX_EXPANSIONS} expansions, primitives {ks.KINDS}\n")
    for course in ks.courses():
        ROUTE[:] = ROUTES[course.name]
        for robust in (False, True):
            r = _bounded(course, robust)
            label = f"{course.name:<26}{'robust' if robust else 'nominal':<9}"
            if not r["found"]:
                print(f"{label} NOT FOUND ({r['reason']}) steps={r['steps']}")
                continue
            plan = r["plan"]
            jumps = [i for i, p in enumerate(plan) if p[0] in ("jump", "walkjump")]
            late_ok = sum(ks.replay(r["world"], r["start"], plan, course, delay_jump_index=i) for i in jumps)
            print(f"{label} ticks={r['ticks']:<4} expanded={r['expanded']:<6} steps={r['steps']:<7} ms={r['ms']:<7.0f} "
                  f"replay={'ok' if ks.replay(r['world'], r['start'], plan, course) else 'FAIL'} "
                  f"jumps={len(jumps)} survive-1-tick-late={late_ok}/{len(jumps)}")
            print(f"{'':<35}{ks.summarize(plan)}")
