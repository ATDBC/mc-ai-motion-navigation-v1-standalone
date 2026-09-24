"""Same-step direct input/look behavior probe. Unpaced results are not timing equivalence."""
from __future__ import annotations

import math
from pathlib import Path
import statistics
import time

from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1, LookV1, OpenInventoryV1, CloseScreenV1
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail
from scripts.client_behavior_probe_support import (
    backend_for_sandbox, sandbox_provenance, evaluate_behavior_lockstep, evaluate_input_samples,
)
from scripts.probe_craftground_timing_parallel import _capture_lockstep_trace_segment
from scripts.timing_parallel_probe_core import parse_lockstep_trace_lines


def evaluate_controls_metrics(metrics: dict) -> list[dict]:
    def number(name):
        value = metrics.get(name)
        return value if type(value) in (float, int) and math.isfinite(value) else float("nan")
    walk = number("walk_median")
    checks = {
        "first_forward_distance": 0.01 < number("first_forward_distance") < 1,
        "yaw_delta": abs(number("yaw_delta") - 30) < 0.01,
        "pitch_delta": abs(number("pitch_delta") - 10) < 0.01,
        "walk_median": 0.05 < walk < 0.5,
        "sprint_median": walk * 1.05 < number("sprint_median") < walk * 2,
        "sneak_median": 0 < number("sneak_median") < walk * 0.75,
        "jump_height": 0.3 < number("jump_height") < 2,
        "release_tail": 0 <= number("release_tail") < walk * 0.1,
        **{name: metrics.get(name) is True for name in ("cancel_confirmed", "gui_conflict_rejected",
                  "gui_closed", "all_alive", "expected_receipts")},
        "device_callbacks": type(metrics.get("device_callbacks")) is int and metrics["device_callbacks"] == 0,
    }
    return [{"name": key, "passed": passed, "actual": metrics.get(key)} for key, passed in checks.items()]


def _state(observation):
    return {"position": trace_projection(observation.position.value), "yaw": observation.yaw_degrees.value,
            "pitch": observation.pitch_degrees.value, "gui_open": observation.gui.value.open,
            "dead": observation.is_dead.value, "world_tick": observation.world_time_ticks.value}


def summarize_controls(rows: list[dict]) -> dict:
    def chosen(label): return [r for r in rows if r["label"] == label]
    def distance(row):
        a, b = row["before"]["position"], row["after"]["position"]
        return math.hypot(b["x"] - a["x"], b["z"] - a["z"])
    def median(label): return statistics.median(distance(r) for r in chosen(label)[-4:])
    camera = chosen("look")[0]
    jump = chosen("jump") + chosen("land")
    return {
        "first_forward_distance": distance(chosen("walk")[0]),
        "yaw_delta": (camera["after"]["yaw"] - camera["before"]["yaw"] + 180) % 360 - 180,
        "pitch_delta": camera["after"]["pitch"] - camera["before"]["pitch"],
        "walk_median": median("walk"), "sprint_median": median("sprint"), "sneak_median": median("sneak"),
        "jump_height": max(r["after"]["position"]["y"] for r in jump) - jump[0]["before"]["position"]["y"],
        "release_tail": max(distance(r) for r in chosen("release")[-3:]),
        "cancel_confirmed": chosen("cancel")[0]["receipt"]["status"] == "cancelled",
        "gui_conflict_rejected": chosen("gui_conflict")[0]["receipt"]["reason"] == "screen_conflict",
        "gui_closed": not rows[-1]["after"]["gui_open"],
        "all_alive": all(r["after"]["dead"] is False and not r["terminated"] and not r["truncated"] for r in rows),
        "expected_receipts": all(r["receipt"]["status"] == (
            "cancelled" if r["label"] == "cancel" else "rejected" if r["label"] == "gui_conflict" else
            "confirmed_local" if r["label"] in ("open", "close") else "executed") for r in rows),
        "device_callbacks": max(r["receipt"]["action_keyboard_callbacks"] + r["receipt"]["action_mouse_callbacks"] for r in rows),
    }


