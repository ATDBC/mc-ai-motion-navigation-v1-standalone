"""R28 Task E: owner facts and the public Session routing chain."""
from dataclasses import FrozenInstanceError, replace
from collections import Counter
import ast
import inspect
import textwrap
from unittest.mock import patch
import unittest

from mc2p.contracts.observation_v3 import AirQueryResultV3
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.navigation_owners import InformationAcquisitionState
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.navigation_lifecycle import NavigationSessionState
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe, LandingEdgeProbeState
from mc2p.motion_nav.action_route_executor import ActionRouteState
from mc2p.motion_nav.body_control import StopCause
from mc2p.motion_nav.execution_supervisor import BodySelectionKind
from mc2p.motion_nav.planning_coordinator import PlanningUpdateKind
from mc2p.motion_nav.retry_ledger import RetryLedger, WaitPolicy, WaitVerdict
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav import test_navigation_session as session_fixtures
from tests.motion_nav import test_planning_coordinator as planning_fixtures
from tests.motion_nav import test_route_body_advance as route_fixtures
from tests.motion_nav.test_navigation_session import _InlinePlanner
from tests.motion_nav.test_navigation_session import _InlineMotionWorker
from tests.motion_nav.test_planning_coordinator import (
    _DeferredPlanner, _request, _permit, _world,
)
from tests.sim.event_sequences import generate_sequence, run_sequence


class InformationOwnerFactsTests(unittest.TestCase):
    def test_classifies_queries_and_returns_immutable_look_facts(self):
        world = _world()
        cells = ((-1, 2, 0), (0, 2, 0), (1, 2, 0))
        current = replace(frame(world, 1, (-.5, 1., .5)), air_query_results=(
            AirQueryResultV3(cells[0], "outside_view"),
            AirQueryResultV3(cells[1], "occluded"),
            AirQueryResultV3(cells[2], "out_of_range"),
        ))
        owner = InformationAcquisitionState(missing_cells=cells)
        update = owner.advance(current, landing_acquisition_pending=False)
        self.assertEqual(update.statuses, tuple(zip(cells, (
            "outside_view", "occluded", "out_of_range"))))
        self.assertEqual(update.missing_cells, cells)
        self.assertIsNotNone(update.look)
        self.assertFalse(update.start_or_continue_probe)
        with self.assertRaises(FrozenInstanceError):
            update.look = None
        owner.missing_cells = cells[1:]
        later = owner.advance(replace(current, air_query_results=()),
                              landing_acquisition_pending=False)
        self.assertEqual(later.statuses, update.statuses[1:])
        self.assertIsNone(later.look)
        self.assertEqual(update.statuses[0], (cells[0], "outside_view"))

    def test_occluded_landing_requests_probe_without_turning(self):
        cell = (0, 0, 0)
        current = replace(frame(_world(), 1, (-.5, 1., .5)),
                          air_query_results=(AirQueryResultV3(cell, "occluded"),))
        owner = InformationAcquisitionState(missing_cells=(cell,))
        update = owner.advance(current, landing_acquisition_pending=True)
        self.assertEqual(update.lower_required, frozenset({cell}))
        self.assertTrue(update.start_or_continue_probe)
        self.assertIsNone(update.look)
        update = owner.advance(current, landing_acquisition_pending=False)
        self.assertFalse(update.lower_required)

    def test_acquired_facts_use_current_queries_without_borrowing_other_cells(self):
        cell, other = (0, 2, 0), (1, 2, 0)
        owner = InformationAcquisitionState(missing_cells=(cell,))
        current = replace(frame(_world(), 1, (-.5, 1., .5)), air_query_results=(
            AirQueryResultV3(other, "visible_air", 1., True),))
        self.assertFalse(owner.observe(current, (), landing_acquisition_pending=False).acquired_cells)
        current = replace(current, air_query_results=(AirQueryResultV3(cell, "visible_air", 1., True),))
        self.assertEqual(owner.observe(current, (), landing_acquisition_pending=False).acquired_cells, (cell,))

    def test_repeat_query_does_not_restart_information_wait(self):
        cell = (0, 2, 0)
        owner = InformationAcquisitionState(missing_cells=(cell,))
        ledger = RetryLedger("information-test")
        current = frame(_world(), 1, (-.5, 1., .5))
        kwargs = dict(landing_acquisition_pending=False, ledger=ledger,
                      wait_owner_id="information-owner", wait_policy=WaitPolicy(2, 2_000_000_000),
                      now_ns=1_000_000_000)
        self.assertIs(owner.advance(current, **kwargs).wait_verdict, WaitVerdict.WAITING)
        later = replace(current, body=replace(current.body, sequence_id=4))
        update = owner.advance(later, **kwargs)
        self.assertIs(update.wait_verdict, WaitVerdict.EXHAUSTED_TICKS)
        self.assertEqual(owner.wait_frames, 3)

    def test_observed_fact_cannot_end_another_information_owners_wait(self):
        cell = (0, 2, 0)
        current = replace(
            frame(_world(), 1, (-.5, 1., .5)),
            air_query_results=(AirQueryResultV3(cell, "visible_air", 1., True),),
        )
        owner = InformationAcquisitionState(missing_cells=(cell,))
        ledger = RetryLedger("information-owner-check")
        ledger.set_blockers(("cell/0/2/0",))
        ledger.begin_wait(
            "information", "information-owner-a",
            WaitPolicy(40, 2_000_000_000), 1, 1,
        )

        with self.assertRaisesRegex(ContractViolation, "wait owner does not match"):
            owner.observe(
                current, (), landing_acquisition_pending=False,
                ledger=ledger, wait_owner_id="information-owner-b",
            )
        self.assertEqual(
            ledger.active_waits()[0].owner_id, "information-owner-a",
        )


