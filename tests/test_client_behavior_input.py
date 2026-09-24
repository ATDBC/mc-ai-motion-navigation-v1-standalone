from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ClientBehaviorInputTests(unittest.TestCase):
    def test_real_input_samples_lease_slowdown_expiry_gui_and_cancel(self):
        source = ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorInput.java"
        self.assertTrue(source.is_file(), "shared player input is not implemented")
        mapped = ROOT / ".gradle/caches/fabric-loom/minecraftMaven/net/minecraft/minecraft-merged" / (
            "1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2/minecraft-merged-1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2.jar")
        self.assertTrue(mapped.is_file(), "build the pinned MC 1.21 runtime before the Java integration tests")
        java_bin = ROOT / ".venv/Library/bin"
        with TemporaryDirectory(prefix="mc2p-behavior-input-") as directory:
            compiled = subprocess.run([str(java_bin / "javac.exe"), "-J-Duser.language=en",
                "-encoding", "UTF-8", "-cp", str(mapped), "-d", directory, str(source),
                str(source.with_name("ClientRequestGate.java")), str(ROOT / "tests/java/ClientBehaviorInputTest.java")],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            tested = subprocess.run([str(java_bin / "java.exe"), "-cp", directory + ";" + str(mapped),
                "ClientBehaviorInputTest"], capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            self.assertIn("CLIENT_BEHAVIOR_INPUT_OK", tested.stdout)
