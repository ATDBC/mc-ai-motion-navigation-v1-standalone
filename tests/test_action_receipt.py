from dataclasses import FrozenInstanceError, asdict
import json
import unittest

from mc2p.contracts.common import ContractViolation


def receipt_value(**overrides):
    return dict({"schema_version": "mc2p.client_action_receipt.v2", "generation_id": 1,
        "execution_path": "client_behavior_v1", "episode_id": "episode", "request_sequence_id": 0,
        "status": "executed", "reason": "neutral", "execution_thread": "Render thread",
        "on_client_thread": True, "execution_phase": "client_tick_action_boundary", "world_tick": 100,
        "action_keyboard_callbacks": 0, "action_mouse_callbacks": 0,
        "handled_screen_render_attempts": 0, "handled_screen_render_completions": 0,
        "input_samples": 1, "leased_input_samples": 1}, **overrides)


def receipt_v3_value(**overrides):
    value = receipt_value(input_samples=4, leased_input_samples=4, **overrides)
    value["schema_version"] = "mc2p.client_action_receipt.v3"
    value["input_applications"] = [{
        "schema_version": "mc2p.input-application.v1",
        "movement_tick_id": 4,
        "episode_id": "episode",
        "request_sequence_id": 0,
        "sampled_at_jvm_ns": 123,
        "state": "leased",
        "forward": 1.0,
        "strafe": 0.0,
        "jump": True,
        "sneak": False,
        "sprint": True,
    }]
    return value


class ActionReceiptTests(unittest.TestCase):
    def test_v3_receipt_preserves_exact_last_input_application(self):
        from mc2p.contracts.action_receipt import (
            ClientBehaviorReceiptV3, ClientInputApplicationV1,
            behavior_receipt_from_mapping,
        )
        receipt = behavior_receipt_from_mapping(receipt_v3_value())
        self.assertIs(type(receipt), ClientBehaviorReceiptV3)
        expected = ClientInputApplicationV1(
            "mc2p.input-application.v1", 4, "episode", 0, 123, "leased",
            1.0, 0.0, True, False, True,
        )
        self.assertEqual(receipt.input_applications, (expected,))
        self.assertEqual(receipt.last_input_sample, expected)

        from mc2p.backends.client_behavior_payload import decode_behavior_receipt
        encoded = json.dumps(receipt_v3_value(), separators=(",", ":")).encode()
        self.assertEqual(decode_behavior_receipt(encoded), receipt_v3_value())

    def test_v3_receipt_accepts_no_sample_but_rejects_malformed_sample(self):
        from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
        value = receipt_v3_value()
        value["input_applications"] = []
        self.assertIsNone(behavior_receipt_from_mapping(value).last_input_sample)
        for change in ({"movement_tick_id": True}, {"state": "mystery"},
                       {"forward": 2.0}, {"episode_id": None}):
            invalid = receipt_v3_value()
            invalid["input_applications"] = [{
                **invalid["input_applications"][0], **change,
            }]
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                behavior_receipt_from_mapping(invalid)

        unowned = receipt_v3_value()
        unowned["input_applications"][0].update(
            episode_id=None, request_sequence_id=None, state="lease_exhausted",
            forward=0.0, jump=False, sprint=False,
        )
        parsed = behavior_receipt_from_mapping(unowned)
        self.assertIsNone(parsed.last_input_sample.episode_id)
        self.assertIsNone(parsed.last_input_sample.request_sequence_id)

        inactive_with_motion = receipt_v3_value()
        inactive_with_motion["input_applications"][0].update(
            state="expired", forward=1.0,
        )
        with self.assertRaises(ContractViolation):
            behavior_receipt_from_mapping(inactive_with_motion)

        active_without_owner = receipt_v3_value()
        active_without_owner["input_applications"][0].update(
            episode_id=None, request_sequence_id=None,
        )
        with self.assertRaises(ContractViolation):
            behavior_receipt_from_mapping(active_without_owner)

    def test_immutable_receipt_preserves_exact_wire_evidence(self):
        from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
        raw = receipt_value(status="pending_confirmation", reason="slot_click_sent")
        receipt = ClientBehaviorReceiptV2.from_mapping(raw)
        raw["reason"] = "mutated"
        self.assertEqual(receipt.reason, "slot_click_sent")
        with self.assertRaises(FrozenInstanceError): receipt.status = "confirmed_local"
        from mc2p.backends.client_behavior_payload import decode_behavior_receipt
        self.assertEqual(decode_behavior_receipt(json.dumps(asdict(receipt)).encode()), asdict(receipt))

    def test_operation_rejection_can_report_a_partially_applied_control_frame(self):
        from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
        raw = receipt_value(
            status="operation_rejected", reason="entity_target_mismatch",
        )
        receipt = ClientBehaviorReceiptV2.from_mapping(raw)
        self.assertEqual(receipt.status, "operation_rejected")
        self.assertEqual(receipt.reason, "entity_target_mismatch")

    def test_invalid_evidence_fails_contract_instead_of_coercion(self):
        from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
        for change in ({"extra": 1}, {"world_tick": True}, {"status": []}, {"reason": None},
                       {"execution_path": "legacy"}, {"leased_input_samples": 2}, {"on_client_thread": 1}):
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                ClientBehaviorReceiptV2.from_mapping(receipt_value(**change))
