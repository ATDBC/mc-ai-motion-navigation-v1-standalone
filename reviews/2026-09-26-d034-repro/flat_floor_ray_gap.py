"""Why the induced-turn trial waited: a gap in the vertical ray sampling.

Run from the repository root of commit cba2899:

    python -B <this-review-branch>/reviews/2026-09-26-d034-repro/flat_floor_ray_gap.py \
        evidence/motion_navigation/representative-v1/archives/b12b-pass-20260926T091904725912Z-deb6726e.tar.gz

Part 1 reads the formal ray grid from ``ClientObservationCollector.java`` and
prints where its downward rows meet a flat floor for a standing player with
level gaze.  Part 2 reads the public B12-B archive and prints which floor rows
straight ahead (x = 0) the robot actually observed while it waited at
(0.5, 100, -2.5) before the induced-turn trial moved.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import math
from pathlib import Path
import re
import sys
import tarfile

COLLECTOR = Path(
    "mc2p/backends/runtime_overlays/mc121_observation/ClientObservationCollector.java"
)
EYE_HEIGHT = 1.62


def ray_grid() -> tuple[int, float, float]:
    source = COLLECTOR.read_text(encoding="utf-8")
    rows = int(re.search(r"RAY_ROWS\s*=\s*(\d+)", source).group(1))
    fov = float(re.search(r"VERTICAL_FOV\s*=\s*([\d.]+)", source).group(1))
    reach = float(re.search(r"BLOCK_MAX_DISTANCE\s*=\s*([\d.]+)", source).group(1))
    return rows, fov, reach


def main(archive_path: str) -> None:
    rows, fov, reach = ray_grid()
    offsets = [-fov / 2 + row * fov / (rows - 1) for row in range(rows)]
    print(f"ray grid: {rows} rows over {fov:.0f} deg -> pitch offsets {offsets}")
    for pitch in (0.0, 1.9551898):
        hits = [
            EYE_HEIGHT / math.tan(math.radians(pitch + offset))
            for offset in offsets
            if pitch + offset > 0
            and EYE_HEIGHT / math.sin(math.radians(pitch + offset)) <= reach
        ]
        print(f"  gaze pitch {pitch:.2f} deg: flat floor hit at horizontal "
              f"distances {[round(h, 2) for h in sorted(hits)]} blocks")
    print("  from z = -2.5 facing +z the 2.81 and 6.05 arcs land in floor rows "
          "z = 0 and z = 3; rows z = 1 and z = 2 lie between them")

    steps = {}
    with tarfile.open(archive_path) as archive:
        for name in sorted(
            member.name for member in archive.getmembers()
            if member.isfile() and "runtime-trace/trace/segment" in member.name
        ):
            for line in archive.extractfile(name):
                record = json.loads(line)
                if record.get("record_type") != "step":
                    continue
                observation = (
                    (record.get("payload") or {}).get("backend_result") or {}
                ).get("observation") or {}
                if observation.get("sequence_id") is not None:
                    steps[observation["sequence_id"]] = observation

    groups: "OrderedDict[tuple, list[int]]" = OrderedDict()  # key -> frames
    for sequence in range(609, 654):
        observation = steps[sequence]
        own = (observation.get("self_state") or {}).get("value") or {}
        blocks = ((observation.get("perception") or {}).get("value") or {}).get(
            "blocks") or []
        floor = tuple(sorted(
            block["position"][2] for block in blocks
            if block["position"][0] == 0 and block["position"][1] == 99
        ))
        key = (round(own["position"]["z"], 2), own.get("yaw_degrees"),
               round(own.get("pitch_degrees") or 0.0, 2), floor)
        groups.setdefault(key, []).append(sequence)
    print("\nobserved floor rows at x = 0 (scan 609-613, wait 614-653):")
    for (z, yaw, pitch, floor), sequences in groups.items():
        print(f"  {len(sequences):>2} frames ({_ranges(sequences)}): body z={z} "
              f"yaw={yaw} pitch={pitch} -> floor z rows {list(floor)}")


def _ranges(values: list[int]) -> str:
    parts = []
    start = previous = values[0]
    for value in values[1:] + [None]:
        if value is not None and value == previous + 1:
            previous = value
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        if value is not None:
            start = previous = value
    return ", ".join(parts)


if __name__ == "__main__":
    main(sys.argv[1])
