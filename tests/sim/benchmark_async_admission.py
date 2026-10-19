"""Measure the same formal preparation path in HEAD and the working export."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from scripts.export_motion_navigation_standalone import export_tree, ROOT


def measure(repeats: int) -> dict:
    from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
    from tests.sim.runner import run
    from tests.sim.scenarios import SCENARIOS
    names = ("flat_walk", "half_steps_up_down", "direct_drop_2")
    scenes = {scene.name: scene for scene in SCENARIOS}
    original = RuntimeNavigationDriver.prepare_proposals
    elapsed, cpu = [], []
    recording = False
    def preparation(driver, *args, **kwargs):
        started, thread = time.perf_counter_ns(), time.thread_time_ns()
        try:
            return original(driver, *args, **kwargs)
        finally:
            if recording:
                elapsed.append(time.perf_counter_ns() - started)
                cpu.append(time.thread_time_ns() - thread)
    with patch.object(RuntimeNavigationDriver, "prepare_proposals", preparation):
        for repeat in range(repeats + 1):
            recording = repeat > 0
            for name in names:
                result = run(scenes[name])
                if result.violations or result.outcome != result.expect:
                    raise ValueError(f"benchmark scenario failed: {name}/{result.reason}")
    def statistics(samples):
        values = sorted(samples)
        return {"samples": len(values), "p50_ms": values[math.ceil(.50 * len(values)) - 1] / 1e6,
                "p95_ms": values[math.ceil(.95 * len(values)) - 1] / 1e6,
                "p99_ms": values[math.ceil(.99 * len(values)) - 1] / 1e6,
                "maximum_ms": values[-1] / 1e6}
    return {"scenes": names, "repeats": repeats, "warmup": 1,
            "scope": "RuntimeNavigationDriver.prepare_proposals; includes inline test workers; excludes game/IPC and trace serialization",
            "elapsed": statistics(elapsed), "thread_cpu": statistics(cpu)}


def compare(output: Path, repeats: int) -> dict:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite benchmark evidence: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r27-perf-", dir=ROOT / ".tmp") as temporary:
        root = Path(temporary) / "export"
        export_tree(root, clean=False)
        def worker():
            result = subprocess.run([sys.executable, "-m", "tests.sim.benchmark_async_admission",
                                     "--worker", "--repeats", str(repeats)], cwd=root,
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return json.loads(result.stdout)
        current = worker()
        modified = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "mc2p", "tests/sim"],
                                  cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        for relative in modified:
            if not relative.endswith(".py"):
                continue
            content = subprocess.run(["git", "show", "HEAD:" + relative], cwd=ROOT, capture_output=True, check=True).stdout
            root.joinpath(relative).write_bytes(content)
        baseline = worker()
    result = {"schema_version": "mc2p.r27-control-preparation-benchmark.v1",
              "baseline_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
              "baseline": baseline, "current": current}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("repeats must be 1..20")
    if not args.worker and args.output is None:
        parser.error("--output is required")
    print(json.dumps(measure(args.repeats) if args.worker else compare(args.output, args.repeats)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