class PlanningDeliveryFactsTests(unittest.TestCase):
    def test_actual_advance_delivers_request_and_missing_cells_for_each_result(self):
        for expected in (PlanningUpdateKind.RUNNING, PlanningUpdateKind.ROUTE_READY,
                         PlanningUpdateKind.NEEDS_INFORMATION, PlanningUpdateKind.FAILED):
            with self.subTest(kind=expected):
                world = _world(unknown=frozenset({(0, 1, 0)})
                               if expected is PlanningUpdateKind.NEEDS_INFORMATION else frozenset())
                request = _request(world)
                fixture = planning_fixtures.PlanningCoordinatorTests()
                coordinator = fixture.coordinator(_DeferredPlanner() if expected is PlanningUpdateKind.RUNNING else None)
                current = frame(world, 0, (-.5, 1., .5))
                coordinator.begin(request, current, permit=_permit(), state_anchor=None,
                                  remaining_damage_budget=request.damage_budget)
                if expected is PlanningUpdateKind.FAILED:
                    current = replace(current, session=type(current.session)("another-world"))
                update = coordinator.advance(current, state_anchor=None, edge_probe=None,
                                             remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
                self.assertIs(update.kind, expected)
                self.assertEqual(update.request.request_id, update.request_id)
                self.assertEqual(update.request.goal_revision, update.goal_revision)
                if expected is PlanningUpdateKind.NEEDS_INFORMATION:
                    self.assertEqual(update.missing_cells, tuple(dict.fromkeys(
                        blocker.position for blocker in update.information_need.blockers))[:64])
                else:
                    self.assertEqual(update.missing_cells, ())

    def test_session_advances_information_owner_before_planning_delivery(self):
        world = _world()
        fixture = session_fixtures.NavigationSessionTests()
        session = NavigationSession("routing-order", fixture.profiles(),
                                    planner_worker=_InlinePlanner(), motion_worker=_InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        self.addCleanup(session.close)
        current = frame(world, 0, (-.5, 1., .5))
        session.start(_request(world), current)
        calls = []
        original = InformationAcquisitionState.advance
        planning = session._planning_coordinator
        original_planning = planning.advance
        def information(owner, *args, **kwargs):
            calls.append("information")
            return original(owner, *args, **kwargs)
        def advance(*args, **kwargs):
            calls.append("planning")
            return original_planning(*args, **kwargs)
        with patch.object(InformationAcquisitionState, "advance", information), patch.object(planning, "advance", advance):
            session.propose(current, None, 1_500_000_000)
        self.assertLess(calls.index("information"), calls.index("planning"))

    def test_session_consumes_delivery_without_request_property_readback(self):
        world = _world()
        current = frame(world, 0, (-.5, 1., .5))
        session = NavigationSession("delivery-only", session_fixtures.NavigationSessionTests().profiles(),
                                    planner_worker=_InlinePlanner(), motion_worker=_InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        self.addCleanup(session.close)
        session.start(_request(world), current)
        owner = session._planning_coordinator
        delivered = []
        class DeliveryOnly:
            def advance(self, *args, **kwargs):
                update = owner.advance(*args, **kwargs)
                delivered.append(update)
                return update
            @property
            def request(self):
                raise AssertionError("Session must consume the delivered request")
            def __getattr__(self, name):
                return getattr(owner, name)
        session._planning_coordinator = DeliveryOnly()
        result = session._advance_planning(current, None, None)
        self.assertIs(result, delivered[0])
        self.assertIs(session._request, result.request)
        self.assertIsNotNone(session.body_route_snapshot)

    def test_propose_is_an_ordered_router_without_query_classification(self):
        node = ast.parse(textwrap.dedent(inspect.getsource(NavigationSession.propose)))
        calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)
                 and isinstance(item.func, ast.Attribute)]
        def first(name):
            return min(item.lineno for item in calls if item.func.attr == name)
        self.assertLess(first("observe"), first("_advance_existing_handoff"))
        self.assertLess(first("_advance_existing_handoff"), first("advance"))
        self.assertLess(first("advance"), first("_advance_planning"))
        self.assertLess(first("_advance_planning"), first("advance_body"))
        self.assertLess(first("advance_body"), first("_route_proposal"))
        self.assertFalse(any(isinstance(item, (ast.For, ast.ListComp, ast.DictComp, ast.SetComp))
                             for item in ast.walk(node)))
        kinds = {item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute)
                 and isinstance(item.value, ast.Name) and item.value.id == "PlanningUpdateKind"}
        self.assertFalse(kinds)


