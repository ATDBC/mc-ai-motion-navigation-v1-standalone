from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from scripts.export_motion_navigation_standalone import discover_java_tools


ROOT = Path(__file__).resolve().parents[1]


class StandaloneJavaGateTests(unittest.TestCase):
    def test_pure_java_control_gates_compile_and_run_with_discovered_jdk21(self):
        tools = discover_java_tools()
        cases = (
            ("ClientOperationCompatibility", "CLIENT_OPERATION_COMPATIBILITY_OK"),
            ("ClientEntityGuard", "CLIENT_ENTITY_GUARD_OK"),
            ("ClientRequestGate", "CLIENT_REQUEST_GATE_OK"),
            ("ClientBlockGuard", "CLIENT_BLOCK_GUARD_OK"),
            ("ClientUsePulse", "CLIENT_USE_PULSE_OK"),
        )
        source_root = ROOT / "mc2p/backends/runtime_overlays/mc121_actions"
        harness_root = ROOT / "tests/java"
        for class_name, marker in cases:
            with self.subTest(class_name=class_name), \
                    TemporaryDirectory(prefix="mc2p-standalone-java-") as directory:
                compiled = subprocess.run(
                    [
                        str(tools.javac), "-J-Duser.language=en",
                        "-encoding", "UTF-8", "-d", directory,
                        str(source_root / f"{class_name}.java"),
                        str(harness_root / f"{class_name}Test.java"),
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(
                    compiled.returncode, 0, compiled.stdout + compiled.stderr,
                )
                executed = subprocess.run(
                    [str(tools.java), "-cp", directory, f"{class_name}Test"],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(
                    executed.returncode, 0, executed.stdout + executed.stderr,
                )
                self.assertIn(marker, executed.stdout)


if __name__ == "__main__":
    unittest.main()
