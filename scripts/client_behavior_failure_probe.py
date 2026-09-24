"""Fault-injection worker: late formal request rejection and clean backend recreation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import time

from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
from mc2p.backends.client_behavior_payload import decode_behavior_receipt
from mc2p.backends.client_observation_payload import extract_length_delimited_field
from mc2p.backends.client_observation_payload_v3 import decode_client_observation_payload_v3
from mc2p.contracts.action_v1 import ActionSnapshotV1, OpenInventoryV1, CloseScreenV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail
from scripts.client_behavior_probe_support import backend_for_sandbox


@dataclass
class DeadlineEvidence:
    sender_timed_out: bool = False
    client_status: str = "missing"
    client_reason: str = "missing"
    gui_stayed_open: bool = False
    further_io_blocked: bool = False
    old_reset_blocked: bool = False
    recreated_episode_clean: bool = False
    recovered_open_close: bool = False


def decode_expected_v3_payload(payload: bytes, *, expected_generation_id: int):
    """Decode first, then bind the late frame to the request generation."""
    if type(expected_generation_id) is not int or expected_generation_id < 0:
        raise ContractViolation("invalid expected observation generation")
    decoded = decode_client_observation_payload_v3(payload)
    if decoded.generation_id != expected_generation_id:
        raise ContractViolation("late observation generation mismatch")
    return decoded


def evaluate_deadline_evidence(evidence: DeadlineEvidence) -> list[dict]:
    expected = {"sender_timed_out": True, "client_status": "rejected", "client_reason": "deadline_exceeded",
                "gui_stayed_open": True, "further_io_blocked": True, "old_reset_blocked": True,
                "recreated_episode_clean": True, "recovered_open_close": True}
    return [{"name": name, "actual": getattr(evidence, name), "expected": want,
             "passed": getattr(evidence, name) == want} for name, want in expected.items()]


def run_deadline_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float) -> int:
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    overall_deadline = time.perf_counter_ns() + round((timeout - 15) * 1e9)
    evidence = DeadlineEvidence()
    failure = None
    cleanup_failures = []
    cleanup_statuses = []
    records = []
    try:
        for phase in ("fault", "recovery"):
            phase_offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
            backend = backend_for_sandbox(sandbox, port)
            episode = f"deadline-{phase}-{seed}"
            try:
                reset = backend.reset(ResetRequestV0(f"reset-{phase}", episode, "flat-safe", seed, overall_deadline))
                if not reset.succeeded or reset.observation is None:
                    raise RuntimeError(f"{phase} reset failed: {reset.failure}")
                append_jsonl(run_dir / "observations.jsonl", trace_projection(reset.observation))
                if phase == "recovery":
                    evidence.recreated_episode_clean = (backend.last_behavior_receipt is None
                        and reset.observation.episode_id == episode and reset.observation.sequence_id == 0
                        and reset.observation.gui.value.open is False
                        and reset.observation.gui.value.gui_session_id is None)
                opened = backend.step(ActionSnapshotV1(episode, 0, 0, overall_deadline,
                                      operation=OpenInventoryV1()), overall_deadline)
                append_jsonl(run_dir / "observations.jsonl", trace_projection(opened.observation))
                append_jsonl(run_dir / "receipts.jsonl", backend.last_behavior_receipt)
                if not opened.observation.gui.value.open:
                    raise RuntimeError("open_inventory did not open")
                if phase == "recovery":
                    closed = backend.step(ActionSnapshotV1(episode, 1, 1, overall_deadline,
                                          operation=CloseScreenV1()), overall_deadline)
                    append_jsonl(run_dir / "observations.jsonl", trace_projection(closed.observation))
                    append_jsonl(run_dir / "receipts.jsonl", backend.last_behavior_receipt)
                    evidence.recovered_open_close = (closed.observation.gui.value.open is False
                        and backend.last_behavior_receipt["status"] == "confirmed_local"
                        and backend.last_behavior_receipt["episode_id"] == episode)
                    continue

                # The real socket remains in use. Delay only the already encoded request;
                # record its real response so a sender TimeoutError cannot masquerade as
                # proof that the client rejected the operation.
                ipc = backend._env.ipc
                original_send, original_read = ipc.send_action, ipc.read_observation
                captured = []
                send_count = 0

                def delayed_send(message, commands):
                    nonlocal send_count
                    send_count += 1
                    time.sleep(0.75)
                    original_send(message, commands)

                def capture_read():
                    raw = original_read()
                    captured.append(raw)
                    return raw

                ipc.send_action, ipc.read_observation = delayed_send, capture_read
                try:
                    short_deadline = time.perf_counter_ns() + 500_000_000
                    try:
                        backend.step(ActionSnapshotV1(episode, 1, 1, short_deadline,
                                     operation=CloseScreenV1()), short_deadline)
                    except TimeoutError:
                        evidence.sender_timed_out = True
                    try:
                        backend.step(ActionSnapshotV1(episode, 2, 1, overall_deadline,
                                     operation=CloseScreenV1()), overall_deadline)
                    except ContractViolation as error:
                        evidence.further_io_blocked = "recreate" in str(error) and send_count == 1
                finally:
                    ipc.send_action, ipc.read_observation = original_send, original_read
                refused_reset = backend.reset(ResetRequestV0("refused-reset", "must-not-start", "flat-safe",
                                                             seed, overall_deadline))
                evidence.old_reset_blocked = (not refused_reset.succeeded and backend.last_behavior_receipt is None)
                if len(captured) != 1:
                    raise RuntimeError("late request has no captured client response")
                raw = captured[0].SerializeToString()
                receipt = decode_behavior_receipt(extract_length_delimited_field(raw, 50002))
                append_jsonl(run_dir / "receipts.jsonl", receipt)
                late = decode_expected_v3_payload(extract_length_delimited_field(raw), expected_generation_id=2)
                evidence.client_status, evidence.client_reason = receipt["status"], receipt["reason"]
                evidence.gui_stayed_open = (late.gui.value.open is True
                    and late.gui.value.gui_session_id == opened.observation.gui.value.gui_session_id)
                append_jsonl(run_dir / "late-observation.jsonl", trace_projection(late))
            finally:
                try:
                    backend.close()
                except Exception as error:
                    cleanup_failures.append(f"{phase}: {type(error).__name__}: {error}")
                cleanup_statuses.append(trace_projection(backend.cleanup_status))
                # Each JVM owns a separate diagnostic session and sequence [1..3].
                # Validate continuity inside that session, never across the restart.
                phase_records = _read_diagnostic_tail(diagnostic, phase_offset)
                if len(phase_records) != 3:
                    raise RuntimeError(f"{phase} diagnostic count is not three")
                records.extend(phase_records)
                if cleanup_failures:
                    raise RuntimeError("previous lifecycle cleanup failed; recreation skipped")
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    checks = evaluate_deadline_evidence(evidence)
    try:
        checks.append({"name": "six_zero_image_hidden_observations", "passed": len(records) == 6 and all(
            row["image_bytes"] == row["framebuffer_capture_calls"] == row["image_encode_calls"] == 0
            and row["render_world_completions"] == 0 and row["window_visible"] is False
            and row["window_visible_at_creation"] is False for row in records), "actual": len(records)})
    except Exception as error:
        failure = failure or {"type": type(error).__name__, "message": str(error)}
    checks.append({"name": "both_lifecycles_clean", "passed": len(cleanup_statuses) == 2 and all(
        row is not None and row["process_stopped"] and row["port_released"] for row in cleanup_statuses),
        "actual": cleanup_statuses})
    if failure is None and not all(row["passed"] for row in checks):
        failure = {"type": "DeadlineEvidenceFailure", "message": str([row["name"] for row in checks if not row["passed"]])}
    result = {"schema_version": "mc2p.client-behavior-deadline-probe.v2",
              "observation_schema_version": "mc2p.observation.v3", "knowledge_model": "block_state_v1",
              "field_profile": "navigation_v1", "evidence": asdict(evidence),
              "status": "passed" if failure is None and not cleanup_failures else "failed",
              "primary_failure": failure, "cleanup_failures": cleanup_failures, "checks": checks}
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2
