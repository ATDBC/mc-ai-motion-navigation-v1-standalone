"""One-frame route advancement and pending-body selection boundaries."""
from dataclasses import replace
import math
import inspect
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteExecutor, ActionRouteState,
)
from mc2p.motion_nav.action_route import (
    ActionRoute,
    ControlledDropSegment,
    JumpUpSegment,
)
from mc2p.motion_nav.body_control import HandoffDisposition, StopCause
from mc2p.motion_nav.execution_supervisor import BodyFrameAdvance, ExecutionSupervisor
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.jump_up import JumpUpEdge
from mc2p.motion_nav.landing_edge_probe import (
    LandingEdgeProbe, LandingEdgeProbeState,
)
from mc2p.motion_nav.ground_modes import observed_ground_mode
from mc2p.motion_nav.segment_entry import body_fits_segment_entry
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.motion_risk import (
    RiskReservationResult, RiskReservationStatus, TaskRiskLedger,
)
from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutor
from tests.motion_nav import (
    test_execution_supervisor, test_b07_step_route, test_b10_motion_candidate,
)
from mc2p.motion_nav.route_admission import RouteAdmitter
from tests.motion_nav.test_b07_step_transition import frame as make_frame
from tests.motion_nav.test_navigation_session import _RepeatingRecoveryExecutor, _ground_anchor
from tests.sim.runner import run
from tests.sim.scenarios import SCENARIOS
from mc2p.contracts.behavior import BehaviorProfileV0


class _FrameExecutor(_RepeatingRecoveryExecutor):
    def __init__(self, route, state=ActionRouteState.RUNNING, *, submit=True):
        super().__init__(state, "frame_decision")
        self.route = route
        self.submit = submit
        self.calls = []

    def decide(self, frame, **kwargs):
        self.calls.append(kwargs)
        return ActionRouteDecision(self.state, MovementV1(forward=1),
            LookV1(0, 0), 1, 0, self.reason, (), 0,
            submit_input=self.submit)


