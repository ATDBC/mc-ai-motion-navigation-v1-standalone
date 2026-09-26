"""Exercise the real collector on detached vanilla objects, without a game server."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VisibleEquipmentProjectionTests(unittest.TestCase):
    def test_real_collector_does_not_serialize_other_equipment_private_components(self):
        self._run_java_harness("VisibleEquipmentProjectionTest", "VISIBLE_EQUIPMENT_PROJECTION_OK")

    def test_native_entity_name_visibility_conditions_without_rendering(self):
        self._run_java_harness("VisibleEntityNameTest", "VISIBLE_ENTITY_NAME_OK")

    def _run_java_harness(self, harness: str, marker: str, *,
                          extra_sources: tuple[Path, ...] = (), args: tuple[str, ...] = ()) -> None:
        # Use only the locked 1.21 runtime's already-cached Java dependencies. No downloads,
        # arbitrary newest-version glob, or dependency on a generated test sandbox.
        metadata = json.loads((ROOT / ".gradle/caches/fabric-loom/1.21/minecraft-info.json").read_text("utf-8"))
        self.assertEqual(metadata["id"], "1.21")
        named = ROOT / ".gradle/caches/fabric-loom/minecraftMaven/net/minecraft/minecraft-merged/1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2/minecraft-merged-1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2.jar"
        self.assertTrue(named.is_file())
        jars = [named]
        for library in metadata["libraries"]:
            coordinate = library["name"].split(":")
            if len(coordinate) != 3 or coordinate[0] == "ca.weblite":
                continue  # Native classifier jars and macOS-only bridge are not needed.
            group, name, version = coordinate
            folder = ROOT / ".gradle/caches/modules-2/files-2.1" / group / name / version
            matches = list(folder.glob(f"*/{name}-{version}.jar"))
            self.assertEqual(len(matches), 1, f"locked Java library missing or ambiguous: {library['name']}")
            jars.append(matches[0])
        for group, name, version in (("net.fabricmc", "fabric-loader", "0.15.11"),
                                     ("org.ow2.asm", "asm", "9.6"),
                                     ("org.jetbrains", "annotations", "24.1.0")):
            matches = list((ROOT / ".gradle/caches/modules-2/files-2.1" / group / name / version).glob(f"*/{name}-{version}.jar"))
            self.assertEqual(len(matches), 1)
            jars.append(matches[0])
        classpath = ";".join(map(str, jars))
        java = ROOT / ".venv/Library/bin"
        sources = list((ROOT / "mc2p/backends/runtime_overlays/mc121_observation").glob("*.java"))
        sources.extend((
            ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientRequestGate.java",
            ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorInput.java",
        ))
        harness_path = Path(harness)
        harness_class = harness_path.name
        with TemporaryDirectory(prefix="mc2p-visible-equipment-") as directory:
            compiled = subprocess.run([str(java / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8", "-proc:none",
                "-cp", classpath, "-d", directory, *map(str, sources),
                str(ROOT / "tests/java" / harness_path.with_suffix(".java")),
                str(ROOT / "tests/java/DetachedTestWorld.java"),
                str(ROOT / "tests/java/VanillaObjectTestHost.java"), *map(str, extra_sources)],
                cwd=directory, capture_output=True, text=True, timeout=45)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run([str(java / "java.exe"), "-cp", directory + ";" + classpath,
                "VanillaObjectTestHost", harness_class, *args],
                cwd=directory, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(marker, result.stdout)
