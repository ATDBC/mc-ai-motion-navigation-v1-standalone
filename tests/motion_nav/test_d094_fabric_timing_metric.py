"""D094 fixture distinguishes declared deadlines from ordinary input delay."""
from dataclasses import replace
from types import SimpleNamespace
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from scripts.action_entry_late_hardening import scenario_for
from scripts import f2_ground_route_runtime as fixture
from scripts.f2_ground_route_runtime import _entry_late_walk_gate
from scripts.r28_product_fabric_runtime import _rotate
from tests.sim.backend import Scene
from tests.sim.runner import run


class D094FabricTimingMetricTests(unittest.TestCase):
    def test_gate_selects_before_complete_entry_position(self):
        from mc2p.motion_nav.action_route import WalkSegment
        from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
        from mc2p.motion_nav.ground_motion import PlanarBodyState
        from mc2p.motion_nav.movement_transition import MovementMode
        from mc2p.motion_nav.segment_entry import SegmentEntryWindow
        from tests.motion_nav.test_fixed_route_walk import FlatFixture
        frame = FlatFixture().frame(1, PlanarBodyState(.5, 1.9, 0., .6, 0.))
        window = SegmentEntryWindow((.5, 1., 2.5), (0., 1.), -.16, .16, .16,
            .99, 1.01, 0., .1, math.pi, frozenset({'standing'}),
            frozenset({MovementMode.WALK}), 0., .035, 'entry-test')
        action = WalkSegment(FixedRoute('gate',
            (RoutePoint(.5, 1., .5), RoutePoint(.5, 1., 2.5))), ((0, 0),), ())
        session = SimpleNamespace(_active_route=SimpleNamespace(action_route=SimpleNamespace(
            actions=(action, SimpleNamespace(entry_window=window)))),
            diagnostics=SimpleNamespace(action_index=0))
        proposals = (SimpleNamespace(intents=(SimpleNamespace(intent=SimpleNamespace(
            movement=MovementV1(forward=1))),)),)
        self.assertIsNotNone(_entry_late_walk_gate(session, frame, proposals))

    def test_declared_timing_belongs_to_winning_movement_only(self):
        envelope = SimpleNamespace(intent=SimpleNamespace(intent_id='nav-move',
            movement=MovementV1(forward=1)))
        proposal = SimpleNamespace(control_frame=SimpleNamespace(intents=(envelope,)),
            route_decision=SimpleNamespace(expected_movement_tick=5,
                verified_command_index=None))
        result = SimpleNamespace(selected_intents=(('movement', 'nav-move'),))
        self.assertTrue(fixture._selected_input_has_declared_timing(proposal, result))
        proposal.route_decision.expected_movement_tick = None
        proposal.route_decision.verified_command_index = 0
        self.assertTrue(fixture._selected_input_has_declared_timing(proposal, result))
        result.selected_intents = (('movement', 'combat-move'),)
        self.assertFalse(fixture._selected_input_has_declared_timing(proposal, result))
        result.selected_intents = (('movement', 'nav-move'),)
        proposal.route_decision.verified_command_index = None
        self.assertFalse(fixture._selected_input_has_declared_timing(proposal, result))

    @staticmethod
    def frame(*, explicit=False, tick=12, requested=11, latest=11):
        return dict(request_sequence=7, requested_first_tick=requested,
            actual_application_ticks=[tick], latest_allowed_first_tick=latest,
            input_status='applied_outside_window', explicit_input_timing=explicit,
            tick=tick, position=(.5, 1., .5), on_ground=True,
            formal_goal_status='satisfied')

    def test_ordinary_delay_reported_and_raw_outside_status_preserved(self):
        frame = self.frame()
        evidence = fixture._input_timing_evidence([frame], task_success=True)
        self.assertEqual(evidence['input_deadline_miss_count'], 0)
        self.assertEqual(evidence['raw_applied_outside_window_count'], 1)
        self.assertEqual(evidence['unwindowed_input_delay_count'], 1)
        delay = evidence['unwindowed_input_delays'][0]
        self.assertEqual(delay['requested_first_tick'], 11)
        self.assertEqual(delay['actual_application_ticks'], [12])
        self.assertEqual(delay['delay_ticks'], 1)
        self.assertTrue(delay['task_success'])
        self.assertEqual(delay['input_status'], 'applied_outside_window')
        self.assertEqual(frame['input_status'], 'applied_outside_window')

    def test_extra_strict_lateness_and_startup_window_still_fail(self):
        frames = [self.frame(), self.frame(explicit=True),
                  {**self.frame(explicit=True, tick=13, latest=12), 'request_sequence': 8}]
        evidence = fixture._input_timing_evidence(frames, task_success=False)
        self.assertEqual(evidence['input_deadline_miss_count'], 2)
        self.assertEqual(evidence['raw_applied_outside_window_count'], 3)
        self.assertEqual(evidence['unwindowed_input_delay_count'], 1)
        self.assertEqual(evidence['violations'], ['input_deadline_miss'])
        evidence = fixture._input_timing_evidence(frames,
            task_success=False, expected_late_sequence=7)
        self.assertEqual(evidence['input_deadline_miss_count'], 2)
        self.assertEqual(evidence['expected_injected_explicit_miss_count'], 1)
        self.assertEqual(evidence['unexpected_explicit_deadline_miss_count'], 1)
        self.assertEqual(evidence['violations'], ['input_deadline_miss'])
        allowed = self.frame(explicit=True, tick=12, latest=12)
        allowed['input_status'] = 'applied'
        evidence = fixture._input_timing_evidence([allowed], task_success=True)
        self.assertEqual(evidence['input_deadline_miss_count'], 0)
        self.assertEqual(evidence['unwindowed_input_delay_count'], 0)

    def test_four_cardinal_formal_routes_select_one_ordinary_grounded_frame(self):
        original_prepare = RuntimeNavigationDriver.prepare_proposals
        for direction in range(4):
            with self.subTest(direction=direction):
                base = scenario_for('column_outer_turn', None)
                solids = {}
                for (x, y, z), material in base.scene.solids.items():
                    px, pz = _rotate(x + .5, z + .5, direction)
                    solids[(math.floor(px), y, math.floor(pz))] = material
                sx, sz = _rotate(base.start[0], base.start[2], direction)
                gx, gz = _rotate(base.goal[0], base.goal[2], direction)
                scenario = replace(base, name=f'D094-gate-{direction}',
                    scene=Scene(solids, ((-16, 16), (60, 70), (-16, 16))),
                    start=(sx, base.start[1], sz), goal=(gx, base.goal[1], gz),
                    yaw_degrees=base.yaw_degrees - 90 * direction)
                selected = []
                repeated_gate_count = []

                def inspect(driver, *args, **kwargs):
                    proposals = original_prepare(driver, *args, **kwargs)
                    frame = driver.runtime.navigation_observation_adapter.latest_frame
                    decision = driver._prepared_proposal.route_decision
                    gate = _entry_late_walk_gate(driver.session, frame, proposals,
                        route_decision=decision)
                    if gate is not None:
                        repeated_gate_count.append(gate)
                    late_record = None if not selected else selected[0]
                    inject = fixture._should_inject_late_input('late_entry',
                        late_record, gate is not None)
                    if inject:
                        selected.append(gate)
                        self.assertTrue(frame.body.is_on_ground)
                        self.assertIsNone(decision.expected_movement_tick)
                        self.assertIsNone(decision.verified_command_index)
                        self.assertIsNone(_entry_late_walk_gate(driver.session,
                            replace(frame, body=replace(frame.body, is_on_ground=False)),
                            proposals, route_decision=decision))
                        self.assertIsNone(_entry_late_walk_gate(driver.session,
                            frame, proposals, route_decision=replace(decision,
                                expected_movement_tick=frame.body.movement_tick_id + 1)))
                    return proposals

                with patch.object(RuntimeNavigationDriver, 'prepare_proposals', inspect):
                    result = run(scenario)
                self.assertEqual(result.outcome, 'success', result.reason)
                self.assertEqual(len(selected), 1)
                self.assertGreater(len(repeated_gate_count), 1)
                self.assertFalse(fixture._should_inject_late_input('late_entry',
                    selected[0], True))
                self.assertGreaterEqual(selected[0]['distance_before_entry_blocks'], 0.)
                self.assertLessEqual(selected[0]['distance_before_entry_blocks'], .8)

    def test_landing_matrix_is_four_directions_with_terminal_landing_cell_goal(self):
        rows = fixture.frozen_plan(d094_landing_late=True)
        self.assertEqual(len(rows), 4)
        self.assertEqual([row['direction'] for row in rows], list(range(4)))
        self.assertEqual(fixture.digest(rows),
            '2ea31c3eabaacddb85c07e2b7d1b3a0e1a5db786bbd45a199d15d057dd112f83')
        for row in rows:
            solids, start, target = fixture.fixture(row)
            self.assertEqual(row['condition'], 'late_air')
            self.assertEqual(row['d094_source_family'], 'column_landing_turn')
            gx, gz = _rotate(3.5, 9.5, row['direction'])
            self.assertEqual(target, (gx, 101., gz))
            self.assertEqual(row['start_position'], list(start))
            self.assertEqual(row['scene_sha256'], fixture.digest(
                sorted((list(p), material) for p, material in solids.items())))

    def test_declared_final_air_command_is_injected_once_and_landing_walk_never_rejumps(self):
        from mc2p.contracts.behavior import BehaviorProfileV0
        from mc2p.motion_nav.action_route_executor import ActionRouteState
        from mc2p.motion_nav.body_control import StopCause
        for row in fixture.frozen_plan(d094_landing_late=True):
            with self.subTest(direction=row['direction']):
                solids, start, target = fixture.fixture(row)
                scenario = replace(scenario_for('column_landing_turn', None),
                    name=row['id'], scene=Scene(solids, ((-16, 16), (96, 106), (-16, 16))),
                    start=start, goal=target, yaw_degrees=-90 * row['direction'])
                injections = []
                repeated_gate_count = []
                recovery_decisions = []
                actual_lates = []

                def step(context):
                    deadline = context.clock[0] + 500_000_000
                    driver = context.driver
                    proposals = driver.prepare_proposals(deadline)
                    frame = driver.runtime.navigation_observation_adapter.latest_frame
                    prepared = driver._prepared_proposal
                    if (prepared.route_decision is not None
                            and prepared.route_decision.state is ActionRouteState.NEEDS_REPLAN):
                        recovery_decisions.append(prepared.route_decision)
                    gate = fixture._landing_late_air_gate(driver.session, frame,
                        prepared.route_decision)
                    if gate is not None:
                        repeated_gate_count.append(gate)
                        self.assertIsNone(fixture._landing_late_air_gate(driver.session,
                            replace(frame, body=replace(frame.body, is_on_ground=True)),
                            prepared.route_decision))
                    late_record = None if not injections else injections[0]
                    inject = fixture._should_inject_late_input('late_air', late_record, gate is not None)
                    if inject:
                        injections.append(gate)
                        context.backend.perturbations = replace(context.backend.perturbations,
                            late_ticks=frozenset({frame.body.movement_tick_id + 1}))
                    result = driver.runtime.control_frame(driver._task(deadline),
                        BehaviorProfileV0(), deadline, proposals=proposals)
                    if gate is not None:
                        self.assertTrue(fixture._selected_input_has_declared_timing(
                            prepared, result.decision))
                    if inject:
                        record = driver.runtime.input_ledger.record(
                            result.decision.action.request_sequence_id)
                        actual_lates.append(record)
                    driver.adopt_result(result)
                    if actual_lates:
                        updated = driver.runtime.input_ledger.record(
                            actual_lates[0].control_sequence)
                        if updated is not None:
                            actual_lates[0] = updated
                    return ()

                result = run(scenario, control_step=step)
                self.assertEqual(result.outcome, 'success', result.reason)
                self.assertEqual(len(injections), 1)
                self.assertEqual(len(actual_lates), 1)
                # The simulator defers this command. Safety's next neutral
                # input supersedes it before application: input-loss evidence,
                # not evidence that the client applied a late command.
                self.assertEqual(actual_lates[0].status.value, 'superseded')
                self.assertEqual(actual_lates[0].applied_ticks, ())
                self.assertFalse(fixture._should_inject_late_input('late_air', injections[0], True))
                self.assertTrue(recovery_decisions)
                self.assertTrue(all(decision.failure_cause is StopCause.INPUT_LOST
                    for decision in recovery_decisions))
                landing = next(i for i, value in enumerate(result.trace)
                    if value['movement_tick'] > injections[0]['movement_tick']
                    and value['on_ground'])
                self.assertTrue(all(not value['applied_movement']['jump']
                    for value in result.trace[landing:]))
                self.assertTrue(all(value['on_ground'] for value in result.trace[landing:]))
                self.assertEqual(result.damage, 0.)
                self.assertFalse(result.violations)


if __name__ == '__main__':
    unittest.main()
