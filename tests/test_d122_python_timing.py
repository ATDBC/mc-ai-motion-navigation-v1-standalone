"""D122 Python contract, transport and real calculator sampling checks."""
from dataclasses import asdict, replace
import json
import unittest

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1, LookV1, MovementTickWindowV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.backends.client_behavior_payload import encode_behavior_action
from mc2p.runtime.arbiter_v1 import ActionArbiterV1


class ContractAndWireTests(unittest.TestCase):
    def default(self, **changes):
        from mc2p.contracts.action_v1 import SafeDefaultV1
        value = dict(activation_tick=8, movement=MovementV1(sneak=True), valid_for_ticks=8,
                     wall_budget_ns=800_000_000)
        return SafeDefaultV1(**(value | changes))

    def test_v1_wire_keys_and_bytes_remain_identical(self):
        action = ActionSnapshotV1("ep", 1, 1, 2_000_000_000)
        value = asdict(action)
        value.pop("first_effect_window", None)
        value.pop("safe_default", None)
        del value["deadline_monotonic_ns"]
        value.update(schema_version="mc2p.client_action.v1", remaining_budget_ns=1_999_999_999,
                     observation_budget_ns=1_999_999_999)
        self.assertEqual(encode_behavior_action(action, now_ns=1),
                         json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                                    separators=(",", ":")).encode())

    def test_snapshot_projects_exact_window_and_default_only_with_negotiated_v2(self):
        action = ActionSnapshotV1("ep", 1, 1, 2_000_000_000,
                                  first_effect_window=MovementTickWindowV1(8, 8),
                                  safe_default=self.default(activation_tick=9))
        from mc2p.backends.client_behavior_payload import ActionCapabilityUnsupported
        with self.assertRaises(ActionCapabilityUnsupported):
            encode_behavior_action(action, now_ns=1)
        value = json.loads(encode_behavior_action(action, now_ns=1,
                                                   negotiated_action_schema="mc2p.client_action.v2"))
        self.assertEqual(value["first_effect_window"], {"earliest_tick": 8, "latest_tick": 8})
        self.assertEqual(value["safe_default"]["movement"], asdict(MovementV1(sneak=True)))

    def test_default_is_bounded_and_cancel_cannot_arm(self):
        from mc2p.contracts.action_v1 import SafeDefaultV1
        base = dict(activation_tick=8, movement=MovementV1(sneak=True), valid_for_ticks=8, wall_budget_ns=800_000_000)
        for changes in ({"activation_tick": -1}, {"valid_for_ticks": 0}, {"valid_for_ticks": 21},
                        {"wall_budget_ns": 0}, {"wall_budget_ns": 30_000_000_001}, {"movement": LookV1()}):
            with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                SafeDefaultV1(**(base | changes))
        with self.assertRaises(ContractViolation):
            ActionSnapshotV1("ep", 2, 1, 2_000_000_000, cancel_request_sequence_id=1, safe_default=self.default())

    def test_movement_winner_owns_both_timing_fields(self):
        arbiter = ActionArbiterV1()
        intent = ActionIntentV1("move", "nav", "ep", 1, ActionPriorityV0.TASK,
                                1, 2_000_000_000, movement=MovementV1(forward=1),
                                movement_tick_window=MovementTickWindowV1(8, 8),
                                safe_default=self.default(activation_tick=9))
        arbiter.submit(intent)
        decision = arbiter.resolve(episode_id="ep", observation_sequence_id=1, request_sequence_id=1,
                                  now_ns=2, deadline_monotonic_ns=2_000_000_000)
        self.assertEqual(decision.action.first_effect_window, intent.movement_tick_window)
        self.assertEqual(decision.action.safe_default, intent.safe_default)
        with self.assertRaises(ContractViolation):
            replace(intent, movement=None, look=LookV1())

    def test_losing_movement_cannot_smuggle_default_through_its_winning_look(self):
        arbiter = ActionArbiterV1()
        claimed = ActionIntentV1("old", "nav", "ep", 1, ActionPriorityV0.TASK,
                                 1, 2_000_000_000, movement=MovementV1(forward=1), look=LookV1(10),
                                 movement_tick_window=MovementTickWindowV1(8,8), safe_default=self.default())
        arbiter.submit(claimed)
        arbiter.submit(replace(claimed, intent_id="new", source_id="player", priority=ActionPriorityV0.PLAYER,
                                movement=MovementV1(strafe=1), look=None, movement_tick_window=None, safe_default=None))
        result = arbiter.resolve(now_ns=2, episode_id="ep", observation_sequence_id=1,
                                  request_sequence_id=1, deadline_monotonic_ns=2_000_000_000)
        self.assertEqual(result.action.look, LookV1(10))
        self.assertEqual(result.action.movement, MovementV1(strafe=1))
        self.assertIsNone(result.action.first_effect_window)
        self.assertIsNone(result.action.safe_default)


