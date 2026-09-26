from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[1]


class FabricDeploymentProbeTests(unittest.TestCase):
    def test_c1_source_freeze_includes_full_control_and_replay_chain(self):
        from scripts.probe_fabric_deployment_observation import (
            C1_FIXED_MELEE_SOURCES, frozen_deployment_sources,
        )
        required = {
            "mc2p/skills/fixed_melee.py",
            "mc2p/skills/fixed_melee_driver.py",
            "mc2p/skills/melee_strike_driver.py",
            "mc2p/skills/attack_evidence.py",
            "mc2p/skills/attack_evidence_replay.py",
            "mc2p/skills/navigation_session_driver.py",
            "mc2p/motion_nav/navigation_session.py",
            "mc2p/motion_nav/known_map_planner.py",
            "mc2p/motion_nav/route_admission.py",
            "mc2p/contracts/action_v1.py",
            "mc2p/contracts/observation_v2.py",
            "mc2p/runtime/async_trace.py",
            "scripts/c1_melee_evidence.py",
            "scripts/c1_fixed_melee_runtime.py",
            "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java",
            "mc2p/backends/runtime_overlays/mc121_observation/ClientEntityIndex.java",
            "mc2p/backends/runtime_overlays/mc121_observation/ClientDamageEventBuffer.java",
            "deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/mixin/ClientDamagePacketMixin.java",
        }
        self.assertTrue(required <= set(C1_FIXED_MELEE_SOURCES))
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False,
            c1_fixed_melee_probe=True,
        )
        self.assertTrue(required <= set(frozen))

    def test_c1b_source_freeze_includes_moving_chain_and_server_fixture(self):
        from scripts.probe_fabric_deployment_observation import (
            C1_MOVING_MELEE_SOURCES, frozen_deployment_sources,
        )
        required = {
            "mc2p/skills/engagement_memory.py",
            "mc2p/skills/moving_target.py",
            "mc2p/skills/melee_strike_driver.py",
            "mc2p/skills/moving_melee_driver.py",
            "scripts/c1_moving_melee_runtime.py",
            "scripts/c1_moving_melee_evidence.py",
            "deployment/fabric-c1-fixture-server/src/main/java/com/mc2p/fixture/C1FixtureServer.java",
        }
        self.assertTrue(required <= set(C1_MOVING_MELEE_SOURCES))
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False, c1_moving_melee_probe=True,
        )
        self.assertTrue(required <= set(frozen))

    def test_c1b_cli_is_bounded_and_mutually_exclusive(self):
        script = ROOT / "scripts/probe_fabric_deployment_observation.py"
        invalid = subprocess.run(
            [sys.executable, str(script), "--c1-moving-melee-probe",
             "--c1-fixed-melee-probe"], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("mutually exclusive", invalid.stderr)
        too_long = subprocess.run(
            [sys.executable, str(script), "--c1-moving-melee-probe",
             "--timeout-seconds", "1201"], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(too_long.returncode, 0)
        self.assertIn("invalid bounded probe configuration", too_long.stderr)

    def test_c1c_source_freeze_and_cli_are_independent(self):
        from scripts.probe_fabric_deployment_observation import (
            C1_EXTERNAL_MOTION_SOURCES, frozen_deployment_sources,
        )
        required = {
            "mc2p/motion_nav/external_motion.py",
            "mc2p/motion_nav/external_motion_recovery.py",
            "mc2p/skills/external_motion_recovery_driver.py",
            "scripts/c1_external_motion_runtime.py",
            "scripts/c1_external_motion_evidence.py",
        }
        self.assertTrue(required <= set(C1_EXTERNAL_MOTION_SOURCES))
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False, c1_external_motion_probe=True,
        )
        self.assertTrue(required <= set(frozen))
        script = ROOT / "scripts/probe_fabric_deployment_observation.py"
        invalid = subprocess.run(
            [sys.executable, str(script), "--c1-external-motion-probe",
             "--c1-moving-melee-probe"], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("mutually exclusive", invalid.stderr)

    def test_c1r_control_frame_source_freeze_and_cli_require_diagnostics(self):
        from scripts.probe_fabric_deployment_observation import (
            C1R_CONTROL_FRAME_SOURCES, frozen_deployment_sources,
        )
        required = {
            "scripts/c1r_control_frame_runtime.py",
            "mc2p/runtime/player_runtime_v1.py",
            "mc2p/runtime/arbiter_v1.py",
            "mc2p/skills/melee_strike_driver.py",
            "mc2p/backends/runtime_overlays/mc121_diagnostics/ClientControlDiagnostics.java",
        }
        self.assertTrue(required <= set(C1R_CONTROL_FRAME_SOURCES))
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False, c1r_control_frame_probe=True,
        )
        self.assertTrue(required <= set(frozen))
        script = ROOT / "scripts/probe_fabric_deployment_observation.py"
        missing_diagnostics = subprocess.run(
            [sys.executable, str(script), "--c1r-control-frame-probe"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(missing_diagnostics.returncode, 0)
        self.assertIn("requires --time-diagnostics", missing_diagnostics.stderr)

    def test_b12a_source_freeze_and_cli_are_independent(self):
        from scripts.probe_fabric_deployment_observation import (
            B12A_ATTACK_EVIDENCE_SOURCES, frozen_deployment_sources,
        )
        required = {
            "scripts/b12_attack_evidence_runtime.py",
            "scripts/b12a_fabric_runtime.py",
            "mc2p/skills/attack_evidence.py",
            "mc2p/skills/attack_evidence_replay.py",
            "mc2p/skills/melee_strike_driver.py",
        }
        self.assertTrue(required <= set(B12A_ATTACK_EVIDENCE_SOURCES))
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False,
            b12a_attack_evidence_probe=True,
        )
        self.assertTrue(required <= set(frozen))
        script = ROOT / "scripts/probe_fabric_deployment_observation.py"
        invalid = subprocess.run(
            [sys.executable, str(script), "--b12a-attack-evidence-probe",
             "--c1-fixed-melee-probe"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("mutually exclusive", invalid.stderr)

    def test_b12b_source_freeze_and_cli_require_control_diagnostics(self):
        from scripts.probe_fabric_deployment_observation import (
            B12B_PARTIAL_COMBAT_SOURCES, frozen_deployment_sources,
        )
        required = {
            "scripts/b12b_partial_combat_runtime.py",
            "mc2p/contracts/action_v1.py",
            "mc2p/runtime/arbiter_v1.py",
            "mc2p/motion_nav/navigation_session.py",
            "mc2p/skills/navigation_session_driver.py",
            "mc2p/skills/engagement_memory.py",
            "mc2p/skills/moving_melee_driver.py",
            "mc2p/skills/melee_strike_driver.py",
        }
        self.assertTrue(required <= set(B12B_PARTIAL_COMBAT_SOURCES))
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False,
            b12b_partial_combat_probe=True,
        )
        self.assertTrue(required <= set(frozen))
        script = ROOT / "scripts/probe_fabric_deployment_observation.py"
        missing_diagnostics = subprocess.run(
            [sys.executable, str(script), "--b12b-partial-combat-probe"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(missing_diagnostics.returncode, 0)
        self.assertIn("requires --time-diagnostics", missing_diagnostics.stderr)

    def test_b10_source_freeze_includes_the_receipt_driven_executor(self):
        from scripts.probe_fabric_deployment_observation import (
            B10_GAP_SOLVER_SOURCES,
        )
        self.assertIn("mc2p/motion_nav/motion_candidate.py",
                      B10_GAP_SOLVER_SOURCES)
        self.assertIn("mc2p/motion_nav/navigation_session.py",
                      B10_GAP_SOLVER_SOURCES)
        self.assertIn("mc2p/skills/navigation_session_driver.py",
                      B10_GAP_SOLVER_SOURCES)

    def test_b11_source_freeze_includes_world_change_chain(self):
        from scripts.probe_fabric_deployment_observation import (
            B11_WORLD_CHANGE_SOURCES, frozen_deployment_sources,
        )
        required = {
            "scripts/b11_world_change_runtime.py",
            "mc2p/motion_nav/bridge_planner.py",
            "mc2p/motion_nav/world_interaction.py",
            "mc2p/skills/block_placement_driver.py",
            "mc2p/skills/world_change_navigation_driver.py",
        }
        self.assertTrue(required <= set(B11_WORLD_CHANGE_SOURCES))
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False, b11_world_change_probe=True,
        )
        self.assertTrue(required <= set(frozen))
        script = ROOT / "scripts/probe_fabric_deployment_observation.py"
        invalid = subprocess.run(
            [sys.executable, str(script), "--b11-world-change-probe",
             "--b10-gap-solver-probe"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("mutually exclusive", invalid.stderr)

    def test_input_buffer_idle_source_is_frozen_for_live_evidence(self):
        from scripts.probe_fabric_deployment_observation import (
            INPUT_BUFFER_IDLE_SOURCES,
            frozen_deployment_sources,
        )
        self.assertEqual(
            INPUT_BUFFER_IDLE_SOURCES,
            ("scripts/input_buffer_idle_runtime.py",),
        )
        frozen = frozen_deployment_sources(
            b03_fixed_route_probe=False,
            input_buffer_idle_probe=True,
        )
        self.assertIn(INPUT_BUFFER_IDLE_SOURCES[0], frozen)

    def setUp(self):
        self.assertTrue((ROOT / "scripts/probe_fabric_deployment_observation.py").is_file(), "independent real-game probe is missing")

    def test_java_probe_routes_negotiated_v3_steps_and_consumes_each_profile_once(self):
        source = (ROOT / "deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentObservationProbe.java").read_text("utf-8")
        self.assertIn("DeploymentStepV3.decode(frame)", source)
        self.assertIn("ClientObservationCollector.collectV3", source)
        self.assertIn('"mc2p.deployment_sample.v2"', source)
        self.assertIn("observationRequest = ClientObservationRequestV3.navigation()", source)
        self.assertIn("if (!executor.receiptReady()) return;", source)

    def test_formal_probe_has_one_surface_depth_sensor_path(self):
        from scripts.probe_fabric_deployment_observation import (
            B03_SOURCES,
            DEPLOYMENT_BASE_SOURCES,
        )
        block_source = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientBlockObservationV3.java"
        )).read_text("utf-8")
        entry_source = (ROOT / (
            "deployment/fabric-observation-probe/src/main/java/"
            "com/mc2p/deployment/DeploymentObservationProbe.java"
        )).read_text("utf-8")
        build_source = (ROOT / "deployment/fabric-observation-probe/build.gradle").read_text("utf-8")
        collector_source = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientObservationCollector.java"
        )).read_text("utf-8")
        self.assertNotIn("MC2P_SURFACE_PERCEPTION", block_source)
        self.assertNotIn("FIRST_HIT_RAY", block_source)
        self.assertNotIn("legacyFirstHitPositions", block_source)
        self.assertIn("SurfacePerception.install()", entry_source)
        self.assertIn("mc121_surface", build_source)
        for retired in ("LEGACY_RAY", "collectBlockRay", "legacyFirstHitPositions", "block_rays"):
            self.assertNotIn(retired, collector_source)
        self.assertTrue({
            "scripts/process_tree.py",
            "scripts/formal_observation_v3_evidence.py",
            "scripts/formal_observation_v3_trace.py",
            "scripts/fabric_runtime_smoke_evidence.py",
        } <= set(DEPLOYMENT_BASE_SOURCES))
        self.assertIn("mc2p/motion_nav/observed_block_adapter.py", B03_SOURCES)

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

    def test_segmented_c1_trace_captures_diagnostics_for_every_formal_observation(self):
        import json
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from scripts import probe_fabric_deployment_observation as probe

        class Trace:
            def __init__(self):
                self.writes = []
                self.closed = False
                self.stats = "stats"

            def write(self, kind, payload):
                self.writes.append((kind, payload))

            def close(self):
                self.closed = True

        backend = SimpleNamespace(last_diagnostics={"client_tick": 7})
        observation = SimpleNamespace(episode_id="episode", sequence_id=3)
        with TemporaryDirectory(prefix="mc2p-c1-diagnostics-") as directory:
            delegate = Trace()
            trace = probe._RuntimeDiagnosticsTrace(Path(directory), backend, delegate)
            trace.write("step", {"backend_result": SimpleNamespace(observation=observation)})
            trace.close()
            rows = [json.loads(line) for line in
                    (Path(directory) / "diagnostics.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(delegate.writes[0][0], "step")
        self.assertTrue(delegate.closed)
        self.assertEqual(trace.stats, "stats")
        self.assertEqual(rows, [{
            "diagnostics": {"client_tick": 7},
            "episode_id": "episode",
            "observation_sequence_id": 3,
        }])

    def test_runtime_diagnostics_disk_wait_does_not_block_control_trace(self):
        import threading
        import time
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch
        from scripts import probe_fabric_deployment_observation as probe

        class Trace:
            stats = "stats"
            def write(self, kind, payload): pass
            def close(self): pass

        started = threading.Event()
        release = threading.Event()
        original = probe._DiagnosticsJsonlSink.write

        def delayed_write(sink, kind, payload):
            if kind == "diagnostics":
                started.set()
                if not release.wait(5):
                    raise TimeoutError("diagnostics test sink remained blocked")
            return original(sink, kind, payload)

        backend = SimpleNamespace(last_diagnostics={"client_tick": 7})
        observation = SimpleNamespace(episode_id="episode", sequence_id=3)
        with TemporaryDirectory(prefix="mc2p-c1-diagnostics-async-") as directory, \
                patch.object(probe._DiagnosticsJsonlSink, "write", new=delayed_write):
            trace = probe._RuntimeDiagnosticsTrace(Path(directory), backend, Trace())
            started_at = time.perf_counter_ns()
            trace.write("step", {"backend_result": SimpleNamespace(observation=observation)})
            elapsed_ns = time.perf_counter_ns() - started_at
            self.assertTrue(started.wait(1))
            self.assertLess(elapsed_ns, 50_000_000)
            release.set()
            trace.close()

    def test_diagnostic_trace_does_not_hide_a_failed_reset_without_observation(self):
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from scripts import probe_fabric_deployment_observation as probe

        class Trace:
            stats = "stats"
            def __init__(self): self.writes = []
            def write(self, kind, payload): self.writes.append((kind, payload))
            def close(self): pass

        with TemporaryDirectory(prefix="mc2p-c1-failed-reset-") as directory:
            delegate = Trace()
            trace = probe._RuntimeDiagnosticsTrace(
                Path(directory), SimpleNamespace(last_diagnostics={}), delegate,
            )
            trace.write("reset", {"result": SimpleNamespace(observation=None)})
            self.assertEqual(delegate.writes[0][0], "reset")
            self.assertFalse((Path(directory) / "diagnostics.jsonl").exists())

    def test_collector_keeps_damage_clock_unknown_until_movement_clock_exists(self):
        source = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientObservationCollector.java"
        )).read_text("utf-8")
        before_sample, after_sample = source.split("if (inputSample == null) {", 1)
        null_branch, live_branch = after_sample.split("} else {", 1)

        self.assertNotIn('addProperty("hurt_animation_ticks"', before_sample[-500:])
        self.assertIn('add("hurt_animation_ticks", JsonNull.INSTANCE)', null_branch)
        self.assertIn('add("movement_tick_id", JsonNull.INSTANCE)', null_branch)
        self.assertIn('addProperty("hurt_animation_ticks", player.hurtTime)', live_branch[:500])
        self.assertIn('addProperty("movement_tick_id", inputSample.movementTickId())', live_branch[:500])

    def test_collector_reports_current_pose_eye_height(self):
        source = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientObservationCollector.java"
        )).read_text("utf-8")

        self.assertIn('"eye_height_blocks"', source)
        self.assertIn("getCameraPosVec(1.0f).y - player.getY()", source)

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
        from scripts.bounded_process import BoundedProcessResultV0
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
        from tests.fabric_runtime_fixtures import trace_evidence
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
