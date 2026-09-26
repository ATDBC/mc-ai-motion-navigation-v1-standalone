"""Per-frame cost of re-sending every visible block in Observation V3.

Run from the repository root of commit 3e0c64b:

    PYTHONPATH=. python -B <review-branch>/reviews/2026-09-26-d035-repro/observation_payload_cost.py

Each frame carries N surface_depth full-cube blocks (the same N every frame,
as a steady view would).  The script times the formal decode path
(decode + profile-4 gate + snapshot) and NavigationObservationAdapter.ingest.
Profile 3 frames in the public B12-B archive carried about 33 blocks.
"""
import json, time, statistics
from mc2p.backends.client_observation_payload_v3 import (
    decode_client_observation_payload_v3, snapshot_v3_from_payload, require_formal_surface_perception)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from tests.observation_v3_fixtures import valid_payload_value, block_value, encoded

def payload(n, seq):
    v = valid_payload_value()
    v["generation_id"] = seq
    v["sample_world_tick"] = 100 + seq
    v["client_sample"].update(started_at_monotonic_ns=9000 + seq * 100, completed_at_monotonic_ns=9010 + seq * 100)
    for k in ("self_state", "inventory", "gui", "perception", "targeting", "tracked_entity"):
        v[k]["sample_world_tick"] = 100 + seq
    blocks = []
    side = int(n ** .5) + 1
    for i in range(n):
        x, z = i % side - side // 2, i // side - side // 2
        blocks.append(block_value((x, 63, z)))
    blocks.sort(key=lambda b: tuple(b["position"]))
    v["perception"]["value"]["blocks"] = blocks
    return encoded(v)

for n in (33, 300, 1500, 5000):
    adapter = NavigationObservationAdapter()
    size = None; dec = []; ing = []
    for seq in range(1, 41):
        raw = payload(n, seq); size = len(raw)
        t0 = time.perf_counter()
        decoded = decode_client_observation_payload_v3(raw)
        require_formal_surface_perception(decoded)
        snap = snapshot_v3_from_payload(decoded, episode_id="bench", request_sequence_id=seq,
            request_started_at_monotonic_ns=seq * 50_000_000, received_at_monotonic_ns=seq * 50_000_000 + 1_000_000,
            controller_clock_id="bench", source_backend="fixture")
        t1 = time.perf_counter()
        adapter.ingest(snap)
        t2 = time.perf_counter()
        if seq > 5: dec.append((t1 - t0) * 1000); ing.append((t2 - t1) * 1000)
    q = lambda xs: (round(statistics.median(xs), 2), round(sorted(xs)[int(len(xs) * .95) - 1], 2))
    print(f"{n:>5} blocks: {size/1024:7.1f} KiB/frame ({size*20/1024/1024:5.2f} MiB/s at 20 Hz); decode+snapshot P50/P95 ms {q(dec)}; adapter ingest P50/P95 ms {q(ing)}")