def evaluate_idle_release(rows: list[dict], idle_ns: int) -> dict:
    try:
        first, after, *tail = rows
        samples = after["receipt"]["input_samples"] - first["receipt"]["input_samples"]
        leased = after["receipt"]["leased_input_samples"] - first["receipt"]["leased_input_samples"]
        def distance(row):
            a, b = row["before"]["position"], row["after"]["position"]
            return math.hypot(b["x"] - a["x"], b["z"] - a["z"])
        move = {"forward": 1, "strafe": 0, "jump": False, "sneak": False, "sprint": False}
        budget = first["request_budget_ns"]
        passed = ([r["label"] for r in rows] == ["idle_lease", "after_idle"] + ["idle_tail"] * 3
            and type(idle_ns) is int and idle_ns >= 1_200_000_000
            and first["request"]["valid_for_ticks"] == 20 and first["request"]["movement"] == move
            and type(budget) is int and 0 < budget <= 250_000_000
            and .01 < distance(first) < 1 and samples >= 21 and 2 <= leased < 20
            and .01 < distance(after) < 3
            and all(distance(row) < .01 for row in tail))
        return {"name": "idle_ticks_continue_and_wall_lease_releases", "passed": bool(passed),
                "idle_ns": idle_ns, "input_tick_delta": samples, "leased_input_delta": leased}
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return {"name": "idle_ticks_continue_and_wall_lease_releases", "passed": False, "detail": str(error)}


