"""Collect identical Runtime/follow preparation spans on two isolated code trees."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import platform
import time
from unittest.mock import patch


def collect(block: int, arm: str) -> dict:
    from scripts.benchmark_d061_long_session import _FrameTimingRecorder, _statistics
    from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
    from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
    from mc2p.skills.known_world_follow_driver import KnownWorldFollowDriver
    from tests.sim import known_world_following as follow
    from tests.sim.backend import Scene
    from tests.test_player_runtime import _RecordingTrace

    samples, outcomes = [], []
    original_lane = follow.lane

    def clutter_lane(*args, **kwargs):
        scene = original_lane(*args, **kwargs)
        # Fixed, declared clutter; the central pursuit lane remains reachable.
        solids = dict(scene.solids)
        for z in range(3, 41, 3):
            for x in (-2, 2, -4, 4):
                for y in (64, 65, 66):
                    solids[(x, y, z)] = "minecraft:stone"
        return Scene(solids, scene.volume)

    for ordinal in range(3):
        recorder = _FrameTimingRecorder()
        current = {"tick": 0}
        def begin(tick, *_):
            current["tick"] = tick
            recorder.start_frame()
        def finish(tick, revised, *_):
            row = recorder.finish_frame(following_revision=revised)
            if tick > 10:
                samples.append({"scenario": ordinal, "tick": tick,
                    "revision": revised, "production_ns": row.production_ns,
                    "backend_ns": row.simulation_backend_ns,
                    "full_ns": row.full_harness_ns,
                    "segments_ns": dict(row.production_segments)})
        def production(original, label):
            def measured(*args, **kwargs):
                if not recorder.frame_active:
                    return original(*args, **kwargs)
                recorder.start_production(label)
                try:
                    return original(*args, **kwargs)
                finally:
                    recorder.finish_production(label)
            return measured
        original_backend = PlayerRuntimeV1._backend_step
        def backend(*args, **kwargs):
            if not recorder.frame_active:
                return original_backend(*args, **kwargs)
            recorder.start_backend()
            try:
                return original_backend(*args, **kwargs)
            finally:
                recorder.finish_backend()
        with ExitStack() as stack:
            stack.enter_context(patch.object(follow, "lane", clutter_lane))
            for owner, method, label in (
                (KnownWorldFollowDriver, "update", "follow_update"),
                (KnownWorldFollowDriver, "cancel", "follow_cancel"),
                (RuntimeNavigationDriver, "prepare_proposals", "navigation_prepare"),
                (RuntimeNavigationDriver, "adopt_result", "navigation_adopt"),
                (PlayerRuntimeV1, "control_frame", "runtime_control_frame"),
            ):
                stack.enter_context(patch.object(owner, method,
                    production(getattr(owner, method), label)))
            stack.enter_context(patch.object(PlayerRuntimeV1, "_backend_step", backend))
            result = follow.run_scenario_with_trace(
                follow.FollowScenario(f"r2-clutter-{ordinal}", 3.3,
                    move_ticks=180, final_hold_ticks=60), _RecordingTrace(),
                harness_frame_started=begin, harness_frame_finished=finish)
        outcomes.append(result)
    revisions = [row["production_ns"] for row in samples if row["revision"]]
    return {"schema_version": "mc2p.f2rec-r2-paired-revision.v1",
        "arm": arm, "block": block, "platform": platform.platform(),
        "warmup": "First ten frames of each independent scenario excluded",
        "timing_boundary": "D061 disjoint Runtime shell + follow update + navigation prepare/adopt; backend excluded; full separately retained",
        "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "revision_statistics": _statistics(revisions),
        "production_statistics": _statistics([r["production_ns"] for r in samples]),
        "full_statistics": _statistics([r["full_ns"] for r in samples]),
        "raw_samples": samples, "outcomes": outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=("R0", "R2"), required=True)
    parser.add_argument("--block", type=int, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = collect(args.block, args.arm)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n", "utf-8")
    print(json.dumps({"arm": args.arm, "block": args.block,
        "revision": result["revision_statistics"], "production": result["production_statistics"],
        "full": result["full_statistics"]}))


if __name__ == "__main__":
    main()
