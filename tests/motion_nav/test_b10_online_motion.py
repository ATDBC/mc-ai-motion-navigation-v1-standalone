from dataclasses import replace
import math
import unittest

from mc2p.contracts.action_receipt import (
    ClientInputApplicationV1, behavior_receipt_from_mapping,
)
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.online_motion import (
    AnchorBuildStatus, CandidateExecutionWindow, InputApplicationLedger,
    InputApplicationStatus, MotionTickPhase, PredictionValidity,
    ProjectionStatus, StateAnchorBuilder, project_movement_command,
)
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.world_model import WorldSessionId


SESSION = WorldSessionId("fabric:test:clock")


def state(*, movement_tick_id=0, yaw=0.0, using_item=False):
    return PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        SESSION, movement_tick_id, (0.5, 1.0, 0.5), (0.0, 0.0, 0.0),
        yaw, 0.0, "standing", 0.6, 1.8, True, False, True,
        False, False, 0, 0.0, 0.1, 0.6, 0.08, 0.42, 20, 5.0,
        "survival", (), False, False, False, False, False, False,
        is_using_item=using_item,
    )


def action(sequence, movement=MovementV1(forward=1), *, lease=1):
    return ActionSnapshotV1(
        "episode", sequence, sequence, 1_000_000,
        movement=movement, valid_for_ticks=lease,
    )


def sample(sequence, tick, *, forward=1.0, strafe=0.0, jump=False,
           sneak=False, sprint=False, sample_state="leased"):
    return ClientInputApplicationV1(
        "mc2p.input-application.v1", tick, "episode", sequence, 100 + tick,
        sample_state, forward, strafe, jump, sneak, sprint,
    )