class CalculatorTimingTests(unittest.TestCase):
    def backend(self, *, tick=26, duration=8):
        from tests.sim.backend import CalculatorBackend, Scene
        self.clock = [1_000_000]
        scene = Scene({(x, 0, z): "minecraft:stone" for x in range(-2, 3) for z in range(-2, 5)},
                      ((-2, 2), (-2, 4), (-2, 4)))
        backend = CalculatorBackend(self.clock, scene, (.5, 1., .5), episode="ep", action_timing_v2=True)
        backend.movement_tick = tick - 1
        self.tick, self.duration = tick, duration
        return backend

    def action(self, sequence, **changes):
        return ActionSnapshotV1("ep", sequence, 0 if sequence == 25 else 1, 10_000_000_000, **changes)

    def arm(self, backend):
        from mc2p.contracts.action_v1 import SafeDefaultV1
        parent = self.action(25, movement=MovementV1(), safe_default=SafeDefaultV1(
            self.tick + 1, MovementV1(sneak=True), self.duration, 2_000_000_000))
        result = backend.step(parent, parent.deadline_monotonic_ns)
        self.assertEqual(result.receipt.timing_events[-1].status.value, "fallback_armed")
        return parent

    def test_future_pending_does_not_replace_default_and_response_is_held(self):
        for tick, duration in ((26, 8), (107, 11)):
            backend = self.backend(tick=tick, duration=duration)
            self.arm(backend)
            successor = self.action(26, movement=MovementV1(jump=True),
                                    look=LookV1(yaw_delta_degrees=90),
                                    first_effect_window=MovementTickWindowV1(tick + 3, tick + 3))
            result = backend.step(successor, successor.deadline_monotonic_ns)
            rows = backend.applied_commands[-3:]
            self.assertEqual([row[2] for row in rows], [MovementV1(sneak=True)] * 2 + [MovementV1(jump=True)])
            self.assertEqual(result.observation.self_state.value.movement_tick_id, tick + 3)
            self.assertEqual(backend.response_timing[-1]["waited_ticks"], 2)
            self.assertEqual([event.status.value for event in result.receipt.timing_events],
                             ["window_pending", "default_active", "default_active", "window_applied"])
            self.assertEqual(backend.timing_admissions, 2)

    def test_missed_window_never_jumps_and_does_not_release_response_before_default_ends(self):
        backend = self.backend()
        self.arm(backend)
        backend.free_tick()  # L+1, default is already active before late transport arrival.
        late = self.action(26, movement=MovementV1(jump=True), first_effect_window=MovementTickWindowV1(27, 27))
        result = backend.step(late, late.deadline_monotonic_ns)
        self.assertFalse(any(item.jump for item in backend.applied[1:]))
        self.assertEqual(backend.movement_tick, 35)
        self.assertEqual(sum(item.sneak for item in backend.applied), 8)
        self.assertEqual(result.receipt.timing_events[-1].status.value, "default_exhausted")
        self.assertEqual(backend.response_timing[-1]["waited_ticks"], 7)
        self.assertTrue(all(sample.request_sequence_id == 25 for sample in result.receipt.input_applications
                            if sample.state == "default_active"))

    def test_ordinary_request_clears_old_default_permanently(self):
        backend = self.backend()
        self.arm(backend)
        ordinary = self.action(27, movement=MovementV1(forward=1))
        backend.step(ordinary, ordinary.deadline_monotonic_ns)
        for _ in range(15):
            backend.free_tick()
        self.assertEqual(backend.applied[1], MovementV1(forward=1))
        self.assertFalse(any(item.sneak for item in backend.applied[1:]))

    def test_armed_pending_are_not_applications_and_default_is_parent_evidence(self):
        from mc2p.motion_nav.online_motion import InputApplicationLedger, InputApplicationStatus
        from mc2p.motion_nav.world_model import WorldSessionId
        from mc2p.contracts.action_receipt import (ClientBehaviorReceiptV4, ClientTimingEventV1,
                                                  ClientTimingStatusV1, behavior_receipt_from_mapping)
        from tests.test_action_receipt import receipt_value
        backend = self.backend()
        parent = self.arm(backend)
        ledger = InputApplicationLedger()
        ledger.submit(WorldSessionId("w"), parent, requested_first_tick=26)
        empty = dict(receipt_value(episode_id="ep", request_sequence_id=25, input_samples=26),
                     schema_version="mc2p.client_action_receipt.v4", input_applications=[],
                     dropped_input_samples=0, oldest_retained_input_tick=26,
                     timing_events=[dict(status="fallback_armed", movement_tick_id=26,
                                         episode_id="ep", request_sequence_id=25)])
        ledger.observe_receipt(behavior_receipt_from_mapping(empty))
        self.assertEqual(ledger.record(25).status, InputApplicationStatus.IN_FLIGHT)
        self.assertEqual(ledger.record(25).applied_ticks, ())
        backend.free_tick()
        sample = backend._sample_records[-1]
        from mc2p.contracts.action_receipt import ClientInputApplicationV1
        ledger.observe_sample(ClientInputApplicationV1.from_mapping(sample))
        self.assertEqual(ledger.record(25).applied_ticks, ())
        self.assertEqual(len(ledger.record(25).default_samples), 1)

    def test_parent_responsibility_outlives_its_last_normal_input(self):
        from mc2p.motion_nav.online_motion import InputApplicationLedger, input_application_has_finished
        from mc2p.motion_nav.world_model import WorldSessionId
        backend = self.backend()
        parent = self.arm(backend)
        from mc2p.contracts.action_receipt import ClientInputApplicationV1
        ledger = InputApplicationLedger()
        ledger.submit(WorldSessionId("w"), parent, requested_first_tick=26)
        ledger.observe_sample(ClientInputApplicationV1.from_mapping(backend._sample_records[-1]))
        self.assertFalse(input_application_has_finished(ledger.record(25)))
        ordinary = self.action(26)
        ledger.submit(WorldSessionId("w"), ordinary, requested_first_tick=27)
        result = backend.step(ordinary, ordinary.deadline_monotonic_ns)
        ledger.observe_receipt(result.receipt)
        self.assertTrue(input_application_has_finished(ledger.record(25)))

    def test_never_advances_client_without_synthetic_observations(self):
        backend = self.backend()
        self.arm(backend)
        sequence = backend.sequence
        for _ in range(9):
            backend.free_tick()
        self.assertEqual(backend.sequence, sequence)
        self.assertEqual(sum(item.sneak for item in backend.applied), 8)
        self.assertEqual(backend.applied[-1], MovementV1())

    def test_delayed_transport_holds_response_until_actual_admission(self):
        from tests.sim.backend import Perturbations
        backend = self.backend()
        self.arm(backend)
        backend.perturbations = Perturbations(late_ticks=frozenset({27}))
        late = self.action(26, movement=MovementV1(jump=True), first_effect_window=MovementTickWindowV1(27, 27))
        result = backend.step(late, late.deadline_monotonic_ns)
        self.assertEqual(backend.movement_tick, 35)
        self.assertFalse(any(item.jump for item in backend.applied[1:]))
        self.assertEqual([event.movement_tick_id for event in result.receipt.timing_events
                          if event.status.value == "window_missed"], [28])


