import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from mc2p.runtime.segmented_trace import iter_segmented_jsonl


ROOT = Path(__file__).resolve().parents[1]


class ClientControlDiagnosticsTests(unittest.TestCase):
    def test_opt_in_records_real_stage_values_and_seals_independently(self):
        diagnostics = ROOT / "mc2p/backends/runtime_overlays/mc121_diagnostics"
        source = diagnostics / "ClientControlDiagnostics.java"
        self.assertTrue(source.is_file(), "actual control-stage sidecar missing")
        java = ROOT / ".venv/Library/lib/jvm/bin"
        gson = next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar"))
        sources = [diagnostics / name for name in (
            "ClientControlDiagnostics.java", "ClientTimeDiagnostics.java",
            "ClientTimeTrace.java", "ClientTimeSegmentWriter.java",
            "ClientMovementDiagnostics.java", "ClientPhysicsTickDiagnostics.java",
        )]
        sources += [ROOT / "mc2p/backends/runtime_overlays/mc121_observation/ClientSampleClock.java"]
        sources += list((ROOT / "tests/java/diagnostics_stubs").rglob("*.java"))
        sources += [ROOT / "tests/java/ClientControlDiagnosticsTest.java"]
        with TemporaryDirectory(prefix="mc2p-control-diagnostics-") as temporary:
            root = Path(temporary)
            compiled = subprocess.run([
                str(java / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8",
                "-cp", str(gson), "-d", str(root), *map(str, sources),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            for timing, control in ((False, False), (False, True), (True, False), (True, True)):
                run = root / f"{timing}-{control}"
                run.mkdir()
                environment = dict(
                    os.environ,
                    MC2P_TIME_DIAGNOSTICS="1" if timing else "0",
                    MC2P_TIME_SEGMENTED="1" if timing else "0",
                    MC2P_MOVEMENT_DIAGNOSTICS="0",
                    MC2P_CONTROL_DIAGNOSTICS="1" if control else "0",
                    MC2P_PHYSICS_TICK_DIAGNOSTICS="0",
                )
                result = subprocess.run([
                    str(java / "java.exe"), "-cp", str(root) + ";" + str(gson),
                    "ClientControlDiagnosticsTest",
                ], cwd=run, env=environment, capture_output=True, text=True, timeout=30)
                if control and not timing:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(list(run.iterdir()), [])
                    continue
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("CLIENT_CONTROL_DIAGNOSTICS_OK", result.stdout)
                if not control:
                    self.assertFalse((run / "control-events").exists())
                    continue
                rows = list(iter_segmented_jsonl(run / "control-events"))
                self.assertEqual([row["event"] for row in rows], [
                    "look_applied", "input_consumed", "input_consumed",
                ])
                self.assertEqual([row["event_sequence"] for row in rows], [1, 2, 3])
                self.assertEqual(rows[0]["actual_look"], {"yaw": 15.0, "pitch": -10.0})
                self.assertEqual(rows[1]["actual_input"], {
                    "forward": 1.0, "strafe": -1.0, "jump": False,
                    "sneak": False, "sprint": False,
                })
                self.assertEqual(rows[1]["input_state"], "leased")
                self.assertEqual(rows[2]["input_state"], "lease_exhausted")
                self.assertEqual(rows[1]["actual_pose"]["velocity"], {
                    "x": .2, "y": 0.0, "z": -.1,
                })
                for row in rows:
                    self.assertEqual(row["schema_version"], "mc2p.client-control-event.v1")
                    self.assertEqual((row["episode_id"], row["request_sequence_id"]), ("probe", 4))
                    self.assertEqual(row["client_ticks"], 1 if row is not rows[2] else 2)
            shutdown = root / "shutdown"
            shutdown.mkdir()
            environment = dict(
                os.environ, MC2P_TIME_DIAGNOSTICS="1", MC2P_TIME_SEGMENTED="1",
                MC2P_MOVEMENT_DIAGNOSTICS="0", MC2P_CONTROL_DIAGNOSTICS="1",
                MC2P_PHYSICS_TICK_DIAGNOSTICS="0",
            )
            result = subprocess.run([
                str(java / "java.exe"), "-cp", str(root) + ";" + str(gson),
                "ClientControlDiagnosticsTest", "shutdown",
            ], cwd=shutdown, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(list(iter_segmented_jsonl(shutdown / "control-events"))), 3)


if __name__ == "__main__":
    unittest.main()