class InputApplicationLedgerTests(unittest.TestCase):
    def test_tracks_exact_application_and_duplicate_sample_once(self):
        ledger = InputApplicationLedger(max_records=3)
        ledger.submit(SESSION, action(7, MovementV1(forward=1, jump=True)),
                      requested_first_tick=4)
        first = ledger.observe_sample(sample(7, 4, jump=True))
        duplicate = ledger.observe_sample(sample(7, 4, jump=True))
        self.assertIs(first.status, InputApplicationStatus.APPLIED)
        self.assertEqual(first.applied_ticks, (4,))
        self.assertEqual(duplicate, first)
        self.assertEqual(len(ledger.snapshot()), 1)

    def test_marks_actual_late_application_instead_of_calling_it_safe(self):
        ledger = InputApplicationLedger(max_records=3)
        ledger.submit(SESSION, action(1), requested_first_tick=2)
        record = ledger.observe_sample(sample(1, 3))
        self.assertIs(record.status, InputApplicationStatus.APPLIED_OUTSIDE_WINDOW)
        self.assertEqual(record.applied_ticks, (3,))

    def test_does_not_evict_in_flight_records_to_hide_uncertainty(self):
        ledger = InputApplicationLedger(max_records=1)
        ledger.submit(SESSION, action(1), requested_first_tick=2)
        with self.assertRaises(ContractViolation):
            ledger.submit(SESSION, action(2), requested_first_tick=3)
        ledger.expire(1, at_tick=3)
        ledger.submit(SESSION, action(2), requested_first_tick=4)
        self.assertEqual(tuple(r.control_sequence for r in ledger.snapshot()), (2,))

    def test_multi_tick_lease_stays_partial_until_every_tick_is_accounted_for(self):
        ledger = InputApplicationLedger(max_records=1)
        ledger.submit(SESSION, action(1, lease=2), requested_first_tick=4)
        partial = ledger.observe_sample(sample(1, 4))
        self.assertIs(partial.status, InputApplicationStatus.PARTIALLY_APPLIED)
        with self.assertRaises(ContractViolation):
            ledger.submit(SESSION, action(2), requested_first_tick=6)
        complete = ledger.observe_sample(sample(1, 5))
        self.assertIs(complete.status, InputApplicationStatus.APPLIED)

    def test_single_tick_command_keeps_start_window_separate_from_lease(self):
        ledger = InputApplicationLedger(max_records=1)

        submitted = ledger.submit(
            SESSION, action(1), requested_first_tick=4,
            latest_allowed_first_tick=5,
        )

        self.assertEqual(submitted.requested_last_tick, 4)
        self.assertEqual(submitted.latest_allowed_first_tick, 5)
        applied = ledger.observe_sample(sample(1, 5))
        self.assertIs(applied.status, InputApplicationStatus.APPLIED)

    def test_does_not_widen_a_multi_tick_lease_with_a_start_window(self):
        ledger = InputApplicationLedger(max_records=1)

        with self.assertRaises(ContractViolation):
            ledger.submit(
                SESSION, action(1, lease=2), requested_first_tick=4,
                latest_allowed_first_tick=5,
            )

    def test_ingests_a_formal_receipt_batch_without_guessing_from_counters(self):
        ledger = InputApplicationLedger(max_records=3)
        ledger.submit(SESSION, action(3, lease=2), requested_first_tick=7)
        receipt = behavior_receipt_from_mapping({
            "schema_version": "mc2p.client_action_receipt.v3",
            "generation_id": 5, "execution_path": "client_behavior_v1",
            "episode_id": "episode", "request_sequence_id": 4,
            "status": "executed", "reason": "movement_applied",
            "execution_thread": "Render thread", "on_client_thread": True,
            "execution_phase": "client_tick_action_boundary", "world_tick": 100,
            "action_keyboard_callbacks": 0, "action_mouse_callbacks": 0,
            "handled_screen_render_attempts": 0,
            "handled_screen_render_completions": 0,
            "input_samples": 8, "leased_input_samples": 8,
            "input_applications": [
                {
                    "schema_version": "mc2p.input-application.v1",
                    "movement_tick_id": 7, "episode_id": "episode",
                    "request_sequence_id": 3, "sampled_at_jvm_ns": 107,
                    "state": "leased", "forward": 1.0, "strafe": 0.0,
                    "jump": False, "sneak": False, "sprint": False,
                },
                {
                    "schema_version": "mc2p.input-application.v1",
                    "movement_tick_id": 8, "episode_id": "episode",
                    "request_sequence_id": 3, "sampled_at_jvm_ns": 108,
                    "state": "leased", "forward": 1.0, "strafe": 0.0,
                    "jump": False, "sneak": False, "sprint": False,
                },
            ],
        })
        ledger.observe_receipt(receipt)
        self.assertEqual(ledger.snapshot()[0].applied_ticks, (7, 8))
        self.assertIs(ledger.snapshot()[0].status, InputApplicationStatus.APPLIED)

    def test_missing_exact_samples_turn_an_unresolved_command_into_ambiguity(self):
        ledger = InputApplicationLedger(max_records=3)
        baseline = behavior_receipt_from_mapping({
            "schema_version": "mc2p.client_action_receipt.v3",
            "generation_id": 1, "execution_path": "client_behavior_v1",
            "episode_id": None, "request_sequence_id": None,
            "status": "idle", "reason": "none", "execution_thread": "none",
            "on_client_thread": False,
            "execution_phase": "client_tick_action_boundary", "world_tick": 1,
            "action_keyboard_callbacks": 0, "action_mouse_callbacks": 0,
            "handled_screen_render_attempts": 0,
            "handled_screen_render_completions": 0,
            "input_samples": 6, "leased_input_samples": 0,
            "input_applications": [],
        })
        ledger.observe_receipt(baseline)
        ledger.submit(SESSION, action(3, lease=2), requested_first_tick=7)
        missing_tick = behavior_receipt_from_mapping({
            "schema_version": "mc2p.client_action_receipt.v3",
            "generation_id": 2, "execution_path": "client_behavior_v1",
            "episode_id": "episode", "request_sequence_id": 3,
            "status": "executed", "reason": "movement_applied",
            "execution_thread": "Render thread", "on_client_thread": True,
            "execution_phase": "client_tick_action_boundary", "world_tick": 2,
            "action_keyboard_callbacks": 0, "action_mouse_callbacks": 0,
            "handled_screen_render_attempts": 0,
            "handled_screen_render_completions": 0,
            "input_samples": 8, "leased_input_samples": 1,
            "input_applications": [{
                "schema_version": "mc2p.input-application.v1",
                "movement_tick_id": 8, "episode_id": "episode",
                "request_sequence_id": 3, "sampled_at_jvm_ns": 108,
                "state": "leased", "forward": 1.0, "strafe": 0.0,
                "jump": False, "sneak": False, "sprint": False,
            }],
        })
        ledger.observe_receipt(missing_tick)
        self.assertIs(ledger.snapshot()[0].status, InputApplicationStatus.AMBIGUOUS)

    def test_unowned_neutral_sample_advances_motion_tick_without_claiming_a_command(self):
        ledger = InputApplicationLedger(max_records=2)
        unowned = ClientInputApplicationV1(
            "mc2p.input-application.v1", 9, None, None, 109,
            "lease_exhausted", 0.0, 0.0, False, False, False,
        )
        self.assertIsNone(ledger.observe_sample(unowned))
        self.assertEqual(ledger.latest_movement_tick_id, 9)

    def test_terminal_sample_states_resolve_an_unapplied_command(self):
        ledger = InputApplicationLedger(max_records=3)
        ledger.submit(SESSION, action(1), requested_first_tick=4)
        expired = ledger.observe_sample(sample(
            1, 4, forward=0.0, sample_state="expired",
        ))
        self.assertIs(expired.status, InputApplicationStatus.EXPIRED)
        ledger.submit(SESSION, action(2), requested_first_tick=5)
        rejected = ledger.observe_sample(sample(
            2, 5, forward=0.0, sample_state="disallowed",
        ))
        self.assertIs(rejected.status, InputApplicationStatus.REJECTED)

    def test_duplicate_sample_for_an_evicted_record_remains_harmless(self):
        ledger = InputApplicationLedger(max_records=1)
        ledger.submit(SESSION, action(1), requested_first_tick=4)
        old_sample = sample(1, 4)
        ledger.observe_sample(old_sample)
        ledger.submit(SESSION, action(2), requested_first_tick=5)
        self.assertIsNone(ledger.observe_sample(old_sample))


