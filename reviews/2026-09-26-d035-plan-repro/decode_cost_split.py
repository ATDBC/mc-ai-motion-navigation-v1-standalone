"""How much of Observation V3 decode time is JSON parsing.

Run from the repository root of commit 3e0c64b:

    PYTHONPATH=. python -B <review-branch>/reviews/2026-09-26-d035-plan-repro/decode_cost_split.py
"""
from __future__ import annotations

import json
import statistics
import time

from mc2p.backends.client_observation_payload_v3 import (
    decode_client_observation_payload_v3,
)
from tests.observation_v3_fixtures import block_value, encoded, valid_payload_value


def payload(count: int) -> bytes:
    value = valid_payload_value()
    side = int(count ** .5) + 1
    blocks = [block_value((i % side - side // 2, 63, i // side - side // 2))
              for i in range(count)]
    blocks.sort(key=lambda block: tuple(block["position"]))
    value["perception"]["value"]["blocks"] = blocks
    return encoded(value)


def main() -> None:
    for count in (300, 1500):
        raw = payload(count)
        parse, full = [], []
        for _ in range(30):
            start = time.perf_counter()
            json.loads(raw)
            parsed = time.perf_counter()
            decode_client_observation_payload_v3(raw)
            done = time.perf_counter()
            parse.append((parsed - start) * 1000)
            full.append((done - parsed) * 1000)
        print(f"{count:>5} blocks: json.loads P50 {statistics.median(parse):.2f} ms, "
              f"full V3 decode P50 {statistics.median(full):.2f} ms")


if __name__ == "__main__":
    main()
