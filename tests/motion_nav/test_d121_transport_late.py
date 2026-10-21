"""D121 Task 5 transport-late diagnostic gate."""
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1
from scripts.d121_transport_late_probe import run_case, run_matrix, semantic_digest
from scripts.d121_handoff_execution_probe import ExecutionCase


class D121TransportLateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # D122 replaces the live timing contract.  Keep D121's RED as an
        # immutable historical gate instead of fabricating a current client
        # with v2 windows but without v2 defaults.
        path = (Path(__file__).resolve().parents[2] / "evidence" /
                "motion_navigation" / "d121-momentum-handoff-v1" /
                "release-repair" / "task-4" / "matrix-v2.json")
        matrix = json.loads(path.read_text(encoding="utf-8"))
        runs = {run["policy"]: run for run in matrix["runs"]}
        cls.timely = runs["timely_l1"]
        cls.late = runs["late_l2"]
        cls.never = runs["never_applied"]

    def test_timely_control_uses_exact_python_window_and_l1_physics(self):
        self.assertEqual(self.timely["target_window"], [27, 27])
        self.assertEqual(self.timely["actual_effect_tick"], 27)
        self.assertEqual(self.timely["outcome"], "TIMELY_SUCCESS")
        self.assertEqual(self.timely["final_driver_state"], "success")
        self.assertTrue(self.timely["source_released"])
        self.assertFalse(self.timely["expired_jump_physically_applied"])

    def test_l2_probe_records_real_expired_jump_before_ledger_rejection(self):
        self.assertEqual(self.late["target_window"], [27, 27])
        self.assertEqual(self.late["actual_effect_tick"], 28)
        self.assertTrue(self.late["actual_movement"]["jump"])
        self.assertTrue(self.late["expired_jump_physically_applied"])
        self.assertFalse(self.late["protocol_prevented_expired_application"])
        self.assertEqual(
            self.late["final_target_ledger"]["status"],
            "applied_outside_window",
        )

    def test_l2_queue_and_physics_precede_the_truthful_receipt(self):
        events = {event["movement_tick"]: event
                  for event in self.late["backend_sampling_events"]}
        sequence = self.late["target_sequence"]
        queued = events[27]["pending_after"]
        self.assertEqual([item["action"]["request_sequence_id"] for item in queued],
                         [sequence])
        self.assertEqual(queued[0]["arrival_tick"], 28)
        self.assertEqual(events[27]["sample"]["state"], "lease_exhausted")
        self.assertNotEqual(events[27]["sample"]["request_sequence_id"], sequence)
        self.assertEqual(events[28]["sample"]["request_sequence_id"], sequence)
        self.assertEqual(events[28]["sample"]["state"], "leased")
        self.assertTrue(events[28]["sample"]["jump"])
        self.assertTrue(events[28]["on_ground_before"])
        self.assertFalse(events[28]["on_ground_after"])
        self.assertGreater(events[28]["position_after"][1],
                           events[28]["position_before"][1])
        self.assertGreater(events[28]["velocity_after"][1], 0)
        self.assertFalse(events[28]["received_action"]["movement"]["jump"])
        rows = {row["effect_tick"]: row for row in self.late["frames"]}
        self.assertNotIn(sequence, [sample["sequence"]
                                   for sample in rows[27]["receipt_applications"]])
        self.assertEqual(rows[28]["target_ledger"]["status"], "applied_outside_window")
        self.assertEqual(rows[28]["receipt_applications"][0]["sequence"], sequence)

    def test_never_applied_is_distinct_from_l2_physical_application(self):
        self.assertEqual(self.never["target_window"], [27, 27])
        self.assertIsNone(self.never["actual_effect_tick"])
        self.assertFalse(self.never["expired_jump_physically_applied"])
        self.assertEqual(
            self.never["outcome"], "RED_LOST_SUCCESSOR_BODY_UNRESOLVED"
        )
        self.assertNotEqual(self.never["outcome"], self.late["outcome"])

    def test_transport_dispositions_do_not_claim_unperformed_protocol_rejection(self):
        self.assertIsNone(self.never["protocol_prevented_expired_application"])
        expected = (
            (self.timely, "applied_in_window", "not_applicable", None),
            (self.late, "applied_outside_window", "not_prevented", False),
            (self.never, "not_delivered", "not_applicable", None),
        )
        for run, delivery, prevention, compatible in expected:
            with self.subTest(policy=run["policy"]):
                self.assertEqual(run["transport_disposition"], delivery)
                self.assertEqual(run["expired_application_prevention"], prevention)
                self.assertIs(run["protocol_prevented_expired_application"], compatible)
        self.assertEqual(len({run["transport_disposition"] for run, *_ in expected}), 3)
        with patch("scripts.d121_transport_late_probe.run_case",
                   side_effect=[self.timely, self.late, self.never]):
            self.assertEqual(run_matrix()["result"], "FAILED_STOPPED")

    def test_only_transport_policy_changes_the_submitted_successor(self):
        controls = [self.timely, self.late, self.never]
        self.assertTrue(all(run["target_sequence"] == 26 for run in controls))
        self.assertTrue(all(run["target_window"] == [27, 27] for run in controls))
        self.assertTrue(all(
            run["target_action"] == controls[0]["target_action"]
            for run in controls[1:]
        ))

    def test_actual_route_proof_world_and_prepared_content_have_equal_semantics(self):
        runs = [self.timely, self.late, self.never]
        expected = runs[0]["semantic_controls"]["component_sha256"]
        self.assertEqual(len(expected), 12)
        for run in runs[1:]:
            for component, digest in expected.items():
                with self.subTest(policy=run["policy"], component=component):
                    self.assertEqual(run["semantic_controls"]["component_sha256"][component], digest)
        for run in runs:
            self.assertEqual(run["L_movement_winner"]["movement_type"], "MovementV1")
            self.assertEqual(run["L_movement_winner"]["source_kinds"], ["ordered"])
            self.assertEqual(run["L_movement_winner"]["count"], 1)
        identities = [run["semantic_controls"]["runtime_identity"] for run in runs]
        for key in ("world_owner_snapshot_epoch", "parent_main_digest", "dependency_digest",
                    "job_token", "payload_digest", "stream_generation_id", "stream_request_id"):
            self.assertEqual(len({identity[key] for identity in identities}), 3, key)

    def test_semantic_projection_detects_real_state_world_and_command_changes(self):
        case = ExecutionCase()
        try:
            state = case.proof.entry_state
            changed = replace(state, position=(state.position[0] + .01, *state.position[1:]))
            self.assertNotEqual(semantic_digest(state), semantic_digest(changed))
            command = case.proof.commands[0]
            movement = replace(command.movement, jump=not command.movement.jump)
            self.assertNotEqual(semantic_digest(command), semantic_digest(replace(command, movement=movement)))
            position = next(iter(case.backend.scene.solids))
            original = case.world.cell(position)
            changed_fact = replace(original, block=case.backend.scene.geometry("minecraft:stone"))
            self.assertNotEqual(semantic_digest(original), semantic_digest(changed_fact))
            changed_route = replace(case.route, fixed_route_length_blocks=13.)
            self.assertNotEqual(semantic_digest(case.route), semantic_digest(changed_route))
        finally:
            case.close()

    def test_matrix_requires_semantic_equality_as_well_as_the_final_action(self):
        runs = deepcopy([self.timely, self.late, self.never])
        runs[1]["semantic_controls"]["component_sha256"]["world_facts"] = "changed-world"
        with patch("scripts.d121_transport_late_probe.run_case", side_effect=runs):
            matrix = run_matrix()
        self.assertTrue(matrix["same_successor_action_and_window"])
        self.assertFalse(matrix["same_control_semantics"])
        self.assertEqual(matrix["result"], "FAILED_STOPPED")

    def test_snapshot_and_wire_have_no_exact_movement_tick_window(self):
        action = self.late["target_action"]
        audit = self.late["protocol_audit"]
        self.assertTrue(audit["action_intent_has_movement_tick_window"])
        self.assertFalse(audit["action_snapshot_has_movement_tick_window"])
        self.assertFalse(audit["wire_has_movement_tick_window"])
        self.assertFalse(audit["wire_has_prepared_generation"])
        self.assertFalse(audit["client_preapplication_exact_tick_guard"])
        self.assertEqual(action["valid_for_ticks"], 1)
        self.assertIsNone(action["cancel_request_sequence_id"])
        self.assertNotIn("movement_tick_window", audit["backend_step_signature"])

    def _apparently_successful_controls(self):
        runs = deepcopy([self.timely, self.late, self.never])
        runs[1].update(outcome="LATE_COMMAND_BLOCKED_BEFORE_PHYSICS",
                       expired_jump_physically_applied=False,
                       terminal_failure=None, source_released=True)
        runs[1]["final_body"].update(on_ground=True, damage=0.0)
        runs[2]["outcome"] = "LOST_SUCCESSOR_SAFELY_BOUNDED"
        return runs

    def test_matrix_does_not_treat_nonapplication_as_prephysics_rejection(self):
        runs = self._apparently_successful_controls()
        with patch("scripts.d121_transport_late_probe.run_case", side_effect=runs):
            self.assertEqual(run_matrix()["result"], "FAILED_STOPPED")

    def test_matrix_does_not_pass_with_late_body_responsibility_unresolved(self):
        runs = self._apparently_successful_controls()
        runs[1]["protocol_audit"]["client_preapplication_exact_tick_guard"] = True
        runs[1]["terminal_failure"] = {"code": "backend_io"}
        runs[1]["source_released"] = False
        with patch("scripts.d121_transport_late_probe.run_case", side_effect=runs):
            self.assertEqual(run_matrix()["result"], "FAILED_STOPPED")


if __name__ == "__main__":
    unittest.main()
