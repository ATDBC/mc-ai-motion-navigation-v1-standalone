from dataclasses import replace
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionSnapshotV0, ActionPriorityV0
from mc2p.contracts.action_v1 import ActionSnapshotV1, ActionIntentV1, OpenInventoryV1
from mc2p.contracts import action_v1 as action_values
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.reset import ResetRequestV0
from tests.deployment_fixtures import TOKEN, backend_peer, encoded, sample_value
from tests.test_deployment_python_transport import deadline


class FabricBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1] / "mc2p/backends/fabric_behavior.py").is_file(),
                        "formal Fabric backend is missing")

    def reset(self, backend, **kwargs):
        request = ResetRequestV0("reset", "ep", "remote-session", 0, deadline())
        return backend.reset(replace(request, **kwargs))

    def test_backend_import_does_not_require_craftground(self):
        code = ("import sys,importlib.abc; "
                "guard=type('Guard',(importlib.abc.MetaPathFinder,),{'find_spec':"
                "lambda self,name,*args: (_ for _ in ()).throw(ImportError('CraftGround forbidden')) "
                "if name.startswith('craftground') else None})(); "
                "sys.meta_path.insert(0,guard); "
                "from mc2p.backends.fabric_behavior import FabricBehaviorBackendV1; "
                "assert not any(k.startswith('craftground') for k in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_configuration_is_exact_and_never_coerces_session_credentials(self):
        import psutil
        from mc2p.backends.deployment_transport import ClientProcessIdentity, DeploymentTransport
        from mc2p.backends.fabric_behavior import FabricBehaviorBackendV1
        transport = DeploymentTransport()
        try:
            baseline = dict(transport=transport, client_identity=ClientProcessIdentity(os.getpid(), psutil.Process().create_time()),
                            token=TOKEN, server_port=25599)
            for changes in ({"token": ""}, {"token": TOKEN.upper()}, {"token": TOKEN + "0"}, {"token": TOKEN.encode()},
                            {"token": None}, {"server_port": True}, {"server_port": 0}, {"server_port": 65536},
                            {"server_port": "25599"}, {"client_identity": {"pid": os.getpid()}}, {"transport": object()}):
                with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                    FabricBehaviorBackendV1(**{**baseline, **changes})
        finally:
            transport.close()

    def test_reset_step_preserve_shared_contract_raw_time_and_diagnostic_separation(self):
        with backend_peer([sample_value(), sample_value(1, tick=99)]) as (backend, transport, requests):
            reset = self.reset(backend)
            self.assertTrue(reset.succeeded, reset.failure)
            self.assertEqual(reset.observation.source_backend, "fabric")
            result = backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
            self.assertEqual(result.observation.sequence_id, 1)
            self.assertEqual(result.observation.world_time_ticks.value, 99)
            self.assertEqual(result.observation.client_sample.clock_id, "jvm-test")
            self.assertEqual(result.observation.controller_clock_id, reset.observation.controller_clock_id)
            self.assertIsNone(result.observation.server_state_age_ns.value)
            self.assertEqual(result.receipt.world_tick, 99)
            self.assertEqual(backend.last_diagnostics["client_tick"], 103)
            self.assertEqual(set(requests[0]), {"schema_version", "episode_id", "token"})
            self.assertEqual(requests[0]["token"], TOKEN)
            self.assertEqual(requests[1]["schema_version"], "mc2p.client_action.v1")
            self.assertNotIn("deadline_monotonic_ns", requests[1])
            self.assertNotIn("token", requests[1])
            self.assertNotIn("pov", result.observation.__dataclass_fields__)

    def test_v3_receipt_preserves_exact_input_applications(self):
        current = sample_value(1)
        current["receipt"].update(
            schema_version="mc2p.client_action_receipt.v3",
            input_applications=[{
                "schema_version": "mc2p.input-application.v1",
                "movement_tick_id": 1,
                "episode_id": "ep",
                "request_sequence_id": 0,
                "sampled_at_jvm_ns": 123,
                "state": "neutral",
                "forward": 0.0,
                "strafe": 0.0,
                "jump": False,
                "sneak": False,
                "sprint": False,
            }],
        )
        with backend_peer([sample_value(), current]) as (backend, _, _):
            self.assertTrue(self.reset(backend).succeeded)
            result = backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
        self.assertEqual(result.receipt.last_input_sample.movement_tick_id, 1)
        self.assertEqual(result.receipt.last_input_sample.request_sequence_id, 0)

    def test_normal_rejection_allows_new_request_without_replaying_old_operation(self):
        rejected = sample_value(1, status="rejected")
        rejected["receipt"]["reason"] = "screen_conflict"
        with backend_peer([sample_value(), rejected, sample_value(2)]) as (backend, _, requests):
            self.assertTrue(self.reset(backend).succeeded)
            result = backend.step(ActionSnapshotV1("ep", 0, 0, deadline(), operation=OpenInventoryV1()), deadline())
            self.assertEqual(result.receipt.status, "rejected")
            recovered = backend.step(ActionSnapshotV1("ep", 1, 1, deadline()), deadline())
            self.assertEqual(recovered.receipt.status, "executed")
            self.assertEqual(requests[1]["operation"], {"kind": "open_inventory"})
            self.assertIsNone(requests[2]["operation"])

    def test_targeted_attack_serializes_exact_entity_identity_once(self):
        attack = action_values.AttackEntityV1("entity-session-7")
        with backend_peer([sample_value(), sample_value(1), sample_value(2)]) as (backend, _, requests):
            self.assertTrue(self.reset(backend).succeeded)
            result = backend.step(
                ActionSnapshotV1("ep", 0, 0, deadline(), operation=attack), deadline()
            )
            self.assertEqual(result.receipt.status, "executed")
            backend.step(ActionSnapshotV1("ep", 1, 1, deadline()), deadline())
        self.assertEqual(requests[1]["operation"], {
            "kind": "attack_entity", "entity_ref": "entity-session-7",
        })
        self.assertIsNone(requests[2]["operation"])

    def test_schema_correlation_clock_and_zero_image_failures_seal_the_stream(self):
        cases = [
            lambda item: item.update(episode_id="wrong"),
            lambda item: item.update(pov="forbidden"),
            lambda item: item["observation"].update(generation_id=7),
            lambda item: item["observation"]["client_sample"].update(clock_id="replaced-jvm"),
            lambda item: item["observation"]["client_sample"].update(started_at_monotonic_ns=5, completed_at_monotonic_ns=10),
            lambda item: item["receipt"].update(request_sequence_id=8),
            lambda item: item["receipt"].update(episode_id="wrong"),
            lambda item: item["receipt"].update(world_tick=9),
            lambda item: item["receipt"].update(on_client_thread=False),
            lambda item: item["receipt"].update(status="idle"),
            lambda item: item["receipt"].update(action_mouse_callbacks=1),
            lambda item: item["diagnostics"].update(window_visible=True),
            lambda item: item["diagnostics"].update(framebuffer_capture_attempts=1),
            lambda item: item["diagnostics"].update(client_tick=100),
            lambda item: item["diagnostics"].update(remote_address="192.168.1.1:25599"),
            lambda item: item["diagnostics"].update(image_encode_attempts=True),
            lambda item: item["diagnostics"].update(has_integrated_server=True),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                bad = sample_value(1)
                mutate(bad)
                with backend_peer([sample_value(), bad]) as (backend, transport, requests):
                    self.assertTrue(self.reset(backend).succeeded)
                    with self.assertRaises((ContractViolation, ValueError)):
                        backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
                    self.assertTrue(transport.closed)
                    self.assertIsNone(backend.last_behavior_receipt)
                    with self.assertRaises(ContractViolation):
                        backend.step(ActionSnapshotV1("ep", 1, 0, deadline()), deadline())
                    self.assertEqual(len(requests), 2)

    def test_duplicate_json_and_image_payloads_are_rejected_on_reset(self):
        good = encoded(sample_value())
        for bad in (good[:-1] + b',"episode_id":"ep"}', b"\xff", b"[]",
                    good.replace(b'"observation":{', b'"observation":{"image":"forbidden",')):
            with self.subTest(prefix=bad[:30]), backend_peer([bad]) as (backend, transport, _):
                result = self.reset(backend)
                self.assertFalse(result.succeeded)
                self.assertTrue(transport.closed)
                self.assertNotIn(TOKEN, result.failure.message)

    def test_second_reset_and_world_generation_options_are_explicitly_unsupported(self):
        with backend_peer([sample_value()]) as (backend, transport, requests):
            self.assertTrue(self.reset(backend).succeeded)
            result = self.reset(backend, request_id="again", episode_id="new")
            self.assertFalse(result.succeeded)
            self.assertIn("recreate", result.failure.message)
            self.assertTrue(transport.closed)
            self.assertEqual(len(requests), 1)
        for kwargs in ({"scenario_id": "flat-safe"}, {"seed": 21001}, {"backend_options_json": '{"world":"new"}'}):
            with self.subTest(kwargs=kwargs), backend_peer([]) as (backend, transport, requests):
                self.assertFalse(self.reset(backend, **kwargs).succeeded)
                self.assertTrue(transport.closed)
                self.assertEqual(requests, [])

    def test_late_response_is_not_replayed_and_fresh_instance_recovers(self):
        with backend_peer([sample_value(), sample_value(1)], delay=lambda i: time.sleep(.15) if i == 1 else None) as (backend, transport, requests):
            self.assertTrue(self.reset(backend).succeeded)
            with self.assertRaises(TimeoutError):
                backend.step(ActionSnapshotV1("ep", 0, 0, deadline(.05)), deadline(.05))
            self.assertTrue(transport.closed)
            with self.assertRaises(ContractViolation):
                backend.step(ActionSnapshotV1("ep", 1, 0, deadline()), deadline())
            self.assertEqual(len(requests), 2)
        with backend_peer([sample_value(), sample_value(1)]) as (backend, _, _):
            self.assertTrue(self.reset(backend).succeeded)
            self.assertEqual(backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline()).receipt.status, "executed")

    def test_legacy_stale_and_duplicate_requests_cannot_reach_socket(self):
        with backend_peer([sample_value(), sample_value(1)]) as (backend, _, requests):
            self.assertTrue(self.reset(backend).succeeded)
            for bad in (ActionSnapshotV0.neutral(0), ActionSnapshotV1("other", 0, 0, deadline()),
                        ActionSnapshotV1("ep", 0, 1, deadline())):
                with self.assertRaises(ContractViolation):
                    backend.step(bad, deadline())
            backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
            with self.assertRaises(ContractViolation):
                backend.step(ActionSnapshotV1("ep", 0, 1, deadline()), deadline())
            self.assertEqual(len(requests), 2)

    def test_runtime_uses_backend_without_craftground_or_device_actions(self):
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        from tests.test_player_runtime import _RecordingTrace, _task
        with backend_peer([sample_value(), sample_value(1, status="rejected"), sample_value(2)]) as (backend, _, requests):
            runtime = PlayerRuntimeV1(backend, _RecordingTrace())
            try:
                self.assertTrue(runtime.reset(ResetRequestV0("r", "ep", "remote-session", 0, deadline())).succeeded)
                runtime.submit_intent(ActionIntentV1("open", "task", "ep", 0, ActionPriorityV0.TASK,
                    time.perf_counter_ns(), deadline(), operation=OpenInventoryV1()))
                first = runtime.step(_task(deadline=deadline()), BehaviorProfileV0(), deadline())
                self.assertEqual(first.report.phase, "action_rejected")
                self.assertEqual(runtime.state.value, "ready")
                runtime.cancel("stop")
                result = runtime.step(_task(deadline=deadline()), BehaviorProfileV0(), deadline())
                self.assertEqual(result.report.status.value, "cancelled")
                self.assertEqual(requests[1]["operation"], {"kind": "open_inventory"})
                self.assertIsNone(requests[2]["operation"])
            finally:
                runtime.close()

    def test_reset_cleanup_error_is_secondary_and_never_exposes_session_token(self):
        with backend_peer([b"[]"]) as (backend, transport, _):
            original_close = transport.close
            def close_failure():
                original_close()
                raise OSError(TOKEN)
            with patch.object(transport, "close", side_effect=close_failure):
                result = self.reset(backend)
            self.assertFalse(result.succeeded)
            self.assertEqual(result.failure.code.value, "observation_invariant")
            self.assertEqual(json.loads(result.failure.detail_json), {"cleanup_failures": ["OSError"]})
            self.assertNotIn(TOKEN, str(result))
            self.assertTrue(transport.closed)

    def test_reset_and_step_interrupt_preserved_even_when_cleanup_fails(self):
        for phase in ("reset", "step"):
            with self.subTest(phase=phase), backend_peer([sample_value(), sample_value(1)]) as (backend, transport, _):
                if phase == "step":
                    self.assertTrue(self.reset(backend).succeeded)
                interruption = KeyboardInterrupt("test cancellation")
                original_close = transport.close
                def close_failure():
                    original_close()
                    raise OSError(TOKEN)
                with patch.object(transport, "receive", side_effect=interruption), patch.object(transport, "close", side_effect=close_failure):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        if phase == "reset":
                            self.reset(backend)
                        else:
                            backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
                self.assertIs(caught.exception, interruption)
                self.assertTrue(transport.closed)
                self.assertEqual(interruption.__notes__, ["Fabric cleanup failed: OSError"])
                with self.assertRaises(ContractViolation):
                    backend.step(ActionSnapshotV1("ep", 1, 0, deadline()), deadline())

    def test_snapshot_projection_must_finish_before_deadline(self):
        from mc2p.backends.fabric_behavior import snapshot_v2_from_payload
        for phase in ("reset", "step"):
            offset = [0]
            def clock():
                return time.perf_counter_ns() + offset[0]
            def delayed_projection(*args, **kwargs):
                result = snapshot_v2_from_payload(*args, **kwargs)
                offset[0] = 10_000_000_000
                return result
            with self.subTest(phase=phase), backend_peer([sample_value(), sample_value(1)], clock_ns=clock) as (backend, transport, _):
                if phase == "step":
                    self.assertTrue(self.reset(backend).succeeded)
                with patch("mc2p.backends.fabric_behavior.snapshot_v2_from_payload", side_effect=delayed_projection):
                    if phase == "reset":
                        result = self.reset(backend)
                        self.assertFalse(result.succeeded)
                        self.assertEqual(result.failure.code.value, "deadline_exceeded")
                    else:
                        with self.assertRaises(TimeoutError):
                            backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
                self.assertTrue(transport.closed)
                self.assertIsNone(backend.last_behavior_receipt)

    def test_runtime_classifies_nested_observation_rejection_as_invariant_not_io(self):
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        from tests.test_player_runtime import _RecordingTrace, _task
        malformed = sample_value(1)
        malformed["observation"]["private_extra"] = "forbidden"
        with backend_peer([sample_value(), malformed]) as (backend, transport, requests):
            runtime = PlayerRuntimeV1(backend, _RecordingTrace())
            try:
                self.assertTrue(runtime.reset(ResetRequestV0("r", "ep", "remote-session", 0, deadline())).succeeded)
                result = runtime.step(_task(deadline=deadline()), BehaviorProfileV0(), deadline())
                self.assertEqual(result.report.failure.code.value, "observation_invariant")
                self.assertEqual(runtime.state.value, "failed")
                self.assertTrue(transport.closed)
                self.assertEqual(len(requests), 2)
            finally:
                runtime.close()
