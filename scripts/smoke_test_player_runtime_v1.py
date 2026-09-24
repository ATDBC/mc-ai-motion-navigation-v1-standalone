"""Supervised real-game formal Runtime smoke; no direct backend step calls in the scenario."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.backends.craftground_runtime import (CraftGroundClockModeV0, capture_runtime_source_fingerprints,
    resolve_mc121_runtime_path, validate_sandbox_for_mode)
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
from mc2p.contracts.action_v1 import ActionIntentV1, ClickSlotV1, CloseScreenV1, LookV1, MovementV1, OpenInventoryV1, SelectHotbarV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.report import ExecutionStatusV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.task import TaskIntentV0, SuccessCriterionV0, ComparisonOperatorV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import JsonlTraceWriterV0, trace_projection
from scripts.client_behavior_probe_support import backend_for_sandbox, sandbox_provenance
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.probe_craftground_timing_parallel import run_bounded_process
from scripts.probe_observation_v2_gui import _port_free, _run_id
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail
from scripts.formal_observation_v3_evidence import validate_formal_observations_v3
from scripts.smoke_test_player_runtime import _formal_observation_violations
from scripts.visibility_fixture_world import _no_links

STAGES = ("start", "moving", "released", "open", "open_again", "closed", "closed_again",
          "reopened", "stale_rejected", "recovered", "hotbar", "no_repeat", "cancelled")


def evaluate_stages(stages: dict) -> list[dict]:
    checks = {"complete_ordered_stages": set(stages) == set(STAGES)}
    if not checks["complete_ordered_stages"]:
        return [{"name": k, "passed": v} for k, v in checks.items()]
    s = stages
    sequences = [s[k]["sequence"] for k in STAGES]
    checks.update({
        "complete_ordered_stages": all(a < b for a, b in zip(sequences, sequences[1:])),
        "movement_has_real_effect": math.hypot(s["moving"]["x"] - s["start"]["x"], s["moving"]["z"] - s["start"]["z"]) > .1,
        "look_delta_applied_once": abs((s["moving"]["yaw"] - s["start"]["yaw"] + 180) % 360 - 180 - 12) < 1e-5,
        "cancelled_camera_never_replays": all(abs((s[k]["yaw"] - s["moving"]["yaw"] + 180) % 360 - 180) < 1e-5
                                              for k in STAGES[2:]),
        "source_cancel_releases_motion": s["released"]["speed"] < .01 and s["released"]["speed"] < s["moving"]["speed"],
        "inventory_repeated_open_close": [s[k]["gui_open"] for k in ("open", "open_again", "closed", "closed_again", "reopened")]
            == [True, True, False, False, True] and s["open"]["gui_session"] == s["open_again"]["gui_session"]
            and s["open"]["gui_session"] != s["reopened"]["gui_session"],
        "stale_request_rejected_then_recovered": s["stale_rejected"]["receipt_status"] == "rejected"
            and s["stale_rejected"]["reason"] == "stale_gui_session" and s["stale_rejected"]["status"] == "failed"
            and s["stale_rejected"]["gui_open"] and s["stale_rejected"]["gui_session"] == s["reopened"]["gui_session"]
            and not s["recovered"]["gui_open"] and s["recovered"]["status"] == "running",
        "hotbar_pending_is_not_task_success": s["hotbar"]["hotbar"] == 2
            and s["hotbar"]["receipt_status"] == "pending_confirmation" and s["hotbar"]["status"] == "running"
            and s["no_repeat"]["operation"] is None and s["no_repeat"]["hotbar"] == 2,
        "final_cancel_locally_accepted": s["cancelled"]["status"] == "cancelled"
            and s["cancelled"]["receipt_status"] in {"executed", "confirmed_local", "cancelled"}
            and s["cancelled"]["speed"] < .01,
    })
    return [{"name": k, "passed": bool(v)} for k, v in checks.items()]


def finalize_result(worker: dict, supervision, port_free: bool) -> dict:
    result = dict(worker)
    okay = (supervision.return_code == 0 and supervision.primary_failure is None
            and not supervision.cleanup_failures and supervision.process_stopped and port_free)
    result["checks"] = list(worker.get("checks", [])) + [{"name": "parent_supervision_and_port_clean", "passed": bool(okay)}]
    result["parent_supervision"] = trace_projection(supervision)
    result["parent_port_free"] = port_free
    if not okay:
        result["status"] = "failed"
        result["primary_failure"] = worker.get("primary_failure") or {"type": "SupervisorFailure", "message": str(supervision)}
        result["cleanup_failures"] = list(worker.get("cleanup_failures", [])) + list(supervision.cleanup_failures)
    return result


def evaluate_trace(records: list[dict], rows: list[dict], *, scheduling_mode: str = "legacy_step_v0",
                   time_events: list[dict] | None = None, expected_steps: int = 28) -> list[dict]:
    """Audit persisted evidence, including dispatch-before-result causal order."""
    try:
        if type(expected_steps) is not int or not 1 <= expected_steps <= 2400:
            raise ValueError("expected step count must be bounded and positive")
        if scheduling_mode not in {"legacy_step_v0", "reference_nonblocking_v1", "lockstep_nonblocking_v1"}:
            raise ValueError("unknown scheduling evidence mode")
        reference = scheduling_mode == "reference_nonblocking_v1"
        reset = [r["payload"]["result"] for r in records if r["record_type"] == "reset"]
        steps = [r["payload"] for r in records if r["record_type"] == "step"]
        observations = [r["observation"] for r in reset] + [r["backend_result"]["observation"] for r in steps]
        receipts = [ClientBehaviorReceiptV2.from_mapping(r["backend_result"]["receipt"]) for r in steps]
        dispatch = [r["payload"]["decision"] for r in records if r["record_type"] == "dispatch"]
        order = [r["record_type"] for r in records if r["record_type"] in {"reset", "dispatch", "step"}]
        checks = {
            f"one_reset_{expected_steps}_arbitrated_steps": len(reset) == 1 and len(steps) == len(dispatch) == expected_steps
                and dispatch == [r["decision"] for r in steps] and order == ["reset"] + ["dispatch", "step"] * expected_steps,
            "exact_continuous_formal_observations_v3": not validate_formal_observations_v3(observations)
                and len(observations) == expected_steps + 1 and [o["sequence_id"] for o in observations] == list(range(expected_steps + 1))
                and len({o["episode_id"] for o in observations}) == 1,
            "formal_v1_requests_receipts_and_input_samples": len(receipts) == expected_steps and all(
                p["decision"]["action"]["schema_version"] == "mc2p.action-snapshot.v1"
                and p["decision"]["action"]["request_sequence_id"] == i == receipts[i].request_sequence_id
                and p["decision"]["action"]["observation_sequence_id"] == i
                and p["decision"]["action"]["episode_id"] == receipts[i].episode_id == observations[i + 1]["episode_id"]
                and observations[i + 1]["request_sequence_id"] == i
                and receipts[i].world_tick == observations[i + 1]["world_time_ticks"]["value"]
                and receipts[i].generation_id == i + 1 and (reference or receipts[i].input_samples == i + 1)
                and receipts[i].on_client_thread and receipts[i].status != "idle"
                and receipts[i].action_keyboard_callbacks == receipts[i].action_mouse_callbacks == 0
                and receipts[i].handled_screen_render_completions == 0 for i, p in enumerate(steps)),
            "formal_trace_has_no_image_or_binary_projection": not _formal_observation_violations(records, path="trace"),
            "continuous_zero_image_hidden_client": len(rows) == expected_steps + 1 and len({r["session_id"] for r in rows}) == 1
                and [r["observation_sequence"] for r in rows] == list(range(1, expected_steps + 2)) and all(
                    r["image_bytes"] == r["framebuffer_capture_calls"] == r["image_encode_calls"] == r["render_world_completions"] == 0
                    and r["window_visible"] is False and r["window_visible_at_creation"] is False for r in rows),
        }
        result = [{"name": k, "passed": bool(v)} for k, v in checks.items()]
        if reference:
            from scripts.reference_input_evidence import evaluate_reference_input_evidence
            result.append(evaluate_reference_input_evidence(
                [r["backend_result"]["receipt"] for r in steps], observations, time_events or []))
        return result
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return [{"name": "well_formed_trace_evidence", "passed": False, "detail": str(error)}]


def run_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float) -> int:
    provenance = sandbox_provenance(sandbox)
    time_source = sandbox / "run/mc2p-client-time.jsonl"
    time_offset = time_source.stat().st_size if time_source.is_file() else 0
    mode = CraftGroundClockModeV0(provenance["clock_mode"])
    if mode not in (CraftGroundClockModeV0.REFERENCE_20_TPS, CraftGroundClockModeV0.LOCKSTEP_ACCELERATED):
        raise ValueError("Runtime probe requires reference or lockstep mode")
    validate_sandbox_for_mode(sandbox, mode)
    clock_source = sandbox / "run/mc2p-lockstep.jsonl"
    clock_offset = clock_source.stat().st_size if clock_source.is_file() else 0
    deadline = time.perf_counter_ns() + round((timeout - 15) * 1e9)
    source = resolve_mc121_runtime_path()
    source_before = capture_runtime_source_fingerprints(source)
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    backend = runtime = None
    failure, cleanup_failures, stages = None, [], {}
    trace_path = run_dir / "trace.jsonl"
    try:
        backend = backend_for_sandbox(sandbox, port)
        runtime = PlayerRuntimeV1(backend, JsonlTraceWriterV0(trace_path))
        episode = f"runtime-v1-{seed}"
        task = TaskIntentV0("runtime-v1-smoke", "control-gui-probe", "{}",
            (SuccessCriterionV0("horizontal_displacement", ComparisonOperatorV0.GREATER_THAN, .1, "blocks"),),
            200, deadline, True, 0.0)
        profile = BehaviorProfileV0()
        result = runtime.reset(ResetRequestV0("runtime-reset", episode, "flat-safe", seed, deadline))
        if not result.succeeded: raise RuntimeError(str(result.failure))
        last = None
        counter = 0

        def submit(source_id, **kwargs):
            nonlocal counter
            counter += 1
            runtime.submit_intent(ActionIntentV1(f"intent-{counter}", source_id, episode,
                runtime.observation.sequence_id, ActionPriorityV0.TASK, time.perf_counter_ns(), deadline, **kwargs))

        def step(operation=None, *, expect=ExecutionStatusV0.RUNNING):
            nonlocal last
            if operation is not None: submit("gui", operation=operation)
            last = runtime.step(task, profile, deadline)
            if last.report.status is not expect or last.observation is None:
                raise RuntimeError(f"unexpected Runtime result: {last.report}")

        def mark(label):
            obs = runtime.observation
            p, v, gui = obs.position.value, obs.self_state.value.velocity, obs.gui.value
            receipt = None if last is None else last.backend_result.receipt
            op = None if last is None else last.decision.action.operation
            value = {"sequence": obs.sequence_id, "x": p.x, "z": p.z, "yaw": obs.yaw_degrees.value,
                "speed": math.hypot(v.x, v.z), "gui_open": gui.open, "gui_session": gui.gui_session_id,
                "hotbar": obs.inventory.value.selected_hotbar_slot,
                "status": None if last is None else last.report.status.value,
                "receipt_status": None if receipt is None else receipt.status,
                "reason": None if receipt is None else receipt.reason, "operation": None if op is None else op.kind}
            stages[label] = value
            append_jsonl(run_dir / "stages.jsonl", {"label": label, "state": value})
            print(f"RUNTIME_V1_STAGE={label} sequence={obs.sequence_id}", flush=True)

        mark("start")
        submit("movement", movement=MovementV1(forward=1))
        submit("camera", look=LookV1(12, 0))
        for _ in range(11): step()
        submit("camera", look=LookV1(90, 0))
        runtime.cancel_source("camera")
        step()  # Observe the cancellation while the independent movement source remains active.
        mark("moving")
        runtime.cancel_source("movement")
        for _ in range(6): step()
        mark("released")
        step(OpenInventoryV1()); mark("open")
        old = runtime.observation.gui.value
        step(OpenInventoryV1()); mark("open_again")
        step(CloseScreenV1()); mark("closed")
        step(CloseScreenV1()); mark("closed_again")
        step(OpenInventoryV1()); mark("reopened")
        step(ClickSlotV1(old.gui_session_id, old.sync_id, old.revision, 0, 0, "pickup"), expect=ExecutionStatusV0.FAILED)
        mark("stale_rejected")
        step(CloseScreenV1()); mark("recovered")
        step(SelectHotbarV1(2)); mark("hotbar")
        step(); mark("no_repeat")
        runtime.cancel("player_stop")
        step(expect=ExecutionStatusV0.CANCELLED); mark("cancelled")
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
        (run_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if runtime is not None:
            runtime.close()
            cleanup_failures.extend(trace_projection(runtime.cleanup_failures))
        elif backend is not None:
            try: backend.close()
            except Exception as error: cleanup_failures.append(str(error))
    checks = evaluate_stages(stages)
    observations, receipts = [], []
    try:
        records = [json.loads(line) for line in trace_path.read_text("utf-8").splitlines()]
        reset = [r["payload"]["result"] for r in records if r["record_type"] == "reset"]
        steps = [r["payload"] for r in records if r["record_type"] == "step"]
        observations = [r["observation"] for r in reset] + [r["backend_result"]["observation"] for r in steps]
        receipts = [r["backend_result"]["receipt"] for r in steps]
        for obs in observations: append_jsonl(run_dir / "observations.jsonl", obs)
        for receipt in receipts: append_jsonl(run_dir / "receipts.jsonl", receipt)
        rows = _read_diagnostic_tail(diagnostic, offset)
        for row in rows: append_jsonl(run_dir / "jvm-diagnostics.jsonl", row)
        events = None
        if provenance["scheduling_mode"] in {"reference_nonblocking_v1", "lockstep_nonblocking_v1"}:
            from scripts.client_time_evidence import export_time_evidence, _read_jsonl
            timing = export_time_evidence(time_source, time_offset, run_dir, observations=observations)
            checks.append({"name": "complete_native_time_attribution", "passed": timing["status"] == "passed"})
            if timing["status"] == "passed":
                events = _read_jsonl(run_dir / "time-events.jsonl")
        checks.extend(evaluate_trace(records, rows, scheduling_mode=provenance["scheduling_mode"], time_events=events))
        if mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED:
            from scripts.client_time_evidence import evaluate_lockstep_world_ticks
            checks.append(evaluate_lockstep_world_ticks(events or [], observations))
            from scripts.client_behavior_probe_support import evaluate_behavior_lockstep
            from scripts.probe_craftground_timing_parallel import _capture_lockstep_trace_segment
            from scripts.timing_parallel_probe_core import parse_lockstep_trace_lines
            segment = run_dir / "lockstep-events.jsonl"
            _capture_lockstep_trace_segment(sandbox, clock_offset, segment)
            clocks = parse_lockstep_trace_lines(segment.read_text("utf-8").splitlines())
            checks.extend(evaluate_behavior_lockstep([o["world_time_ticks"]["value"] for o in observations],
                clocks, action_count=28))
    except Exception as error:
        failure = failure or {"type": type(error).__name__, "message": str(error)}
    cleanup = backend.cleanup_status if backend is not None else None
    checks.extend([
        {"name": "backend_process_and_port_clean", "passed": cleanup is not None and cleanup.process_stopped and cleanup.port_released},
        {"name": "installed_runtime_sources_unchanged", "passed": source_before == capture_runtime_source_fingerprints(source)},
    ])
    if failure is None and not all(c["passed"] for c in checks):
        failure = {"type": "RuntimeEvidenceFailure", "message": str([c["name"] for c in checks if not c["passed"]])}
    result = {"schema_version": "mc2p.runtime-v1-smoke.v2", "observation_schema_version": "mc2p.observation.v3",
        "knowledge_model": "block_state_v1", "field_profile": "navigation_v1",
        "status": "passed" if failure is None and not cleanup_failures else "failed",
        "seed": seed, "checks": checks, "stages": stages, "observation_count": len(observations), "receipt_count": len(receipts),
        "primary_failure": failure, "cleanup_failures": cleanup_failures, "provenance": provenance,
        "backend_cleanup": trace_projection(cleanup), "source_fingerprints": source_before,
        "limits": ["not standalone deployment or complete timing equivalence", "cooperative cancellation; blocking IPC still bounded by supervisor",
                   "full equipment visibility and attack/use/mining gates remain open; no training"]}
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-path", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=21001)
    parser.add_argument("--port", type=int, default=8128)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--artifacts-dir", type=Path, default=ROOT / "artifacts/player-runtime-v1")
    parser.add_argument("--time-diagnostics", action="store_true", help="read-only native timing evidence (required for nonblocking reference)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds < 60:
        parser.error("timeout must be finite and at least 60 seconds")
    sandbox = args.sandbox_path.absolute()
    _no_links(sandbox)
    try:
        provenance = sandbox_provenance(sandbox)
    except ValueError as error:
        parser.error(str(error))
    if args.worker:
        if args.run_dir is None: parser.error("worker requires --run-dir")
        _no_links(args.run_dir.absolute())
        return run_worker(args.run_dir.absolute(), sandbox, args.seed, args.port, args.timeout_seconds)
    if not args.time_diagnostics and provenance["scheduling_mode"] in {
            "reference_nonblocking_v1", "lockstep_nonblocking_v1"}:
        parser.error("nonblocking Runtime requires --time-diagnostics")
    if not _port_free(args.port): parser.error("control port is occupied")
    root = args.artifacts_dir.absolute()
    ancestor = root
    while not ancestor.exists():
        if ancestor.parent == ancestor: parser.error("output volume does not exist")
        ancestor = ancestor.parent
    _no_links(ancestor)
    run_dir = root / _run_id()
    run_dir.mkdir(parents=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run-dir", str(run_dir),
        "--sandbox-path", str(sandbox), "--seed", str(args.seed), "--port", str(args.port), "--timeout-seconds", str(args.timeout_seconds)]
    environment = dict(os.environ)
    if args.time_diagnostics:
        environment["MC2P_TIME_DIAGNOSTICS"] = "1"
    supervision = run_bounded_process(command, cwd=ROOT, environment=environment,
                                      log_path=run_dir / "worker.log", timeout_seconds=args.timeout_seconds)
    write_json_atomic(run_dir / "supervision.json", trace_projection(supervision))
    path = run_dir / "result.json"
    worker = json.loads(path.read_text("utf-8")) if path.is_file() else {
        "status": "failed", "primary_failure": "worker_result_missing", "cleanup_failures": [], "checks": []}
    write_json_atomic(run_dir / "worker-result.json", worker)
    result = finalize_result(worker, supervision, _port_free(args.port))
    write_json_atomic(path, result)
    print(f"PLAYER_RUNTIME_V1_RUN_DIR={run_dir}")
    if result["status"] == "passed":
        print("PLAYER_RUNTIME_V1_SMOKE_OK")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