def run_controls_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float,
                        *, idle_release: bool = False) -> int:
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    provenance = sandbox_provenance(sandbox)
    reference = provenance["scheduling_mode"] == "reference_nonblocking_v1"
    if idle_release and not reference:
        raise ValueError("idle release probe requires nonblocking reference mode")
    action_count = 72 if idle_release else 67
    idle_ns = 0
    time_source = sandbox / "run/mc2p-client-time.jsonl"
    time_offset = time_source.stat().st_size if time_source.is_file() else 0
    clock_trace = sandbox / "run/mc2p-lockstep.jsonl"
    clock_offset = clock_trace.stat().st_size if clock_trace.is_file() else 0
    deadline = time.perf_counter_ns() + round((timeout - 15) * 1e9)
    backend = backend_for_sandbox(sandbox, port)
    episode = f"controls-{seed}"
    rows, cleanup_failures, checks, metrics = [], [], [], {}
    failure = None
    try:
        reset = backend.reset(ResetRequestV0("controls-reset", episode, "flat-safe", seed, deadline))
        if not reset.succeeded or reset.observation is None:
            raise RuntimeError(f"controls reset failed: {reset.failure}")
        observation = reset.observation
        append_jsonl(run_dir / "observations.jsonl", trace_projection(observation))

        def step(label, *, action_deadline_ns=None, **kwargs):
            nonlocal observation
            before = _state(observation)
            action_deadline = deadline if action_deadline_ns is None else min(deadline, action_deadline_ns)
            action = ActionSnapshotV1(episode, len(rows), observation.sequence_id, action_deadline, **kwargs)
            append_jsonl(run_dir / "requests.jsonl", trace_projection(action))
            request_budget_ns = action_deadline - time.perf_counter_ns()
            result = backend.step(action, action_deadline)
            observation = result.observation
            row = {"label": label, "before": before, "after": _state(observation),
                   "request": trace_projection(action), "request_budget_ns": request_budget_ns,
                   "receipt": backend.last_behavior_receipt, "terminated": result.terminated, "truncated": result.truncated}
            rows.append(row)
            append_jsonl(run_dir / "steps.jsonl", row)
            append_jsonl(run_dir / "observations.jsonl", trace_projection(observation))

        for _ in range(8): step("settle")
        step("look", look=LookV1(30, 10))
        for _ in range(8): step("walk", movement=MovementV1(forward=1))
        for _ in range(8): step("sprint", movement=MovementV1(forward=1, sprint=True))
        for _ in range(3): step("jump", movement=MovementV1(forward=1, jump=True))
        for _ in range(12): step("land")
        for _ in range(8): step("sneak", movement=MovementV1(forward=1, sneak=True))
        for _ in range(8): step("release")
        step("leased", movement=MovementV1(forward=1), valid_for_ticks=8)
        step("cancel", cancel_request_sequence_id=len(rows) - 1)
        step("open", operation=OpenInventoryV1())
        step("gui_conflict", movement=MovementV1(forward=1), look=LookV1(10, 0))
        step("close", operation=CloseScreenV1())
        for _ in range(6): step("release")
        if idle_release:
            step("idle_lease", movement=MovementV1(forward=1), valid_for_ticks=20,
                 action_deadline_ns=time.perf_counter_ns() + 250_000_000)
            idle_started = time.perf_counter_ns()
            time.sleep(1.2)  # Explicit bounded fault injection: controller sends no new request.
            idle_ns = time.perf_counter_ns() - idle_started
            step("after_idle")
            for _ in range(3): step("idle_tail")
        metrics = summarize_controls(rows)
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    finally:
        try:
            backend.close()
        except Exception as error:
            cleanup_failures.append(f"{type(error).__name__}: {error}")
    checks = evaluate_controls_metrics(metrics)
    if idle_release:
        checks.append(evaluate_idle_release(rows[-5:], idle_ns))
    if reference:
        from scripts.reference_input_evidence import export_reference_input_evidence
        checks.append(export_reference_input_evidence(time_source, time_offset, run_dir, [r["receipt"] for r in rows]))
    else:
        checks.append(evaluate_input_samples([r["receipt"]["input_samples"] for r in rows], action_count=67))
        if provenance["scheduling_mode"] == "lockstep_nonblocking_v1":
            from scripts.client_time_evidence import export_lockstep_world_ticks
            checks.append(export_lockstep_world_ticks(time_source, time_offset, run_dir))
    try:
        diagnostics = _read_diagnostic_tail(diagnostic, offset)
        checks.append({"name": "complete_zero_image_hidden_observations", "passed": len(rows) == action_count
            and len(diagnostics) == action_count + 1 and all(
                r["image_bytes"] == r["framebuffer_capture_calls"] == r["image_encode_calls"] == 0
                and r["render_world_completions"] == 0 and r["window_visible"] is False
                and r["window_visible_at_creation"] is False for r in diagnostics)})
        if provenance["clock_mode"] == "lockstep_accelerated":
            segment = run_dir / "lockstep-events.jsonl"
            _capture_lockstep_trace_segment(sandbox, clock_offset, segment)
            events = parse_lockstep_trace_lines(segment.read_text("utf-8").splitlines())
            ticks = [rows[0]["before"]["world_tick"]] + [r["after"]["world_tick"] for r in rows] if rows else []
            checks.extend(evaluate_behavior_lockstep(ticks, events, action_count=67))
    except Exception as error:
        failure = failure or {"type": type(error).__name__, "message": str(error)}
    cleanup = backend.cleanup_status
    checks.append({"name": "process_and_port_clean", "passed": cleanup is not None
                   and cleanup.process_stopped and cleanup.port_released})
    if failure is None and not all(r["passed"] for r in checks):
        failure = {"type": "ControlsEvidenceFailure", "message": str([r["name"] for r in checks if not r["passed"]])}
    result = {"schema_version": "mc2p.client-controls-probe.v1", "checks": checks, "metrics": metrics,
              "provenance": provenance,
              "idle_release": idle_release,
              "step_count": len(rows), "status": "passed" if failure is None and not cleanup_failures else "failed",
              "primary_failure": failure, "cleanup_failures": cleanup_failures}
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2
