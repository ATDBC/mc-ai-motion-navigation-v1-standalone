"""Locked launch export and cache integrity, without launching a Minecraft process."""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


class DeploymentLaunchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.build_fabric_deployment_probe import validate_probe_gradle_home
        cls.script = ROOT / "scripts/fabric_deployment_launch.py"
        cls.gradle_home = validate_probe_gradle_home(
            (ROOT / ".tmp/d122-gradle-home").absolute(),
        )

    def test_real_loom_export_is_independent_and_all_runtime_inputs_are_verified(self):
        self.assertTrue(self.script.is_file(), "independent launch provenance driver is missing")
        from scripts.fabric_deployment_launch import export_launch, inspect_launch, verify_assets
        path = export_launch(gradle_home=self.gradle_home)
        launch = json.loads(path.read_text("utf-8"))
        self.assertEqual(launch["schema_version"], "mc2p.deployment_launch.v1")
        self.assertEqual(launch["main_class"], "net.fabricmc.devlaunchinjector.Main")
        evidence = inspect_launch(launch, gradle_home=self.gradle_home)
        self.assertGreater(evidence["jar_count"], 0)
        self.assertGreater(evidence["remap_jar_count"], 0)
        self.assertEqual(evidence["project_directory_count"], 2)
        self.assertTrue(evidence["client_only"])
        self.assertEqual(set(launch["auxiliary"]), {
            "deployment/fabric-observation-probe/.gradle/loom-cache/launch.cfg",
            "deployment/fabric-observation-probe/.gradle/loom-cache/log4j.xml",
            "deployment/fabric-observation-probe/.gradle/loom-cache/remapClasspath.txt",
        })
        assets = verify_assets(gradle_home=self.gradle_home)
        self.assertEqual(assets["index_sha1"], "6ae5eb70fe411facbb5c6c66003475c07a251a76")
        self.assertGreater(assets["object_count"], 1000)
        for mutate in (
            lambda item: item.update(main_class="unwanted.Server"),
            lambda item: item["classpath"].clear(),
            lambda item: item["remap_classpath"].clear(),
            lambda item: item["classpath"][0].update(sha256="0" * 64),
            lambda item: item["jvm_args"].append("-javaagent:unwanted.jar"),
            lambda item: item["jvm_args"].append("@unverified.args"),
            lambda item: item["environment"].update(FABRIC_ADD_MODS="unverified.jar"),
            lambda item: item["auxiliary"].pop("deployment/fabric-observation-probe/.gradle/loom-cache/remapClasspath.txt"),
            lambda item: item["classpath"].append({"path": "../outside", "kind": "directory", "files": {}}),
        ):
            changed = deepcopy(launch)
            mutate(changed)
            with self.subTest(changed=changed["main_class"]), self.assertRaises(ValueError):
                inspect_launch(changed, gradle_home=self.gradle_home)

        # Model stale-but-consistent dev output without modifying any real cache/artifact.
        # Only file/ZIP reads are substituted; every provenance and launch validator is real.
        dev = ROOT / "deployment/fabric-observation-probe/build/devlibs/mc2p-deployment-probe-0.1.0-dev.jar"
        resource = ROOT / "deployment/fabric-observation-probe/build/resources/main/mc2p-compiled-sources.json"
        original = resource.read_bytes()
        source_hash = next(iter(json.loads(original)["sources"].values()))
        wrong_hash = ("0" if source_hash[0] != "0" else "1") + source_hash[1:]
        stale = original.replace(source_hash.encode(), wrong_hash.encode(), 1)
        buffer = BytesIO()
        with ZipFile(dev) as old, ZipFile(buffer, "w") as out:
            for info in old.infolist():
                out.writestr(info, stale if info.filename == resource.name else old.read(info.filename))
        stale_zip = buffer.getvalue()
        changed = deepcopy(launch)
        entry, = [item for item in changed["classpath"] if item["path"] == resource.parent.relative_to(ROOT).as_posix()]
        entry["files"][resource.name]["sha256"] = hashlib.sha256(stale).hexdigest()
        original_open = Path.open
        def read_file(path, *args, **kwargs):
            if path == resource:
                self.assertEqual(args[0] if args else kwargs.get("mode"), "rb")
                return BytesIO(stale)
            return original_open(path, *args, **kwargs)
        def read_zip(path, *args, **kwargs):
            return ZipFile(BytesIO(stale_zip) if path == dev else path, *args, **kwargs)
        with patch.object(Path, "open", read_file), \
                patch("scripts.fabric_deployment_launch.ZipFile", read_zip), \
                patch("scripts.build_fabric_deployment_probe.ZipFile", read_zip):
            with self.assertRaisesRegex(ValueError, "provenance/manifest"):
                inspect_launch(changed, gradle_home=self.gradle_home)
