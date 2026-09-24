from copy import deepcopy
from dataclasses import asdict, replace
import json
import unittest

from mc2p.contracts.action_v1 import ActionSnapshotV1
from tests.observation_v3_fixtures import valid_snapshot_v3
from tests.test_action_receipt import receipt_value


def trace_evidence():
    observations = [asdict(replace(valid_snapshot_v3(sequence=i), episode_id="episode",
                    request_sequence_id=None if i == 0 else i - 1)) for i in range(29)]
    records = [{"record_type": "reset", "payload": {"result": {"observation": observations[0]}}}]
    for i in range(28):
        decision = {"action": asdict(ActionSnapshotV1("episode", i, i, 1000))}
        receipt = receipt_value(episode_id="episode", request_sequence_id=i, generation_id=i + 1,
                               world_tick=observations[i + 1]["world_time_ticks"]["value"], input_samples=i + 1)
        records += [{"record_type": "dispatch", "payload": {"decision": deepcopy(decision)}},
            {"record_type": "step", "payload": {"decision": decision,
                "backend_result": {"observation": observations[i + 1], "receipt": receipt}}}]
    rows = [{"session_id": "client", "observation_sequence": i + 1, "image_bytes": 0,
             "framebuffer_capture_calls": 0, "image_encode_calls": 0, "render_world_completions": 0,
             "window_visible": False, "window_visible_at_creation": False} for i in range(29)]
    return json.loads(json.dumps(records)), rows


def stages():
    labels = ("start", "moving", "released", "open", "open_again", "closed", "closed_again",
              "reopened", "stale_rejected", "recovered", "hotbar", "no_repeat", "cancelled")
    result = {label: {"sequence": n, "x": 0.0, "z": 1.0, "yaw": 12.0, "speed": 0.0,
              "gui_open": label in {"open", "open_again", "reopened", "stale_rejected"},
              "gui_session": "new" if label in {"reopened", "stale_rejected"} else "old",
              "hotbar": 2, "status": "running", "receipt_status": "executed", "reason": "neutral",
              "operation": None} for n, label in enumerate(labels)}
    result["start"].update(z=0.0, yaw=0.0)
    result["moving"].update(speed=.2)
    result["stale_rejected"].update(status="failed", receipt_status="rejected", reason="stale_gui_session")
    result["hotbar"].update(receipt_status="pending_confirmation", operation="select_hotbar")
    result["cancelled"].update(status="cancelled")
    return result


