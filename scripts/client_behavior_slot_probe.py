"""Real empty-slot protocol/stale-reference probe, not an item-transfer acceptance."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
from mc2p.contracts.action_v1 import ActionSnapshotV1, OpenInventoryV1, CloseScreenV1, ClickSlotV1
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail
from scripts.client_behavior_probe_support import backend_for_sandbox


EXPECTED = [("confirmed_local", "opened_locally"), ("pending_confirmation", "slot_click_sent"),
            ("rejected", "stale_revision"), ("rejected", "stale_handler"),
            ("confirmed_local", "closed_normally"), ("confirmed_local", "opened_locally"),
            ("rejected", "stale_gui_session"), ("rejected", "invalid_slot"),
            ("pending_confirmation", "slot_click_sent"), ("confirmed_local", "closed_normally")]


def evaluate_slot_evidence(receipts: list[dict], guis: list[dict]) -> list[dict]:
    observed = [(r.get("status"), r.get("reason")) for r in receipts]
    outcomes = {
        "exact_operation_outcomes": observed == EXPECTED,
        "new_session_after_reopen": len(guis) == 11 and guis[1]["gui_session_id"] is not None
            and guis[6]["gui_session_id"] is not None and guis[1]["gui_session_id"] != guis[6]["gui_session_id"],
        "rejected_clicks_leave_gui_unchanged": len(guis) == 11 and all(
            guis[i] == guis[i - 1] for i in (3, 4, 7, 8)),
        "empty_fixture_not_misreported_as_item_transfer": len(guis) == 11 and all(
            g["cursor_stack"]["empty"] and all(s["item"]["empty"] for s in g["slots"]) for g in guis),
        "final_closed_and_no_stale_reference": len(guis) == 11 and not guis[-1]["open"]
            and guis[-1]["gui_session_id"] is None and not guis[-1]["slots"],
        "zero_device_callbacks": len(receipts) == 10 and all(
            r.get("action_keyboard_callbacks") == 0 and r.get("action_mouse_callbacks") == 0 for r in receipts),
    }
    return [{"name": name, "passed": passed} for name, passed in outcomes.items()]


def run_slot_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float) -> int:
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    deadline = time.perf_counter_ns() + round((timeout - 15) * 1e9)
    backend = backend_for_sandbox(sandbox, port)
    episode = f"slot-{seed}"
    receipts, guis, cleanup_failures, checks = [], [], [], []
    failure = None
    try:
        reset = backend.reset(ResetRequestV0("slot-reset", episode, "flat-safe", seed, deadline))
        if not reset.succeeded or reset.observation is None:
            raise RuntimeError(f"slot reset failed: {reset.failure}")
        observation = reset.observation

        def record():
            guis.append(trace_projection(observation.gui.value))
            append_jsonl(run_dir / "observations.jsonl", trace_projection(observation))

        def step(operation):
            nonlocal observation
            action = ActionSnapshotV1(episode, len(receipts), observation.sequence_id, deadline, operation=operation)
            append_jsonl(run_dir / "requests.jsonl", trace_projection(action))
            observation = backend.step(action, deadline).observation
            receipts.append(backend.last_behavior_receipt)
            append_jsonl(run_dir / "receipts.jsonl", receipts[-1])
            record()

        def current_click():
            gui = observation.gui.value
            return ClickSlotV1(gui.gui_session_id, gui.sync_id, gui.revision, 9, 0, "pickup")

        record()
        step(OpenInventoryV1())
        old_session = observation.gui.value.gui_session_id
        step(current_click())
        step(replace(current_click(), expected_revision=(observation.gui.value.revision + 1) % 32768))
        step(replace(current_click(), sync_id=observation.gui.value.sync_id + 1))
        step(CloseScreenV1())
        step(OpenInventoryV1())
        step(replace(current_click(), gui_session_id=old_session))
        step(replace(current_click(), slot=1000))
        step(current_click())
        step(CloseScreenV1())
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    finally:
        try:
            backend.close()
        except Exception as error:
            cleanup_failures.append(f"{type(error).__name__}: {error}")
    try:
        checks = evaluate_slot_evidence(receipts, guis)
        rows = _read_diagnostic_tail(diagnostic, offset)
        checks.append({"name": "eleven_zero_image_hidden_observations", "passed": len(rows) == 11 and all(
            row["image_bytes"] == row["framebuffer_capture_calls"] == row["image_encode_calls"] == 0
            and row["render_world_completions"] == 0 and row["window_visible"] is False
            and row["window_visible_at_creation"] is False for row in rows)})
    except Exception as error:
        failure = failure or {"type": type(error).__name__, "message": str(error)}
    cleanup = backend.cleanup_status
    checks.append({"name": "process_and_port_clean", "passed": cleanup is not None
                   and cleanup.process_stopped and cleanup.port_released})
    if failure is None and not all(row["passed"] for row in checks):
        failure = {"type": "SlotEvidenceFailure", "message": str([r["name"] for r in checks if not r["passed"]])}
    result = {"schema_version": "mc2p.client-slot-reference-probe.v1", "checks": checks,
              "status": "passed" if failure is None and not cleanup_failures else "failed",
              "receipts": receipts, "primary_failure": failure, "cleanup_failures": cleanup_failures}
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2
