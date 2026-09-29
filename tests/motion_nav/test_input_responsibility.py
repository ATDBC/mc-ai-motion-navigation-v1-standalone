"""Input history must neither lose an old command nor poison a new task."""
from dataclasses import replace
import unittest

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputApplicationStatus,
    InputResponsibilityDisposition, InputResponsibilityStatus,
    MotionTickPhase, StateAnchor, assess_input_responsibility,
    input_responsibility_status,
)
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.world_model import WorldSessionId
from tests.motion_nav.test_b10_online_motion import SESSION, action, sample, state


def anchor(tick=6):
    return StateAnchor(
        SESSION, tick, tick, MotionTickPhase.AFTER_MOVEMENT, None, None,
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        "mc2p.input-projection.v1", state(movement_tick_id=tick),
    )


class InputResponsibilityTests(unittest.TestCase):
    def ambiguous_ledger(self):
        ledger = InputApplicationLedger()
        ledger.submit(SESSION, action(1), requested_first_tick=4)
        ledger.mark_ambiguous(1, at_tick=4)
        return ledger

    def confirm_replacement(self, ledger):
        ledger.submit(SESSION, action(2, MovementV1()), requested_first_tick=5)
        ledger.observe_sample(sample(2, 5, forward=0.0, sample_state="neutral"))

    def test_older_in_flight_jump_is_not_ignored_by_sequence_floor(self):
        ledger = InputApplicationLedger()
        ledger.submit(SESSION, action(1, MovementV1(forward=1, jump=True)),
                      requested_first_tick=4)
        self.assertIs(input_responsibility_status(
            ledger, anchor(), previous_sequence_floor=10,
        ), InputResponsibilityStatus.IN_FLIGHT)

    def test_new_body_anchor_alone_does_not_cancel_delayed_old_command(self):
        ledger = self.ambiguous_ledger()
        self.assertIs(input_responsibility_status(
            ledger, anchor(), previous_sequence_floor=1,
        ), InputResponsibilityStatus.AMBIGUOUS)

    def test_later_anchor_can_transfer_current_body_without_rewriting_history(self):
        ledger = self.ambiguous_ledger()

        assessment = assess_input_responsibility(
            ledger, anchor(6), previous_sequence_floor=0,
        )

        self.assertIs(
            assessment.disposition,
            InputResponsibilityDisposition.TRANSFERABLE_FROM_CURRENT_ANCHOR,
        )
        self.assertEqual(assessment.blocking_sequences, (1,))
        self.assertIs(ledger.record(1).status, InputApplicationStatus.AMBIGUOUS)

    def test_anchor_inside_possible_application_window_keeps_ambiguity(self):
        ledger = self.ambiguous_ledger()

        assessment = assess_input_responsibility(
            ledger, anchor(4), previous_sequence_floor=0,
        )

        self.assertIs(
            assessment.disposition,
            InputResponsibilityDisposition.AMBIGUOUS_WAITING,
        )

    def test_confirmed_replacement_and_reanchor_clear_historical_ambiguity(self):
        ledger = self.ambiguous_ledger()
        self.confirm_replacement(ledger)
        self.assertIs(input_responsibility_status(
            ledger, anchor(), previous_sequence_floor=1,
        ), InputResponsibilityStatus.CLEAR)
        # Retain the original evidence; do not rewrite history to claim receipt.
        self.assertIs(ledger.record(1).status, InputApplicationStatus.AMBIGUOUS)

    def test_replacement_without_current_anchor_keeps_responsibility(self):
        ledger = self.ambiguous_ledger()
        self.confirm_replacement(ledger)
        self.assertIs(input_responsibility_status(
            ledger, None, previous_sequence_floor=1,
        ), InputResponsibilityStatus.AMBIGUOUS)

    def test_anchor_before_replacement_cannot_clear_historical_ambiguity(self):
        ledger = self.ambiguous_ledger()
        ledger.submit(SESSION, action(2, MovementV1()), requested_first_tick=8)
        ledger.observe_sample(sample(2, 8, forward=0.0, sample_state="neutral"))
        self.assertIs(input_responsibility_status(
            ledger, anchor(6), previous_sequence_floor=1,
        ), InputResponsibilityStatus.AMBIGUOUS)

    def test_current_action_ambiguity_is_not_cleared_as_old_history(self):
        ledger = self.ambiguous_ledger()
        self.confirm_replacement(ledger)
        self.assertIs(input_responsibility_status(
            ledger, anchor(), previous_sequence_floor=0,
        ), InputResponsibilityStatus.AMBIGUOUS)

    def test_foreign_world_anchor_cannot_discharge_responsibility(self):
        ledger = self.ambiguous_ledger()
        self.confirm_replacement(ledger)
        foreign = replace(anchor(), session=WorldSessionId("other-world"))
        self.assertIs(input_responsibility_status(
            ledger, foreign, previous_sequence_floor=1,
        ), InputResponsibilityStatus.AMBIGUOUS)

    def test_look_only_command_also_blocks_completion_until_applied(self):
        ledger = InputApplicationLedger()
        look = replace(action(1, MovementV1()), look=LookV1(30.0, 0.0))
        ledger.submit(SESSION, look, requested_first_tick=4)
        self.assertIs(input_responsibility_status(
            ledger, anchor(), previous_sequence_floor=10,
        ), InputResponsibilityStatus.IN_FLIGHT)


if __name__ == "__main__":
    unittest.main()