class RuntimeSmokeTests(unittest.TestCase):
    def test_polling_lockstep_still_rejects_extra_input_samples(self):
        from scripts.smoke_test_player_runtime_v1 import evaluate_trace
        records, rows = trace_evidence()
        self.assertTrue(all(c["passed"] for c in evaluate_trace(records, rows,
            scheduling_mode="lockstep_nonblocking_v1")))
        records[2]["payload"]["backend_result"]["receipt"]["input_samples"] = 2
        self.assertFalse(all(c["passed"] for c in evaluate_trace(records, rows,
            scheduling_mode="lockstep_nonblocking_v1")))

    def test_reference_trace_requires_native_ticks_instead_of_one_sample_per_request(self):
        from scripts import smoke_test_player_runtime_v1 as smoke
        records, rows = trace_evidence()
        steps = [r["payload"] for r in records if r["record_type"] == "step"]
        observations = [records[0]["payload"]["result"]["observation"]] + [s["backend_result"]["observation"] for s in steps]
        samples = [1, 4, *range(5, 31)]  # Three idle-inclusive ticks between the first two requests.
        for i, (observation, count) in enumerate(zip(observations, [0, *samples])):
            observation.clear()
            replacement = replace(valid_snapshot_v3(sequence=i), episode_id="episode",
                request_sequence_id=i - 1 if i else None)
            replacement = replace(replacement,
                world_time_ticks=replace(replacement.world_time_ticks, value=10 + count),
                self_state=replace(replacement.self_state, sample_world_tick=10 + count),
                inventory=replace(replacement.inventory, sample_world_tick=10 + count),
                gui=replace(replacement.gui, sample_world_tick=10 + count),
                perception=replace(replacement.perception, sample_world_tick=10 + count),
                targeting=replace(replacement.targeting, sample_world_tick=10 + count))
            observation.update(json.loads(json.dumps(asdict(replacement))))
        for step, count in zip(steps, samples):
            step["backend_result"]["receipt"].update(input_samples=count, world_tick=10 + count)
        def event(kind, tick, **fields):
            return dict(schema_version="mc2p.client-time-event.v1", session_id="session", world_id="world",
                event=kind, client_ticks=100 + tick, world_ticks=tick, **fields)
        events = [event("observation", 0, generation_id=0, world_time=10)]
        for tick in range(1, 31):
            events.append(event("world_tick", tick, before=9 + tick, after=10 + tick))
            if tick in samples:
                events.append(event("observation", tick, generation_id=samples.index(tick) + 1, world_time=10 + tick))
        for i, value in enumerate(events): value["event_sequence"] = i + 1
        try:
            checks = smoke.evaluate_trace(records, rows, scheduling_mode="reference_nonblocking_v1", time_events=events)
        except TypeError as error:
            self.fail(f"Runtime reference evidence cannot be supplied: {error}")
        self.assertTrue(all(c["passed"] for c in checks), checks)
        self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(records, rows,
            scheduling_mode="reference_nonblocking_v1", time_events=[])))
        steps[1]["backend_result"]["receipt"]["input_samples"] = 2
        self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(records, rows,
            scheduling_mode="reference_nonblocking_v1", time_events=events)))

    def test_cancelled_camera_cannot_execute_after_cancel_boundary(self):
        from scripts.smoke_test_player_runtime_v1 import evaluate_stages
        value = stages()
        for label in ("released", "open", "open_again", "closed", "closed_again",
                      "reopened", "stale_rejected", "recovered", "hotbar", "no_repeat", "cancelled"):
            value[label]["yaw"] = 102.0
        self.assertFalse(all(c["passed"] for c in evaluate_stages(value)))

    def test_trace_evidence_requires_exact_association_order_and_zero_images(self):
        from scripts import smoke_test_player_runtime_v1 as smoke
        self.assertTrue(hasattr(smoke, "evaluate_trace"), "trace evidence must be independently testable")
        records, rows = trace_evidence()
        self.assertTrue(all(c["passed"] for c in smoke.evaluate_trace(records, rows)))
        for field, value in (("request_sequence_id", 99), ("generation_id", 99), ("input_samples", 2),
                ("execution_path", "legacy"), ("on_client_thread", False), ("action_keyboard_callbacks", 1),
                ("action_mouse_callbacks", 1), ("handled_screen_render_completions", 1), ("episode_id", "foreign")):
            bad = deepcopy(records)
            bad[2]["payload"]["backend_result"]["receipt"][field] = value
            with self.subTest(field=field):
                self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(bad, rows)))
        for field, value in (("image_bytes", 1), ("framebuffer_capture_calls", 1), ("image_encode_calls", 1),
                ("render_world_completions", 1), ("window_visible", True), ("window_visible_at_creation", True),
                ("observation_sequence", 4), ("session_id", "another")):
            bad = deepcopy(rows)
            bad[0][field] = value
            with self.subTest(field=field):
                self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(records, bad)))
        bad = deepcopy(records)
        bad[1], bad[2] = bad[2], bad[1]
        self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(bad, rows)))
        bad = deepcopy(records)
        bad[2]["payload"]["backend_result"]["observation"]["rgb"] = []
        self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(bad, rows)))
        bad = deepcopy(records)
        bad[2]["payload"]["backend_result"]["observation"]["schema_version"] = "mc2p.observation.v2"
        self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(bad, rows)))
        bad = deepcopy(records)
        bad[2]["payload"]["backend_result"]["observation"]["privileged_fields_present"] = ["server_position"]
        self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(bad, rows)))
        self.assertFalse(all(c["passed"] for c in smoke.evaluate_trace(records[:-1], rows)))

    def test_stages_require_real_effects_rejection_recovery_and_cancel(self):
        from scripts.smoke_test_player_runtime_v1 import evaluate_stages
        self.assertTrue(all(c["passed"] for c in evaluate_stages(stages())))
        for label, mutation in (("moving", {"z": 0.0}), ("moving", {"yaw": 144.0}),
                ("released", {"speed": .2}), ("open_again", {"gui_session": "different"}),
                ("stale_rejected", {"receipt_status": "executed"}), ("recovered", {"gui_open": True}),
                ("no_repeat", {"operation": "select_hotbar"}), ("cancelled", {"status": "running"}),
                ("hotbar", {"status": "succeeded"})):
            value = deepcopy(stages())
            value[label].update(mutation)
            with self.subTest(label=label, mutation=mutation):
                self.assertFalse(all(c["passed"] for c in evaluate_stages(value)))
        for label in stages():
            value = stages()
            del value[label]
            self.assertFalse(all(c["passed"] for c in evaluate_stages(value)))

    def test_parent_cannot_promote_worker_or_cleanup_failure_to_passed(self):
        from scripts.smoke_test_player_runtime_v1 import finalize_result
        from scripts.probe_craftground_timing_parallel import BoundedProcessResultV0
        okay = BoundedProcessResultV0(0, None, (), True, ())
        result = {"status": "passed", "primary_failure": None, "cleanup_failures": [], "checks": []}
        self.assertEqual(finalize_result(result, okay, True)["status"], "passed")
        for supervision, port in ((BoundedProcessResultV0(1, None, (), True, ()), True),
                (BoundedProcessResultV0(0, "timeout", (), True, ()), True),
                (BoundedProcessResultV0(0, None, ("survived",), True, ()), True),
                (BoundedProcessResultV0(0, None, (), False, ()), True), (okay, False)):
            self.assertEqual(finalize_result(result, supervision, port)["status"], "failed")
        failed = dict(result, status="failed", primary_failure="worker failure")
        self.assertEqual(finalize_result(failed, okay, True)["status"], "failed")
