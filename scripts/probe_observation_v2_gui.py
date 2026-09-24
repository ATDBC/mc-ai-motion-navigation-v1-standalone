"""Prove player-action GUI open/hold/close transitions in structured-only mode."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
from typing import Mapping, Sequence
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
from mc2p.backends.craftground_runtime import (
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    prepare_runtime_sandbox,
    resolve_mc121_runtime_path,
)
from mc2p.contracts.action_v1 import ActionSnapshotV1, OpenInventoryV1, CloseScreenV1
from mc2p.contracts.observation_request_v3 import OBSERVATION_V3
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.probe_craftground_timing_parallel import run_bounded_process
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail
from scripts.client_behavior_probe_support import backend_for_sandbox, sandbox_provenance
from scripts.formal_observation_v3_evidence import validate_formal_observations_v3


DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "observation-v3-gui"
DEFAULT_PORT = 8128
DEFAULT_TIMEOUT_SECONDS = 300.0


def _check(name: str, passed: bool, actual: object, expected: object) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "actual": actual, "expected": expected}


def evaluate_gui_states(states: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    opens = [item.get("open") for item in states]
    open_states = [item for item in states if item.get("open") is True]
    closed_states = [item for item in states if item.get("open") is False]
    sync_ids = [item.get("sync_id") for item in open_states]
    revisions = [item.get("revision") for item in open_states]
    return [
        _check(
            "inventory_open_hold_close",
            opens == [False, True, True, False, False],
            opens,
            [False, True, True, False, False],
        ),
        _check(
            "open_state_has_synchronized_slots",
            bool(open_states) and all(type(item.get("slots")) is int and item["slots"] > 0 for item in open_states),
            [item.get("slots") for item in open_states],
            "all positive",
        ),
        _check(
            "open_sync_continuity",
            bool(sync_ids) and len(set(sync_ids)) == 1 and all(type(value) is int for value in revisions),
            {"sync_ids": sync_ids, "revisions": revisions},
            "one sync id and integer revisions",
        ),
        _check(
            "closed_state_has_no_stale_slots",
            bool(closed_states) and all(item.get("slots") == 0 for item in closed_states),
            [item.get("slots") for item in closed_states],
            "all zero",
        ),
    ]


def _state(observation) -> dict[str, object]:
    gui = observation.gui.value
    if gui is None:
        raise RuntimeError("GUI observation group is unavailable")
    return {
        "open": gui.open,
        "screen_kind": gui.screen_kind,
        "handler_type": gui.handler_type,
        "sync_id": gui.sync_id,
        "revision": gui.revision,
        "slots": len(gui.slots),
        "cursor_empty": gui.cursor_stack.empty,
    }


def _actions(episode: str, deadline: int) -> tuple[ActionSnapshotV1, ...]:
    return tuple(
        ActionSnapshotV1(episode, index, index, deadline, operation=operation)
        for index, operation in enumerate((OpenInventoryV1(), OpenInventoryV1(),
                                            CloseScreenV1(), CloseScreenV1()))
    )


def run_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float) -> int:
    diagnostic = sandbox / "run" / "mc2p-structured-observation.jsonl"
    offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    deadline = time.perf_counter_ns() + round(max(1.0, timeout - 15.0) * 1e9)
    backend = None
    states: list[dict[str, object]] = []
    observations: list[dict] = []
    receipts = []
    world_ticks = []
    primary_failure = None
    cleanup_failures: list[str] = []
    try:
        backend = backend_for_sandbox(sandbox, port)
        reset = backend.reset(ResetRequestV0(
            request_id="gui-reset",
            episode_id=f"gui-seed-{seed}",
            scenario_id="flat-safe",
            seed=seed,
            deadline_monotonic_ns=deadline,
        ))
        if not reset.succeeded or reset.observation is None:
            raise RuntimeError(reset.failure.message if reset.failure else "GUI reset failed")
        states.append(_state(reset.observation))
        world_ticks.append(reset.observation.world_time_ticks.value)
        projected = trace_projection(reset.observation)
        observations.append(projected)
        append_jsonl(run_dir / "observations.jsonl", projected)
        for action in _actions(f"gui-seed-{seed}", deadline):
            append_jsonl(run_dir / "requests.jsonl", trace_projection(action))
            result = backend.step(action, deadline)
            states.append(_state(result.observation))
            world_ticks.append(result.observation.world_time_ticks.value)
            receipts.append(backend.last_behavior_receipt)
            append_jsonl(run_dir / "receipts.jsonl", backend.last_behavior_receipt)
            projected = trace_projection(result.observation)
            observations.append(projected)
            append_jsonl(run_dir / "observations.jsonl", projected)
    except BaseException as error:
        primary_failure = {"type": type(error).__name__, "message": str(error) or type(error).__name__}
        (run_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception as error:
                cleanup_failures.append(f"{type(error).__name__}: {error}")
    records = []
    try:
        records = _read_diagnostic_tail(diagnostic, offset)
    except Exception as error:
        if primary_failure is None:
            primary_failure = {"type": type(error).__name__, "message": str(error)}
    cleanup = None if backend is None else backend.cleanup_status
    checks = evaluate_gui_states(states)
    checks.append(_check("five_formal_observation_v3_snapshots",
        len(observations) == 5 and not validate_formal_observations_v3(observations),
        len(observations), 5))
    checks.append(_check("formal_gui_confirmed_locally", len(receipts) == 4 and all(
        receipt.get("status") == "confirmed_local" for receipt in receipts), receipts,
        "four locally confirmed GUI operations, not server confirmation"))
    checks.append(_check("zero_action_generated_device_callbacks", len(receipts) == 4 and all(
        receipt.get("action_keyboard_callbacks") == 0 and receipt.get("action_mouse_callbacks") == 0
        for receipt in receipts), receipts, "zero keyboard/mouse callbacks"))
    checks.append(_check("handled_gui_draw_is_skipped", len(receipts) == 4 and any(
        receipt.get("handled_screen_render_attempts", 0) > 0 for receipt in receipts) and all(
        receipt.get("handled_screen_render_completions") == 0 for receipt in receipts), receipts,
        "actual handled-screen draw attempted, zero completed drawing"))
    checks.append(_check("gui_does_not_fake_world_tick_progress", len(world_ticks) == 5 and all(
        second > first for first, second in zip(world_ticks, world_ticks[1:])), world_ticks,
        "world time strictly advances; this unpaced probe does not prove 20 TPS equivalence"))
    checks.extend(
        (
            _check("five_structured_observations", len(records) == 5, len(records), 5),
            _check("zero_image_work", bool(records) and all(
                record["image_bytes"] == 0
                and record["framebuffer_capture_calls"] == 0
                and record["image_encode_calls"] == 0
                for record in records
            ), "zero" if records else "missing", "all zero"),
            _check("hidden_window_and_no_world_render", bool(records) and all(
                record["window_visible_at_creation"] is False
                and record["window_visible"] is False
                and record["render_world_completions"] == 0 for record in records
            ), "hidden" if records else "missing", "all hidden, zero renderWorld completion"),
            _check("process_stopped", cleanup is not None and cleanup.process_stopped, None if cleanup is None else cleanup.process_stopped, True),
            _check("port_released", cleanup is not None and cleanup.port_released, None if cleanup is None else cleanup.port_released, True),
        )
    )
    failed = [item["name"] for item in checks if not item["passed"]]
    if primary_failure is None and failed:
        primary_failure = {"type": "GuiEvidenceFailure", "message": f"failed checks: {failed}"}
    result = {
        "schema_version": "mc2p.observation-v3-gui-probe.v1",
        "observation_schema_version": "mc2p.observation.v3",
        "knowledge_model": "block_state_v1",
        "field_profile": "navigation_v1",
        "status": "passed" if primary_failure is None and not cleanup_failures else "failed",
        "states": states,
        "receipts": receipts,
        "world_ticks": world_ticks,
        "checks": checks,
        "primary_failure": primary_failure,
        "cleanup_failures": cleanup_failures,
    }
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8]


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--sandbox-path", type=Path)
    parser.add_argument("--time-diagnostics", action="store_true",
                        help="opt-in raw client tick/time-packet sidecar; never relax the behavior/timing gates")
    parser.add_argument("--idle-release", action="store_true",
                        help="controls-only reference check: stop sending for 1.2 seconds after a 250 ms move")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--deadline-probe", action="store_true",
                        help="inject one delayed request and require client rejection plus clean recreation")
    modes.add_argument("--slot-probe", action="store_true", help="empty-slot dispatch and stale GUI reference rejection")
    modes.add_argument("--controls-probe", action="store_true", help="direct input/look same-step behavior and cancellation")
    modes.add_argument("--reset-probe", action="store_true", help="three formal episodes in one JVM, held input and open GUI reset")
    modes.add_argument("--container-probe", action="store_true", help="reference-clock normal bonus chest and nonempty transfer/reopen")
    modes.add_argument("--furnace-probe", action="store_true", help="reference-clock normal smelting and public property synchronization")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--sandbox", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.idle_release and not args.controls_probe:
        parser.error("--idle-release requires --controls-probe")
    if args.idle_release and not args.worker and (args.sandbox_path is None or not args.time_diagnostics):
        parser.error("--idle-release requires explicit --sandbox-path and --time-diagnostics")
    if args.time_diagnostics and args.deadline_probe:
        parser.error("time diagnostics currently require one JVM and an ordered observations.jsonl; deadline uses a separate late observation")
    if args.worker:
        if args.container_probe or args.furnace_probe:
            from scripts.client_behavior_container_probe import run_container_worker
            return run_container_worker(args.run_dir.resolve(), args.sandbox.resolve(), args.seed, args.port, args.timeout_seconds,
                                        furnace=args.furnace_probe)
        if args.reset_probe:
            from scripts.client_behavior_reset_probe import run_reset_worker
            return run_reset_worker(args.run_dir.resolve(), args.sandbox.resolve(), args.seed, args.port, args.timeout_seconds)
        if args.controls_probe:
            from scripts.client_behavior_controls_probe import run_controls_worker
            return run_controls_worker(args.run_dir.resolve(), args.sandbox.resolve(), args.seed, args.port, args.timeout_seconds,
                                       idle_release=args.idle_release)
        if args.slot_probe:
            from scripts.client_behavior_slot_probe import run_slot_worker
            return run_slot_worker(args.run_dir.resolve(), args.sandbox.resolve(), args.seed, args.port, args.timeout_seconds)
        if args.deadline_probe:
            from scripts.client_behavior_failure_probe import run_deadline_worker
            return run_deadline_worker(args.run_dir.resolve(), args.sandbox.resolve(), args.seed, args.port, args.timeout_seconds)
        return run_worker(args.run_dir.resolve(), args.sandbox.resolve(), args.seed, args.port, args.timeout_seconds)
    run_dir = args.artifacts_dir.resolve() / _run_id()
    run_dir.mkdir(parents=True)
    if args.sandbox_path is None:
        if args.container_probe or args.furnace_probe:
            parser.error("container/furnace probes require an explicit reference_20_tps --sandbox-path")
        sandbox = prepare_runtime_sandbox(
            source_root=resolve_mc121_runtime_path(),
            sandbox_parent=run_dir / "sandboxes",
            sandbox_id="accelerated-structured",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
            observation_schema_version=OBSERVATION_V3,
        ).path
    else:
        sandbox = args.sandbox_path.resolve()
    try:
        provenance = sandbox_provenance(sandbox)
    except ValueError as error:
        parser.error(str(error))
    if args.idle_release and provenance["scheduling_mode"] != "reference_nonblocking_v1":
        parser.error("--idle-release requires nonblocking reference mode")
    if (args.controls_probe or args.reset_probe) and not args.time_diagnostics and (
            provenance["scheduling_mode"] in {"reference_nonblocking_v1", "lockstep_nonblocking_v1"}):
        parser.error("nonblocking controls/reset require --time-diagnostics")
    time_source = sandbox / "run/mc2p-client-time.jsonl"
    time_offset = time_source.stat().st_size if time_source.is_file() else 0
    environment = dict(os.environ)
    if args.time_diagnostics:
        environment["MC2P_TIME_DIAGNOSTICS"] = "1"
    command = [
        sys.executable, str(Path(__file__).resolve()), "--worker",
        "--run-dir", str(run_dir), "--sandbox", str(sandbox),
        "--seed", str(args.seed), "--port", str(args.port),
        "--timeout-seconds", str(args.timeout_seconds),
    ]
    if args.deadline_probe:
        command.append("--deadline-probe")
    if args.slot_probe:
        command.append("--slot-probe")
    if args.controls_probe:
        command.append("--controls-probe")
    if args.idle_release:
        command.append("--idle-release")
    if args.reset_probe:
        command.append("--reset-probe")
    if args.container_probe:
        command.append("--container-probe")
    if args.furnace_probe:
        command.append("--furnace-probe")
    supervision = run_bounded_process(
        command, cwd=PROJECT_ROOT, environment=environment,
        log_path=run_dir / "worker.log", timeout_seconds=args.timeout_seconds,
    )
    write_json_atomic(run_dir / "supervision.json", trace_projection(supervision))
    time_ok = True
    if args.time_diagnostics:
        from scripts.client_time_evidence import export_time_evidence
        time_report = export_time_evidence(time_source, time_offset, run_dir)
        time_ok = time_report["status"] == "passed"
    try:
        result = json.loads((run_dir / "result.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        result = {"status": "failed", "primary_failure": supervision.primary_failure or "worker_result_missing",
                  "cleanup_failures": list(supervision.cleanup_failures)}
        write_json_atomic(run_dir / "result.json", result)
    if (time_ok and supervision.return_code == 0 and supervision.primary_failure is None
            and not supervision.cleanup_failures and supervision.process_stopped
            and result.get("status") == "passed" and _port_free(args.port)):
        print(f"OBSERVATION_V3_GUI_RUN_DIR={run_dir}")
        print("CLIENT_FURNACE_REFERENCE_OK" if args.furnace_probe else "CLIENT_CONTAINER_REFERENCE_OK" if args.container_probe else "CLIENT_BEHAVIOR_RESET_OK" if args.reset_probe else "CLIENT_CONTROLS_OK" if args.controls_probe else "CLIENT_SLOT_REFERENCE_OK" if args.slot_probe else
              "CLIENT_BEHAVIOR_DEADLINE_OK" if args.deadline_probe else "OBSERVATION_V3_GUI_OK")
        return 0
    print(f"OBSERVATION_V3_GUI_RUN_DIR={run_dir}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
