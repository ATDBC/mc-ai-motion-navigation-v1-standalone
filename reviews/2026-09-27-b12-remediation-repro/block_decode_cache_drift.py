"""Round-14 review: the V3 block decode cache stops helping once the bot moves.

The cache in FabricBehaviorBackend is keyed by the full block wire record
(including position), keeps at most 4096 entries, never evicts, and is only
cleared on close().  This script feeds the same decoder a static arena and a
walk that shifts the visible window by one block every 5 frames (~walking speed).

Run from the repository root of a b740098 checkout:
    PYTHONPATH=. python -B <this script>
"""
from __future__ import annotations

import copy
import statistics
import time

from mc2p.backends.client_observation_payload_v3 import decode_client_observation_value_v3
from tests.observation_v3_fixtures import block_value, valid_payload_value

VISIBLE = 480          # close to the forest scene's visible P95 (481)
FRAMES = 400
WALK_FRAMES = 1800   # 90 s at 20 Hz, ~360 blocks walked


def frame(base, offset: int) -> dict:
    value = copy.deepcopy(base)
    blocks = [block_value((offset + i % 24, 63, i // 24)) for i in range(VISIBLE)]
    blocks.sort(key=lambda block: tuple(block["position"]))
    value["perception"]["value"]["blocks"] = blocks
    return value


def run(label: str, offsets: list[int]) -> None:
    base = valid_payload_value()
    frames = [frame(base, offset) for offset in sorted(set(offsets))]
    by_offset = {offset: frames[i] for i, offset in enumerate(sorted(set(offsets)))}
    cache: dict = {}
    times = []
    for offset in offsets:
        value = copy.deepcopy(by_offset[offset])  # the backend decodes a freshly parsed dict
        start = time.perf_counter()
        decode_client_observation_value_v3(value, block_cache=cache)
        times.append((time.perf_counter() - start) * 1000)
    early, late = times[20:100], times[-80:]
    print(f"{label:<34} cache entries {len(cache):>5}; decode P50 frames 20-100: "
          f"{statistics.median(early):5.2f} ms, last 80 frames: {statistics.median(late):5.2f} ms")


def main() -> None:
    uncached_base = valid_payload_value()
    sample = frame(uncached_base, 0)
    plain = []
    for _ in range(60):
        value = copy.deepcopy(sample)
        start = time.perf_counter()
        decode_client_observation_value_v3(value)
        plain.append((time.perf_counter() - start) * 1000)
    print(f"{'no cache':<34} decode P50 {statistics.median(plain):5.2f} ms")
    run("static arena (C1/B12 fixtures)", [0] * FRAMES)
    run("walking 90 s, 1 block per 5 frames", [i // 5 for i in range(WALK_FRAMES)])


if __name__ == "__main__":
    main()
