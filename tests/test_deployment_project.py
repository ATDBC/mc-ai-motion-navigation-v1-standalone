"""The deployed jar, not source-string markers, defines client-only/source parity evidence."""
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


class DeploymentProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.build_fabric_deployment_probe import build_probe
        cls.jar = build_probe(timeout_seconds=180)

    def test_real_offline_build_contains_shared_client_only_classes_and_provenance(self):
        script = ROOT / "scripts/build_fabric_deployment_probe.py"
        self.assertTrue(script.is_file(), "standalone build driver is missing")
        from scripts.build_fabric_deployment_probe import inspect_probe
        evidence = inspect_probe(self.jar)
        self.assertTrue(evidence["client_only"])
        self.assertTrue(evidence["shared_sources_match"])
        self.assertTrue(evidence["no_craftground_dependency"])
        self.assertGreaterEqual(evidence["shared_class_count"], 18)
        self.assertTrue(evidence["required_mixins_present"])
        self.assertGreater(evidence["verified_classpath_file_count"], 0)

    def test_actual_jar_binds_shared_time_diagnostics_and_native_hooks(self):
        with ZipFile(self.jar) as archive:
            names = set(archive.namelist())
            self.assertTrue({"com/mc2p/diagnostics/ClientTimeTrace.class",
                "com/mc2p/diagnostics/ClientTimeDiagnostics.class"} <= names,
                "independent client has no shared time diagnostic implementation")
            mixins = json.loads(archive.read("mc2p-deployment.mixins.json"))
            self.assertTrue({"ClientClockTickMixin", "ClientClockWorldMixin", "ClientClockPacketMixin"} <= set(mixins["client"]))

    def test_rejects_tampered_entrypoints_sources_dependencies_and_missing_mixin(self):
        from scripts.build_fabric_deployment_probe import inspect_probe
        with ZipFile(self.jar) as source:
            contents = {name: source.read(name) for name in source.namelist()}
        cases = [
            ("fabric.mod.json", lambda item: item["entrypoints"].update(main=["unwanted.Server"])),
            ("fabric.mod.json", lambda item: item.update(environment="*")),
            ("fabric.mod.json", lambda item: item.update(mixins=[])),
            ("mc2p-compiled-sources.json", lambda item: item["sources"].update({"foreign/Hidden.java": "0" * 64})),
            ("mc2p-compiled-sources.json", lambda item: item["source_roots"].append("foreign")),
            ("mc2p-compiled-sources.json", lambda item: item["sources"].pop(next(iter(item["sources"])))),
            ("mc2p-compiled-sources.json", lambda item: item["classpath"].append("com.craftground:runtime:1")),
            ("mc2p-compiled-sources.json", lambda item: item.pop("classpath_files")),
            ("mc2p-compiled-sources.json", lambda item: item.update(classpath_files=[])),
            ("mc2p-compiled-sources.json", lambda item: item["classpath_files"][0].update(sha256="0" * 64)),
            ("mc2p-deployment.mixins.json", lambda item: item.update(required=False)),
            ("mc2p-deployment.mixins.json", lambda item: item.update(package="unrelated.mixin")),
            ("mc2p-deployment.mixins.json", lambda item: item.update(refmap="missing.refmap.json")),
            ("mc2p-deployment.mixins.json", lambda item: item["client"].remove("ScreenshotGuardMixin")),
            ("mc2p-deployment.mixins.json", lambda item: item["client"].remove("ClientClockPacketMixin")),
        ]
        with TemporaryDirectory(prefix="mc2p-probe-jar-") as directory:
            for number, (name, mutate) in enumerate(cases):
                with self.subTest(number=number):
                    changed = dict(contents)
                    item = json.loads(changed[name])
                    mutate(item)
                    changed[name] = json.dumps(item).encode()
                    target = Path(directory) / f"bad-{number}.jar"
                    with ZipFile(target, "w") as artifact:
                        for key, value in changed.items():
                            artifact.writestr(key, value)
                    with self.assertRaises(ValueError):
                        inspect_probe(target)

    def test_rejects_packaged_foreign_classes_and_missing_shared_class(self):
        from scripts.build_fabric_deployment_probe import inspect_probe
        with ZipFile(self.jar) as source:
            contents = {name: source.read(name) for name in source.namelist()}
        with TemporaryDirectory(prefix="mc2p-probe-jar-") as directory:
            for missing in (False, "com/mc2p/actions/ClientBehaviorExecutor.class", "com/mc2p/diagnostics/ClientTimeTrace.class"):
                with self.subTest(missing=missing):
                    changed = dict(contents)
                    if missing:
                        del changed[missing]
                    else:
                        changed["com/kyhsgeekcode/Unexpected.class"] = b"not a real class"
                    target = Path(directory) / ("bad-missing.jar" if missing else "bad-foreign.jar")
                    with ZipFile(target, "w") as artifact:
                        for key, value in changed.items():
                            artifact.writestr(key, value)
                    with self.assertRaises(ValueError):
                        inspect_probe(target)

    def test_c1_fixture_is_not_packaged_in_the_formal_client_jar(self):
        with ZipFile(self.jar) as archive:
            names = set(archive.namelist())
        self.assertFalse(any(name.startswith("com/mc2p/fixture/") for name in names))
        self.assertNotIn("c1-fixture-events.jsonl", names)
