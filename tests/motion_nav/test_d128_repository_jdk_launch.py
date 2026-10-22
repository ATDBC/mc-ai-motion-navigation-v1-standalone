from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import d123_fabric_transport as probe
from scripts import d128_repository_jdk as binding
from scripts import probe_fabric_deployment_observation as deployment
from scripts.export_motion_navigation_standalone import ExportViolation, JavaTools


ROOT = Path(__file__).resolve().parents[2]


class D128RepositoryJdkLaunchTests(unittest.TestCase):
    def test_empty_terminal_environment_uses_only_repository_jdk21(self):
        with patch.dict(os.environ, {}, clear=True):
            tools = probe.resolve_repository_java_tools(repository_root=ROOT)
        expected = (ROOT / ".venv/Library/bin").resolve()
        self.assertEqual(tools.major_version, 21)
        self.assertEqual(tools.java, expected / "java.exe")
        self.assertEqual(tools.javac, expected / "javac.exe")

    def test_missing_or_wrong_repository_jdk_is_rejected_without_system_fallback(self):
        with TemporaryDirectory() as root:
            repository = Path(root)
            with self.assertRaisesRegex(ExportViolation, "repository OpenJDK 21"):
                probe.resolve_repository_java_tools(repository_root=repository)
            binary = repository / ".venv/Library/bin"
            binary.mkdir(parents=True)
            (binary / "java.exe").write_bytes(b"wrong-java")
            (binary / "javac.exe").write_bytes(b"wrong-javac")
            wrong = SimpleNamespace(returncode=0, stdout="javac 17.0.12", stderr="")
            with patch(
                "scripts.export_motion_navigation_standalone.subprocess.run",
                return_value=wrong,
            ), self.assertRaisesRegex(ExportViolation, "repository OpenJDK 21"):
                probe.resolve_repository_java_tools(repository_root=repository)

    def test_ambient_java_and_injected_binding_variables_are_removed(self):
        tools = probe.resolve_repository_java_tools(repository_root=ROOT)
        with TemporaryDirectory() as root:
            ambient = Path(root)
            (ambient / "java.exe").write_bytes(b"ambient-java")
            (ambient / "javac.exe").write_bytes(b"ambient-javac")
            with patch.dict(os.environ, {
                "JAVA_HOME": str(ambient),
                "PATH": str(ambient),
                "MC2P_D128_EVIL": "injected",
                binding.ENV_VENDOR: "injected-vendor",
            }, clear=True):
                selected = probe.resolve_repository_java_tools(repository_root=ROOT)
                environment = probe.repository_java_environment(selected)
        self.assertEqual(selected, tools)
        self.assertNotIn("MC2P_D128_EVIL", environment)
        self.assertEqual(environment[binding.ENV_VENDOR], tools.vendor)
        self.assertEqual(environment["JAVA_HOME"], str(tools.java.parent.parent))
        self.assertNotIn(str(ambient), environment["PATH"].split(os.pathsep))

    def test_different_canonical_home_and_reparse_are_rejected(self):
        with TemporaryDirectory() as root:
            repository = Path(root)
            binary = repository / ".venv/Library/bin"
            binary.mkdir(parents=True)
            java = binary / "java.exe"
            javac = binary / "javac.exe"
            java.write_bytes(b"java")
            javac.write_bytes(b"javac")
            tools = JavaTools(java.resolve(), javac.resolve(), 21)
            first = {
                "java.home": str(repository / ".venv/Library/lib/jvm"),
                "java.vendor": "vendor", "java.version": "21.0.1",
            }
            second = {**first, "java.home": str(repository / "other-jdk")}
            with patch.object(binding, "discover_java_tools", return_value=tools), \
                    patch.object(binding, "_no_links"), \
                    patch.object(binding, "_properties", side_effect=(first, second)), \
                    self.assertRaisesRegex(ExportViolation, "repository OpenJDK 21"):
                binding.resolve_repository_jdk(repository_root=repository)
        with patch.object(binding, "_no_links", side_effect=ValueError("reparse")), \
                self.assertRaisesRegex(ExportViolation, "repository OpenJDK 21"):
            binding.resolve_repository_jdk(repository_root=ROOT)

    def test_bound_environment_rejects_identity_and_hash_mutations(self):
        tools = probe.resolve_repository_java_tools(repository_root=ROOT)
        environment = probe.repository_java_environment(tools, base_environment={})
        self.assertEqual(
            binding.validate_bound_environment(environment, repository_root=ROOT),
            tools.canonical_java,
        )
        for name, value in (
            ("JAVA_HOME", str(ROOT / "other-jdk")),
            (binding.ENV_CANONICAL_HOME, str(ROOT / "other-jdk")),
            (binding.ENV_JAVA_SHA256, "0" * 64),
            (binding.ENV_VENDOR, ""),
        ):
            changed = dict(environment)
            changed[name] = value
            with self.subTest(name=name), self.assertRaises(ExportViolation):
                binding.validate_bound_environment(changed, repository_root=ROOT)

    def test_invalid_probe_binding_stops_before_server_or_client_popen(self):
        argv = [
            "--d123-transport-probe", "--time-diagnostics",
            "--physics-tick-diagnostics", "--ftick-diagnostics",
            "--timeout-seconds", "600",
        ]
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(deployment.subprocess, "Popen") as popen, \
                self.assertRaises(ExportViolation):
            deployment.main(argv)
        popen.assert_not_called()

    def test_server_and_client_java_is_the_verified_canonical_executable(self):
        tools = probe.resolve_repository_java_tools(repository_root=ROOT)
        environment = probe.repository_java_environment(tools, base_environment={})
        self.assertEqual(deployment.JAVA.resolve(), tools.canonical_java)
        self.assertEqual(
            binding.validate_bound_environment(environment, repository_root=ROOT),
            deployment.JAVA.resolve(),
        )
        self.assertEqual(
            deployment._d128_child_environment(environment),
            {
                "JAVA_HOME": environment["JAVA_HOME"],
                "PATH": environment["PATH"],
                **{name: value for name, value in environment.items()
                   if name.startswith(binding.ENV_PREFIX)},
            },
        )

    def test_java_unittest_and_gradle_receive_the_same_bound_environment(self):
        tools = probe.resolve_repository_java_tools(repository_root=ROOT)
        environment = probe.repository_java_environment(
            tools, base_environment={"SYSTEMROOT": os.environ["SYSTEMROOT"]},
        )
        completed = SimpleNamespace(
            returncode=0, stdout="passed", stderr="",
        )
        with patch.object(probe.subprocess, "run", return_value=completed) as run:
            result = probe._java_overlay_gate(tools, environment)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(run.call_count, 2)
        java_environment = run.call_args_list[0].kwargs["env"]
        gradle_environment = run.call_args_list[1].kwargs["env"]
        self.assertEqual(java_environment, environment)
        self.assertEqual(
            {name: value for name, value in gradle_environment.items()
             if name != "GRADLE_USER_HOME"},
            environment,
        )
        self.assertEqual(
            gradle_environment["GRADLE_USER_HOME"],
            str(ROOT / ".gradle"),
        )

    def test_run_worker_passes_the_complete_binding_to_server_and_client(self):
        tools = probe.resolve_repository_java_tools(repository_root=ROOT)
        environment = probe.repository_java_environment(
            tools, base_environment={"SYSTEMROOT": os.environ["SYSTEMROOT"]},
        )
        expected_binding = {
            name: value for name, value in environment.items()
            if name.startswith(binding.ENV_PREFIX)
        }
        launched = []

        def prepare(run_dir, **_kwargs):
            (run_dir / "server").mkdir(parents=True)
            return {}

        def child_environment(source, **_kwargs):
            return {"SYSTEMROOT": source["SYSTEMROOT"]}

        def popen(command, **kwargs):
            launched.append((command, kwargs["env"]))
            for name, value in expected_binding.items():
                self.assertEqual(kwargs["env"].get(name), value)
            self.assertEqual(kwargs["env"]["JAVA_HOME"], environment["JAVA_HOME"])
            self.assertEqual(kwargs["env"]["PATH"], environment["PATH"])
            if len(launched) == 1:
                kwargs["stdout"].write(b"Done (")
                kwargs["stdout"].flush()
                return SimpleNamespace(pid=41001)
            self.assertEqual(command[0], str(tools.canonical_java))
            raise RuntimeError("stop after client Popen boundary")

        process = SimpleNamespace(create_time=lambda: 1.0)
        launch = {"jvm_args": [], "classpath": [], "main_class": "TestMain"}
        with TemporaryDirectory() as root, patch.dict(
                os.environ, environment, clear=True), \
                patch.object(deployment, "validate_probe_gradle_home",
                             side_effect=lambda path: path), \
                patch.object(deployment, "frozen_deployment_sources",
                             return_value={"frozen": "same"}), \
                patch.object(deployment, "inspect_launch", return_value={}), \
                patch.object(deployment, "verify_assets", return_value={}), \
                patch.object(deployment, "prepare_scenario", side_effect=prepare), \
                patch.object(deployment, "client_environment",
                             side_effect=child_environment), \
                patch.object(deployment.subprocess, "Popen", side_effect=popen), \
                patch.object(deployment.psutil, "Process", return_value=process), \
                patch.object(deployment, "_live"), \
                patch.object(deployment, "DeploymentTransport",
                             return_value=SimpleNamespace()), \
                patch.object(deployment, "_close_client",
                             return_value=(None, [])), \
                patch.object(deployment, "collect_time_evidence",
                             return_value=({"name": "time", "passed": True}, None)), \
                patch.object(deployment, "_stop", return_value={
                    "passed": True, "graceful": True, "return_code": 0,
                }):
            result = deployment.run_worker(
                Path(root), launch, 21001, 25597, 8140, 600,
                d123_transport_probe=True,
                gradle_home=ROOT / ".gradle",
            )
        self.assertEqual(result, 1)
        self.assertEqual(len(launched), 2)
        for command, child in launched:
            self.assertEqual(command[0], str(tools.canonical_java))
            self.assertEqual(child["JAVA_HOME"], environment["JAVA_HOME"])
            self.assertEqual(child["PATH"], environment["PATH"])
            self.assertEqual(
                {name: child.get(name) for name in expected_binding},
                expected_binding,
            )

    def test_launch_passes_one_verified_jdk_identity_to_preflight_and_child(self):
        tools = probe.resolve_repository_java_tools(repository_root=ROOT)
        expected_java_hash = hashlib.sha256(tools.java.read_bytes()).hexdigest()
        expected_javac_hash = hashlib.sha256(tools.javac.read_bytes()).hexdigest()
        preflight = {"schema": "test", "status": "PASS"}
        with TemporaryDirectory() as root:
            output = Path(root) / "launch"
            with patch.object(probe, "resolve_repository_java_tools",
                              return_value=tools) as resolved, \
                    patch.object(probe, "offline_preflight",
                                 return_value=preflight) as gate, \
                    patch.object(probe, "validate_offline_preflight",
                                 return_value=True), \
                    patch.object(probe.subprocess, "run", return_value=SimpleNamespace(
                        stdout="", stderr="blocked-before-fabric", returncode=2,
                    )) as launched:
                result = probe.main([
                    "--launch", "smoke", "--output", str(output),
                ])
        self.assertEqual(result, 2)
        resolved.assert_called_once_with(repository_root=ROOT)
        gate_kwargs = gate.call_args.kwargs
        child_environment = launched.call_args.kwargs["env"]
        self.assertIs(gate_kwargs["java_tools"], tools)
        self.assertEqual(gate_kwargs["java_environment"], child_environment)
        self.assertEqual(
            Path(child_environment["JAVA_HOME"]).resolve(),
            tools.java.parent.parent,
        )
        self.assertEqual(
            Path(child_environment["PATH"].split(os.pathsep)[0]).resolve(),
            tools.java.parent,
        )
        self.assertEqual(child_environment[binding.ENV_JAVA_SHA256],
                         expected_java_hash)
        self.assertEqual(child_environment[binding.ENV_JAVAC_SHA256],
                         expected_javac_hash)


if __name__ == "__main__":
    unittest.main()
