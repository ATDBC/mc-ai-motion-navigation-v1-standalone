"""Three episodes in one formal-client JVM; controlled fixture reset is not an actor action."""
from __future__ import annotations

import math
from pathlib import Path
import time

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1, OpenInventoryV1, CloseScreenV1, ClickSlotV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.client_behavior_probe_support import (
    backend_for_sandbox, sandbox_provenance, evaluate_behavior_lockstep, evaluate_input_samples,
)
from scripts.formal_observation_v3_evidence import validate_formal_observations_v3
from scripts.probe_craftground_timing_parallel import _capture_lockstep_trace_segment
from scripts.timing_parallel_probe_core import parse_lockstep_trace_lines
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail


def reset_start_is_clean(observation: ObservationSnapshotV3, receipt: dict | None, episode: str) -> bool:
    return (observation.sequence_id == 0 and observation.episode_id == episode
            and observation.is_dead.value is False and observation.gui.value is not None
            and not observation.gui.value.open and observation.gui.value.gui_session_id is None
            and not observation.gui.value.slots and receipt is None)


def run_reset_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float) -> int:
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    clock_trace = sandbox / "run/mc2p-lockstep.jsonl"
    clock_offset = clock_trace.stat().st_size if clock_trace.is_file() else 0
    provenance = sandbox_provenance(sandbox)
    reference = provenance["scheduling_mode"] == "reference_nonblocking_v1"
    time_source = sandbox / "run/mc2p-client-time.jsonl"
    time_offset = time_source.stat().st_size if time_source.is_file() else 0
    deadline = time.perf_counter_ns() + round((timeout - 15) * 1e9)
    backend = backend_for_sandbox(sandbox, port)
    checks, episodes, processes, sessions, cleanup_failures, observations = [], [], [], [], [], []
    failure = None
    old_action = None

    def check(name: str, passed: bool, actual: object = None) -> None:
        checks.append({"name": name, "passed": bool(passed), "actual": actual})

    try:
        for cycle in range(3):
            episode = f"reset-{seed}-{cycle}"
            reset = backend.reset(ResetRequestV0(f"reset-{cycle}", episode, "flat-safe", seed, deadline))
            if not reset.succeeded or reset.observation is None:
                raise RuntimeError(f"reset {cycle} failed: {reset.failure}")
            observation = reset.observation
            processes.append(backend._env.process.pid)  # Diagnostic process identity, never actor input.
            check(f"{cycle}:reset_clean", reset_start_is_clean(observation, backend.last_behavior_receipt, episode))
            ticks = [observation.world_time_ticks.value]
            rows = []
            episodes.append({"episode": episode, "ticks": ticks, "rows": rows})
            append_jsonl(run_dir / "observations.jsonl", trace_projection(observation))
            observations.append(trace_projection(observation))
            if old_action is not None:
                try:
                    backend.step(old_action, deadline)
                except ContractViolation:
                    check(f"{cycle}:old_episode_rejected", True)
                else:
                    check(f"{cycle}:old_episode_rejected", False)

            def step(label: str, **kwargs) -> None:
                nonlocal observation, old_action
                before = observation.position.value
                action = ActionSnapshotV1(episode, len(rows), observation.sequence_id, deadline, **kwargs)
                append_jsonl(run_dir / "requests.jsonl", trace_projection(action))
                result = backend.step(action, deadline)
                observation = result.observation
                after = observation.position.value
                row = {"label": label, "receipt": backend.last_behavior_receipt,
                       "distance": math.hypot(after.x - before.x, after.z - before.z)}
                rows.append(row)
                ticks.append(observation.world_time_ticks.value)
                append_jsonl(run_dir / "steps.jsonl", {"episode": episode, **row})
                append_jsonl(run_dir / "observations.jsonl", trace_projection(observation))
                observations.append(trace_projection(observation))
                check(f"{cycle}:{len(rows)}:alive", observation.is_dead.value is False
                      and not result.terminated and not result.truncated)
                old_action = action

            for _ in range(8):
                step("neutral_after_reset")
            check(f"{cycle}:no_old_horizontal_input", all(r["distance"] < 1e-6 for r in rows),
                  [r["distance"] for r in rows])
            step("open", operation=OpenInventoryV1())
            gui = observation.gui.value
            if not gui.open or gui.gui_session_id is None:
                raise RuntimeError("personal inventory failed to open after reset")
            sessions.append(gui.gui_session_id)
            if cycle == 1:
                step("stale_gui_from_old_episode", operation=ClickSlotV1(
                    sessions[0], gui.sync_id, gui.revision, 9, 0, "pickup"))
                check("stale_gui_rejected_after_reset", rows[-1]["receipt"]["reason"] == "stale_gui_session")
                # Deliberately leave GUI open for reset into episode 2.
            else:
                step("close", operation=CloseScreenV1())
                if cycle == 0:
                    step("held_before_reset", movement=MovementV1(forward=1), valid_for_ticks=20)
                    check("held_action_really_moved", rows[-1]["distance"] > 0.01)
            count = 11 if cycle == 0 else 10
            if not reference:
                check(f"{cycle}:input_count", evaluate_input_samples(
                    [r["receipt"]["input_samples"] for r in rows], action_count=count)["passed"])
            check(f"{cycle}:outcomes", len(rows) == count and all(r["receipt"]["status"] == (
                "confirmed_local" if r["label"] in ("open", "close") else
                "rejected" if r["label"] == "stale_gui_from_old_episode" else "executed") for r in rows))
        check("same_client_process", len(processes) == 3 and len(set(processes)) == 1, processes)
        check("gui_sessions_change_across_reset", len(sessions) == len(set(sessions)) == 3, sessions)
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    finally:
        try:
            backend.close()
        except Exception as error:
            cleanup_failures.append(f"{type(error).__name__}: {error}")
    try:
        if reference:
            from scripts.reference_input_evidence import export_reference_input_evidence
            checks.append(export_reference_input_evidence(time_source, time_offset, run_dir,
                [r["receipt"] for ep in episodes for r in ep["rows"]]))
        elif provenance["scheduling_mode"] == "lockstep_nonblocking_v1":
            from scripts.client_time_evidence import export_lockstep_world_ticks
            checks.append(export_lockstep_world_ticks(time_source, time_offset, run_dir))
        diagnostics = _read_diagnostic_tail(diagnostic, offset)
        check("34_zero_image_hidden_observations", len(diagnostics) == 34 and all(
            r["image_bytes"] == r["framebuffer_capture_calls"] == r["image_encode_calls"] == 0
            and r["render_world_completions"] == 0 and r["window_visible"] is False
            and r["window_visible_at_creation"] is False for r in diagnostics))
        check("zero_device_callbacks", len(episodes) == 3 and all(
            r["receipt"]["action_keyboard_callbacks"] == r["receipt"]["action_mouse_callbacks"] == 0
            for ep in episodes for r in ep["rows"]))
        if provenance["clock_mode"] == "lockstep_accelerated":
            segment = run_dir / "lockstep-events.jsonl"
            _capture_lockstep_trace_segment(sandbox, clock_offset, segment)
            events = parse_lockstep_trace_lines(segment.read_text("utf-8").splitlines())
            groups = []
            for event in events:
                if event.event == "server_arm_complete":
                    groups.append([])
                if not groups:
                    raise RuntimeError("reset clock trace missing arm")
                groups[-1].append(event)
            check("three_clock_sessions", len(groups) == 3)
            for cycle, (group, ep) in enumerate(zip(groups, episodes)):
                for result in evaluate_behavior_lockstep(ep["ticks"], group, action_count=11 if cycle == 0 else 10):
                    check(f"{cycle}:{result['name']}", result["passed"])
    except Exception as error:
        failure = failure or {"type": type(error).__name__, "message": str(error)}
    cleanup = backend.cleanup_status
    check("all_formal_observations_are_native_v3", bool(observations)
          and not validate_formal_observations_v3(observations))
    check("process_and_port_clean", cleanup is not None and cleanup.process_stopped and cleanup.port_released)
    if failure is None and not all(c["passed"] for c in checks):
        failure = {"type": "ResetEvidenceFailure", "message": str([c["name"] for c in checks if not c["passed"]])}
    result = {"schema_version": "mc2p.client-behavior-reset-probe.v2",
              "observation_schema_version": "mc2p.observation.v3", "knowledge_model": "block_state_v1",
              "field_profile": "navigation_v1", "checks": checks,
              "provenance": provenance, "episodes": episodes,
              "status": "passed" if failure is None and not cleanup_failures else "failed",
              "primary_failure": failure, "cleanup_failures": cleanup_failures}
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2