class StateAnchorTests(unittest.TestCase):
    def test_builds_post_movement_anchor_from_exact_application(self):
        ledger = InputApplicationLedger(max_records=4)
        ledger.submit(SESSION, action(4), requested_first_tick=9)
        ledger.observe_sample(sample(4, 9))
        builder = StateAnchorBuilder(
            ruleset=JAVA_1_21_RULESET,
            input_projection_version="mc2p.input-projection.v1",
        )
        result = builder.build(
            session=SESSION, observation_sequence_id=10,
            movement_tick_id=9, phase=MotionTickPhase.AFTER_MOVEMENT,
            physics_state=state(), ledger=ledger,
        )
        self.assertIs(result.status, AnchorBuildStatus.READY)
        self.assertEqual(result.anchor.physics_state.movement_tick_id, 9)
        self.assertEqual(result.anchor.confirmed_control_sequence, 4)

    def test_rejects_duplicate_observation_and_waits_for_relevant_in_flight_input(self):
        ledger = InputApplicationLedger(max_records=4)
        ledger.submit(SESSION, action(5), requested_first_tick=9)
        builder = StateAnchorBuilder(
            ruleset=JAVA_1_21_RULESET,
            input_projection_version="mc2p.input-projection.v1",
        )
        uncertain = builder.build(
            session=SESSION, observation_sequence_id=10,
            movement_tick_id=9, phase=MotionTickPhase.AFTER_MOVEMENT,
            physics_state=state(), ledger=ledger,
        )
        self.assertIs(uncertain.status, AnchorBuildStatus.NEEDS_INPUT_CONFIRMATION)
        ledger.observe_sample(sample(5, 9))
        ready = builder.build(
            session=SESSION, observation_sequence_id=10,
            movement_tick_id=9, phase=MotionTickPhase.AFTER_MOVEMENT,
            physics_state=state(), ledger=ledger,
        )
        self.assertIs(ready.status, AnchorBuildStatus.READY)
        duplicate = builder.build(
            session=SESSION, observation_sequence_id=10,
            movement_tick_id=9, phase=MotionTickPhase.AFTER_MOVEMENT,
            physics_state=state(), ledger=ledger,
        )
        self.assertIs(duplicate.status, AnchorBuildStatus.DUPLICATE_OBSERVATION)

    def test_partial_future_lease_does_not_poison_current_anchor(self):
        ledger = InputApplicationLedger(max_records=4)
        ledger.submit(SESSION, action(6, lease=2), requested_first_tick=9)
        ledger.observe_sample(sample(6, 9))
        builder = StateAnchorBuilder(
            ruleset=JAVA_1_21_RULESET,
            input_projection_version="mc2p.input-projection.v1",
        )
        first = builder.build(
            session=SESSION, observation_sequence_id=10,
            movement_tick_id=9, phase=MotionTickPhase.AFTER_MOVEMENT,
            physics_state=state(), ledger=ledger,
        )
        self.assertIs(first.status, AnchorBuildStatus.READY)
        self.assertEqual(first.anchor.confirmed_control_sequence, 6)
        self.assertEqual(first.anchor.confirmed_control_tick_range, (9, 9))
        second = builder.build(
            session=SESSION, observation_sequence_id=11,
            movement_tick_id=10, phase=MotionTickPhase.AFTER_MOVEMENT,
            physics_state=state(), ledger=ledger,
        )
        self.assertIs(second.status, AnchorBuildStatus.NEEDS_INPUT_CONFIRMATION)


