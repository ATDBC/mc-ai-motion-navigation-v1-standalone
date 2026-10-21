"""D122-1 safety duration and ledger termination regression checks."""
from dataclasses import replace
from dataclasses import asdict
import unittest

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1, SafeDefaultV1
from mc2p.contracts.motion_activation import MovementTickWindowV1
from mc2p.contracts.action_receipt import ClientInputApplicationV1, behavior_receipt_from_mapping
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.online_motion import InputApplicationLedger, InputApplicationStatus, input_application_has_finished
from mc2p.motion_nav.world_model import WorldSessionId
from tests.test_action_receipt import receipt_value
from tests import test_d122_python_timing as prior


class TimingRepairTests(unittest.TestCase):
    def setup_parent(self):
        fixture = prior.CalculatorTimingTests()
        backend = fixture.backend()
        parent = fixture.arm(backend)
        ledger = InputApplicationLedger()
        ledger.submit(WorldSessionId("w"), parent, requested_first_tick=26)
        ledger.observe_receipt(self.receipt(26, samples=backend._sample_records,
            events=[asdict(event) for event in backend._client_timing.events]))
        return fixture, backend, parent, ledger

    def receipt(self, tick, *, events=(), samples=(), request=25):
        return behavior_receipt_from_mapping(dict(
            receipt_value(episode_id="ep", request_sequence_id=request, input_samples=tick),
            schema_version="mc2p.client_action_receipt.v4", input_applications=list(samples),
            dropped_input_samples=0, oldest_retained_input_tick=tick, timing_events=list(events)))

    def exhausted(self, tick):
        return dict(status="default_exhausted", movement_tick_id=tick, episode_id="ep", request_sequence_id=25)

    def test_default_budget_must_cover_nominal_proof_duration(self):
        with self.assertRaises(ContractViolation):
            SafeDefaultV1(27, MovementV1(sneak=True), 8, 100_000_000)
        SafeDefaultV1(27, MovementV1(sneak=True), 8, 400_000_000)

    def test_armed_default_finishes_real_ticks_despite_wall_expiry(self):
        from tests.sim.client_timing import ClientTimingSampler
        client = ClientTimingSampler("ep")
        parent = ActionSnapshotV1("ep", 25, 0, 1_000_000_000,
                                  safe_default=SafeDefaultV1(27, MovementV1(sneak=True), 8, 400_000_000))
        client.admit(parent, 26, 0, 0)
        client.sample(26, 0)
        # No physics happened while wall time advanced; already armed fallback
        # must not be cut short, even if the parent's response deadline expired.
        for tick in range(27, 35):
            self.assertTrue(client.sample(tick, 2_000_000_000 + tick * 100_000_000).movement.sneak)
        self.assertEqual(client.sample(35, 6_000_000_000).movement, MovementV1())

    def test_exhausted_before_activation_cannot_retire_parent(self):
        _, _, _, ledger = self.setup_parent()
        with self.assertRaises(ContractViolation):
            ledger.observe_receipt(self.receipt(26, events=[self.exhausted(26)]))
        self.assertFalse(input_application_has_finished(ledger.record(25)))

    def test_exhausted_without_complete_real_samples_cannot_retire_parent(self):
        _, _, _, ledger = self.setup_parent()
        with self.assertRaises(ContractViolation):
            ledger.observe_receipt(self.receipt(35, events=[self.exhausted(35)]))
        self.assertFalse(input_application_has_finished(ledger.record(25)))

    def test_complete_default_samples_and_exhaustion_retire_parent(self):
        _, backend, _, ledger = self.setup_parent()
        for _ in range(9):
            backend.free_tick()
        receipt = self.receipt(35, events=[self.exhausted(35)], samples=backend._sample_records[1:])
        ledger.observe_receipt(receipt)
        self.assertEqual(len(ledger.record(25).default_samples), 8)
        self.assertTrue(input_application_has_finished(ledger.record(25)))

    def test_exhaustion_cannot_claim_a_future_unobserved_tick(self):
        _, backend, _, ledger = self.setup_parent()
        for _ in range(8):
            backend.free_tick()
        with self.assertRaises(ContractViolation):
            ledger.observe_receipt(self.receipt(34, events=[self.exhausted(35)],
                                               samples=backend._sample_records[1:]))
        self.assertFalse(input_application_has_finished(ledger.record(25)))

    def test_inactive_sample_alone_cannot_end_a_proved_default(self):
        _, backend, _, ledger = self.setup_parent()
        sample = dict(backend._sample_records[-1], movement_tick_id=27, state="lease_exhausted")
        ledger.observe_sample(ClientInputApplicationV1.from_mapping(sample))
        self.assertFalse(input_application_has_finished(ledger.record(25)))

    def test_replacement_must_have_a_real_sample_and_retires_old_default(self):
        fixture, backend, _, ledger = self.setup_parent()
        successor = fixture.action(26, movement=MovementV1(forward=1))
        ledger.submit(WorldSessionId("w"), successor, requested_first_tick=27)
        # An executed status alone is not proof of ordinary movement takeover.
        ledger.observe_receipt(self.receipt(26, request=26))
        self.assertFalse(input_application_has_finished(ledger.record(25)))
        result = backend.step(successor, successor.deadline_monotonic_ns)
        ledger.observe_receipt(result.receipt)
        self.assertTrue(input_application_has_finished(ledger.record(25)))
        self.assertTrue(input_application_has_finished(ledger.record(26)))

    def test_cancel_clears_the_whole_default_and_pending_arrangement(self):
        from tests.sim.client_timing import ClientTimingSampler
        fixture, _, parent, ledger = self.setup_parent()
        sampler = ClientTimingSampler("ep")
        sampler.admit(parent, 26, 0, 0)
        sampler.sample(26, 0)
        pending = fixture.action(26, movement=MovementV1(jump=True),
                                 first_effect_window=MovementTickWindowV1(30, 30))
        ledger.submit(WorldSessionId("w"), pending, requested_first_tick=30)
        sampler.admit(pending, 27, 0, 1)
        cancel = fixture.action(27, cancel_request_sequence_id=26)
        ledger.submit(WorldSessionId("w"), cancel, requested_first_tick=27)
        sampler.admit(cancel, 27, 0, 1)
        ledger.observe_receipt(self.receipt(27, request=27,
            events=[asdict(event) for event in sampler.events]))
        for sequence in (25, 26, 27):
            self.assertTrue(input_application_has_finished(ledger.record(sequence)))
            self.assertNotEqual(ledger.record(sequence).status, InputApplicationStatus.REJECTED)
        for tick in range(27, 40):
            self.assertEqual(sampler.sample(tick, 0).movement, MovementV1())

    def test_accepted_cancel_retires_target_default_and_cancel_record(self):
        fixture, backend, _, ledger = self.setup_parent()
        cancel = fixture.action(26, cancel_request_sequence_id=25)
        ledger.submit(WorldSessionId("w"), cancel, requested_first_tick=27)
        receipt = backend.step(cancel, cancel.deadline_monotonic_ns).receipt
        self.assertEqual(receipt.status, "executed")
        ledger.observe_receipt(receipt)
        self.assertTrue(input_application_has_finished(ledger.record(25)))
        self.assertTrue(input_application_has_finished(ledger.record(26)))
        self.assertEqual(ledger.record(26).status.value, "cancelled")
        self.assertNotEqual(ledger.record(25).status, InputApplicationStatus.REJECTED)

    def test_unaccepted_cancel_does_not_retire_default(self):
        fixture, _, _, ledger = self.setup_parent()
        cancel = fixture.action(26, cancel_request_sequence_id=25)
        ledger.submit(WorldSessionId("w"), cancel, requested_first_tick=27)
        rejected = replace(self.receipt(27, request=26), status="rejected")
        ledger.observe_receipt(rejected)
        self.assertFalse(input_application_has_finished(ledger.record(25)))
        self.assertTrue(input_application_has_finished(ledger.record(26)))

    def test_response_deadline_cannot_release_an_unfinished_default(self):
        for window in (MovementTickWindowV1(27, 27), MovementTickWindowV1(30, 30)):
            fixture, backend, _, _ = self.setup_parent()
            backend.free_tick()
            request = replace(fixture.action(26, movement=MovementV1(jump=True),
                                            first_effect_window=window),
                              deadline_monotonic_ns=backend.clock[0] + 100_000_000)
            try:
                result = backend.step(request, request.deadline_monotonic_ns)
            except TimeoutError:
                self.fail("response deadline interrupted the required real default samples")
            self.assertEqual(sum(item.sneak for item in backend.applied), 8)
            self.assertEqual(result.observation.self_state.value.movement_tick_id, 35)
            self.assertFalse(any(item.jump for item in backend.applied))
            # Only after the complete real suffix may ordinary control resume.
            ordinary = replace(fixture.action(27), observation_sequence_id=backend.sequence)
            backend.step(ordinary, ordinary.deadline_monotonic_ns)
            self.assertEqual(sum(item.sneak for item in backend.applied), 8)

    def test_cancel_keeps_unrelated_ambiguous_history(self):
        fixture = prior.CalculatorTimingTests()
        backend = fixture.backend()
        parent = fixture.arm(backend)
        ledger = InputApplicationLedger()
        old = replace(parent, request_sequence_id=24, safe_default=None, movement=MovementV1(forward=1))
        ledger.submit(WorldSessionId("w"), old, requested_first_tick=24)
        ledger.mark_ambiguous(24, at_tick=25)
        old_record = ledger.record(24)
        ledger.submit(WorldSessionId("w"), parent, requested_first_tick=26)
        ledger.observe_receipt(self.receipt(26, samples=backend._sample_records,
            events=[asdict(event) for event in backend._client_timing.events]))
        pending = fixture.action(26, movement=MovementV1(jump=True),
                                 first_effect_window=MovementTickWindowV1(30, 30))
        ledger.submit(WorldSessionId("w"), pending, requested_first_tick=30)
        ledger.observe_receipt(self.receipt(27, request=26, events=[dict(
            status="window_pending", movement_tick_id=27, episode_id="ep", request_sequence_id=26)]))
        cancel = fixture.action(27, cancel_request_sequence_id=26)
        ledger.submit(WorldSessionId("w"), cancel, requested_first_tick=28)
        ledger.observe_receipt(self.receipt(28, request=27))
        self.assertEqual(ledger.record(24), old_record)
        for sequence in (25, 26, 27):
            self.assertTrue(input_application_has_finished(ledger.record(sequence)))
            self.assertNotEqual(ledger.record(sequence).status, InputApplicationStatus.REJECTED)

    def test_replaced_pending_finishes_even_when_its_successor_misses(self):
        fixture, _, _, ledger = self.setup_parent()
        for sequence, tick in ((26, 27), (27, 28)):
            pending = fixture.action(sequence, first_effect_window=MovementTickWindowV1(30, 30))
            ledger.submit(WorldSessionId("w"), pending, requested_first_tick=30)
            ledger.observe_receipt(self.receipt(tick, request=sequence, events=[dict(
                status="window_pending", movement_tick_id=tick, episode_id="ep", request_sequence_id=sequence)]))
        ledger.observe_receipt(self.receipt(31, request=27, events=[dict(
            status="window_missed", movement_tick_id=31, episode_id="ep", request_sequence_id=27)]))
        self.assertTrue(input_application_has_finished(ledger.record(26)))
        self.assertEqual(ledger.record(26).status.value, "cancelled")
        self.assertEqual(ledger.record(26).applied_ticks, ())
        self.assertTrue(input_application_has_finished(ledger.record(27)))
        self.assertFalse(input_application_has_finished(ledger.record(25)))

    def test_exhausted_and_neutral_at_same_tick_do_not_rearm_parent(self):
        fixture, backend, _, ledger = self.setup_parent()
        for _ in range(9):
            backend.free_tick()
        neutral = dict(backend._sample_records[-1], state="neutral")
        samples = [*backend._sample_records[1:-1], neutral]
        ledger.observe_receipt(self.receipt(35, events=[self.exhausted(35)], samples=samples))
        self.assertTrue(input_application_has_finished(ledger.record(25)))
        self.assertEqual(ledger.record(25).applied_ticks, (26,))
        # A later accepted cancellation cannot claim a finished arrangement.
        cancel = fixture.action(26, cancel_request_sequence_id=25)
        ledger.submit(WorldSessionId("w"), cancel, requested_first_tick=36)
        with self.assertRaises(ContractViolation):
            ledger.observe_receipt(self.receipt(36, request=26))


if __name__ == "__main__":
    unittest.main()
