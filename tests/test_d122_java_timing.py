from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / ".venv/Library/bin"
GSON = next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar"))


class D122JavaTimingTests(unittest.TestCase):
    def compile_run(self, marker: str, *sources: Path, cp: str = ""):
        with TemporaryDirectory(prefix="mc2p-d122-java-") as directory:
            classpath = str(GSON) + (";" + cp if cp else "")
            built = subprocess.run([str(JAVA / "javac.exe"), "-encoding", "UTF-8", "-cp", classpath,
                                    "-d", directory, *(str(p) for p in sources)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([str(JAVA / "java.exe"), "-cp", directory + ";" + classpath,
                                     sources[-1].stem], capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            self.assertIn(marker, tested.stdout)

    def test_v2_decoder_is_strict_and_v1_remains_isolated(self):
        self.compile_run("CLIENT_ACTION_TIMING_DECODE_OK",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientActionRequest.java",
                         ROOT / "tests/java/ClientActionTimingDecodeTest.java")

    def test_bounded_schedule_selects_pending_before_default_and_holds_response(self):
        self.compile_run("CLIENT_TIMING_SCHEDULE_OK",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java",
                         ROOT / "tests/java/ClientTimingScheduleTest.java")

    def test_timing_semantic_rejection_consumes_identity_without_installing_lease(self):
        self.compile_run("CLIENT_TIMING_ADMISSION_OK",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientActionRequest.java",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientRequestGate.java",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingAdmission.java",
                         ROOT / "tests/java/ClientTimingAdmissionTest.java")

    def test_preinstall_rejection_preserves_incumbent_fallback(self):
        self.compile_run("CLIENT_TIMING_REJECT_ISOLATION_OK",
                         ROOT / "tests/java/stubs/net/minecraft/client/input/Input.java",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientActionRequest.java",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientRequestGate.java",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorInput.java",
                         ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingAdmission.java",
                         ROOT / "tests/java/ClientTimingRejectIsolationTest.java")


if __name__ == "__main__":
    unittest.main()
