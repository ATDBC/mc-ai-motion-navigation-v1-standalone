from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[1]


class FabricDeploymentProbeTests(unittest.TestCase):
    def test_b10_source_freeze_includes_the_receipt_driven_executor(self):
        from scripts.probe_fabric_deployment_observation import (
            B10_GAP_SOLVER_SOURCES,
        )
        self.assertIn("mc2p/motion_nav/motion_candidate.py",
                      B10_GAP_SOLVER_SOURCES)

    def setUp(self):
        self.assertTrue((ROOT / "scripts/probe_fabric_deployment_observation.py").is_file(), "independent real-game probe is missing")

    def test_java_probe_routes_negotiated_v3_steps_and_consumes_each_profile_once(self):
        source = (ROOT / "deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentObservationProbe.java").read_text("utf-8")
        self.assertIn("DeploymentStepV3.decode(frame)", source)
        self.assertIn("ClientObservationCollector.collectV3", source)
        self.assertIn('"mc2p.deployment_sample.v2"', source)
        self.assertIn("observationRequest = ClientObservationRequestV3.navigation()", source)

    def test_python_probe_constructs_fabric_backend_in_explicit_v3_mode(self):
        from mc2p.contracts.observation_request_v3 import OBSERVATION_V3
        from scripts.probe_fabric_deployment_observation import create_deployment_backend
        with patch("scripts.probe_fabric_deployment_observation.FabricBehaviorBackendV1") as backend:
            result = create_deployment_backend("transport", "identity", "a" * 64, 25597)
        self.assertIs(result, backend.return_value)
        self.assertEqual(backend.call_args.kwargs["observation_schema_version"], OBSERVATION_V3)

    def test_time_diagnostic_runtime_trace_captures_close_release_sample(self):
        from tempfile import TemporaryDirectory
        from scripts import probe_fabric_deployment_observation as probe
        from scripts.fabric_visibility_scenario import CloseDiagnosticsTrace
        from mc2p.runtime.trace import JsonlTraceWriterV0

        with TemporaryDirectory(prefix="mc2p-runtime-trace-") as directory:
            root = Path(directory)
            backend = object()
            trace = probe._runtime_trace(root, backend, capture_close_diagnostics=True)
            self.assertIsInstance(trace, CloseDiagnosticsTrace)
            trace.close()

            plain_root = root / "plain"
            plain_root.mkdir()
            plain = probe._runtime_trace(plain_root, backend, capture_close_diagnostics=False)
            self.assertIsInstance(plain, JsonlTraceWriterV0)
            plain.close()

    def test_time_report_write_failure_cannot_replace_an_active_runtime_failure(self):
        from tempfile import TemporaryDirectory
        from scripts import probe_fabric_deployment_observation as probe
        self.assertTrue(callable(getattr(probe, "collect_time_evidence", None)), "exception-isolated diagnostic owner is missing")
        with TemporaryDirectory(prefix="mc2p-time-write-failure-") as directory:
            root = Path(directory)
            # A real directory at the report target makes atomic replacement fail on the filesystem.
            (root / "time-report.json").mkdir()
            try:
                try:
                    raise TimeoutError("original action timeout")
                finally:
                    check, diagnostic_failure = probe.collect_time_evidence(root, enabled=True)
            except TimeoutError as error:
                self.assertEqual(str(error), "original action timeout")
            else:
                self.fail("original Runtime failure disappeared")
            self.assertFalse(check["passed"])
            self.assertEqual(diagnostic_failure["stage"], "time_evidence_export")
            self.assertIn(diagnostic_failure["type"], {"PermissionError", "IsADirectoryError"})
            self.assertTrue((root / "time-report.json").is_dir())

    def test_failed_graceful_stop_still_terminates_the_registered_process(self):
        from scripts.probe_fabric_deployment_observation import _stop
        from mc2p.backends.deployment_transport import ClientProcessIdentity
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE)
        identity = ClientProcessIdentity(child.pid, psutil.Process(child.pid).create_time())
        try:
            # Inject only the OS pipe fault, retaining real process identity and termination.
            with patch.object(child.stdin, "write", side_effect=BrokenPipeError("fixture")):
                try:
                    result = _stop(child, identity, server=True)
                except BrokenPipeError:
                    self.fail("graceful-stop pipe failure bypassed registered process cleanup")
            self.assertIsNotNone(child.poll())
            self.assertFalse(result["passed"])
            self.assertFalse(result["graceful"])
            self.assertEqual(result["errors"], [{"stage": "graceful_stop", "type": "BrokenPipeError"}])
            self.assertEqual(result["cleanup"]["surviving"], [])
            self.assertTrue(child.stdin.closed)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
            child.stdin.close()

    def test_runtime_close_failure_does_not_skip_listener_or_process_cleanup(self):
        from scripts import probe_fabric_deployment_observation as probe
        from mc2p.backends.deployment_transport import ClientProcessIdentity, DeploymentTransport
        self.assertTrue(callable(getattr(probe, "_close_client", None)), "exception-safe client owner cleanup is missing")
        transport = DeploymentTransport()
        port = transport.port
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE)
        identity = ClientProcessIdentity(child.pid, psutil.Process(child.pid).create_time())
        class BrokenRuntime:
            cleanup_failures = ()
            def close(self):
                # Model an interrupt while the runtime releases its backend/trace.
                raise KeyboardInterrupt()
        try:
            # An exited process permits an immediate wait; owner cleanup must
            # still seal the real listener after the earlier runtime fault.
            child.terminate()
            child.wait(timeout=3)
            cleanup, failures = probe._close_client(BrokenRuntime(), None, transport, child, identity)
            self.assertTrue(transport.closed)
            self.assertTrue(probe.port_free(port))
            self.assertIsNotNone(child.poll())
            self.assertEqual(failures, [{"stage": "runtime_close", "type": "KeyboardInterrupt"}])
            self.assertEqual(cleanup["cleanup"]["surviving"], [])
            self.assertTrue(child.stdin.closed)
        finally:
            transport.close()
            if child.poll() is None: child.terminate()
            child.wait(timeout=3)
            child.stdin.close()

    @unittest.skipUnless(sys.platform == "win32", "owned Popen handle fallback is Windows-specific")
    def test_registration_failure_uses_owned_process_handle_and_remains_failed(self):
        from scripts.probe_fabric_deployment_observation import _stop
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE)
        try:
            try:
                result = _stop(child, None)
            except (AttributeError, TypeError):
                self.fail("registration failure left the owned process alive")
            self.assertIsNotNone(child.poll())
            self.assertFalse(result["passed"])
            self.assertEqual(result["errors"][0]["stage"], "identity_registration")
            self.assertTrue(child.stdin.closed)
        finally:
            if child.poll() is None: child.terminate()
            child.wait(timeout=3)
            child.stdin.close()

    def test_os_identity_requires_distinct_processes_and_both_socket_directions(self):
        from scripts.probe_fabric_deployment_observation import validate_connection_evidence
        evidence = dict(server_identity={"pid": 101, "create_time": 1000.0},
            client_identity={"pid": 102, "create_time": 1001.0},
            peer_proof={"pid": 102, "create_time": 1001.0, "controller_address": ["127.0.0.1", 8140],
                        "client_address": ["127.0.0.1", 61001]},
            server_listener=["127.0.0.1", 25597],
            client_game_connection={"local": ["127.0.0.1", 61002], "remote": ["127.0.0.1", 25597]},
            server_game_connection={"local": ["127.0.0.1", 25597], "remote": ["127.0.0.1", 61002]})
        self.assertTrue(validate_connection_evidence(evidence, server_port=25597, ipc_port=8140))
        for mutate in (
            lambda value: value["client_identity"].update(pid=101),
            lambda value: value["peer_proof"].update(create_time=1001.001),
            lambda value: value["client_game_connection"].update(remote=["192.168.1.1", 25597]),
            lambda value: value["server_game_connection"].update(remote=["127.0.0.1", 61003]),
            lambda value: value.update(server_listener=["0.0.0.0", 25597]),
        ):
            changed = deepcopy(evidence)
            mutate(changed)
            self.assertFalse(validate_connection_evidence(changed, server_port=25597, ipc_port=8140))

    def test_parent_failure_or_missing_worker_evidence_cannot_be_promoted(self):
        from scripts.probe_fabric_deployment_observation import finalize_result
        from scripts.probe_craftground_timing_parallel import BoundedProcessResultV0
        supervision = BoundedProcessResultV0(0, None, (), True, ())
        worker = dict(status="passed", primary_failure=None, cleanup_failures=[],
                      checks=[{"name": "fixture", "passed": True}])
        self.assertEqual(finalize_result(worker, supervision, True)["status"], "passed")
        for changed, parent, ports in (
            ({**worker, "checks": []}, supervision, True),
            ({**worker, "checks": [{"name": "fixture", "passed": "true"}]}, supervision, True),
            ({**worker, "primary_failure": "kept"}, supervision, True),
            ({**worker, "cleanup_failures": ["kept"]}, supervision, True),
            (worker, BoundedProcessResultV0(1, None, (), True, ()), True),
            (worker, BoundedProcessResultV0(0, "watchdog", (), True, ()), True),
            (worker, BoundedProcessResultV0(0, None, ("survivor",), False, ()), True),
            (worker, supervision, False),
        ):
            self.assertEqual(finalize_result(changed, parent, ports)["status"], "failed")

    def test_formal_trace_requires_real_association_and_zero_image_diagnostics(self):
        from scripts.probe_fabric_deployment_observation import evaluate_trace
        from tests.test_player_runtime_v1_smoke import trace_evidence
        from tests.deployment_fixtures import sample_value
        records, _ = trace_evidence()
        for record in records:
            if record["record_type"] == "reset":
                observation = record["payload"]["result"]["observation"]
            elif record["record_type"] == "step":
                observation = record["payload"]["backend_result"]["observation"]
            else:
                continue
            i = observation["sequence_id"]
            observation["source_backend"] = "fabric"
            observation["client_sample"].update(started_at_monotonic_ns=9000 + 100 * i, completed_at_monotonic_ns=9010 + 100 * i)
            observation.update(request_started_at_monotonic_ns=10 + 100 * i, received_at_monotonic_ns=20 + 100 * i)
        rows = [dict(episode_id="episode", observation_sequence_id=i, diagnostics=sample_value(i)["diagnostics"]) for i in range(29)]
        for i, row in enumerate(rows):
            row["diagnostics"]["gui_render_attempts"] = 0 if i < 20 else 3
        self.assertTrue(all(item["passed"] for item in evaluate_trace(records, rows, server_port=25599)))
        v3_records = deepcopy(records)
        for record in v3_records:
            if record["record_type"] != "step":
                continue
            receipt = record["payload"]["backend_result"]["receipt"]
            receipt["schema_version"] = "mc2p.client_action_receipt.v3"
            receipt["input_applications"] = []
        self.assertTrue(all(item["passed"] for item in evaluate_trace(
            v3_records, rows, server_port=25599,
        )))
        try:
            shorter = evaluate_trace(records[:-2], rows[:-1], server_port=25599, expected_steps=27)
        except TypeError:
            self.fail("trace audit cannot validate a bounded container lifecycle length")
        self.assertTrue(all(item["passed"] for item in shorter))
        self.assertFalse(all(item["passed"] for item in evaluate_trace(records[:-2], rows[:-1], server_port=25599)))
        self.assertFalse(all(item["passed"] for item in evaluate_trace(
            records, rows, server_port=25599, expected_steps=601,
        )))
        for count in (True, 0, 8001, 27.0):
            with self.assertRaises(ValueError):
                evaluate_trace(records, rows, server_port=25599, expected_steps=count)
        for mutate in (
            lambda trace, diag: diag[1]["diagnostics"].update(framebuffer_capture_attempts=1),
            lambda trace, diag: diag[1]["diagnostics"].update(window_visible=True),
            lambda trace, diag: diag[1]["diagnostics"].update(has_integrated_server=True),
            lambda trace, diag: diag[1]["diagnostics"].update(client_tick=100),
            lambda trace, diag: [row["diagnostics"].update(world_render_attempts=0) for row in diag],
            lambda trace, diag: [row["diagnostics"].update(gui_render_attempts=0) for row in diag],
            lambda trace, diag: [row["diagnostics"].update(gui_render_attempts=1) for row in diag],
            lambda trace, diag: [row["diagnostics"].update(world_render_attempts=-100.5+i, gui_render_attempts=True)
                                 for i, row in enumerate(diag)],
            lambda trace, diag: diag[1].update(observation_sequence_id=0),
            lambda trace, diag: trace[2]["payload"]["backend_result"]["receipt"].update(episode_id="wrong"),
            lambda trace, diag: trace[2]["payload"]["backend_result"]["observation"].update(pov="forbidden"),
            lambda trace, diag: trace[2]["payload"]["backend_result"]["observation"]["client_sample"].update(clock_id="foreign"),
            lambda trace, diag: trace.pop(),
        ):
            trace, diag = deepcopy(records), deepcopy(rows)
            mutate(trace, diag)
            self.assertFalse(all(item["passed"] for item in evaluate_trace(trace, diag, server_port=25599)))