class SamplingTableTests(unittest.TestCase):
    def sampler(self, *, landing=26, duration=8):
        from tests.sim.client_timing import ClientTimingSampler
        from mc2p.contracts.action_v1 import SafeDefaultV1
        sampler = ClientTimingSampler("ep")
        action = ActionSnapshotV1("ep", 25, 0, 10_000_000_000, safe_default=SafeDefaultV1(
            landing + 1, MovementV1(sneak=True), duration, 1_000_000_000))
        sampler.admit(action, landing, 0, 0)
        sampler.sample(landing, 0)
        return sampler

    def request(self, seq=26, tick=27, **changes):
        return ActionSnapshotV1("ep", seq, 0, 10_000_000_000, movement=MovementV1(jump=True),
                                first_effect_window=MovementTickWindowV1(tick, tick), **changes)

    def test_activation_rechecks_identity_but_not_consumed_sequence_or_observation(self):
        sampler = self.sampler()
        sampler.admit(self.request(tick=29), 27, 0, 0)
        for tick in (27, 28, 29):
            selected = sampler.sample(tick, tick)
        self.assertTrue(selected.movement.jump)
        self.assertEqual(sampler.admissions, 2)
        self.assertEqual(sampler.activation_checks, 3)
        with self.assertRaises(ContractViolation):
            sampler.admit(self.request(tick=29), 30, 0, 0)

    def test_higher_pending_replaces_only_pending_and_resets_clear_old_inputs(self):
        for event in ("higher", "cancel", "reset", "episode", "permission"):
            with self.subTest(event=event):
                sampler = self.sampler()
                sampler.admit(self.request(tick=28), 27, 0, 0)
                if event == "higher":
                    sampler.admit(self.request(seq=27, tick=29), 27, 0, 0)
                    self.assertTrue(sampler.sample(27, 0).movement.sneak)
                    self.assertTrue(sampler.sample(28, 0).movement.sneak)
                    self.assertTrue(sampler.sample(29, 0).movement.jump)
                else:
                    if event == "cancel":
                        sampler.admit(ActionSnapshotV1("ep", 27, 0, 10_000_000_000,
                                                        cancel_request_sequence_id=26), 27, 0, 0)
                    elif event in {"reset", "episode"}:
                        sampler.reset("next" if event == "episode" else "ep")
                    else:
                        sampler.permitted = False
                    for tick in range(27, 40):
                        self.assertEqual(sampler.sample(tick, 0).movement, MovementV1())

    def test_default_wall_budget_starts_at_activation_and_never_revives_parent(self):
        from tests.sim.client_timing import ClientTimingSampler
        from mc2p.contracts.action_v1 import SafeDefaultV1
        sampler = ClientTimingSampler("ep")
        parent = ActionSnapshotV1("ep", 25, 0, 10_000_000_000, movement=MovementV1(forward=1),
                                  safe_default=SafeDefaultV1(201, MovementV1(sneak=True), 8, 400_000_000))
        sampler.admit(parent, 200, 10, 0)
        sampler.sample(200, 10)
        self.assertTrue(sampler.sample(201, 1_000_000_000).movement.sneak)
        self.assertEqual(sampler.default_wall_deadline, 1_400_000_000)
        self.assertTrue(sampler.sample(202, 1_099_999_999).movement.sneak)
        for tick in range(203, 209):
            self.assertTrue(sampler.sample(tick, 1_400_000_000 + tick).movement.sneak)
        self.assertEqual(sampler.sample(209, 2_000_000_000).movement, MovementV1())

    def test_window_applies_to_first_sample_only(self):
        sampler = self.sampler()
        successor = self.request(valid_for_ticks=3)
        sampler.admit(successor, 27, 0, 0)
        for tick in (27, 28, 29):
            self.assertTrue(sampler.sample(tick, tick).movement.jump)
        self.assertEqual(sampler.sample(30, 30).movement, MovementV1())

    def test_parent_deadline_cannot_arm_an_uncovered_default(self):
        from tests.sim.client_timing import ClientTimingSampler
        from mc2p.contracts.action_v1 import SafeDefaultV1
        sampler = ClientTimingSampler("ep")
        parent = ActionSnapshotV1("ep", 25, 0, 100_000_000,
                                  safe_default=SafeDefaultV1(27, MovementV1(sneak=True), 8, 400_000_000))
        with self.assertRaises(ContractViolation):
            sampler.admit(parent, 26, 0, 0)
        self.assertIsNone(sampler.default_parent)