class SameFrameProbeStoppingTests(unittest.TestCase):
    def test_planning_failure_keeps_owned_probe_stop_input_in_the_same_frame(self):
        original = NavigationSession._proposal
        original_probe_movement = NavigationSession._probe_movement
        # R28-4 ends the third planning failure across mixed causes, so the
        # old seeds now terminate before this later probe-stop overlap.  This
        # seed still reaches the same formal overlap without relying on the
        # deleted per-cause retry rounds.
        for seed in (23000,):
            with self.subTest(seed=seed):
                pending = []
                probe_calls = Counter()
                def probe_movement(session, frame):
                    probe_calls[frame.body.sequence_id] += 1
                    return original_probe_movement(session, frame)
                def proposal(session, *args, **kwargs):
                    probe = session._edge_probe
                    stopping = (session._state is NavigationSessionState.STOPPING
                                and session._task_stop_requested()
                                and probe is not None and probe.owned
                                and probe.state is LandingEdgeProbeState.STOPPING)
                    result = original(session, *args, **kwargs)
                    if stopping and not pending:
                        movements = tuple(envelope.intent.movement
                                          for envelope in result.control_frame.intents
                                          if envelope.intent.movement is not None)
                        pending.append((result.report.terminal, movements,
                                        session._frame.body.sequence_id))
                    return result
                sequence = generate_sequence(seed, event_count=6,
                                             scenario="direct_drop_5_budget_2", max_ticks=400)
                with patch.object(NavigationSession, "_proposal", proposal), patch.object(
                        NavigationSession, "_probe_movement", probe_movement):
                    outcome = run_sequence(sequence)
                self.assertIsNone(outcome.exception)
                self.assertIsNotNone(outcome.result)
                self.assertTrue(pending, "must enter same-frame planning-failure/probe-stop path")
                self.assertTrue(all(not terminal for terminal, _, _ in pending),
                                "deferred terminal must wait for the owned probe to release")
                self.assertTrue(all(movements and all(movement.sneak for movement in movements)
                                    for _, movements, _ in pending))
                self.assertTrue(all(probe_calls[sequence_id] <= 1
                                    for _, _, sequence_id in pending))
                tick = next(row for row in outcome.result.trace if row["movement_tick"] == 23)
                self.assertTrue(tick["source_bound"])
                self.assertIn("landing_edge_probe", tick["controller_ids"])
                self.assertTrue(tick["applied_movement"]["sneak"])
                self.assertFalse(outcome.result.violations)

    def test_deferred_terminal_does_not_consume_ending_while_probe_is_owned(self):
        world = _world()
        current = frame(world, 0, (-.5, 1., .5))
        session = NavigationSession("pending-probe-terminal", session_fixtures.NavigationSessionTests().profiles(),
                                    planner_worker=_InlinePlanner(), motion_worker=_InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        self.addCleanup(session.close)
        session.start(_request(world), current)
        session._edge_probe = LandingEdgeProbe(session._request.goal_id, 1, (0, 0, 0), 0)
        session.cancel("cancelled_with_owned_probe")
        self.assertTrue(session._has_body_owner())
        session._finish_pending_probe_terminal("not_released")
        self.assertFalse(session.report.terminal)
        self.assertTrue(session._task_stop_requested())
        self.assertTrue(session._edge_probe.owned)

    def test_supervisor_selects_stopping_probe_without_requiring_terminal_route_or_candidate(self):
        for route_state in (ActionRouteState.RUNNING, ActionRouteState.CANCELLED):
            for pending_route in (False, True):
                with self.subTest(route_state=route_state, pending_route=pending_route):
                    supervisor, incumbent, candidate, current, ledger, anchor = (
                        route_fixtures.RouteBodyAdvanceTests()._routes(incumbent_state=route_state))
                    if not pending_route:
                        supervisor.discard_pending_route()
                    probe = LandingEdgeProbe("goal", 1, (0, 0, 0), 0)
                    probe.request_stop(StopCause.MOTION_UNSOLVABLE)
                    supervisor.probe = probe
                    advance = supervisor.advance_body(current, ledger, anchor)
                    before_calls = (len(incumbent.executor.calls), len(candidate.executor.calls))
                    result = supervisor.select_body(advance, advance.route_advance.decision,
                                                    current, ledger, anchor)
                    self.assertIs(result.kind, BodySelectionKind.PROBE_STOP)
                    self.assertIs(result.controller.probe, probe)
                    self.assertIs(supervisor.incumbent_route, incumbent)
                    self.assertTrue(probe.owned)
                    self.assertIs(probe.stop_cause, StopCause.MOTION_UNSOLVABLE)
                    self.assertEqual(before_calls, (len(incumbent.executor.calls),
                                                   len(candidate.executor.calls)))
