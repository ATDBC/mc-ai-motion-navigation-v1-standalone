"""Packed block count underground before and after enclosed-block culling.

Run anywhere (pure Python):

    python -B <review-branch>/reviews/2026-09-26-d035-plan-repro/culling_capacity_estimate.py

Mirrors TileGeometryStore at commit 3e0c64b (4-block tiles within 17 blocks
of the eye).  A block is culled only when it is solid and all six neighbours
are solid *and inside the store*; blocks on the store edge are kept because
their outer neighbour is not in the store.  The last line shows the count if
the edge is also resolved by reading one extra layer of neighbours.
"""
from __future__ import annotations

import math

EDGE = 4
FACES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def tiles(x, y, z, radius=17):
    centre = (math.floor(x) // EDGE, math.floor(y) // EDGE, math.floor(z) // EDGE)
    reach = math.ceil(radius / EDGE) + 1
    result = set()
    for a in range(centre[0] - reach, centre[0] + reach + 1):
        for b in range(centre[1] - reach, centre[1] + reach + 1):
            for c in range(centre[2] - reach, centre[2] + reach + 1):
                dx = max(a * EDGE - x, x - (a + 1) * EDGE, 0)
                dy = max(b * EDGE - y, y - (b + 1) * EDGE, 0)
                dz = max(c * EDGE - z, z - (c + 1) * EDGE, 0)
                if dx * dx + dy * dy + dz * dz <= radius * radius:
                    result.add((a, b, c))
    return result


def main() -> None:
    eye = (0.5, 41.62, 0.5)
    store = tiles(*eye)

    def in_store(x, y, z):
        return (x // EDGE, y // EDGE, z // EDGE) in store

    def solid(x, y, z):
        return not (x == 0 and y in (40, 41))  # a 1x2 tunnel along z

    packed = kept_edge = kept_halo = 0
    for a, b, c in store:
        for x in range(a * EDGE, a * EDGE + EDGE):
            for y in range(b * EDGE, b * EDGE + EDGE):
                for z in range(c * EDGE, c * EDGE + EDGE):
                    if not solid(x, y, z):
                        continue
                    packed += 1
                    around = [(x + dx, y + dy, z + dz) for dx, dy, dz in FACES]
                    if not all(in_store(*p) and solid(*p) for p in around):
                        kept_edge += 1
                    if not all(solid(*p) for p in around):
                        kept_halo += 1
    print(f"packed now (cap 25,000):              {packed:>6}")
    print(f"after culling, store edge kept:       {kept_edge:>6}")
    print(f"after culling, edge resolved by halo: {kept_halo:>6}")


if __name__ == "__main__":
    main()
