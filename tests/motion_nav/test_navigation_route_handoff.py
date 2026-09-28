"""Formal Runtime checks for route replacement while the body keeps moving."""
from dataclasses import replace
import unittest

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.runner import Event, _goal, run
from tests.sim.backend import Perturbations
from tests.sim.scenarios import SCENARIOS
from tests.test_player_runtime import _task


def revise_forward(context):
    position = (.5, 64.0, 6.5)
    goal = _goal(position, context.risk_policy_id)
    context.driver.replace_goal(
        "goal", 2, goal, context.clock[0],
        damage_budget=TaskDamageBudget(context.risk_policy_id,
                                       context.damage_points),
    )
    context.goal_state = goal
    context.goal_position = position


class RouteHandoffTests(unittest.TestCase):
    def test_goal_revision_keeps_valid_walk_prefix_moving(self):
        source = next(s for s in SCENARIOS if s.name == "flat_walk")
        scenario = replace(
            source, name="flat_walk_revision_mid_route",
            events=[Event("revise_forward", lambda c: c.tick >= 14,
                          revise_forward)],
        )
        result = run(scenario)
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.violations, [], result.reason)
        self.assertEqual(result.damage, 0.0)
        revised_at = next(row["tick"] for row in result.trace
                          if row["goal_revision"] == 2)
        moving = [row for row in result.trace
                  if revised_at <= row["tick"]
                  and row["session_state"] not in {"complete", "failed"}
                  and row["position"][2] < 5.8]
        self.assertTrue(moving)
        self.assertTrue(all(row["applied_movement"]["forward"] > 0
                            for row in moving),
                        "safe predecessor prefix gained a neutral frame")

    def test_unselected_successor_cannot_retire_moving_predecessor(self):
        source_scenario = next(s for s in SCENARIOS if s.name == "flat_walk")
        configured = replace(
            source_scenario, name="flat_walk_handoff_loses_arbitration_once",
            events=[Event("revise_forward", lambda c: c.tick >= 14,
                          revise_forward)],
        )
        competitor = None
        injected = False
        old_route_id = None

        def shared_control(context):
            nonlocal competitor, injected, old_route_id
            driver = context.driver
            runtime = driver.runtime
            deadline = context.clock[0] + 500_000_000
            proposals = driver.prepare_proposals(deadline)
            ownership = context.session.diagnostics
            external_id = None
            if not injected and ownership.pending_route_id is not None:
                old_route_id = ownership.incumbent_route_id
                self.assertIsNotNone(old_route_id)
                competitor = runtime.register_ordered_source(
                    "test-route-handoff-competitor"
                )
                intent = ActionIntentV1(
                    ordered_intent_id(competitor, 1), competitor.source_id,
                    competitor.episode_id,
                    runtime.observation.sequence_id,
                    ActionPriorityV0.SAFETY,
                    context.clock[0], deadline,
                    movement=MovementV1(forward=1),
                )
                external_id = intent.intent_id
                proposals += (ControlFrameProposalV1(
                    intents=(OrderedIntentV1(competitor, 1, intent),),
                ),)
                injected = True
            result = runtime.control_frame(
                _task(deadline), BehaviorProfileV0(), deadline,
                proposals=proposals,
            )
            driver.adopt_result(result)
            if competitor is not None:
                runtime.unregister_ordered_source(competitor)
                competitor = None
            if injected and old_route_id is not None:
                ownership = context.session.diagnostics
                self.assertIsNotNone(ownership.incumbent_route_id)
                if ownership.incumbent_route_id == old_route_id:
                    self.assertIsNone(ownership.pending_route_id)
                old_route_id = None
            return () if external_id is None else (external_id,)

        result = run(configured, control_step=shared_control)
        self.assertTrue(injected, "successor candidate never faced arbitration")
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.violations, [], result.reason)

    def test_delayed_first_successor_command_keeps_explicit_owner(self):
        source = next(s for s in SCENARIOS if s.name == "flat_walk")
        configured = replace(
            source, name="flat_walk_delayed_successor",
            events=[Event("revise_forward", lambda c: c.tick >= 14,
                          revise_forward)],
            perturbations=Perturbations(late_ticks=frozenset({15})),
        )
        result = run(configured)
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.violations, [], result.reason)
        delayed = next(row for row in result.trace if row["tick"] == 15)
        handoff = delayed["body_handoff"]
        self.assertEqual(delayed["sample_state"], "lease_exhausted")
        self.assertIsNone(delayed["applied_request"])
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["disposition"], "transferable")
        self.assertEqual(handoff["successor"], delayed["route_id"])
        self.assertTrue(delayed["source_bound"])

    def test_discarded_candidate_keeps_predecessor_for_cancel_or_replace(self):
        source = next(s for s in SCENARIOS if s.name == "flat_walk")
        for interrupt in ("cancel", "replace"):
            with self.subTest(interrupt=interrupt):
                configured = replace(
                    source, name=f"flat_walk_discard_then_{interrupt}",
                    events=[Event("revise_forward", lambda c: c.tick >= 14,
                                  revise_forward)],
                )
                discarded = False

                def shared_control(context):
                    nonlocal discarded
                    driver = context.driver
                    deadline = context.clock[0] + 500_000_000
                    if context.tick < 14 or discarded:
                        driver.tick(BehaviorProfileV0(), deadline)
                        return
                    driver.prepare_proposals(deadline)
                    ownership = context.session.diagnostics
                    if not discarded and ownership.pending_route_id is not None:
                        incumbent_id = ownership.incumbent_route_id
                        self.assertIsNotNone(incumbent_id)
                        driver.discard_prepared()
                        ownership = context.session.diagnostics
                        self.assertIsNone(ownership.pending_route_id)
                        self.assertEqual(
                            ownership.incumbent_route_id,
                            incumbent_id,
                        )
                        discarded = True
                        if interrupt == "cancel":
                            driver.release("discard_then_cancel")
                        else:
                            driver.replace_goal(
                                "goal", 3, context.goal_state,
                                context.clock[0],
                            )
                    else:
                        raise AssertionError("revised route has no candidate")
                    if driver.source is not None:
                        driver.tick(BehaviorProfileV0(), deadline)

                result = run(configured, control_step=shared_control)
                self.assertTrue(discarded)
                self.assertEqual(
                    result.outcome,
                    "cancelled" if interrupt == "cancel" else "success",
                    result.reason,
                )
                self.assertEqual(result.violations, [], result.reason)


if __name__ == "__main__":
    unittest.main()