class RouteBodyAdvanceTests(unittest.TestCase):
    def _run_direct_drop_acquisition(self, scenario_name):
        strict_entries = []
        probe_frames = []
        context_now = None
        original_enter = ActionRouteExecutor.enter_upcoming_action_boundary

        def control(context):
            nonlocal context_now
            context_now = context
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )
            session = context.session
            probe = session._edge_probe
            control = session._supervisor.route
            if (probe is not None and control is not None
                    and probe.state.value in {
                        "approaching", "holding_edge",
                        "positioning_entry", "releasing",
                    }):
                action = control.route.action_route.actions[
                    probe.action_index
                ]
                probe_frames.append((
                    control.executor.action_index,
                    control.tracker.identity.action_index,
                    control.effective_dependencies,
                    probe.belongs_to_action(
                        control.route.route_id,
                        control.route.route_revision,
                        probe.action_index,
                    ),
                    probe.entry_window is action.entry_window,
                ))

        def enter(executor, next_index, frame):
            entered = original_enter(executor, next_index, frame)
            if not entered:
                return False
            session = context_now.session
            action = executor.route.actions[next_index]
            mode = observed_ground_mode(frame.body)
            grant = session._completed_acquisition
            active_route = session._active_route
            strict_entries.append((
                frame.body.sequence_id,
                grant is not None
                    and active_route is not None
                    and grant.applies(
                        active_route, next_index, frame,
                    ),
                type(action) is ControlledDropSegment
                    and action.entry_window is not None
                    and mode is not None
                    and body_fits_segment_entry(
                        action.entry_window, frame.body, mode,
                    ),
                session._edge_probe is not None
                    and session._edge_probe.ready,
            ))
            return True

        scenario = next(
            item for item in SCENARIOS
            if item.name == scenario_name
        )

        with patch.object(
            ActionRouteExecutor,
            "enter_upcoming_action_boundary",
            enter,
        ):
            result = run(scenario, control_step=control)

        return result, strict_entries, probe_frames

    def test_direct_drop_acquisition_enters_strict_only_from_typed_entry(self):
        result, strict_entries, probe_frames = (
            self._run_direct_drop_acquisition("direct_drop_5_budget_2")
        )

        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.violations, [])
        self.assertTrue(strict_entries)
        self.assertTrue(strict_entries[0][1], strict_entries[0])
        self.assertTrue(strict_entries[0][2], strict_entries[0])
        self.assertTrue(probe_frames)
        self.assertTrue(all(item[0] == 0 for item in probe_frames))
        self.assertTrue(all(item[1] == 0 for item in probe_frames))
        self.assertTrue(all(
            item[2] == probe_frames[0][2] for item in probe_frames
        ))
        self.assertTrue(all(item[3] and item[4] for item in probe_frames))

    def test_two_block_direct_drop_uses_the_same_typed_entry(self):
        result, strict_entries, probe_frames = (
            self._run_direct_drop_acquisition("direct_drop_2")
        )

        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.violations, [])
        self.assertTrue(strict_entries)
        self.assertTrue(strict_entries[0][1], strict_entries[0])
        self.assertTrue(strict_entries[0][2], strict_entries[0])
        self.assertTrue(probe_frames)

    def test_far_landing_probe_enters_strict_on_its_ready_frame(self):
        result, strict_entries, _ = self._run_direct_drop_acquisition(
            "far_landing_L_walkway",
        )

        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        self.assertEqual(result.violations, [])
        self.assertTrue(strict_entries)
        self.assertTrue(strict_entries[0][1], strict_entries[0])
        self.assertTrue(strict_entries[0][2], strict_entries[0])
        self.assertTrue(strict_entries[0][3], strict_entries[0])
        self.assertNotIn(
            "fixed_route_stalled",
            {row["session_reason"] for row in result.trace},
        )

    def test_probe_transfer_rejects_the_preceding_action_identity(self):
        supervisor, incumbent, _, frame, _, _ = self._routes()
        probe = LandingEdgeProbe(
            "goal", 1, (0, 0, 0), 0,
            acquisition_id="probe-action-one",
            route_id=incumbent.route.route_id,
            route_revision=incumbent.route.route_revision,
            action_index=1,
            state=LandingEdgeProbeState.READY,
        )
        supervisor.probe = probe

        with self.assertRaises(ContractViolation):
            supervisor.transfer_probe_to_route(
                frame,
                route_id=incumbent.route.route_id,
                route_revision=incumbent.route.route_revision,
                action_index=0,
                movement=MovementV1(forward=1),
                control_sequence=1,
                movement_tick_id=frame.body.movement_tick_id,
            )

        self.assertIs(supervisor.probe, probe)
        self.assertTrue(probe.owned)

    def test_ready_probe_stays_owner_when_boundary_staging_defers_once(self):
        original_enter = ActionRouteExecutor.enter_upcoming_action_boundary
        context = None
        deferred = False
        retained = False
        entered = False

        def enter(executor, next_index, frame):
            nonlocal deferred, entered
            probe = context.session._edge_probe
            if not deferred and probe is not None and probe.ready:
                deferred = True
                return False
            result = original_enter(executor, next_index, frame)
            entered |= result
            return result

        def control_step(current):
            nonlocal context, retained
            context = current
            current.driver.tick(
                BehaviorProfileV0(), current.clock[0] + 500_000_000,
            )
            if deferred and not entered:
                probe = current.session._edge_probe
                retained = (
                    probe is not None
                    and probe.ready
                    and current.session._executor.action_index == 0
                    and current.backend.actions[-1].movement == MovementV1()
                )

        scenario = next(
            item for item in SCENARIOS
            if item.name == "far_landing_L_walkway"
        )
        with patch.object(
            ActionRouteExecutor, "enter_upcoming_action_boundary", enter,
        ):
            result = run(scenario, control_step=control_step)

        self.assertTrue(deferred)
        self.assertTrue(retained)
        self.assertTrue(entered)
        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )

    def test_ready_probe_boundary_refusal_is_bounded_by_acquisition_wait(self):
        original_enter = ActionRouteExecutor.enter_upcoming_action_boundary
        context = None
        ready_deferrals = 0

        def enter(executor, next_index, frame):
            nonlocal ready_deferrals
            probe = context.session._edge_probe
            if probe is not None and probe.ready:
                ready_deferrals += 1
                return False
            return original_enter(executor, next_index, frame)

        def control_step(current):
            nonlocal context
            context = current
            current.driver.tick(
                BehaviorProfileV0(), current.clock[0] + 500_000_000,
            )

        base = next(
            item for item in SCENARIOS
            if item.name == "far_landing_L_walkway"
        )
        scenario = replace(base, max_ticks=260, expect="failed")
        with patch.object(
            ActionRouteExecutor, "enter_upcoming_action_boundary", enter,
        ):
            result = run(scenario, control_step=control_step)

        self.assertGreater(ready_deferrals, 1)
        self.assertEqual(
            (result.outcome, result.reason),
            ("failed", "edge_probe_acquisition_timeout"),
        )
        self.assertLess(result.ticks, scenario.max_ticks)
        self.assertEqual(result.violations, [])
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])

    def _run_risk_refusal(self, status, *, airborne=False):
        context_now = None
        refused = None
        advances = []
        reserve = TaskRiskLedger.reserve
        decide = ActionRouteExecutor.decide

        def reject(ledger, *args, **kwargs):
            nonlocal refused
            result = reserve(ledger, *args, **kwargs)
            active_reservation = result.status in {
                RiskReservationStatus.RESERVED, RiskReservationStatus.EXISTING,
            }
            reached_boundary = (not context_now.session._frame.body.is_on_ground
                                if airborne else result.status is RiskReservationStatus.RESERVED)
            if refused is None and active_reservation and reached_boundary:
                control = context_now.session._supervisor.route
                refused = (id(control.executor), context_now.session._frame.body.sequence_id)
                return RiskReservationResult(status, None)
            return result

        def observe(executor, frame, **kwargs):
            caller = inspect.currentframe().f_back
            advances.append((id(executor), frame.body.sequence_id,
                             caller.f_globals.get("__name__"), caller.f_code.co_name))
            return decide(executor, frame, **kwargs)

        def control(context):
            nonlocal context_now
            context_now = context
            context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)

        # Start on the proved drop boundary so this test isolates risk
        # refusal.  The full acquisition chain is covered separately above.
        scenario = replace(
            next(s for s in SCENARIOS
                 if s.name == "direct_drop_5_budget_2"),
            start=(.5, 64.0, 2.5),
        )
        with patch.object(TaskRiskLedger, "reserve", reject), \
                patch.object(ActionRouteExecutor, "decide", observe):
            result = run(scenario, control_step=control)
        self.assertIsNotNone(refused, "formal route never reserved drop risk")
        self.assertEqual(result.outcome, "failed", result.reason)
        self.assertEqual(result.reason, f"risk_{status.value}")
        self.assertEqual(result.violations, [], result.reason)
        self.assertLessEqual(result.damage, scenario.damage_points)
        if not airborne:
            self.assertEqual(result.damage, 0)
        return refused, advances, result

    def test_risk_refusal_never_advances_the_executor_from_session(self):
        refused, advances, result = self._run_risk_refusal(RiskReservationStatus.INSUFFICIENT)
        direct = [row for row in advances
                  if row[2] == "mc2p.motion_nav.navigation_session"]
        self.assertEqual(direct, [], "Session retained a direct body-advancement bypass")

    def test_risk_refusal_protection_uses_one_executor_advance_per_observation(self):
        for status in (RiskReservationStatus.INSUFFICIENT, RiskReservationStatus.STALE_POLICY,
                       RiskReservationStatus.CAPACITY_EXHAUSTED, RiskReservationStatus.RISK_OVERRUN):
            with self.subTest(status=status):
                refused, advances, result = self._run_risk_refusal(status)
                same_frame = [row for row in advances if row[:2] == refused]
                self.assertEqual(len(same_frame), 1,
                    "risk rejection advanced the same body controller twice in one observation")

    def test_formal_risk_refusal_preserves_legacy_stop_timing_inputs_and_body(self):
        def legacy(supervisor, advance, frame, ledger, anchor, *, current_scope):
            raw = advance.route_advance
            decision = raw.control.executor.decide(frame,
                state_anchor=anchor, input_ledger=ledger)
            return BodyFrameAdvance(replace(raw, decision=decision))

        fields = ("tick", "session_state", "session_reason", "source_bound", "position",
                  "velocity", "yaw_radians", "pitch_radians", "applied_movement",
                  "sampled_input", "selected_intents", "body_handoff", "action_index",
                  "input_window", "active_waits", "damage", "damage_spent")
        def signature(result):
            rows = []
            for row in result.trace:
                values = []
                for field in fields:
                    value = row[field]
                    if field == "selected_intents":
                        # Each Runtime has a fresh source token.  Preserve
                        # domain and episode/revision/sequence, not that UUID.
                        value = tuple((domain, intent.split("/")[-3:])
                                      for domain, intent in value)
                    values.append(value)
                rows.append(tuple(values))
            return rows
        for airborne in (False, True):
            with self.subTest(airborne=airborne):
                with patch.object(ExecutionSupervisor, "stop_protection", legacy):
                    _, _, before = self._run_risk_refusal(
                        RiskReservationStatus.INSUFFICIENT, airborne=airborne)
                _, _, after = self._run_risk_refusal(
                    RiskReservationStatus.INSUFFICIENT, airborne=airborne)
                self.assertEqual(signature(after), signature(before))

    def test_stop_protection_does_not_repeat_verified_input_consumption(self):
        fixture = test_b10_motion_candidate.VerifiedMotionRouteIntegrationTests()
        anchor, world, active, executor = fixture.coordinator_fixture()
        prepared = test_b10_motion_candidate.prepare_planned_gap_motion(
            active, 0, anchor, world, candidate_revision=1, intended_start_tick=11)
        frame = fixture.frame(world._world, anchor.physics_state,
                              anchor.observation_sequence_id)
        executor.start(active.action_route, frame, verified_motion=(prepared.candidate,))
        control = RouteControl(active, executor)
        raw = control.advance(frame, InputApplicationLedger(), anchor)
        self.assertTrue(raw.decision.movement.jump)
        action_index = executor.action_index
        control.request_stop(StopCause.CANCELLED)
        with patch.object(VerifiedMotionExecutor, "decide",
                          side_effect=AssertionError("verified controller advanced twice")):
            protected = control.stop_protection(raw, frame, anchor)
        self.assertEqual(protected.decision.movement, MovementV1())
        self.assertIsNone(protected.decision.verified_command_index)
        self.assertEqual(executor.action_index, action_index)
        self.assertIs(executor.state, ActionRouteState.CANCELLING)
        next_anchor = replace(anchor, observation_sequence_id=anchor.observation_sequence_id + 1,
                              movement_tick_id=anchor.movement_tick_id + 1,
                              physics_state=replace(anchor.physics_state,
                                  movement_tick_id=anchor.movement_tick_id + 1))
        next_frame = fixture.frame(world._world, next_anchor.physics_state,
                                   next_anchor.observation_sequence_id)
        stopped = control.advance(next_frame, InputApplicationLedger(), next_anchor)
        self.assertIs(stopped.decision.state, ActionRouteState.CANCELLED)

    def test_airborne_risk_refusal_keeps_verified_landing_without_a_second_advance(self):
        refused, advances, result = self._run_risk_refusal(
            RiskReservationStatus.RISK_OVERRUN, airborne=True)
        self.assertEqual(len([row for row in advances if row[:2] == refused]), 1)
        after_refusal = [row for row in result.trace if row["observation_sequence"] >= refused[1]]
        airborne = [row for row in after_refusal if not row["on_ground"]]
        self.assertTrue(airborne)
        self.assertTrue(all(row["source_bound"] for row in airborne))
        self.assertTrue(result.trace[-1]["on_ground"])

    def _routes(self, incumbent_state=ActionRouteState.RUNNING,
                candidate_state=ActionRouteState.RUNNING, *, candidate_submit=False):
        original, current, anchor = test_execution_supervisor.ExecutionSupervisorTests()._control()
        # These selection tests hand-author frame decisions and therefore do
        # not claim D058 FixedRoute progress proof metadata.
        synthetic = replace(original.route, validation_plan=None)
        incumbent = RouteControl(synthetic,
            _FrameExecutor(synthetic.action_route, incumbent_state))
        candidate_route = replace(synthetic, route_id="candidate")
        candidate = RouteControl(candidate_route,
            _FrameExecutor(candidate_route.action_route, candidate_state,
                           submit=candidate_submit))
        ledger = InputApplicationLedger()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(incumbent, current, ledger, anchor))
        self.assertTrue(supervisor.offer_route(candidate, current, ledger, anchor))
        return supervisor, incumbent, candidate, current, ledger, anchor

    def test_unready_candidate_advances_one_incumbent_frame_without_retiring_it(self):
        supervisor, incumbent, candidate, frame, ledger, anchor = self._routes()
        advance = supervisor.advance_body(frame, ledger, anchor)
        result = supervisor.select_body(advance, advance.route_advance.decision,
                                        frame, ledger, anchor)
        self.assertIs(result.controller, incumbent)
        self.assertEqual(result.kind.value, "incumbent_prefix")
        self.assertIs(supervisor.incumbent_route, incumbent)
        self.assertIs(supervisor.route, candidate)
        self.assertEqual(len(incumbent.executor.calls), 1)
        self.assertEqual(len(candidate.executor.calls), 1)

    def test_each_failed_candidate_returns_typed_fact_and_keeps_predecessor(self):
        for state in (ActionRouteState.FAILED, ActionRouteState.BLOCKED,
                      ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
                      ActionRouteState.NEEDS_REPLAN):
            with self.subTest(state=state):
                supervisor, incumbent, candidate, frame, ledger, anchor = self._routes(
                    candidate_state=state)
                advance = supervisor.advance_body(frame, ledger, anchor)
                self.assertIs(advance.rejected_candidate.control, candidate)
                self.assertIs(advance.rejected_candidate.decision.state, state)
                self.assertIs(supervisor.incumbent_route, incumbent)
                self.assertFalse(supervisor.has_pending_route)

    def test_candidate_rejection_is_routed_before_incumbent_cancel_frame(self):
        supervisor, incumbent, candidate, frame, ledger, anchor = self._routes(
            candidate_state=ActionRouteState.NEEDS_REPLAN)
        rejection = supervisor.advance_body(frame, ledger, anchor)
        self.assertEqual(incumbent.executor.calls, [],
                         "incumbent advanced before Session routed failed recovery")
        # Session's existing retry failure path requests stopping here.
        incumbent.executor.state = ActionRouteState.CANCELLED
        advance = supervisor.continue_rejected_candidate(rejection, frame, ledger, anchor)
        self.assertIs(advance.route_advance.control, incumbent)
        self.assertIs(advance.route_advance.decision.state, ActionRouteState.CANCELLED)
        self.assertEqual(len(incumbent.executor.calls), 1)

    def test_terminal_predecessor_waits_only_with_current_quiescent_evidence(self):
        for moving in (False, True):
            with self.subTest(moving=moving):
                supervisor, incumbent, candidate, frame, ledger, anchor = self._routes(
                    incumbent_state=ActionRouteState.CANCELLED)
                if moving:
                    frame = replace(frame, body=replace(frame.body,
                        velocity_blocks_per_second=(0, 0, 2)))
                    anchor = _ground_anchor(frame)
                advance = supervisor.advance_body(frame, ledger, anchor)
                result = supervisor.select_body(advance, advance.route_advance.decision,
                                                frame, ledger, anchor)
                self.assertEqual(result.kind.value,
                    "incumbent_prefix" if moving else "candidate_wait")
                self.assertIs(result.handoff.disposition,
                    HandoffDisposition.RETAIN if moving else HandoffDisposition.QUIESCENT)
                self.assertIs(supervisor.incumbent_route, incumbent)
                self.assertIs(supervisor.route, candidate)

    def test_ready_candidate_does_not_advance_or_retire_predecessor(self):
        supervisor, incumbent, candidate, frame, ledger, anchor = self._routes(
            candidate_submit=True)
        advance = supervisor.advance_body(frame, ledger, anchor)
        result = supervisor.select_body(advance, advance.route_advance.decision,
                                        frame, ledger, anchor)
        self.assertIs(result.controller, candidate)
        self.assertIs(result.incumbent_route, incumbent)
        self.assertIs(result.pending_route, candidate)
        self.assertIs(supervisor.incumbent_route, incumbent)
        self.assertEqual(incumbent.executor.calls, [])
        supervisor.adopt_route_selection(frame, selected=False)
        self.assertIs(supervisor.route, incumbent)

    def test_terminal_prefix_with_owned_probe_selects_probe_without_releasing_any_owner(self):
        supervisor, incumbent, candidate, frame, ledger, anchor = self._routes(
            incumbent_state=ActionRouteState.CANCELLED)
        probe = LandingEdgeProbe("goal", 1, (0, 0, 0), 0)
        supervisor.probe = probe
        advance = supervisor.advance_body(frame, ledger, anchor)
        result = supervisor.select_body(advance, advance.route_advance.decision,
                                        frame, ledger, anchor)
        self.assertEqual(result.kind.value, "probe_stop")
        self.assertIs(result.controller.probe, probe)
        self.assertTrue(probe.owned)
        self.assertIs(supervisor.incumbent_route, incumbent)
        self.assertIs(supervisor.route, candidate)

    def test_executor_and_coordinator_receive_conditioned_walk_yaw_and_same_frame_facts(self):
        supervisor, incumbent, candidate, frame, ledger, anchor = self._routes()
        for coordinator in (False, True):
            with self.subTest(coordinator=coordinator):
                control = incumbent
                if coordinator:
                    coordinator_owner = object.__new__(
                        MotionRouteCoordinator,
                    )
                    coordinator_owner.route = incumbent.route
                    coordinator_owner.executor = incumbent.executor
                    control = replace(
                        incumbent, coordinator=coordinator_owner,
                    )
                target = MotionRouteCoordinator if coordinator else _FrameExecutor
                expected = math.atan2(math.sin(frame.body.yaw_radians + math.pi / 2),
                                      math.cos(frame.body.yaw_radians + math.pi / 2))
                with patch.object(target, "decide", return_value=incumbent.executor.decide(frame)) as decide:
                    result = control.advance(frame, ledger, anchor,
                        conditioned_yaw_delta_degrees=90, result_poll_sequence=7,
                        allow_grounded_reprepare=False)
                self.assertTrue(result.conditioned_ordinary_walk)
                self.assertAlmostEqual(decide.call_args.kwargs["movement_yaw_radians"], expected)
                if coordinator:
                    self.assertEqual(decide.call_args.kwargs["changed_cells"], frame.changed_cells)
                    self.assertEqual(decide.call_args.kwargs["result_poll_sequence"], 7)
                    self.assertFalse(decide.call_args.kwargs["allow_grounded_reprepare"])

    def test_step_and_air_routes_keep_route_look_under_conditioned_combat_yaw(self):
        original, current, anchor = test_execution_supervisor.ExecutionSupervisorTests()._control()
        world, candidate, request = test_b07_step_route.B07StepRouteTests().candidate()
        initial = make_frame(world, 0, candidate.path[0].position)
        step = RouteAdmitter().admit_surface(candidate, initial,
            expected_request_id=request.request_id, goal_id=request.goal_id,
            goal_revision=request.goal_revision, changed_cells=()).route.action_route
        jump = ActionRoute("air-look", (JumpUpSegment(
            JumpUpEdge((0, 0, 0), (1, 1, 0), "jump", (1, 0), 1, ()), ()),))
        for route in (step, jump):
            with self.subTest(route=route.route_id):
                # This hand-built strict route is not a RouteAdmitter product
                # and must not retain the original Walk revalidation plan.
                control = RouteControl(replace(
                    original.route, action_route=route, validation_plan=None,
                ),
                                       _FrameExecutor(route))
                result = control.advance(current, InputApplicationLedger(), anchor,
                                         conditioned_yaw_delta_degrees=90)
                self.assertFalse(result.conditioned_ordinary_walk)
                self.assertIsNone(control.executor.calls[-1]["movement_yaw_radians"])
                self.assertEqual(result.decision.look, LookV1(0, 0))

    def test_owned_probe_keeps_observed_yaw_even_when_pending_route_is_walk(self):
        supervisor, incumbent, candidate, frame, ledger, anchor = self._routes()
        probe = LandingEdgeProbe("goal", 1, (0, 0, 0), 0)
        supervisor.probe = probe
        advance = supervisor.advance_body(frame, ledger, anchor,
            conditioned_yaw_delta_degrees=90)
        self.assertFalse(advance.route_advance.conditioned_ordinary_walk)
        self.assertIsNone(incumbent.executor.calls[-1]["movement_yaw_radians"])
        self.assertIsNone(candidate.executor.calls[-1]["movement_yaw_radians"])
        self.assertTrue(probe.owned)


if __name__ == "__main__":
    unittest.main()
