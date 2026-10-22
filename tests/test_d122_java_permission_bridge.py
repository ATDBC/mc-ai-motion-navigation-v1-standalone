from pathlib import Path
import json
import subprocess
from tempfile import TemporaryDirectory
import unittest

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementTickWindowV1, MovementV1, SafeDefaultV1
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.motion_nav.online_motion import (InputApplicationLedger, InputApplicationStatus,
                                           input_application_has_finished)
from mc2p.motion_nav.world_model import WorldSessionId
from mc2p.contracts.common import ContractViolation
from mc2p.runtime.transport_lifecycle import prove_remote_rejected_before_sampling
from tests.test_action_receipt import receipt_value


ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / ".venv/Library/bin"
ACTION = ROOT / "mc2p/backends/runtime_overlays/mc121_actions"


class D122JavaPermissionBridgeTests(unittest.TestCase):
    def test_rejected_successor_response_waits_for_parent_default_evidence(self):
        with TemporaryDirectory(prefix="mc2p-d122-rejected-hold-") as directory:
            sources = [ACTION / name for name in (
                "ClientActionRequest.java", "ClientRequestGate.java", "ClientTimingSchedule.java",
                "ClientBehaviorInput.java", "ClientTimingAdmission.java")]
            gson = next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar"))
            built = subprocess.run([
                str(JAVA / "javac.exe"), "-encoding", "UTF-8", "-cp", str(gson), "-d", directory,
                str(ROOT / "tests/java/stubs/net/minecraft/client/input/Input.java"),
                *(str(item) for item in sources), str(ROOT / "tests/java/ClientTimingRejectIsolationTest.java"),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([
                str(JAVA / "java.exe"), "-cp", directory + ";" + str(gson),
                "ClientTimingRejectIsolationTest",
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
        lines = dict(line.split("=", 1) for line in tested.stdout.splitlines() if "=" in line)
        applications = []
        for encoded in filter(None, lines["SAMPLES"].split(";")):
            tick, sequence, state, sneak = encoded.split(":")
            default = state == "default_active"
            applications.append(dict(
                schema_version="mc2p.input-application.v1", movement_tick_id=int(tick),
                episode_id="ep", request_sequence_id=int(sequence), sampled_at_jvm_ns=int(tick),
                state=state, forward=0.0 if default or state != "leased" else 1.0, strafe=0.0,
                jump=False, sneak=sneak == "true", sprint=state == "leased"))
        events = []
        for encoded in filter(None, lines["EVENTS"].split(";")):
            status, tick, sequence = encoded.split(":")
            events.append(dict(status=status, movement_tick_id=int(tick), episode_id="ep",
                               request_sequence_id=int(sequence)))

        parent = ActionSnapshotV1("ep", 25, 7, 2_000_000_000, movement=MovementV1(forward=1, sprint=True),
                                  safe_default=SafeDefaultV1(2, MovementV1(sneak=True), 8, 400_000_000))
        successor = ActionSnapshotV1(
            "ep", 26, 7, 2_000_000_000, movement=MovementV1(forward=1, jump=True, sprint=True),
            first_effect_window=MovementTickWindowV1(1, 1),
            safe_default=SafeDefaultV1(2, MovementV1(sneak=True), 8, 400_000_000))
        ledger = InputApplicationLedger()
        session = WorldSessionId("w")
        ledger.submit(session, parent, requested_first_tick=1)
        ledger.submit(session, successor, requested_first_tick=1)
        value = dict(receipt_value(episode_id="ep", request_sequence_id=26, input_samples=10),
                     schema_version="mc2p.client_action_receipt.v4", status="rejected",
                     generation_id=8,
                     reason="safe_default_budget_insufficient", input_applications=applications,
                     dropped_input_samples=0, oldest_retained_input_tick=1, timing_events=events)
        receipt = behavior_receipt_from_mapping(value)
        proof = prove_remote_rejected_before_sampling(successor, receipt)
        ledger.observe_receipt(
            receipt,
            confirmed_not_applied_sequence=proof.request_sequence_id,
        )
        self.assertEqual(ledger.record(26).status, InputApplicationStatus.REJECTED)
        self.assertEqual(len(ledger.record(25).default_samples), 8)
        self.assertTrue(input_application_has_finished(ledger.record(25)))

        missing_value = dict(
            value,
            timing_events=[
                event for event in events if event["status"] != "not_applied"
            ],
        )
        missing_receipt = behavior_receipt_from_mapping(missing_value)
        unresolved = InputApplicationLedger()
        unresolved.submit(session, parent, requested_first_tick=1)
        unresolved.submit(session, successor, requested_first_tick=1)
        with self.assertRaises(ContractViolation):
            prove_remote_rejected_before_sampling(successor, missing_receipt)
        unresolved.observe_receipt(missing_receipt)
        self.assertEqual(
            unresolved.record(26).status,
            InputApplicationStatus.IN_FLIGHT,
        )

    def test_python_and_java_share_default_relation_vectors(self):
        from tests.sim.client_timing import ClientTimingSampler
        rows = (ROOT / "tests/fixtures/d122_default_relation_vectors.tsv").read_text("utf-8").splitlines()[1:]
        for row in rows:
            kind, next_tick, duration, earliest, latest, activation, accepted = row.split("\t")
            with self.subTest(row=row):
                try:
                    window = None if kind == "ordinary" else MovementTickWindowV1(int(earliest), int(latest))
                    action = ActionSnapshotV1(
                        "ep", 25, 0, 30_000_000_000, movement=MovementV1(forward=1),
                        valid_for_ticks=int(duration), first_effect_window=window,
                        safe_default=SafeDefaultV1(int(activation), MovementV1(sneak=True), 8, 400_000_000),
                    )
                    sampler = ClientTimingSampler("ep")
                    sampler.admit(action, int(next_tick), 0, 0)
                    if window is not None:
                        sampler.sample(window.earliest_tick, 0)
                    actual = True
                except ContractViolation:
                    actual = False
                self.assertEqual(actual, accepted == "true")

    def test_permission_revocation_closes_parent_and_pending_in_python_ledger(self):
        with TemporaryDirectory(prefix="mc2p-d122-permission-") as directory:
            sources = [ACTION / name for name in (
                "ClientRequestGate.java", "ClientTimingSchedule.java", "ClientBehaviorInput.java")]
            built = subprocess.run([
                str(JAVA / "javac.exe"), "-encoding", "UTF-8", "-d", directory,
                str(ROOT / "tests/java/stubs/net/minecraft/client/input/Input.java"),
                *(str(item) for item in sources), str(ROOT / "tests/java/ClientPermissionRevocationTest.java"),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([
                str(JAVA / "java.exe"), "-cp", directory, "ClientPermissionRevocationTest",
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            facts = json.loads(tested.stdout)

        parent = ActionSnapshotV1("ep", 25, 0, 2_000_000_000, movement=MovementV1(forward=1),
                                  safe_default=SafeDefaultV1(2, MovementV1(sneak=True), 8, 400_000_000))
        pending = ActionSnapshotV1("ep", 26, 0, 2_000_000_000, movement=MovementV1(jump=True),
                                   first_effect_window=MovementTickWindowV1(1, 1))
        ledger = InputApplicationLedger()
        session = WorldSessionId("w")
        ledger.submit(session, parent, requested_first_tick=1)
        ledger.submit(session, pending, requested_first_tick=1)
        events = []
        for encoded in filter(None, facts["events"].split(";")):
            status, tick, sequence = encoded.split(":")
            events.append(dict(
                status=status,
                movement_tick_id=int(tick),
                episode_id="ep",
                request_sequence_id=int(sequence),
            ))
        value = dict(receipt_value(episode_id="ep", request_sequence_id=26,
                                   input_samples=facts["input_samples"]),
                     schema_version="mc2p.client_action_receipt.v4",
                     status="rejected", reason=facts["reason"],
                     dropped_input_samples=0, oldest_retained_input_tick=1,
                     input_applications=[dict(
                         schema_version="mc2p.input-application.v1", movement_tick_id=facts["sample_tick"],
                         episode_id="ep", request_sequence_id=facts["sample_sequence"],
                         sampled_at_jvm_ns=1, state="disallowed", forward=0.0, strafe=0.0,
                         jump=False, sneak=False, sprint=False)],
                     timing_events=events)
        receipt = behavior_receipt_from_mapping(value)
        proof = prove_remote_rejected_before_sampling(pending, receipt)
        ledger.observe_receipt(
            receipt,
            confirmed_not_applied_sequence=proof.request_sequence_id,
        )
        self.assertEqual(ledger.record(25).status, InputApplicationStatus.REJECTED)
        self.assertEqual(ledger.record(26).status, InputApplicationStatus.REJECTED)


if __name__ == "__main__":
    unittest.main()
