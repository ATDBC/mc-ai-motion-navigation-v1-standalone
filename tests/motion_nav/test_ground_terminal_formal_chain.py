from __future__ import annotations

from dataclasses import replace
import os
import time
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route import ActionRoute, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion, GroundRouteExecutionContract
from mc2p.motion_nav.ground_terminal_search import GroundTerminalSearchStatus
from mc2p.motion_nav.ground_terminal_search import GroundTerminalSearchResult
from mc2p.motion_nav.ground_terminal_search import solve_ground_terminal_sequence
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, MotionTickPhase,
)
from mc2p.motion_nav.motion_coordination import route_needs_motion_coordination
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveResult, GroundTerminalSolveJob, GroundTerminalSolveResult,
    MotionResultInbox, MotionSolverWorker, _execute_job,
)
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.retry_ledger import RetryLedger
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_ground_terminal_search import _request


class GroundTerminalFormalChainContractTests(unittest.TestCase):
    @staticmethod
    def request():
        return _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))

    def test_worker_returns_typed_ground_result_without_air_wrapper(self):
        request = self.request()
        job = GroundTerminalSolveJob(
            "ground-terminal/0", 1, request,
        )

        result = _execute_job(job)

        self.assertIs(type(result), GroundTerminalSolveResult)
        self.assertIs(result.search_result.status, GroundTerminalSearchStatus.SOLVED)
        self.assertEqual(result.work_identity, request.work_identity)

    def test_worker_exception_is_typed_internal_error(self):
        request = self.request()
        job = GroundTerminalSolveJob("ground-terminal/0", 1, request)

        with patch(
            "mc2p.motion_nav.motion_worker.solve_ground_terminal_sequence",
            side_effect=RuntimeError("boom"),
        ):
            result = _execute_job(job)

        self.assertIs(type(result), GroundTerminalSolveResult)
        self.assertIs(result.search_result.status, GroundTerminalSearchStatus.INTERNAL_ERROR)
        self.assertEqual(result.search_result.reasons, ("RuntimeError",))

    def test_real_worker_records_a_process_distinct_from_control(self):
        request = self.request()
        job = GroundTerminalSolveJob("ground-terminal/real-worker", 1, request)

        with MotionSolverWorker(max_pending=1) as worker:
            self.assertTrue(worker.submit(job))
            deadline = time.monotonic() + 10.0
            available = ()
            while time.monotonic() < deadline and not available:
                available = worker.poll_available()
                if not available:
                    time.sleep(.01)

            self.assertEqual(len(available), 1)
            result = available[0]
            self.assertIs(type(result), GroundTerminalSolveResult)
            self.assertEqual(result.executor_pid, worker.pid)
            self.assertNotEqual(result.executor_pid, os.getpid())

    def test_shared_inbox_keeps_air_and_ground_results_separate(self):
        ground_request = self.request()
        ground_result = _execute_job(GroundTerminalSolveJob(
            "ground-terminal/0", 1, ground_request,
        ))
        other_identity = AsyncWorkIdentity(
            AsyncComputationScope(
                ground_request.work_identity.world_session_id,
                ground_request.work_identity.task_id,
                ground_request.work_identity.scope.generation,
            ),
            "another-owner", AsyncWorkKind.MOTION_SOLVE, "air/0", 1,
        )
        # The constructor above deliberately proves the air result rejects a
        # ground payload instead of allowing the two result domains to blur.
        with self.assertRaises(Exception):
            GapMotionSolveResult(
                "air/0", 1,
                replace(ground_result.search_result,
                        status=GroundTerminalSearchStatus.INTERNAL_ERROR),
                0, other_identity,
            )

        class Worker:
            def __init__(self):
                self.values = [ground_result]
            def submit(self, job): return True
            def poll_available(self):
                values, self.values = tuple(self.values), []
                return values
            def close(self): pass
            def is_alive(self): return True

        inbox = MotionResultInbox(max_results=2)
        self.assertTrue(inbox.register(ground_request.work_identity))
        inbox.drain_once(Worker(), 1)
        self.assertEqual(inbox.take(ground_request.work_identity), (ground_result,))

    def test_only_final_walk_with_completion_needs_motion_coordination(self):
        request = self.request()
        completion = request.completion
        fixed = FixedRoute(
            "walk", (RoutePoint(0.5, 64.0, 0.5),
                     RoutePoint(*completion.reference_point)),
            GroundRouteExecutionContract.for_completion(
                completion, completion.dependencies, request.profile.profile_id,
            ),
        )
        node = SurfaceNodeId(0, 0, 64, 0)
        active = ActiveRoute(
            "route", 1, "request", "goal", 1,
            request.anchor.session.value, fixed, 1.0, 0.0, (),
            ExecutableCorridor((node,), (), 1.0, node),
            ActionRoute("route", (WalkSegment(fixed, ((0, 64, 0),), ()),)),
            planning_generation=1,
        )
        plain_fixed = FixedRoute(
            "plain", (RoutePoint(0.5, 64.0, 0.5), RoutePoint(1.5, 64.0, 0.5)),
        )
        plain = replace(
            active, fixed_route=plain_fixed,
            action_route=ActionRoute(
                "plain-route", (WalkSegment(plain_fixed, ((0, 64, 0),), ()),),
            ),
        )

        self.assertTrue(route_needs_motion_coordination(active))
        self.assertFalse(route_needs_motion_coordination(plain))

    def controller_fixture(self):
        request = self.request()
        start = request.anchor.physics_state.position
        completion = request.completion
        fixed = FixedRoute(
            request.route_id,
            (RoutePoint(*start), RoutePoint(*completion.reference_point)),
            GroundRouteExecutionContract.for_completion(
                completion, completion.dependencies, request.profile.profile_id,
            ),
        )
        controller = FixedRouteController(request.profile)
        controller.start(fixed, request.frame)
        return request, controller

    def coordinator_fixture(self):
        request = self.request()
        start = request.anchor.physics_state.position
        completion = request.completion
        fixed = FixedRoute(
            request.route_id,
            (RoutePoint(*start), RoutePoint(*completion.reference_point)),
            GroundRouteExecutionContract.for_completion(
                completion, completion.dependencies, request.profile.profile_id,
            ),
        )
        node = SurfaceNodeId(0, 0, 64, 0)
        active = ActiveRoute(
            request.route_id, 1, "request", request.goal_id, 1,
            request.anchor.session.value, fixed, .25, 0.0, (),
            ExecutableCorridor((node,), (), .25, node),
            ActionRoute(
                request.route_id,
                (WalkSegment(fixed, ((0, 64, 0),), ()),),
            ),
            planning_generation=1,
        )
        executor = ActionRouteExecutor(
            request.profile, jump_profile(), step_profile(),
        )

        class RecordingWorker:
            def __init__(self): self.jobs = []
            def submit(self, job): self.jobs.append(job); return True
            def poll_available(self): return ()
            def close(self): pass
            def is_alive(self): return True

        worker = RecordingWorker()
        retry = RetryLedger(request.goal_id)
        coordinator = MotionRouteCoordinator(
            active, executor, worker,
            retry_ledger=retry,
            computation_scope=AsyncComputationScope(
                active.world_session, retry.task_id, 1,
            ),
        )
        coordinator.start(request.frame)
        return request, active, executor, worker, coordinator

    def test_fixed_route_installs_sequence_and_publishes_verified_window(self):
        request, controller = self.controller_fixture()
        solved = solve_ground_terminal_sequence(request)
        self.assertIs(solved.status, GroundTerminalSearchStatus.SOLVED)
        self.assertTrue(controller.begin_ground_terminal_solve(request))
        self.assertTrue(controller.install_ground_terminal_sequence(
            solved.sequence, request.anchor,
        ))

        decision = controller.decide(
            request.frame, physics_state=request.anchor.physics_state,
            state_anchor=request.anchor, input_ledger=InputApplicationLedger(),
        )

        self.assertIs(decision.state, FixedRouteState.RUNNING)
        self.assertEqual(decision.verified_command_index, 0)
        self.assertEqual(decision.expected_movement_tick, 21)
        self.assertEqual(decision.latest_movement_tick, 21)

    def test_stale_sequence_identity_anchor_and_dependency_are_rejected(self):
        request, controller = self.controller_fixture()
        sequence = solve_ground_terminal_sequence(request).sequence
        self.assertIsNotNone(sequence)
        self.assertTrue(controller.begin_ground_terminal_solve(request))
        other_goal = solve_ground_terminal_sequence(replace(
            request, goal_revision=2,
        )).sequence
        other_route = solve_ground_terminal_sequence(replace(
            request, route_revision=2,
        )).sequence
        self.assertFalse(controller.install_ground_terminal_sequence(
            other_goal, request.anchor,
        ))
        self.assertFalse(controller.install_ground_terminal_sequence(
            other_route, request.anchor,
        ))
        self.assertFalse(controller.install_ground_terminal_sequence(
            sequence, replace(request.anchor, movement_tick_id=22),
        ))

        self.assertTrue(controller.install_ground_terminal_sequence(
            sequence, request.anchor,
        ))
        changed = replace(request.frame, changed_cells=(sequence.dependencies[0],))
        decision = controller.decide(
            changed, physics_state=request.anchor.physics_state,
            state_anchor=request.anchor, input_ledger=InputApplicationLedger(),
        )
        self.assertIsNone(decision.verified_command_index)
        self.assertEqual(decision.movement.forward, 0)
        self.assertEqual(decision.movement.strafe, 0)

    def test_install_rejects_changed_anchor_phase_projection_and_basis_identity(self):
        request, controller = self.controller_fixture()
        sequence = solve_ground_terminal_sequence(request).sequence
        self.assertIsNotNone(sequence)
        self.assertTrue(controller.begin_ground_terminal_solve(request))

        changed_phase_request = replace(
            request,
            anchor=replace(
                request.anchor,
                phase=MotionTickPhase.BEFORE_INPUT_SAMPLE,
            ),
        )
        changed_phase_sequence = solve_ground_terminal_sequence(
            changed_phase_request,
        ).sequence
        self.assertIsNotNone(changed_phase_sequence)
        self.assertFalse(controller.install_ground_terminal_sequence(
            changed_phase_sequence, request.anchor,
        ))
        self.assertFalse(controller.install_ground_terminal_sequence(
            sequence,
            replace(request.anchor, input_projection_version="changed-projection"),
        ))
        self.assertFalse(controller.install_ground_terminal_sequence(
            sequence,
            replace(request.anchor, phase=MotionTickPhase.BEFORE_INPUT_SAMPLE),
        ))

    def test_execution_retires_when_anchor_phase_or_projection_changes(self):
        for field, value in (
            ("phase", MotionTickPhase.BEFORE_INPUT_SAMPLE),
            ("input_projection_version", "changed-projection"),
        ):
            with self.subTest(field=field):
                request, controller = self.controller_fixture()
                sequence = solve_ground_terminal_sequence(request).sequence
                self.assertIsNotNone(sequence)
                self.assertTrue(controller.begin_ground_terminal_solve(request))
                self.assertTrue(controller.install_ground_terminal_sequence(
                    sequence, request.anchor,
                ))
                changed_anchor = replace(request.anchor, **{field: value})

                decision = controller.decide(
                    request.frame,
                    physics_state=changed_anchor.physics_state,
                    state_anchor=changed_anchor,
                    input_ledger=InputApplicationLedger(),
                )

                self.assertIsNone(decision.verified_command_index)
                self.assertNotEqual(
                    decision.reason,
                    "submit_verified_ground_terminal_command",
                )

    def test_deviation_never_submits_unproved_neutral_from_current_state(self):
        request, controller = self.controller_fixture()
        sequence = solve_ground_terminal_sequence(request).sequence
        self.assertIsNotNone(sequence)
        self.assertTrue(controller.begin_ground_terminal_solve(request))
        self.assertTrue(controller.install_ground_terminal_sequence(
            sequence, request.anchor,
        ))
        moving_state = replace(
            request.anchor.physics_state,
            velocity_blocks_per_tick=(.08, 0.0, 0.0),
        )
        moving_anchor = replace(request.anchor, physics_state=moving_state)
        changed = replace(
            request.frame,
            changed_cells=(sequence.dependencies[0],),
        )

        with patch(
            "mc2p.motion_nav.fixed_route.verified_ground_rollout",
            return_value=None,
        ) as neutral_proof, patch(
            "mc2p.motion_nav.fixed_route.verified_ground_recovery_movement",
            return_value=None,
        ) as recovery_proof:
            decision = controller.decide(
                changed, physics_state=moving_state,
                state_anchor=moving_anchor,
                input_ledger=InputApplicationLedger(),
            )

        neutral_proof.assert_called()
        recovery_proof.assert_called_once()
        self.assertIs(decision.state, FixedRouteState.INPUT_LOST)
        self.assertFalse(decision.submit_input)
        self.assertEqual(
            decision.reason,
            "ground_terminal_current_state_recovery_unavailable",
        )

    def test_deviation_waits_without_new_input_while_old_submission_is_unresolved(self):
        request, controller = self.controller_fixture()
        sequence = solve_ground_terminal_sequence(request).sequence
        self.assertIsNotNone(sequence)
        self.assertTrue(controller.begin_ground_terminal_solve(request))
        self.assertTrue(controller.install_ground_terminal_sequence(
            sequence, request.anchor,
        ))
        controller.register_ground_terminal_submission(
            0,
            control_sequence=41,
            requested_movement_tick=sequence.execution_window.earliest_start_tick,
        )
        changed = replace(
            request.frame,
            changed_cells=(sequence.dependencies[0],),
        )

        decision = controller.decide(
            changed, physics_state=request.anchor.physics_state,
            state_anchor=request.anchor,
            input_ledger=InputApplicationLedger(),
        )

        self.assertIs(decision.state, FixedRouteState.CANCELLING)
        self.assertFalse(decision.submit_input)
        self.assertEqual(
            decision.reason,
            "ground_terminal_recovery_waiting_for_previous_submission",
        )

    def test_ground_terminal_recovery_has_a_tick_deadline_under_continuous_force(self):
        request, controller = self.controller_fixture()
        sequence = solve_ground_terminal_sequence(request).sequence
        self.assertIsNotNone(sequence)
        self.assertTrue(controller.begin_ground_terminal_solve(request))
        self.assertTrue(controller.install_ground_terminal_sequence(
            sequence, request.anchor,
        ))
        state = replace(
            request.anchor.physics_state,
            velocity_blocks_per_tick=(.08, 0.0, 0.0),
        )
        terminal = None
        with patch(
            "mc2p.motion_nav.fixed_route.verified_ground_rollout",
            side_effect=lambda _frame, current, *_args, **_kwargs: current,
        ), patch(
            "mc2p.motion_nav.fixed_route.verified_ground_recovery_movement",
            return_value=None,
        ):
            for offset in range(100):
                tick = request.anchor.movement_tick_id + offset
                current = replace(state, movement_tick_id=tick)
                anchor = replace(
                    request.anchor,
                    observation_sequence_id=(
                        request.anchor.observation_sequence_id + offset
                    ),
                    movement_tick_id=tick,
                    physics_state=current,
                )
                frame = replace(
                    request.frame,
                    body=replace(
                        request.frame.body,
                        sequence_id=anchor.observation_sequence_id,
                        movement_tick_id=tick,
                        position=current.position,
                        velocity_blocks_per_second=(1.6, 0.0, 0.0),
                    ),
                    changed_cells=(
                        (sequence.dependencies[0],) if offset == 0 else ()
                    ),
                )
                decision = controller.decide(
                    frame, physics_state=current, state_anchor=anchor,
                    input_ledger=InputApplicationLedger(),
                )
                if decision.state in {
                    FixedRouteState.NEEDS_REPLAN, FixedRouteState.INPUT_LOST,
                }:
                    terminal = (offset, decision)
                    break

        self.assertIsNotNone(terminal)
        self.assertLessEqual(terminal[0], len(sequence.normal.prefix_tails[0].trajectory))
        self.assertFalse(controller.ground_terminal_holds_body)

    def test_pending_solve_keeps_fixed_route_as_ground_owner(self):
        request, controller = self.controller_fixture()
        self.assertTrue(controller.begin_ground_terminal_solve(request))

        decision = controller.decide(
            request.frame, physics_state=request.anchor.physics_state,
            state_anchor=request.anchor, input_ledger=InputApplicationLedger(),
        )

        self.assertIs(decision.state, FixedRouteState.RUNNING)
        self.assertTrue(controller.ground_terminal_pending)
        self.assertEqual(decision.reason, "awaiting_ground_terminal_sequence")

    def test_formal_coordinator_submits_typed_job_without_searching_control_thread(self):
        request, active, executor, worker, coordinator = self.coordinator_fixture()

        with patch(
            "mc2p.motion_nav.motion_worker.solve_ground_terminal_sequence",
            side_effect=AssertionError("control thread searched"),
        ):
            submitted = coordinator._submit_ground_terminal(
                request.frame, request.anchor,
            )

        self.assertTrue(submitted)
        self.assertEqual(len(worker.jobs), 1)
        self.assertIs(type(worker.jobs[0]), GroundTerminalSolveJob)
        self.assertTrue(executor.ground_terminal_active)
        self.assertTrue(executor.requires_safe_handoff(request.frame))

    def test_formal_coordinator_accepts_late1_and_rejects_stale_domains(self):
        request, active, executor, worker, coordinator = self.coordinator_fixture()
        self.assertTrue(coordinator._submit_ground_terminal(
            request.frame, request.anchor,
        ))
        job = worker.jobs[-1]
        result = _execute_job(job)
        sequence = result.search_result.sequence
        self.assertIsNotNone(sequence)
        late_state = sequence.late1.entry_state
        late_anchor = replace(
            request.anchor,
            observation_sequence_id=request.anchor.observation_sequence_id + 1,
            movement_tick_id=late_state.movement_tick_id,
            physics_state=late_state,
        )
        world = PhysicsWorldView(request.frame.world, JAVA_1_21_RULESET)

        accepted = coordinator._accept_result(
            result, late_anchor, world, (), InputApplicationLedger(),
            current_scope=coordinator.computation_scope,
        )

        self.assertTrue(accepted)
        self.assertTrue(executor.ground_terminal_active)

        for mutation in ("goal", "route", "anchor", "dependency"):
            request2, active2, executor2, worker2, coordinator2 = self.coordinator_fixture()
            self.assertTrue(coordinator2._submit_ground_terminal(
                request2.frame, request2.anchor,
            ))
            result2 = _execute_job(worker2.jobs[-1])
            sequence2 = result2.search_result.sequence
            self.assertIsNotNone(sequence2)
            late2 = sequence2.late1.entry_state
            anchor2 = replace(
                request2.anchor,
                observation_sequence_id=request2.anchor.observation_sequence_id + 1,
                movement_tick_id=late2.movement_tick_id,
                physics_state=late2,
            )
            changed = ()
            if mutation == "goal":
                coordinator2.route = replace(coordinator2.route, goal_revision=2)
            elif mutation == "route":
                coordinator2.route = replace(coordinator2.route, route_revision=2)
            elif mutation == "anchor":
                moved = replace(
                    anchor2.physics_state,
                    movement_tick_id=anchor2.movement_tick_id + 2,
                )
                anchor2 = replace(
                    anchor2,
                    movement_tick_id=anchor2.movement_tick_id + 2,
                    physics_state=moved,
                )
            else:
                changed = (sequence2.dependencies[0],)
            self.assertFalse(coordinator2._accept_result(
                result2, anchor2,
                PhysicsWorldView(request2.frame.world, JAVA_1_21_RULESET),
                changed, InputApplicationLedger(),
                current_scope=coordinator2.computation_scope,
            ), mutation)

    def test_beam_fallback_uses_eleven_tick_lead_and_keeps_owner(self):
        request, active, executor, worker, coordinator = self.coordinator_fixture()
        self.assertTrue(coordinator._submit_ground_terminal(
            request.frame, request.anchor,
        ))
        first = worker.jobs[-1]
        insufficient = GroundTerminalSolveResult(
            first.connection_id, first.candidate_revision,
            GroundTerminalSearchResult(
                GroundTerminalSearchStatus.INSUFFICIENT_LEAD,
            ),
            0, first.work_identity, os.getpid(),
        )
        world = PhysicsWorldView(request.frame.world, JAVA_1_21_RULESET)
        self.assertFalse(coordinator._accept_result(
            insufficient, request.anchor, world, (), InputApplicationLedger(),
            current_scope=coordinator.computation_scope,
        ))

        decision = coordinator.decide(
            request.frame, request.anchor, InputApplicationLedger(), world,
            changed_cells=(), current_scope=coordinator.computation_scope,
        )

        self.assertEqual(decision.reason_code, "preparing_ground_terminal_beam")
        self.assertTrue(executor.requires_safe_handoff(request.frame))
        second = worker.jobs[-1]
        self.assertIs(type(second), GroundTerminalSolveJob)
        self.assertEqual(len(second.request.preparation_inputs), 10)
        self.assertEqual(
            second.request.execution_window.earliest_start_tick,
            request.anchor.movement_tick_id + 11,
        )
        self.assertEqual(
            second.request.execution_window.latest_start_tick,
            request.anchor.movement_tick_id + 12,
        )

    def test_prepared_sequence_requires_actual_input_ledger_samples(self):
        request, active, executor, worker, coordinator = self.coordinator_fixture()
        self.assertTrue(coordinator._submit_ground_terminal(
            request.frame, request.anchor, preparation_ticks=10,
        ))
        result = _execute_job(worker.jobs[-1])
        self.assertIs(result.search_result.status, GroundTerminalSearchStatus.SOLVED)
        sequence = result.search_result.sequence
        self.assertIsNotNone(sequence)
        prepared_anchor = replace(
            request.anchor,
            movement_tick_id=sequence.normal.entry_state.movement_tick_id,
            observation_sequence_id=request.anchor.observation_sequence_id + 10,
            physics_state=sequence.normal.entry_state,
        )

        self.assertFalse(executor.install_ground_terminal_sequence(
            sequence, prepared_anchor, InputApplicationLedger(),
        ))

    def test_formal_evidence_hash_covers_the_complete_ground_terminal_chain(self):
        from scripts.f2r_piecewise_evidence import (
            PRODUCTION_SOURCES, production_source_sha256,
        )

        required = {
            "mc2p/motion_nav/ground_terminal_search.py",
            "mc2p/motion_nav/motion_worker.py",
            "mc2p/motion_nav/motion_coordination.py",
            "mc2p/motion_nav/fixed_route.py",
            "mc2p/motion_nav/action_route_executor.py",
            "mc2p/motion_nav/navigation_session.py",
        }
        self.assertTrue(required.issubset(set(PRODUCTION_SOURCES)))
        self.assertEqual(len(production_source_sha256()), 64)

    def test_formal_evidence_times_the_runtime_control_interval(self):
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
        from scripts.f2_ground_route_evidence import terminal_controller_evidence

        with patch.object(RuntimeNavigationDriver, "tick", return_value=None):
            with terminal_controller_evidence() as evidence:
                driver = object.__new__(RuntimeNavigationDriver)
                driver.tick(None, None)

        self.assertEqual(len(evidence["runtime_frames"]), 1)
        self.assertGreaterEqual(evidence["runtime_frames"][0]["control_ms"], 0.0)
        frame = evidence["runtime_frames"][0]
        self.assertEqual(frame["control_ms_kind"], "mixed_interval")
        self.assertEqual(
            frame["measurement_boundary"], "direct_intervals_no_subtraction",
        )
        self.assertIn("production_prepare_ms", frame)
        self.assertIn("backend_step_ms", frame)
        self.assertIn("full_frame_ms", frame)

    def test_frame_timing_directly_separates_backend_without_subtraction(self):
        from scripts.f2_ground_route_evidence import _FrameTimingRecorder

        clock = iter((0, 2_000_000, 7_000_000, 8_000_000, 11_000_000))
        timing = _FrameTimingRecorder(lambda: next(clock))
        backend_started = timing.pause_for_backend()
        timing.resume_after_backend(backend_started)
        measured = timing.finish()

        self.assertEqual(measured["production_prepare_ms"], 5.0)
        self.assertEqual(measured["backend_step_ms"], 5.0)
        self.assertEqual(measured["full_frame_ms"], 11.0)


if __name__ == "__main__":
    unittest.main()
