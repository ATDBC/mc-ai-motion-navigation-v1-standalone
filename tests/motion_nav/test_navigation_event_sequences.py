from dataclasses import replace
import unittest

from tests.sim.event_sequences import (
    EventKind,
    GeneratedEvent,
    GeneratedSequence,
    generate_sequence,
    run_sequence,
    shrink_sequence,
)


class NavigationEventSequenceTests(unittest.TestCase):
    def test_generation_is_fixed_by_seed_and_keeps_events_ordered(self):
        first = generate_sequence(23001, event_count=7)
        second = generate_sequence(23001, event_count=7)

        self.assertEqual(first, second)
        self.assertEqual(len(first.events), 7)
        self.assertEqual(
            [event.tick for event in first.events],
            sorted(event.tick for event in first.events),
        )
        self.assertGreater(len({event.kind for event in first.events}), 1)

    def test_shrinker_removes_unrelated_events_and_moves_trigger_earlier(self):
        sequence = GeneratedSequence(
            seed=1,
            scenario="direct_drop_2",
            max_ticks=120,
            events=(
                GeneratedEvent(EventKind.LATE_INPUT, 14),
                GeneratedEvent(EventKind.GOAL_OUT_OF_RANGE, 28),
                GeneratedEvent(EventKind.OMIT_RECEIPT, 44),
            ),
        )

        reduced = shrink_sequence(
            sequence,
            lambda candidate: any(
                event.kind is EventKind.GOAL_OUT_OF_RANGE
                and event.tick >= 2
                for event in candidate.events
            ),
        )

        self.assertEqual(len(reduced.events), 1)
        self.assertIs(reduced.events[0].kind, EventKind.GOAL_OUT_OF_RANGE)
        self.assertEqual(reduced.events[0].tick, 2)

    def test_fixed_random_sequences_reach_terminal_without_invariant_failure(self):
        for seed in (23001, 23002, 23003):
            with self.subTest(seed=seed):
                sequence = generate_sequence(seed, event_count=4)
                outcome = run_sequence(sequence)
                self.assertIsNone(outcome.exception)
                self.assertIsNotNone(outcome.result)
                self.assertEqual(outcome.result.violations, [])
                self.assertIn(
                    outcome.result.outcome,
                    {"success", "failed", "cancelled"},
                )

    def test_goal_revision_receipt_gap_and_lost_arbitration_keep_body_owner(self):
        sequence = GeneratedSequence(
            seed=23131,
            scenario="direct_drop_2",
            max_ticks=300,
            events=tuple(sorted((
                GeneratedEvent(EventKind.GOAL_BACK, 12),
                GeneratedEvent(EventKind.OMIT_RECEIPT, 22),
                GeneratedEvent(EventKind.LOSE_ARBITRATION, 36),
            ), key=lambda event: (event.tick, event.kind.value))),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.violations, [])
        self.assertIn(outcome.result.outcome, {"success", "failed"})

    def test_events_are_not_dispatched_after_task_reaches_terminal_state(self):
        sequence = GeneratedSequence(
            seed=23130,
            scenario="direct_drop_2",
            max_ticks=300,
            events=(
                GeneratedEvent(EventKind.REMOVE_LANDING_SUPPORT, 33),
                GeneratedEvent(EventKind.GOAL_BACK, 43),
            ),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.outcome, "failed")
        self.assertNotIn("goal_back-2@43", outcome.result.events)


if __name__ == "__main__":
    unittest.main()
