"""How many non-air blocks SurfaceSensor packs, versus its 25,000 cap.

Run anywhere (pure Python):

    python -B <review-branch>/reviews/2026-09-26-d035-repro/sensor_capacity_estimate.py

It mirrors TileGeometryStore.refresh at commit 3e0c64b: 4-block tiles are
added when their nearest point is within 17 blocks of the eye and dropped only
beyond 21.  SurfaceSensor.pack() then sends every non-air block with a
non-empty outline shape and throws "surface geometry capacity exceeded" above
25,000; any sensor exception ends the formal session.
"""
from __future__ import annotations

import math

EDGE = 4
CAP = 25_000


def _distance2(tile, x, y, z):
    a, b, c = tile
    dx = max(a * EDGE - x, x - (a + 1) * EDGE, 0)
    dy = max(b * EDGE - y, y - (b + 1) * EDGE, 0)
    dz = max(c * EDGE - z, z - (c + 1) * EDGE, 0)
    return dx * dx + dy * dy + dz * dz


def tiles_within(x, y, z, radius):
    centre = (math.floor(x) // EDGE, math.floor(y) // EDGE, math.floor(z) // EDGE)
    reach = math.ceil(radius / EDGE) + 1
    return [
        (a, b, c)
        for a in range(centre[0] - reach, centre[0] + reach + 1)
        for b in range(centre[1] - reach, centre[1] + reach + 1)
        for c in range(centre[2] - reach, centre[2] + reach + 1)
        if _distance2((a, b, c), x, y, z) <= radius * radius
    ]


def packed(eye, solid, radius=17):
    return sum(
        1
        for a, b, c in tiles_within(*eye, radius)
        for x in range(a * EDGE, a * EDGE + EDGE)
        for y in range(b * EDGE, b * EDGE + EDGE)
        for z in range(c * EDGE, c * EDGE + EDGE)
        if solid(x, y, z)
    )


def exposed(eye, solid, radius=17):
    faces = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
    ex, ey, ez = eye
    count = 0
    r = radius + 1
    for x in range(math.floor(ex) - r, math.floor(ex) + r + 1):
        for y in range(math.floor(ey) - r, math.floor(ey) + r + 1):
            for z in range(math.floor(ez) - r, math.floor(ez) + r + 1):
                if not solid(x, y, z):
                    continue
                if (x + .5 - ex) ** 2 + (y + .5 - ey) ** 2 + (z + .5 - ez) ** 2 > radius ** 2:
                    continue
                if any(not solid(x + dx, y + dy, z + dz) for dx, dy, dz in faces):
                    count += 1
    return count


def main() -> None:
    surface_eye = (0.5, 101.62, -2.5)
    tunnel_eye = (0.5, 41.62, 0.5)

    def flat(x, y, z):
        return y <= 99

    def tunnel(x, y, z):
        return not (x == 0 and y in (40, 41))

    rows = (
        ("flat ground, tiles within 17", packed(surface_eye, flat)),
        ("flat ground, all tiles within 21 (retention bound)", packed(surface_eye, flat, 21)),
        ("underground 1x2 tunnel, tiles within 17", packed(tunnel_eye, tunnel)),
        ("underground, only blocks with an exposed face (<=17)", exposed(tunnel_eye, tunnel)),
    )
    for label, count in rows:
        flag = "EXCEEDS 25,000" if count > CAP else "ok"
        print(f"{label:<55} {count:>6}  {flag}")


if __name__ == "__main__":
    main()
