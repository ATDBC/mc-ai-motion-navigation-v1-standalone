import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from mc2p.runtime.segmented_trace import iter_segmented_jsonl


ROOT = Path(__file__).resolve().parents[1]


class ClientPhysicsTickDiagnosticsTests(unittest.TestCase):
    def test_opt_in_pairs_pre_input_and_post_state_in_one_movement_tick(self):
        diagnostics = ROOT / "mc2p/backends/runtime_overlays/mc121_diagnostics"
        java = ROOT / ".venv/Library/lib/jvm/bin"
        gson = next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar"))
        sources = [diagnostics / name for name in (
            "ClientPhysicsTickDiagnostics.java", "ClientTimeDiagnostics.java",
            "ClientTimeTrace.java", "ClientTimeSegmentWriter.java",
            "ClientMovementDiagnostics.java", "ClientControlDiagnostics.java",
        )]
        sources += [ROOT / "mc2p/backends/runtime_overlays/mc121_observation/ClientSampleClock.java"]
        sources += list((ROOT / "tests/java/diagnostics_stubs").rglob("*.java"))
        sources += [ROOT / "tests/java/ClientPhysicsTickDiagnosticsTest.java"]
        with TemporaryDirectory(prefix="mc2p-physics-tick-diagnostics-") as temporary:
            root = Path(temporary)
            compiled = subprocess.run([
                str(java / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8",
                "-cp", str(gson), "-d", str(root), *map(str, sources),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            for enabled in (False, True):
                run = root / str(enabled)
                run.mkdir()
                environment = dict(
                    os.environ, MC2P_TIME_DIAGNOSTICS="1", MC2P_TIME_SEGMENTED="1",
                    MC2P_MOVEMENT_DIAGNOSTICS="0", MC2P_CONTROL_DIAGNOSTICS="0",
                    MC2P_PHYSICS_TICK_DIAGNOSTICS="1" if enabled else "0",
                )
                result = subprocess.run([
                    str(java / "java.exe"), "-cp", str(root) + ";" + str(gson),
                    "ClientPhysicsTickDiagnosticsTest",
                ], cwd=run, env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if not enabled:
                    self.assertFalse((run / "physics-tick-events").exists())
                    continue
                rows = list(iter_segmented_jsonl(run / "physics-tick-events"))
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["schema_version"], "mc2p.client-physics-tick.v1")
                self.assertEqual(row["movement_tick_id"], 1)
                self.assertEqual(row["actual_input"], {
                    "forward": 1.0, "strafe": -0.3, "jump": True,
                    "sneak": False, "sprint": True,
                })
                self.assertEqual(row["movement_yaw"], 30.0)
                self.assertEqual(row["pre_state"]["position"], {"x": 1.5, "y": 64.0, "z": 2.5})
                self.assertEqual(row["post_state"]["position"], {"x": 1.62, "y": 64.42, "z": 2.48})
                self.assertEqual(row["contact_events"], ["left_ground"])

    def test_shutdown_discards_only_the_unfinished_tail_tick(self):
        diagnostics = ROOT / "mc2p/backends/runtime_overlays/mc121_diagnostics"
        java = ROOT / ".venv/Library/lib/jvm/bin"
        gson = next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar"))
        sources = [diagnostics / name for name in (
            "ClientPhysicsTickDiagnostics.java", "ClientTimeDiagnostics.java",
            "ClientTimeTrace.java", "ClientTimeSegmentWriter.java",
            "ClientMovementDiagnostics.java", "ClientControlDiagnostics.java",
        )]
        sources += [ROOT / "mc2p/backends/runtime_overlays/mc121_observation/ClientSampleClock.java"]
        sources += list((ROOT / "tests/java/diagnostics_stubs").rglob("*.java"))
        sources += [ROOT / "tests/java/ClientPhysicsTickDiagnosticsTest.java"]
        with TemporaryDirectory(prefix="mc2p-physics-tick-close-") as temporary:
            root = Path(temporary)
            compiled = subprocess.run([
                str(java / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8",
                "-cp", str(gson), "-d", str(root), *map(str, sources),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            run = root / "run"
            run.mkdir()
            environment = dict(
                os.environ, MC2P_TIME_DIAGNOSTICS="1", MC2P_TIME_SEGMENTED="1",
                MC2P_MOVEMENT_DIAGNOSTICS="0", MC2P_CONTROL_DIAGNOSTICS="0",
                MC2P_PHYSICS_TICK_DIAGNOSTICS="1",
            )
            result = subprocess.run([
                str(java / "java.exe"), "-cp", str(root) + ";" + str(gson),
                "ClientPhysicsTickDiagnosticsTest", "close-during-tick",
            ], cwd=run, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(iter_segmented_jsonl(run / "physics-tick-events")), [])


if __name__ == "__main__":
    unittest.main()
