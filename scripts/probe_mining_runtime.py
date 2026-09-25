"""Supervised reference-speed Runtime mining, interruption, pickup and placement probe."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}: sys.path.insert(0, str(ROOT))

from mc2p.backends.craftground_runtime import (CraftGroundClockModeV0, validate_sandbox_for_mode,
    resolve_mc121_runtime_path, capture_runtime_source_fingerprints)
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import JsonlTraceWriterV0, trace_projection
from scripts.client_behavior_probe_support import backend_for_sandbox, sandbox_provenance
from scripts.client_time_evidence import export_time_evidence, _read_jsonl
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.mining_runtime_core import run_mining_scenario, evaluate_mining
from scripts.block_parity_evidence import frozen_probe_sources
from mc2p.skills.perception_needs import PerceptionConfig
from scripts.probe_craftground_timing_parallel import run_bounded_process
from scripts.probe_observation_v2_gui import _port_free, _run_id
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail
from scripts.smoke_test_player_runtime_v1 import evaluate_trace, finalize_result
from scripts.visibility_fixture_world import _no_links


def run_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float) -> int:
    validate_sandbox_for_mode(sandbox, CraftGroundClockModeV0.REFERENCE_20_TPS)
    provenance = sandbox_provenance(sandbox)
    if provenance["scheduling_mode"] != "reference_nonblocking_v1":
        raise ValueError("mining requires the nonblocking reference client")
    source = resolve_mc121_runtime_path()
    before = capture_runtime_source_fingerprints(source)
    core_before = frozen_probe_sources()
    perception_config = trace_projection(PerceptionConfig())
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    timing_source = sandbox / "run/mc2p-client-time.jsonl"
    diagnostic_offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    timing_offset = timing_source.stat().st_size if timing_source.is_file() else 0
    deadline = time.perf_counter_ns() + round((timeout - 20) * 1e9)
    backend = runtime = None
    failure, cleanup_failures, stages, checks = None, [], {}, []
    records, observations, receipts = [], [], []
    try:
        backend = backend_for_sandbox(sandbox, port)
        runtime = PlayerRuntimeV1(backend, JsonlTraceWriterV0(run_dir / "trace.jsonl"))
        reset = runtime.reset(ResetRequestV0("mining-reset", f"mining-{seed}", "flat-safe", seed, deadline))
        if not reset.succeeded: raise RuntimeError(str(reset.failure))
        stages = run_mining_scenario(runtime, run_dir, deadline)
    except Exception as error:
        failure = dict(type=type(error).__name__, message=str(error))
        (run_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if runtime is not None:
            runtime.close()
            cleanup_failures.extend(trace_projection(runtime.cleanup_failures))
        elif backend is not None:
            try: backend.close()
            except Exception as error: cleanup_failures.append(str(error))
        exit_log = sandbox / "run/logs/latest.log"
        if exit_log.is_file(): shutil.copyfile(exit_log, run_dir / "minecraft-exit.log")
    try:
        path = run_dir / "mining-stages.json"
        if path.is_file(): stages = json.loads(path.read_text("utf-8"))
        records = _read_jsonl(run_dir / "trace.jsonl")
        reset = [r["payload"]["result"] for r in records if r["record_type"] == "reset"]
        steps = [r["payload"] for r in records if r["record_type"] == "step"]
        observations = [r["observation"] for r in reset] + [r["backend_result"]["observation"] for r in steps]
        receipts = [r["backend_result"]["receipt"] for r in steps]
        for obs in observations: append_jsonl(run_dir / "observations.jsonl", obs)
        for receipt in receipts: append_jsonl(run_dir / "receipts.jsonl", receipt)
        rows = _read_diagnostic_tail(diagnostic, diagnostic_offset)
        for row in rows: append_jsonl(run_dir / "jvm-diagnostics.jsonl", row)
        timing = export_time_evidence(timing_source, timing_offset, run_dir, observations=observations)
        checks.append(dict(name="complete_native_time_attribution", passed=timing["status"] == "passed"))
        checks.extend(evaluate_trace(records, rows, expected_steps=len(steps), scheduling_mode=provenance["scheduling_mode"],
                                     time_events=_read_jsonl(run_dir / "time-events.jsonl")))
        checks.extend(evaluate_mining(records, stages))
    except Exception as error:
        failure = failure or dict(type=type(error).__name__, message=str(error))
    cleanup = backend.cleanup_status if backend is not None else None
    core_after = frozen_probe_sources()
    checks.extend([
        dict(name='v3_python_and_java_sources_unchanged', passed=core_before == core_after),
        dict(name="backend_process_and_port_clean", passed=cleanup is not None and cleanup.process_stopped and cleanup.port_released),
        dict(name="installed_runtime_sources_unchanged", passed=before == capture_runtime_source_fingerprints(source)),
    ])
    if failure is None and not all(c["passed"] for c in checks):
        failure = dict(type="MiningEvidenceFailure", message=str([c["name"] for c in checks if not c["passed"]]))
    result = dict(schema_version="mc2p.mining-runtime-probe.v1", status="passed" if failure is None and not cleanup_failures else "failed",
        seed=seed, stages=stages, checks=checks, observation_count=len(observations), receipt_count=len(receipts),
        primary_failure=failure, cleanup_failures=cleanup_failures, backend_cleanup=trace_projection(cleanup), provenance=provenance,
        core_sources_before=core_before, core_sources_after=core_after,
        perception_variant='active_perception_v1', perception_config=perception_config,
        limits=["reference survival surface mining only; not full training or timing equivalence",
                "grass drops dirt; placing dirt does not restore grass", "pending receipt alone is not server confirmation"])
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-path", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=21001)
    parser.add_argument("--port", type=int, default=8128)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--artifacts-dir", type=Path, default=ROOT / "artifacts/mining-runtime")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds < 60 or not 1 <= args.port <= 65535:
        parser.error("timeout must be finite and >=60; port must be in 1..65535")
    sandbox = args.sandbox_path.absolute()
    _no_links(sandbox)
    validate_sandbox_for_mode(sandbox, CraftGroundClockModeV0.REFERENCE_20_TPS)
    if args.worker:
        if args.run_dir is None: parser.error("worker requires run-dir")
        _no_links(args.run_dir.absolute())
        return run_worker(args.run_dir.absolute(), sandbox, args.seed, args.port, args.timeout_seconds)
    if not _port_free(args.port): parser.error("control port occupied")
    root, ancestor = args.artifacts_dir.absolute(), args.artifacts_dir.absolute()
    while not ancestor.exists():
        if ancestor.parent == ancestor: parser.error("output volume missing")
        ancestor = ancestor.parent
    _no_links(ancestor)
    directory = root / _run_id()
    directory.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run-dir", str(directory),
        "--sandbox-path", str(sandbox), "--seed", str(args.seed), "--port", str(args.port), "--timeout-seconds", str(args.timeout_seconds)]
    supervision = run_bounded_process(command, cwd=ROOT, environment=dict(os.environ, MC2P_TIME_DIAGNOSTICS="1"),
                                      log_path=directory / "worker.log", timeout_seconds=args.timeout_seconds)
    write_json_atomic(directory / "supervision.json", trace_projection(supervision))
    path = directory / "result.json"
    worker = json.loads(path.read_text("utf-8")) if path.is_file() else dict(status="failed", primary_failure="worker_result_missing", checks=[])
    write_json_atomic(directory / "worker-result.json", worker)
    result = finalize_result(worker, supervision, _port_free(args.port))
    write_json_atomic(path, result)
    print(f"MINING_RUNTIME_RUN_DIR={directory}")
    if result["status"] == "passed":
        print("MINING_RUNTIME_REFERENCE_OK")
        return 0
    return 1


if __name__ == "__main__": raise SystemExit(main())
