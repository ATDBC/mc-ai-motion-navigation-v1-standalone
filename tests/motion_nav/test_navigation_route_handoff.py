"""Formal Runtime checks for route replacement while the body keeps moving."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.action_route_executor import ActionRouteState
from mc2p.motion_nav.route_body_controller import RouteControl
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
    def test_pending_candidate_wait_or_entry_rejection_preserves_formal_incumbent_input(self):
        source = next(s for s in SCENARIOS if s.name == "flat_walk")
        for state in (ActionRouteState.RUNNING, ActionRouteState.FAILED,
                      ActionRouteState.BLOCKED, ActionRouteState.UNSUPPORTED,
                      ActionRouteState.INPUT_LOST, ActionRouteState.NEEDS_REPLAN):
            with self.subTest(state=state):
                injected = False
                incumbent = None
                original = RouteControl.advance

                def advance(control, *args, **kwargs):
                    nonlocal injected, incumbent
                    raw = original(control, *args, **kwargs)
                    if (not injected and context_session is not None
                            and context_session._supervisor.has_pending_route
                            and control is context_session._supervisor.route):
                        incumbent = context_session._supervisor.incumbent_route
                        self.assertIsNotNone(incumbent)
                        injected = True
                        return replace(raw, decision=replace(raw.decision,
                            state=state, submit_input=False,
                            reason_code="candidate_entry_changed"))
                    return raw

                context_session = None
                checked = False

                def control(context):
                    nonlocal context_session, checked
                    context_session = context.session
                    driver = context.driver
                    deadline = context.clock[0] + 500_000_000
                    proposals = driver.prepare_proposals(deadline)
                    if injected and not checked:
                        proposal = driver._prepared_proposal
                        self.assertIs(context.session._supervisor.incumbent_route, incumbent)
                        self.assertEqual(proposal.route_owner_id, incumbent.route.route_id)
                        self.assertNotEqual(proposal.route_decision.movement, MovementV1())
                        self.assertEqual(context.session._supervisor.has_pending_route,
                                         state is ActionRouteState.RUNNING)
                        checked = True
                    result = driver.runtime.control_frame(_task(deadline), BehaviorProfileV0(),
                        deadline, proposals=proposals)
                    driver.adopt_result(result)

                configured = replace(source, name=f"candidate_entry_{state.value}",
                    events=[Event("revise_forward", lambda c: c.tick >= 14, revise_forward)])
                with patch.object(RouteControl, "advance", advance):
                    result = run(configured, control_step=control)
                self.assertTrue(checked, "candidate never faced body selection")
                self.assertEqual(result.outcome, "success", result.reason)
                self.assertEqual(result.violations, [], result.reason)

    def test_ready_probe_first_route_input_loses_actual_runtime_arbitration(self):
        source = next(s for s in SCENARIOS if s.name == "direct_drop_2")
        injected = False

        def shared_control(context):
            nonlocal injected
            driver, runtime = context.driver, context.driver.runtime
            deadline = context.clock[0] + 500_000_000
            proposals = driver.prepare_proposals(deadline)
            probe = context.session._edge_probe
            decision = driver._prepared_proposal.route_decision
            competitor = None
            external_id = None
            if (not injected and probe is not None and probe.ready
                    and decision is not None and decision.submit_input
                    and decision.movement != MovementV1()):
                competitor = runtime.register_ordered_source("probe-first-input-competitor")
                context.clock[0] += 1
                intent = ActionIntentV1(ordered_intent_id(competitor, 1),
                    competitor.source_id, competitor.episode_id,
                    runtime.observation.sequence_id, ActionPriorityV0.SAFETY,
                    context.clock[0], deadline, movement=MovementV1())
                external_id = intent.intent_id
                proposals += (ControlFrameProposalV1(
                    intents=(OrderedIntentV1(competitor, 1, intent),)),)
            result = runtime.control_frame(_task(deadline), BehaviorProfileV0(),
                                           deadline, proposals=proposals)
            driver.adopt_result(result)
            if competitor is not None:
                runtime.unregister_ordered_source(competitor)
                self.assertIn(("movement", external_id), result.decision.selected_intents)
                self.assertIs(context.session._edge_probe, probe)
                self.assertTrue(probe.owned)
                self.assertIsNotNone(context.session._information.completed_grant)
                injected = True
            return () if external_id is None else (external_id,)

        result = run(replace(source, name="ready_probe_first_route_input_unselected"),
                     control_step=shared_control)
        self.assertTrue(injected)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.outcome, "success", result.reason)

    def test_ready_probe_waits_for_selected_route_and_keeps_late_input_owner(self):
        source = next(s for s in SCENARIOS if s.name == "direct_drop_2")
        for late in (False, True):
            with self.subTest(late=late):
                rejected = False
                transferred = False

                def shared_control(context):
                    nonlocal rejected, transferred
                    driver = context.driver
                    runtime = driver.runtime
                    deadline = context.clock[0] + 500_000_000
                    proposals = driver.prepare_proposals(deadline)
                    probe = context.session._edge_probe
                    decision = driver._prepared_proposal.route_decision
                    if (probe is not None and probe.ready and not rejected
                            and decision is not None and decision.submit_input
                            and decision.movement != MovementV1()):
                        # Preparing or discarding the first route proposal is
                        # not Runtime selection and cannot retire the probe.
                        driver.discard_prepared()
                        self.assertIs(context.session._edge_probe, probe)
                        self.assertTrue(probe.owned)
                        rejected = True
                        proposals = driver.prepare_proposals(deadline)
                        if late:
                            context.backend.perturbations.late_ticks = frozenset({
                                context.backend.movement_tick + 1})
                    result = runtime.control_frame(_task(deadline), BehaviorProfileV0(),
                                                   deadline, proposals=proposals)
                    driver.adopt_result(result)
                    handoff = context.session._supervisor.last_handoff
                    if (rejected and not transferred and handoff is not None
                            and handoff.disposition.value == "transferable"
                            and probe is not None):
                        self.assertNotEqual(handoff.movement, MovementV1())
                        self.assertIsNone(context.session._edge_probe)
                        self.assertIsNotNone(driver.source)
                        if late:
                            self.assertEqual(context.backend.sample_states[-1], "lease_exhausted")
                            self.assertIsNone(context.backend.applied_commands[-1][1])
                        transferred = True

                result = run(replace(source, name=f"ready_probe_first_input_late_{late}"),
                             control_step=shared_control)
                self.assertTrue(rejected, "READY probe boundary was not exercised")
                self.assertTrue(transferred, "selected route did not receive probe responsibility")
                self.assertEqual(result.violations, [])
                self.assertEqual(result.outcome, "success", result.reason)

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
