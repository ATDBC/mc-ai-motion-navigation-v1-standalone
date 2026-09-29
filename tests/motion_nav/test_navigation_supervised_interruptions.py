"""Keep simulating the body after interruption through the formal driver."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, late_ticks, run
from tests.sim.runner import _goal
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.retry_ledger import WaitVerdict
from tests.sim.runner import Scenario
from tests.sim.scenarios import (
    SCENARIOS, airborne_in_drop, drop_ledge, revise_goal_back,
)
from tests.test_player_runtime import _task


def scenario(name):
    return next(item for item in SCENARIOS if item.name == name)


def exposed_edge(context):
    return (
        "landing_edge_probe" in context.diagnostics.controller_ids
        and context.backend.state.on_ground
        and context.backend.state.position[0] >= .92
    )


def request_release(context):
    context.driver.release("test_edge_cancel")


def request_close(context):
    context.session.close()


class SupervisedInterruptionTests(unittest.TestCase):
    def test_goal_revision_during_probe_stop_is_committed_without_partial_state(self):
        revised_position = (.5, 62.0, 30.5)

        def revise(context):
            revised = _goal(revised_position, context.risk_policy_id)
            context.driver.replace_goal(
                "goal", 2, revised, context.clock[0],
                damage_budget=TaskDamageBudget(
                    context.risk_policy_id, context.damage_points,
                ),
            )
            context.goal_state = revised
            context.goal_position = revised_position

        configured = replace(
            scenario("direct_drop_2"),
            name="revise_probe_stop_to_unresolved_goal",
            events=[Event("revise_unresolved", lambda context: context.tick >= 15,
                          revise)],
            max_ticks=300,
            expect="failed",
        )
        result = run(configured)
        self.assertEqual(result.events, ["revise_unresolved@15"])
        self.assertLess(result.ticks, 300)
        self.assertEqual(result.outcome_class, "bounded_safe_failure")
        self.assertFalse(any(code == "I13" for _, code, _ in result.violations))

    def test_repeated_goal_revisions_cannot_leave_stopping_without_controller(self):
        for period in (3, 5, 8, 13):
            with self.subTest(period=period):
                positions = ((-.5, 62.0, 4.5), (.5, 62.0, 4.5))
                events = []
                for index, tick in enumerate(range(12, 160, period)):
                    position = positions[index % 2]

                    def revise(context, revision=index + 2, target=position):
                        revised = _goal(target, context.risk_policy_id)
                        context.driver.replace_goal(
                            "goal", revision, revised, context.clock[0],
                            damage_budget=TaskDamageBudget(
                                context.risk_policy_id, context.damage_points,
                            ),
                        )
                        context.goal_state = revised
                        context.goal_position = target

                    events.append(Event(
                        f"revise_{index}",
                        lambda context, at=tick: context.tick >= at,
                        revise,
                    ))
                result = run(replace(
                    scenario("direct_drop_2"),
                    name=f"direct_drop_goal_flaps_every_{period}_ticks",
                    events=events,
                    max_ticks=400,
                ))
                self.assertLess(result.ticks, 400)
                self.assertNotEqual(result.outcome, "not_terminal")
                self.assertEqual(result.violations, [])

    def test_probe_displaced_to_lower_safe_support_replans_from_current_body(self):
        configured = replace(
            scenario("direct_drop_2"),
            name="edge_probe_displaced_to_lower_support",
            perturbations=Perturbations(impulses={35: (0.0, 0.0, .3)}),
            max_ticks=300,
        )
        result = run(configured)
        self.assertLess(result.ticks, 300)
        self.assertIn(result.outcome, {"success", "failed"}, result.reason)
        self.assertEqual(result.outcome_class,
                         ("task_success" if result.outcome == "success" else
                          "bounded_safe_failure"))
        self.assertEqual(result.violations, [])
        self.assertEqual(result.damage, 0.0)

    def test_probe_pushed_off_tall_ledge_retires_on_lower_safe_support(self):
        configured = replace(
            scenario("direct_drop_5_budget_2"),
            name="edge_probe_pushed_off_tall_ledge",
            perturbations=Perturbations(impulses={35: (0.0, 0.0, .3)}),
            max_ticks=300,
        )
        result = run(configured)
        self.assertLess(result.ticks, 300)
        self.assertNotEqual(result.outcome, "not_terminal")
        self.assertEqual(result.violations, [])
        self.assertNotEqual(result.reason, "recovery_unresolved")

    def test_removed_landing_support_is_rejected_before_drop_submission(self):
        floor_removal = {
            (x, 61, z): None
            for x in (-1, 0, 1)
            for z in (3, 4, 5)
        }
        for removal_tick in (28, 33):
            with self.subTest(removal_tick=removal_tick):
                configured = replace(
                    scenario("direct_drop_2"),
                    name="landing_removed_before_drop_submission",
                    perturbations=Perturbations(
                        world_edits={removal_tick: floor_removal},
                    ),
                    expect="failed",
                )
                result = run(configured)
                self.assertEqual(result.violations, [])
                self.assertEqual(
                    (result.outcome, result.reason, result.damage),
                    ("failed", "landing_support_missing", 0.0),
                )
                self.assertTrue(result.trace[-1]["on_ground"])

    def test_removed_landing_support_is_rechecked_four_ticks_before_departure(self):
        clean = run(replace(
            scenario("direct_drop_2"),
            name="landing_removal_departure_reference",
        ))
        departure_tick = next(
            row["tick"] for row in clean.trace if not row["on_ground"]
        )
        floor_removal = {
            (x, 61, z): None
            for x in (-1, 0, 1)
            for z in (3, 4, 5)
        }
        result = run(replace(
            scenario("direct_drop_2"),
            name="landing_removed_four_ticks_before_departure",
            perturbations=Perturbations(
                world_edits={departure_tick - 4: floor_removal},
            ),
            expect="failed",
        ))
        self.assertEqual(result.violations, [])
        self.assertEqual(
            (result.outcome, result.reason, result.damage),
            ("failed", "landing_support_missing", 0.0),
        )
        self.assertTrue(result.trace[-1]["on_ground"])

    def test_unsolved_side_landing_stops_probe_and_reports_motion_failure(self):
        result = run(Scenario(
            "side_landing_unsolved",
            drop_ledge(2),
            (.5, 64.0, .5),
            (1.5, 62.0, 4.5),
            max_ticks=300,
        ))
        self.assert_safe_settled_result(result)
        self.assertEqual(result.outcome, "failed")
        self.assertTrue(result.reason.startswith("motion_unsolvable:"))

    def test_edge_probe_handoff_finishes_with_constant_one_tick_latency(self):
        configured = replace(
            scenario("direct_drop_2"),
            name="direct_drop_fixed_one_tick_latency",
            perturbations=Perturbations(
                late_ticks=frozenset(range(2, 800)),
            ),
            max_ticks=600,
        )
        result = run(configured)
        self.assert_safe_settled_result(result)
        self.assertEqual(
            (result.outcome, result.reason),
            ("failed", "edge_probe_acquisition_timeout"),
        )

    def test_airborne_budget_cut_retains_executing_owner_until_landing(self):
        def cut_budget(context):
            goal = _goal(context.goal_position, "zero-after-start")
            context.driver.replace_goal(
                "goal", 2, goal, context.clock[0],
                damage_budget=TaskDamageBudget("zero-after-start", 0),
            )
            context.goal_state = goal

        configured = replace(
            scenario("direct_drop_5_budget_2"),
            name="airborne_budget_cut_requires_landing_owner",
            start=(.5, 64.0, 2.5),
            events=[Event("cut_budget_in_air", airborne_in_drop, cut_budget)],
        )
        result = run(configured)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.violations, [])
        after_revision = [row for row in result.trace if row["goal_revision"] == 2]
        self.assertTrue(after_revision)
        airborne = [row for row in after_revision if not row["on_ground"]]
        self.assertTrue(airborne, "budget was not revised during the tested action")
        self.assertFalse(any(row["driver_state"] in {"failed", "cancelled", "stopped"}
                             for row in airborne),
                         "driver ended while an old authorized action still needed landing")
        self.assertTrue(all(row["source_bound"] for row in airborne))
        self.assertEqual(len({row["observation_sequence"] for row in airborne}),
                         len(airborne), "a stale owner survived only in diagnostics")
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertEqual(result.damage, 2.0)

    def test_revised_drop_solve_failure_stops_probe_before_route_restart(self):
        def revise_sideways(context):
            position = (-.5, 62.0, 4.5)
            goal = _goal(position, context.risk_policy_id)
            context.driver.replace_goal(
                "goal", 2, goal, context.clock[0],
                damage_budget=TaskDamageBudget(
                    context.risk_policy_id, context.damage_points,
                ),
            )
            context.goal_state = goal
            context.goal_position = position

        configured = replace(
            scenario("direct_drop_2"),
            name="revised_drop_unsolved_after_probe",
            events=[Event(
                "revise_during_probe",
                lambda context: context.tick >= 34,
                revise_sideways,
            )],
            max_ticks=300,
            expect="failed",
        )

        result = run(configured)

        self.assertEqual(result.events, ["revise_during_probe@34"])
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.violations, [])
        self.assertLess(result.ticks, configured.max_ticks)
        self.assertTrue(result.trace[-1]["on_ground"])

    def assert_safe_settled_result(self, result):
        unsafe = [entry for entry in result.violations
                  if entry[1] in {"I1", "I2", "I3", "I6"}]
        self.assertEqual(unsafe, [], result.reason)
        self.assertEqual(result.damage, 0.0, result.reason)
        self.assertLess(result.ticks, 400, "known safe retreat did not finish")
        self.assertIn(result.outcome, {
            "success", "cancelled", "failed", "closed", "driver_failed/closed",
        })
        last = result.trace[-1]
        self.assertTrue(last["on_ground"], result.reason)
        self.assertLess(abs(last["velocity"][0]), .002)
        self.assertLess(abs(last["velocity"][2]), .002)
        terminal = [row for row in result.trace
                    if row["session_state"] in {
                        "complete", "failed", "cancelled", "closed",
                    }]
        self.assertTrue(terminal, "no confirmed task result")
        self.assertGreaterEqual(last["movement_tick"] - terminal[0]["movement_tick"],
                                19, "missing released-input physics tail")

    def test_edge_probe_timeout_keeps_body_until_safe(self):
        configured = replace(
            scenario("direct_drop_2"),
            name="direct_drop_probe_timeout",
            perturbations=Perturbations(late_ticks=late_ticks(.2, 280001)),
            expect="failed",
        )
        result = run(configured)
        self.assert_safe_settled_result(result)
        self.assertEqual(result.reason, "edge_probe_acquisition_timeout")

    def test_probe_goal_revision_keeps_body_until_safe(self):
        result = run(scenario("direct_drop_2_goal_revised_during_probe"))
        self.assertEqual(len(result.events), 1, "goal revision was never injected")
        self.assert_safe_settled_result(result)
        self.assertEqual(result.outcome, "success",
                         "safe retreat must wait for successor entry posture")

    def test_cancel_at_actual_exposed_edge_has_safe_physics_tail(self):
        configured = replace(
            scenario("direct_drop_2"),
            name="direct_drop_cancel_at_exposed_edge",
            events=[Event("cancel_at_exposed_edge", exposed_edge, request_release)],
            expect="cancelled",
        )
        result = run(configured)
        self.assertEqual(len(result.events), 1, "body never reached the tested edge")
        self.assert_safe_settled_result(result)
        self.assertEqual(result.outcome, "cancelled")

    def test_close_at_actual_exposed_edge_finishes_body_before_closing(self):
        configured = replace(
            scenario("direct_drop_2"),
            name="direct_drop_close_at_exposed_edge",
            events=[Event("close_at_exposed_edge", exposed_edge, request_close)],
            expect="closed",
        )
        result = run(configured)
        self.assertEqual(len(result.events), 1, "body never reached the tested edge")
        self.assert_safe_settled_result(result)
        # The existing driver maps CLOSED to its terminal "failed" label.
        # This test concerns retaining body ownership, not that report mapping.
        self.assertEqual(result.trace[-1]["session_state"], "closed")

    def test_edge_cancellation_retains_protection_with_delayed_inputs(self):
        for delayed in ({19}, {19, 21}, {20, 22, 24}):
            with self.subTest(delayed=delayed):
                configured = replace(
                    scenario("direct_drop_2"),
                    name="direct_drop_cancel_delayed_" + "_".join(map(str, delayed)),
                    perturbations=Perturbations(late_ticks=frozenset(delayed)),
                    events=[Event("cancel_at_exposed_edge", exposed_edge,
                                  request_release)],
                    expect="cancelled",
                )
                result = run(configured)
                self.assertEqual(len(result.events), 1,
                                 "body never reached the tested edge")
                self.assert_safe_settled_result(result)
                self.assertEqual(result.outcome, "cancelled")

    def test_cancel_and_close_after_actual_departure_keep_landing_owner(self):
        for action, expected in ((request_release, "cancelled"),
                                 (request_close, "closed")):
            with self.subTest(expected=expected):
                configured = replace(
                    scenario("direct_drop_2"),
                    name=f"near_edge_air_{expected}",
                    start=(.5, 64.0, 2.5),
                    events=[Event("interrupt_in_air", airborne_in_drop, action)],
                    expect=expected,
                )
                result = run(configured)
                self.assertEqual(len(result.events), 1,
                                 "interruption was not exercised in the air")
                self.assert_safe_settled_result(result)
                self.assertEqual(result.trace[-1]["session_state"], expected)
                ticks = [row["movement_tick"] for row in result.trace]
                self.assertTrue(all(b == a + 1 for a, b in zip(ticks, ticks[1:])),
                                "an applied physics tick was not checked")

    def test_temporary_anchor_gap_during_edge_stop_keeps_owner(self):
        missing_frames = 0

        def shared_control(context):
            nonlocal missing_frames
            driver = context.driver
            deadline = context.clock[0] + 500_000_000
            if (context.diagnostics.state.value == "stopping"
                    and missing_frames < 3):
                missing_frames += 1
                with patch.object(context.session, "execution_anchor",
                                  return_value=None):
                    result = driver.tick(BehaviorProfileV0(), deadline)
                self.assertIsNone(result.report.failure)
                self.assertIsNotNone(driver.source,
                                     "missing evidence released the body source")
                self.assertIn("landing_edge_probe",
                              context.diagnostics.controller_ids)
                self.assertTrue(result.decision.action.movement.sneak)
            else:
                driver.tick(BehaviorProfileV0(), deadline)

        configured = replace(
            scenario("direct_drop_2"),
            name="edge_cancel_temporary_anchor_gap",
            events=[Event("cancel_at_exposed_edge", exposed_edge, request_release)],
            expect="cancelled",
        )
        result = run(configured, control_step=shared_control)
        self.assertEqual(missing_frames, 3)
        self.assert_safe_settled_result(result)
        self.assertEqual(result.outcome, "cancelled")

    def test_stop_deadline_reports_unresolved_without_releasing_body(self):
        missing_frames = 0
        exhausted_frames = 0

        def shared_control(context):
            nonlocal missing_frames, exhausted_frames
            driver = context.driver
            deadline = context.clock[0] + 500_000_000
            if (context.diagnostics.state.value == "stopping"
                    and missing_frames < 45):
                missing_frames += 1
                with patch.object(context.session, "execution_anchor", return_value=None):
                    result = driver.tick(BehaviorProfileV0(), deadline)
                self.assertIsNone(result.report.failure)
                self.assertIsNotNone(driver.source)
                self.assertIn("landing_edge_probe", context.diagnostics.controller_ids)
                self.assertTrue(result.decision.action.movement.sneak)
                if context.diagnostics.recovery_wait_status in {
                    WaitVerdict.EXHAUSTED_TICKS, WaitVerdict.EXHAUSTED_CLOCK,
                }:
                    exhausted_frames += 1
            else:
                driver.tick(BehaviorProfileV0(), deadline)

        configured = replace(
            scenario("direct_drop_2"),
            name="edge_cancel_anchor_gap_past_stop_deadline",
            events=[Event("cancel_at_exposed_edge", exposed_edge, request_release)],
            expect="cancelled",
        )
        result = run(configured, control_step=shared_control)
        self.assertEqual(missing_frames, 45)
        self.assertGreater(exhausted_frames, 0,
                           "the controller never reported its exhausted recovery deadline")
        self.assert_safe_settled_result(result)
        self.assertEqual(result.outcome, "cancelled")

    def test_combat_look_cannot_redirect_retreat_or_linger_after_handoff(self):
        for navigation_first in (False, True):
            with self.subTest(navigation_first=navigation_first):
                source = None
                sequence = 0
                protected_frames = 0
                stopping_seen = False
                successor_look_frames = 0

                def shared_control(context):
                    nonlocal source, sequence, protected_frames
                    nonlocal stopping_seen, successor_look_frames
                    driver = context.driver
                    runtime = driver.runtime
                    deadline = context.clock[0] + 500_000_000
                    stopping = context.diagnostics.state.value == "stopping"
                    stopping_seen = stopping_seen or stopping
                    after_handoff = stopping_seen and not stopping
                    if not stopping and not (after_handoff and successor_look_frames < 2):
                        driver.tick(BehaviorProfileV0(), deadline)
                        return
                    if source is None:
                        source = runtime.register_ordered_source("test-combat-look")
                    sequence += 1
                    intent = ActionIntentV1(
                        ordered_intent_id(source, sequence), source.source_id,
                        source.episode_id, runtime.observation.sequence_id,
                        ActionPriorityV0.TASK, context.clock[0], deadline,
                        look=LookV1(30.0, 0.0),
                    )
                    look = OrderedIntentV1(source, sequence, intent)
                    navigation = driver.prepare_proposals(deadline, conditioned_look=look)
                    combat = (ControlFrameProposalV1(intents=(look,)),)
                    proposals = navigation + combat if navigation_first else combat + navigation
                    requires_sneak = any(
                        ordered.intent.movement is not None
                        and ordered.intent.movement.sneak
                        for proposal in navigation for ordered in proposal.intents
                    )
                    result = runtime.control_frame(
                        _task(deadline), BehaviorProfileV0(), deadline,
                        proposals=proposals,
                    )
                    driver.adopt_result(result)
                    self.assertIsNone(result.report.failure)
                    self.assertIsNotNone(result.decision)
                    action = result.decision.action
                    if stopping and requires_sneak:
                        protected_frames += 1
                        self.assertTrue(action.movement.sneak,
                                        "combat arbitration removed protective sneak")
                        self.assertEqual(action.look.yaw_delta_degrees, 0.0,
                                         "retreat used keys under a different view")
                    if after_handoff:
                        successor_look_frames += 1
                        self.assertEqual(action.look.yaw_delta_degrees, 30.0,
                                         "old safety intent blocked successor look")

                configured = replace(
                    scenario("direct_drop_2"),
                    name="direct_drop_revision_with_combat_look",
                    events=[Event("revise_at_exposed_edge", exposed_edge, revise_goal_back)],
                )
                result = run(configured, control_step=shared_control)
                self.assertEqual(len(result.events), 1)
                self.assertGreater(protected_frames, 0, "no combined stopping frame tested")
                self.assertEqual(successor_look_frames, 2, "successor never received look")
                self.assert_safe_settled_result(result)
                self.assertEqual(result.outcome, "success")

    def test_combat_look_during_active_probe_cannot_release_edge_guard(self):
        source = None
        sequence = 0
        injected = 0
        lost_sneak = []

        def shared_control(context):
            nonlocal source, sequence, injected
            driver = context.driver
            runtime = driver.runtime
            deadline = context.clock[0] + 500_000_000
            begin = ("landing_edge_probe" in context.diagnostics.controller_ids
                     and context.backend.state.position[0] >= .92)
            if injected >= 7 or not (begin or injected):
                driver.tick(BehaviorProfileV0(), deadline)
                return
            if source is None:
                source = runtime.register_ordered_source("test-active-probe-look")
            sequence += 1
            injected += 1
            intent = ActionIntentV1(
                ordered_intent_id(source, sequence), source.source_id,
                source.episode_id, runtime.observation.sequence_id,
                ActionPriorityV0.TASK, context.clock[0], deadline,
                look=LookV1(30.0, 0.0),
            )
            look = OrderedIntentV1(source, sequence, intent)
            proposals = driver.prepare_proposals(deadline, conditioned_look=look)
            result = runtime.control_frame(
                _task(deadline), BehaviorProfileV0(), deadline,
                proposals=proposals + (ControlFrameProposalV1(intents=(look,)),),
            )
            driver.adopt_result(result)
            if not result.decision.action.movement.sneak:
                lost_sneak.append(context.tick)

        result = run(scenario("direct_drop_2"), control_step=shared_control)
        self.assertEqual(injected, 7, "active edge combination was not exercised")
        self.assert_safe_settled_result(result)
        self.assertEqual(lost_sneak, [],
                         "combat view must not remove a live probe's edge guard")

    def test_continuous_combat_look_does_not_starve_valid_edge_acquisition(self):
        source = None
        sequence = 0

        def shared_control(context):
            nonlocal source, sequence
            driver = context.driver
            runtime = driver.runtime
            deadline = context.clock[0] + 500_000_000
            if "landing_edge_probe" not in context.diagnostics.controller_ids:
                driver.tick(BehaviorProfileV0(), deadline)
                return
            if source is None:
                source = runtime.register_ordered_source("test-continuous-probe-look")
            sequence += 1
            intent = ActionIntentV1(
                ordered_intent_id(source, sequence), source.source_id,
                source.episode_id, runtime.observation.sequence_id,
                ActionPriorityV0.TASK, context.clock[0], deadline,
                look=LookV1(30.0, 0.0),
            )
            look = OrderedIntentV1(source, sequence, intent)
            proposals = driver.prepare_proposals(deadline, conditioned_look=look)
            result = runtime.control_frame(
                _task(deadline), BehaviorProfileV0(), deadline,
                proposals=proposals + (ControlFrameProposalV1(intents=(look,)),),
            )
            driver.adopt_result(result)

        configured = replace(scenario("direct_drop_2"),
                             name="near_edge_continuous_combat_look",
                             start=(.5, 64.0, 2.5))
        result = run(configured, control_step=shared_control)
        self.assertGreater(sequence, 3, "continuous look conflict was not exercised")
        self.assert_safe_settled_result(result)
        self.assertEqual(result.outcome, "success",
                         "view competition must not starve a feasible acquisition")


if __name__ == "__main__":
    unittest.main()
