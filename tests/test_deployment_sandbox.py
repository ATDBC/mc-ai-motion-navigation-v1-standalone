import base64
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeploymentSandboxTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "scripts/fabric_deployment_sandbox.py").is_file(), "independent sandbox driver is missing")

    def test_new_vanilla_server_has_verified_jar_loopback_only_and_no_actor_commands(self):
        from scripts.fabric_deployment_sandbox import prepare_server
        with TemporaryDirectory(prefix="mc2p-server-setup-") as directory:
            target = Path(directory) / "server"
            evidence = prepare_server(target, seed=21001, port=25597)
            self.assertEqual(evidence["upstream_sha1"], "450698d1863ab5180c25d7c804ef0fe6369dd1ba")
            self.assertEqual(set(path.name for path in target.iterdir()), {"server.jar", "server.properties", "eula.txt"})
            properties = dict(line.split("=", 1) for line in (target / "server.properties").read_text().splitlines())
            self.assertEqual(properties["server-ip"], "127.0.0.1")
            self.assertEqual(properties["server-port"], "25597")
            self.assertEqual(properties["level-seed"], "21001")
            self.assertEqual(properties["gamemode"], "survival")
            flat = json.loads(properties["generator-settings"])
            self.assertEqual(flat["layers"], [{"height": 1, "block": "minecraft:bedrock"},
                {"height": 2, "block": "minecraft:dirt"}, {"height": 1, "block": "minecraft:grass_block"}])
            self.assertEqual(properties["online-mode"], "false")
            self.assertEqual(properties["enable-command-block"], "false")
            self.assertNotIn("commands", evidence)
            with self.assertRaises(FileExistsError):
                prepare_server(target, seed=21002, port=25597)
            self.assertEqual((target / "server.properties").read_text().splitlines().count("level-seed=21001"), 1)

    def test_bad_configuration_fails_before_creating_any_files(self):
        from scripts.fabric_deployment_sandbox import prepare_server
        with TemporaryDirectory(prefix="mc2p-server-setup-") as directory:
            target = Path(directory) / "server"
            for kwargs in ({"seed": True, "port": 25597}, {"seed": 21001, "port": True},
                           {"seed": 21001, "port": 0}, {"seed": 21001, "port": 65536}):
                with self.assertRaises(ValueError):
                    prepare_server(target, **kwargs)
                self.assertFalse(target.exists())

    def test_only_declared_local_fixture_world_names_can_change_server_world(self):
        from scripts.fabric_deployment_sandbox import prepare_server
        with TemporaryDirectory(prefix="mc2p-server-fixture-") as directory:
            target = Path(directory) / "server"
            try:
                evidence = prepare_server(target, seed=21001, port=25597,
                    world_name="mc2p-visibility-deployment-21001")
            except TypeError:
                self.fail("dedicated server cannot select the declared prebuilt fixture")
            properties = dict(line.split("=", 1) for line in (target / "server.properties").read_text().splitlines())
            self.assertEqual(properties["level-name"], "mc2p-visibility-deployment-21001")
            self.assertEqual(evidence["world_name"], properties["level-name"])
            for name in ("..", "../world", "D:/outside", "mc2p-visibility-x\nserver-ip=0.0.0.0", True, "unapproved"):
                rejected = Path(directory) / "rejected"
                with self.subTest(name=name), self.assertRaises(ValueError):
                    prepare_server(rejected, seed=21001, port=25597, world_name=name)
                self.assertFalse(rejected.exists())

    def test_java_argument_file_preserves_values_without_shell_expansion(self):
        from scripts.fabric_deployment_sandbox import write_argument_file
        with TemporaryDirectory(prefix="mc2p-args-") as directory:
            target = Path(directory) / "client.args"
            values = ["space value", "D:\\windows\\path", 'quote"value', "#not-comment", "中文"]
            write_argument_file(target, [str(ROOT / "tests/java/LaunchArgumentEcho.java"), *values])
            result = subprocess.run([str(ROOT / ".venv/Library/bin/java.exe"), "@" + str(target)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [base64.b64encode(value.encode()).decode() for value in values])
            with self.assertRaises(FileExistsError):
                write_argument_file(target, ["no overwrite"])

    def test_container_server_loads_only_the_fresh_hash_verified_fixture(self):
        from scripts.probe_fabric_deployment_observation import prepare_scenario
        from scripts.visibility_fixture_world import _world_hashes
        with TemporaryDirectory(prefix="mc2p-deployment-world-") as directory:
            root = Path(directory)
            result = prepare_scenario(root, seed=21001, port=25597, container_probe=True)
            manifest = result["fixture"]
            world = root / "server" / result["server"]["world_name"]
            self.assertEqual(manifest["level_name"], "mc2p-visibility-deployment-21001")
            self.assertEqual(manifest["seed"], 21001)
            self.assertEqual(_world_hashes(world), manifest["files"])
            self.assertEqual(_world_hashes(root / "fixture/world"), manifest["files"])
            with self.assertRaises(FileExistsError):
                prepare_scenario(root, seed=21001, port=25597, container_probe=True)
            self.assertEqual(_world_hashes(world), manifest["files"])

    def test_parent_environment_cannot_silently_inject_java_or_fabric_launch_options(self):
        from scripts.fabric_deployment_sandbox import client_environment
        value = client_environment({"PATH": "preserve", "JAVA_TOOL_OPTIONS": "-javaagent:foreign.jar",
            "_JAVA_OPTIONS": "-Xmx20G", "JDK_JAVA_OPTIONS": "-Xmx30G", "CLASSPATH": "foreign.jar",
            "FABRIC_ADD_MODS": "foreign.jar", "MC2P_SESSION_TOKEN": "old"}, token="a" * 64, server_port=25597, ipc_port=8140)
        self.assertEqual(value["PATH"], "preserve")
        for key in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH", "FABRIC_ADD_MODS"):
            self.assertNotIn(key, value)
        self.assertEqual(value["MC2P_SESSION_TOKEN"], "a" * 64)
        self.assertEqual(value["MC2P_IPC_PORT"], "8140")
        self.assertEqual(value["HTTPS_PROXY"], "http://127.0.0.1:7897")

    def test_time_diagnostics_require_explicit_sanitized_client_opt_in(self):
        from scripts.fabric_deployment_sandbox import client_environment
        inputs = dict(token="a" * 64, server_port=25597, ipc_port=8140)
        parent = {"MC2P_TIME_DIAGNOSTICS": "1"}
        self.assertNotIn("MC2P_TIME_DIAGNOSTICS", client_environment(parent, **inputs))
        try:
            result = client_environment(parent, **inputs, time_diagnostics=True)
        except TypeError:
            self.fail("client launch cannot explicitly opt into time diagnostics")
        self.assertEqual(result["MC2P_TIME_DIAGNOSTICS"], "1")
        for invalid in (1, "1", None):
            with self.assertRaises(ValueError):
                client_environment(parent, **inputs, time_diagnostics=invalid)

    def test_physics_tick_diagnostics_require_time_and_explicit_opt_in(self):
        from scripts.fabric_deployment_sandbox import client_environment
        inputs = dict(token="a" * 64, server_port=25597, ipc_port=8140)
        parent = {"MC2P_PHYSICS_TICK_DIAGNOSTICS": "1"}
        self.assertNotIn("MC2P_PHYSICS_TICK_DIAGNOSTICS", client_environment(parent, **inputs))
        with self.assertRaises(ValueError):
            client_environment(parent, **inputs, physics_tick_diagnostics=True)
        result = client_environment(
            parent, **inputs, time_diagnostics=True, physics_tick_diagnostics=True)
        self.assertEqual(result["MC2P_PHYSICS_TICK_DIAGNOSTICS"], "1")
        self.assertEqual(result["MC2P_TIME_DIAGNOSTICS"], "1")

    def test_registered_launch_pid_is_the_actual_jvm_not_a_conda_forwarder(self):
        from scripts.fabric_deployment_sandbox import JAVA
        import psutil
        from scripts.timing_parallel_probe_core import ProcessIdentityV0, capture_registered_tree, terminate_registered_tree
        child = subprocess.Popen([str(JAVA), str(ROOT / "tests/java/LaunchProcessIdentity.java")],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        identity = ProcessIdentityV0(child.pid, psutil.Process(child.pid).create_time())
        registered = (identity,)
        try:
            registered = capture_registered_tree(identity).identities
            stdout, stderr = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(int(stdout.strip()), child.pid)
        finally:
            if child.poll() is None:
                registered = capture_registered_tree(identity).identities
            cleanup = terminate_registered_tree(identity, registered, grace_seconds=1)
            child.communicate(timeout=3)
            self.assertEqual(cleanup.surviving, ())