class HandshakeTests(unittest.TestCase):
    def sample_v4(self, sequence=0):
        from tests.deployment_fixtures import sample_value
        sample = sample_value(sequence)
        sample["receipt"].update(schema_version="mc2p.client_action_receipt.v4", input_applications=[],
                                  dropped_input_samples=0, oldest_retained_input_tick=0, timing_events=[])
        return sample

    def reset(self, backend):
        from tests.test_deployment_python_transport import deadline
        from mc2p.contracts.reset import ResetRequestV0
        return backend.reset(ResetRequestV0("reset", "ep", "remote-session", 0, deadline()))

    def test_v2_is_confirmed_only_by_successful_correlated_first_sample(self):
        from tests.deployment_fixtures import backend_peer
        from tests.test_deployment_python_transport import deadline
        with backend_peer([self.sample_v4(), self.sample_v4(1)],
                          requested_action_schema="mc2p.client_action.v2") as (backend, _, requests):
            self.assertIsNone(backend.negotiated_action_schema)
            self.assertTrue(self.reset(backend).succeeded)
            self.assertEqual(backend.negotiated_action_schema, "mc2p.client_action.v2")
            backend.step(ActionSnapshotV1("ep", 0, 0, deadline(),
                                          first_effect_window=MovementTickWindowV1(1, 1)), deadline())
            self.assertEqual(requests[0]["schema_version"], "mc2p.deployment_session.v3")
            self.assertEqual(requests[0]["action_schema_version"], "mc2p.client_action.v2")
            self.assertEqual(requests[1]["schema_version"], "mc2p.client_action.v2")

    def test_missing_confirmation_fails_reset_and_cannot_claim_v2(self):
        from tests.deployment_fixtures import backend_peer, sample_value
        with backend_peer([sample_value()], requested_action_schema="mc2p.client_action.v2") as (backend, _, _):
            self.assertFalse(self.reset(backend).succeeded)
            self.assertIsNone(backend.negotiated_action_schema)

    def test_session_cannot_downgrade_receipt_schema_after_confirmation(self):
        from tests.deployment_fixtures import backend_peer, sample_value
        from tests.test_deployment_python_transport import deadline
        with backend_peer([self.sample_v4(), sample_value(1)],
                          requested_action_schema="mc2p.client_action.v2") as (backend, _, _):
            self.assertTrue(self.reset(backend).succeeded)
            with self.assertRaisesRegex(ContractViolation, "downgraded"):
                backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())

    def test_v1_rejects_timing_before_send_and_keeps_plain_behavior_available(self):
        from tests.deployment_fixtures import backend_peer, sample_value
        from tests.test_deployment_python_transport import deadline
        from mc2p.backends.client_behavior_payload import ActionCapabilityUnsupported
        with backend_peer([sample_value(), sample_value(1)]) as (backend, _, requests):
            self.assertTrue(self.reset(backend).succeeded)
            with self.assertRaises(ActionCapabilityUnsupported):
                backend.step(ActionSnapshotV1("ep", 0, 0, deadline(),
                                               first_effect_window=MovementTickWindowV1(1, 1)), deadline())
            backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1]["schema_version"], "mc2p.client_action.v1")