class InputProjectionTests(unittest.TestCase):
    def test_projects_real_command_once_without_rescaling_crouch_axes(self):
        command = MovementV1(forward=1, strafe=-1, sneak=True)
        projected = project_movement_command(state(yaw=math.pi / 3), command)
        again = project_movement_command(state(yaw=math.pi / 3), command)
        self.assertIs(projected.status, ProjectionStatus.READY)
        self.assertEqual(projected.tick_input, again.tick_input)
        self.assertEqual(projected.tick_input.forward, 1.0)
        self.assertEqual(projected.tick_input.strafe, -1.0)
        self.assertTrue(projected.tick_input.sneak)
        self.assertEqual(projected.tick_input.movement_yaw_radians, math.pi / 3)

    def test_refuses_item_slowdown_until_that_projection_is_supported(self):
        projected = project_movement_command(state(using_item=True), MovementV1(forward=1))
        self.assertIs(projected.status, ProjectionStatus.UNSUPPORTED)
        self.assertEqual(projected.reasons, ("item_slowdown_not_supported",))


class ValidityTests(unittest.TestCase):
    def test_prediction_horizon_and_execution_window_are_not_interchangeable(self):
        prediction = PredictionValidity(9, 29, ("world:1",))
        window = CandidateExecutionWindow(10, 12)
        self.assertTrue(prediction.covers(20))
        self.assertFalse(window.allows_start(20))
        self.assertTrue(window.allows_start(11))

    def test_b10_contracts_are_available_from_public_motion_package(self):
        import mc2p.motion_nav as motion_nav
        self.assertIs(motion_nav.InputApplicationLedger, InputApplicationLedger)
        self.assertIs(motion_nav.StateAnchorBuilder, StateAnchorBuilder)


if __name__ == "__main__":
    unittest.main()
