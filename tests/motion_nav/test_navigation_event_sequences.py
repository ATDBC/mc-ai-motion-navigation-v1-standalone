from dataclasses import replace
import unittest
from types import SimpleNamespace

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.event_sequences import (
    _configured,
    EventKind,
    GeneratedEvent,
    GeneratedSequence,
    generate_sequence,
    run_sequence,
    shrink_sequence,
)
from tests.sim.backend import CalculatorBackend, Perturbations, Scene
from tests.sim.runner import Event, _goal, run
from tests.sim.run_navigation_event_sequences import (
    _is_declared_result,
    _result_class,
)
from tests.sim.scenarios import SCENARIOS


class NavigationEventSequenceTests(unittest.TestCase):
    def test_gate_rejects_a_bounded_result_not_frozen_for_the_scenario(self):
        outcome = SimpleNamespace(
            exception=None,
            result=SimpleNamespace(
                violations=[],
                outcome="failed",
                reason="new_unreviewed_failure",
            ),
        )

        self.assertFalse(_is_declared_result(
            outcome,
            {("success", "goal_state_satisfied")},
        ))

    def test_generated_events_report_applied_and_skipped_separately(self):
        sequence = GeneratedSequence(
            seed=24001,
            scenario="direct_drop_2",
            max_ticks=300,
            events=(
                GeneratedEvent(EventKind.CANCEL, 10),
                GeneratedEvent(EventKind.EXTERNAL_PUSH, 70),
            ),
        )

        outcome = run_sequence(sequence)

        self.assertEqual(
            [(item.kind, item.status) for item in outcome.event_applications],
            [
                (EventKind.CANCEL, "dispatched"),
                (EventKind.EXTERNAL_PUSH, "skipped"),
            ],
        )

    def test_internal_contract_failure_is_never_a_bounded_accepted_result(self):
        outcome = SimpleNamespace(
            exception=None,
            result=SimpleNamespace(
                violations=[],
                outcome="failed",
                reason="navigation_internal_contract_failure",
            ),
        )

        self.assertEqual(_result_class(outcome), "internal_contract_failure")

    def test_omitted_receipt_then_goal_revision_always_reaches_a_terminal_result(self):
        combinations = (
            (3, 5),
            (6, 9),
            (10, 14),
        )
        for scenario in (
                "direct_drop_2",
                "terrace_two_ledges",
                "far_landing_L_walkway"):
            for omitted_tick, revision_tick in combinations:
                with self.subTest(
                        scenario=scenario,
                        omitted_tick=omitted_tick,
                        revision_tick=revision_tick):
                    sequence = GeneratedSequence(
                        22001,
                        scenario,
                        400,
                        tuple(sorted((
                            GeneratedEvent(EventKind.OMIT_RECEIPT, omitted_tick),
                            GeneratedEvent(EventKind.GOAL_BACK, revision_tick),
                        ), key=lambda event: (event.tick, event.kind.value))),
                    )

                    outcome = run_sequence(sequence)

                    self.assertIsNone(outcome.exception)
                    self.assertIsNotNone(outcome.result)
                    self.assertEqual(outcome.result.violations, [])
                    self.assertIn(
                        outcome.result.outcome,
                        {"success", "failed", "cancelled"},
                    )

    def test_removed_landing_then_external_push_cannot_strand_probe(self):
        sequence = GeneratedSequence(
            23037,
            "direct_drop_5_budget_2",
            400,
            (
                GeneratedEvent(EventKind.REMOVE_LANDING_SUPPORT, 2),
                GeneratedEvent(EventKind.EXTERNAL_PUSH_BACKWARD, 14),
            ),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.violations, [])
        self.assertIn(
            outcome.result.outcome,
            {"success", "failed", "cancelled"},
        )

    def test_stopping_goal_revision_cannot_reenter_planning_without_handoff(self):
        sequence = GeneratedSequence(
            22002,
            "direct_drop_2",
            400,
            tuple(sorted((
                GeneratedEvent(EventKind.GOAL_BACK, 29),
                GeneratedEvent(EventKind.GOAL_OUT_OF_RANGE, 38),
            ), key=lambda event: (event.tick, event.kind.value))),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.violations, [])
        self.assertIn(
            outcome.result.outcome,
            {"success", "failed", "cancelled"},
        )

    def test_repeated_reachable_goal_revisions_do_not_leak_probe_waits(self):
        base = next(
            scenario for scenario in SCENARIOS
            if scenario.name == "direct_drop_2"
        )
        goals = ((-.5, 62.0, 4.5), (.5, 62.0, 4.5))

        def revise(index, position):
            def action(context):
                goal = _goal(position, context.risk_policy_id)
                context.driver.replace_goal(
                    "goal", index + 2, goal, context.clock[0],
                    damage_budget=TaskDamageBudget(
                        context.risk_policy_id, context.damage_points,
                    ),
                )
                context.goal_state = goal
                context.goal_position = position
            return action

        events = [
            Event(
                f"flap-{index}",
                lambda context, at=tick: context.tick >= at,
                revise(index, goals[index % 2]),
            )
            for index, tick in enumerate(range(12, 120, 3))
        ]

        result = run(replace(base, events=events, max_ticks=400))

        self.assertNotEqual(result.reason, "acquisition_wait_capacity_exhausted")
        self.assertEqual(result.violations, [])
        self.assertIn(result.outcome, {"success", "failed", "cancelled"})

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

    def test_generation_allows_repeated_event_kinds_before_all_kinds_are_used(self):
        sequence = generate_sequence(23001, event_count=7)

        self.assertLess(
            len({event.kind for event in sequence.events}),
            len(sequence.events),
        )

    def test_remove_support_uses_each_scenarios_declared_dependency(self):
        for scenario_name in (
                "direct_drop_2",
                "direct_drop_5_budget_2",
                "far_landing_L_walkway",
                "terrace_two_ledges"):
            with self.subTest(scenario=scenario_name):
                sequence = GeneratedSequence(
                    seed=23002,
                    scenario=scenario_name,
                    max_ticks=120,
                    events=(GeneratedEvent(
                        EventKind.REMOVE_LANDING_SUPPORT,
                        20,
                    ),),
                )

                configured, _ = _configured(sequence)
                base = next(
                    item for item in SCENARIOS
                    if item.name == scenario_name
                )

                self.assertTrue(base.landing_support_cells)
                self.assertEqual(
                    frozenset(configured.perturbations.world_edits[20]),
                    frozenset(base.landing_support_cells),
                )
                self.assertTrue(all(
                    cell in base.scene.solids
                    for cell in base.landing_support_cells
                ))

    def test_backend_can_stop_future_external_perturbations_without_dropping_lease(self):
        clock = [0]
        scene = Scene({(0, 63, 0): "minecraft:stone"}, ((-2, 2), (60, 68), (-2, 2)))
        backend = CalculatorBackend(
            clock,
            scene,
            (.5, 64.0, .5),
            0.0,
            perturbations=Perturbations(
                impulses={2: (0.0, 0.0, 1.0)},
                world_edits={2: {(0, 63, 0): None}},
                omitted_receipt_ticks=frozenset({2}),
            ),
        )

        backend.stop_external_perturbations()
        backend.free_tick()

        self.assertIn((0, 63, 0), backend.scene.solids)
        self.assertEqual(backend.state.velocity_blocks_per_tick[2], 0.0)
        self.assertEqual(backend.perturbations.omitted_receipt_ticks, frozenset())

    def test_repeated_world_removal_is_dispatched_twice_but_applied_once(self):
        clock = [0]
        cell = (0, 63, 0)
        backend = CalculatorBackend(
            clock,
            Scene({cell: "minecraft:stone"}, ((-2, 2), (60, 68), (-2, 2))),
            (.5, 64.0, .5),
            0.0,
            perturbations=Perturbations(world_edits={
                2: {cell: None},
                3: {cell: None},
            }),
        )

        backend.free_tick()
        backend.free_tick()

        self.assertEqual(
            backend.dispatched_perturbations,
            [
                (EventKind.REMOVE_LANDING_SUPPORT.value, 2),
                (EventKind.REMOVE_LANDING_SUPPORT.value, 3),
            ],
        )
        self.assertEqual(
            backend.applied_perturbations,
            [(EventKind.REMOVE_LANDING_SUPPORT.value, 2)],
        )

    def test_arbitration_without_a_navigation_movement_is_only_dispatched(self):
        outcome = run_sequence(GeneratedSequence(
            seed=24002,
            scenario="direct_drop_2",
            max_ticks=300,
            events=(GeneratedEvent(EventKind.LOSE_ARBITRATION, 33),),
        ))

        self.assertEqual(len(outcome.event_applications), 1)
        self.assertEqual(outcome.event_applications[0].status, "dispatched")

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

    def test_same_tick_post_completion_push_does_not_create_a_false_violation(self):
        sequence = GeneratedSequence(
            seed=23006,
            scenario="direct_drop_2",
            max_ticks=300,
            events=tuple(sorted((
                GeneratedEvent(EventKind.GOAL_BACK, 12),
                GeneratedEvent(EventKind.EXTERNAL_PUSH_BACKWARD, 19),
                GeneratedEvent(EventKind.EXTERNAL_PUSH_BACKWARD, 53),
            ), key=lambda event: (event.tick, event.kind.value))),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.outcome, "success")
        self.assertEqual(outcome.result.violations, [])

    def test_goal_revision_does_not_release_a_moving_body_before_it_is_stable(self):
        sequence = GeneratedSequence(
            seed=23046,
            scenario="direct_drop_2",
            max_ticks=400,
            events=tuple(sorted((
                GeneratedEvent(EventKind.EXTERNAL_PUSH, 9),
                GeneratedEvent(EventKind.GOAL_OUT_OF_RANGE, 9),
                GeneratedEvent(EventKind.EXTERNAL_PUSH, 10),
            ), key=lambda event: (event.tick, event.kind.value))),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.violations, [])
        self.assertIn(outcome.result.outcome, {"success", "failed", "cancelled"})

    def test_repeated_navigation_scope_shoves_have_a_bounded_result(self):
        sequence = GeneratedSequence(
            seed=23083,
            scenario="direct_drop_5_budget_2",
            max_ticks=400,
            events=tuple(sorted((
                GeneratedEvent(EventKind.EXTERNAL_PUSH_BACKWARD, 3),
                GeneratedEvent(EventKind.EXTERNAL_PUSH_BACKWARD, 4),
                GeneratedEvent(EventKind.LATE_INPUT, 4),
            ), key=lambda event: (event.tick, event.kind.value))),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.violations, [])
        self.assertIn(outcome.result.outcome, {"success", "failed", "cancelled"})

    def test_failed_replacement_stops_the_safe_prefix_instead_of_waiting_forever(self):
        sequence = GeneratedSequence(
            seed=23156,
            scenario="terrace_two_ledges",
            max_ticks=400,
            events=tuple(sorted((
                GeneratedEvent(EventKind.LOSE_ARBITRATION, 14),
                GeneratedEvent(EventKind.EXTERNAL_PUSH, 17),
                GeneratedEvent(EventKind.LOSE_ARBITRATION, 20),
                GeneratedEvent(EventKind.EXTERNAL_PUSH_BACKWARD, 38),
                GeneratedEvent(EventKind.GOAL_BACK, 63),
            ), key=lambda event: (event.tick, event.kind.value))),
        )

        outcome = run_sequence(sequence)

        self.assertIsNone(outcome.exception)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.violations, [])
        self.assertIn(outcome.result.outcome, {"success", "failed", "cancelled"})


if __name__ == "__main__":
    unittest.main()