class RuntimeTimingTests(unittest.TestCase):
    def test_runtime_keeps_window_and_consumes_v4_real_application(self):
        from tests.test_player_runtime import _RecordingTrace, _task
        from tests.sim.backend import CalculatorBackend, Scene
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        from mc2p.contracts.reset import ResetRequestV0
        from mc2p.contracts.behavior import BehaviorProfileV0
        from mc2p.motion_nav.online_motion import InputApplicationStatus
        clock = [10]
        scene = Scene({(x, 0, z): "minecraft:stone" for x in range(-2, 3) for z in range(-2, 5)},
                      ((-2, 2), (-2, 4), (-2, 4)))
        backend = CalculatorBackend(clock, scene, (.5, 1., .5), episode="episode-1", action_timing_v2=True)
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), clock_ns=lambda: clock[0])
        try:
            self.assertTrue(runtime.reset(ResetRequestV0("r", "episode-1", "sim", 0, 2_000_000_000)).succeeded)
            runtime.submit_intent(ActionIntentV1("m", "nav", "episode-1", 0, ActionPriorityV0.TASK,
                                                 10, 2_000_000_000, movement=MovementV1(forward=1),
                                                 movement_tick_window=MovementTickWindowV1(2, 2)))
            result = runtime.step(replace(_task(), deadline_monotonic_ns=2_000_000_000),
                                  BehaviorProfileV0(), 2_000_000_000)
            self.assertIsNone(result.report.failure)
            self.assertEqual(backend.actions[-1].first_effect_window, MovementTickWindowV1(2, 2))
            self.assertEqual(result.backend_result.receipt.schema_version, "mc2p.client_action_receipt.v4")
            self.assertEqual(runtime.input_ledger.record(0).status, InputApplicationStatus.APPLIED)
        finally:
            runtime.close()


class ReceiptV4Tests(unittest.TestCase):
    def test_timing_event_batch_is_bounded_typed_and_not_future_evidence(self):
        from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
        from tests.test_action_receipt import receipt_value
        base = dict(receipt_value(episode_id="ep", request_sequence_id=25, input_samples=26),
                     schema_version="mc2p.client_action_receipt.v4", input_applications=[],
                     dropped_input_samples=0, oldest_retained_input_tick=26,
                     timing_events=[dict(status="fallback_armed", movement_tick_id=26,
                                         episode_id="ep", request_sequence_id=25)])
        self.assertEqual(behavior_receipt_from_mapping(base).timing_events[0].status.value, "fallback_armed")
        for events in ([dict(base["timing_events"][0], status="invented")],
                       [dict(base["timing_events"][0], movement_tick_id=27)],
                       base["timing_events"] * 65):
            with self.subTest(events=len(events)), self.assertRaises(ContractViolation):
                behavior_receipt_from_mapping(base | {"timing_events": events})


if __name__ == "__main__":
    unittest.main()
