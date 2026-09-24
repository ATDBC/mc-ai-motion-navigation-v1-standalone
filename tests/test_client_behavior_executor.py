from pathlib import Path
import json
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ClientRequestGateTests(unittest.TestCase):
    def test_entity_guard_binds_fresh_crosshair_identity_and_reach(self):
        source = ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientEntityGuard.java"
        self.assertTrue(source.is_file(), "targeted entity guard is not implemented")
        java_bin = ROOT / ".venv/Library/bin"
        with TemporaryDirectory(prefix="mc2p-java-entity-") as directory:
            built = subprocess.run([
                str(java_bin / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8",
                "-d", directory, str(source), str(ROOT / "tests/java/ClientEntityGuardTest.java"),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([
                str(java_bin / "java.exe"), "-cp", directory, "ClientEntityGuardTest",
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            self.assertIn("CLIENT_ENTITY_GUARD_OK", tested.stdout)

    def test_one_shot_use_releases_fallback_even_on_dispatch_failure(self):
        source = ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientUsePulse.java"
        self.assertTrue(source.is_file(), "bounded one-shot use is not implemented")
        java_bin = ROOT / ".venv/Library/bin"
        with TemporaryDirectory(prefix="mc2p-java-use-") as directory:
            built = subprocess.run([str(java_bin / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8",
                                    "-d", directory, str(source), str(ROOT / "tests/java/ClientUsePulseTest.java")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([str(java_bin / "java.exe"), "-cp", directory, "ClientUsePulseTest"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            self.assertIn("CLIENT_USE_PULSE_OK", tested.stdout)

    def test_block_guard_uses_actual_raycast_and_reach(self):
        source = ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientBlockGuard.java"
        self.assertTrue(source.is_file(), "normal block target guard is not implemented")
        java_bin = ROOT / ".venv/Library/bin"
        with TemporaryDirectory(prefix="mc2p-java-block-") as directory:
            built = subprocess.run([str(java_bin / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8",
                                    "-d", directory, str(source), str(ROOT / "tests/java/ClientBlockGuardTest.java")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([str(java_bin / "java.exe"), "-cp", directory, "ClientBlockGuardTest"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            self.assertIn("CLIENT_BLOCK_GUARD_OK", tested.stdout)

    def test_java_decoder_rejects_unknown_duplicate_coerced_and_oversize_payloads(self):
        from mc2p.contracts.action_v1 import ActionSnapshotV1, OpenInventoryV1
        from mc2p.backends.client_behavior_payload import encode_behavior_action
        source = ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientActionRequest.java"
        self.assertTrue(source.is_file(), "strict client decoder is not implemented")
        gson = next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar"))
        java_bin = ROOT / ".venv/Library/bin"
        valid = encode_behavior_action(ActionSnapshotV1("ep", 0, 0, 100,
                                                        operation=OpenInventoryV1()), now_ns=0).decode()
        cases = [(valid, "accepted:open_inventory"),
                 (valid.replace('"episode_id":"ep"', '"episode_id":"ep","episode_id":"other"'), "rejected"),
                 (valid.replace('"request_sequence_id":0', '"request_sequence_id":true'), "rejected"),
                 (valid.replace('"request_sequence_id":0', '"request_sequence_id":0.5'), "rejected"),
                 (valid.replace('"request_sequence_id":0', '"request_sequence_id":"0"'), "rejected"),
                 (valid.replace('"jump":false', '"jump":"false"'), "rejected"),
                 (valid.replace('"open_inventory"', '"run_command"'), "rejected"),
                 (valid.replace('"kind":"open_inventory"', '"kind":"open_inventory","key":"E"'), "rejected"),
                 (valid + "{}", "rejected"), ("[" * 40 + "0" + "]" * 40, "rejected"),
                 (json.dumps({"extra": "x" * 17000}), "rejected")]
        block = json.loads(valid)
        block["operation"] = {"kind": "interact_block", "block_x": 2, "block_y": -60, "block_z": -3, "face": "up"}
        cases.append((json.dumps(block), "accepted:interact_block"))
        mining = {**block, "operation": {**block["operation"], "kind": "mine_block"}}
        cases.append((json.dumps(mining), "accepted:mine_block"))
        attack = {**block, "operation": {"kind": "attack_entity", "entity_ref": "entity-session-7"}}
        cases.append((json.dumps(attack), "accepted:attack_entity"))
        for invalid_operation in (
            {"kind": "attack_entity", "entity_ref": ""},
            {"kind": "attack_entity", "entity_ref": True},
            {"kind": "attack_entity", "entity_ref": "x" * 129},
            {"kind": "attack_entity", "entity_ref": "entity-1", "extra": 1},
        ):
            cases.append((json.dumps({**block, "operation": invalid_operation}), "rejected"))
        for change in ({"block_x": True}, {"block_y": 1.5}, {"block_z": 30_000_001},
                       {"face": "sideways"}, {"hit_x": 0.5}, {"hand": "skip_main"}):
            invalid = {**block, "operation": {**block["operation"], **change}}
            cases.append((json.dumps(invalid), "rejected"))
            invalid_mining = {**mining, "operation": {**mining["operation"], **change}}
            cases.append((json.dumps(invalid_mining), "rejected"))
        with TemporaryDirectory(prefix="mc2p-java-decode-") as directory:
            built = subprocess.run([str(java_bin / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8",
                                    "-cp", str(gson), "-d", directory, str(source),
                                    str(ROOT / "tests/java/ClientActionDecodeTest.java")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            for payload, expected in cases:
                with self.subTest(payload=payload[:100]):
                    tested = subprocess.run([str(java_bin / "java.exe"), "-cp",
                                             directory + ";" + str(gson), "ClientActionDecodeTest", payload],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(tested.returncode, 0, tested.stderr)
                    self.assertEqual(tested.stdout.strip(), expected)

    def test_real_java_gate_enforces_identity_expiry_cancel_and_thread(self):
        source = ROOT / "mc2p/backends/runtime_overlays/mc121_actions/ClientRequestGate.java"
        self.assertTrue(source.is_file(), "shared client request gate is not implemented")
        java_bin = ROOT / ".venv/Library/bin"
        with TemporaryDirectory(prefix="mc2p-java-gate-") as directory:
            built = subprocess.run([
                str(java_bin / "javac.exe"), "-J-Duser.language=en", "-encoding", "UTF-8", "-d", directory,
                str(source), str(ROOT / "tests/java/ClientRequestGateTest.java"),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([
                str(java_bin / "java.exe"), "-cp", directory, "ClientRequestGateTest",
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            self.assertIn("CLIENT_REQUEST_GATE_OK", tested.stdout)
