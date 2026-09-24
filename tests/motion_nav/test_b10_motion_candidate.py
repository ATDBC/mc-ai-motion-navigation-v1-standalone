from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.motion_candidate import (
    MotionCandidateAdmitter, MotionCandidateContext, MotionCandidateStatus,
    VerifiedMotionCandidate, VerifiedMotionExecutor, VerifiedMotionExecutorState,
)
from mc2p.motion_nav.motion_solver import SolveResult, SolveStatus, solve_one_cell_gap
from mc2p.motion_nav.motion_coordination import (
    GapPreparationResult, GapPreparationStatus, MotionRouteCoordinator,
    prepare_planned_gap_motion,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveResult, MotionSolverWorker, _execute_job,
)
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion, SupportSurface, SurfaceNodeId,
)
from mc2p.motion_nav.world_model import Aabb, ObservationStamp
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor, RouteAdmitter
from tests.motion_nav.test_b10_gap_solver import fixture
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile
from mc2p.motion_nav.movement_transition import MovementMode


def solved_candidate():
    anchor, world, target, _ = fixture()
    from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion
    from mc2p.motion_nav.online_motion import CandidateExecutionWindow
    request = GapSolveRequest(
        direction=(0, 1),
        landing=LandingRegion(*target),
        execution_window=CandidateExecutionWindow(11, 12),
    )
    result = solve_one_cell_gap(anchor, world, request)
    assert result.status is SolveStatus.SOLVED
    candidate = VerifiedMotionCandidate(
        proof=result.proof,
        context=MotionCandidateContext(
            planning_request_id="request-1",
            planning_generation=4,
            goal_id="goal-1",
            goal_revision=2,
            route_id="route-1",
            route_revision=3,
            action_index=1,
            candidate_revision=5,
            risk_policy_id="no_expected_damage",
            accepted_resource_incomplete_reasons=(
                "server_hunger_clock_not_in_physics_state",
            ),
        ),
    )
    return anchor, candidate


class MotionCandidateAdmissionTests(unittest.TestCase):
    def admit(self, anchor, candidate, **overrides):
        values = dict(
            planning_request_id="request-1",
            planning_generation=4,
            goal_id="goal-1",
            goal_revision=2,
            route_id="route-1",
            route_revision=3,
            action_index=1,
            candidate_revision=5,
            risk_policy_id="no_expected_damage",
            intended_start_tick=11,
            changed_cells=(),
        )
        values.update(overrides)
        return MotionCandidateAdmitter().admit(candidate, anchor, **values)

    def test_binds_reusable_proof_to_current_task_before_admission(self):
        anchor, candidate = solved_candidate()

        result = self.admit(anchor, candidate)

        self.assertIs(result.status, MotionCandidateStatus.ACCEPTED)
        self.assertIs(result.candidate.candidate, candidate)

    def test_goal_change_alone_invalidates_candidate(self):
        anchor, candidate = solved_candidate()

        result = self.admit(anchor, candidate, goal_revision=3)

        self.assertIs(result.status, MotionCandidateStatus.REJECTED)
        self.assertEqual(result.reason, "goal_replaced")

    def test_request_route_revision_window_and_world_changes_are_checked(self):
        anchor, candidate = solved_candidate()
        cases = (
            ({"planning_generation": 5}, "planning_request_replaced"),
            ({"route_revision": 4}, "route_replaced"),
            ({"candidate_revision": 6}, "candidate_replaced"),
            ({"intended_start_tick": 13}, "execution_window_expired"),
            ({"changed_cells": (candidate.proof.world_dependencies[0],)},
             "world_dependency_changed"),
        )
        for values, reason in cases:
            with self.subTest(reason=reason):
                result = self.admit(anchor, candidate, **values)
                self.assertIs(result.status, MotionCandidateStatus.REJECTED)
                self.assertEqual(result.reason, reason)

    def test_entry_state_must_still_fit_the_verified_entry(self):
        anchor, candidate = solved_candidate()
        moved = replace(
            anchor,
            physics_state=replace(
                anchor.physics_state,
                position=(anchor.physics_state.position[0] + .2,
                          anchor.physics_state.position[1],
                          anchor.physics_state.position[2]),
            ),
        )

        result = self.admit(moved, candidate)

        self.assertIs(result.status, MotionCandidateStatus.REJECTED)
        self.assertEqual(result.reason, "entry_state_changed")

    def test_same_body_values_at_a_later_tick_do_not_revive_old_proof(self):
        anchor, candidate = solved_candidate()
        later = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=anchor.movement_tick_id + 1,
            physics_state=replace(
                anchor.physics_state,
                movement_tick_id=anchor.physics_state.movement_tick_id + 1,
            ),
        )

        result = self.admit(later, candidate, intended_start_tick=12)

        self.assertIs(result.status, MotionCandidateStatus.REJECTED)
        self.assertEqual(result.reason, "state_anchor_advanced")


class VerifiedMotionExecutorTests(unittest.TestCase):
    def admitted(self):
        anchor, candidate = solved_candidate()
        admitted = MotionCandidateAdmitter().admit(
            candidate, anchor,
            planning_request_id="request-1", planning_generation=4,
            goal_id="goal-1", goal_revision=2,
            route_id="route-1", route_revision=3,
            action_index=1, candidate_revision=5,
            risk_policy_id="no_expected_damage", intended_start_tick=11,
            changed_cells=(),
        )
        self.assertIs(admitted.status, MotionCandidateStatus.ACCEPTED)
        return anchor, admitted.candidate

    def test_executor_does_not_accept_an_unadmitted_reusable_proof(self):
        _, candidate = solved_candidate()
        with self.assertRaises(ContractViolation):
            VerifiedMotionExecutor().start(candidate)

    @staticmethod
    def applied(ledger, anchor, sequence, tick, movement, *, requested_tick=None,
                requested_latest_tick=None):
        requested_tick = tick if requested_tick is None else requested_tick
        action = ActionSnapshotV1(
            "episode", sequence, anchor.observation_sequence_id, 1_000_000,
            movement=movement, valid_for_ticks=1,
        )
        ledger.submit(
            anchor.session, action,
            requested_first_tick=requested_tick,
            latest_allowed_first_tick=requested_latest_tick,
        )
        ledger.observe_sample(ClientInputApplicationV1(
            "mc2p.input-application.v1", tick, "episode", sequence, 100 + tick,
            "leased", float(movement.forward), float(movement.strafe),
            movement.jump, movement.sneak, movement.sprint,
        ))

    def test_advances_only_after_exact_application_receipt(self):
        anchor, candidate = self.admitted()
        executor = VerifiedMotionExecutor()
        executor.start(candidate)
        ledger = InputApplicationLedger(max_records=64)

        first = executor.decide(anchor, ledger)
        self.assertIs(first.state, VerifiedMotionExecutorState.RUNNING)
        self.assertEqual(first.command_index, 0)
        self.assertEqual(first.movement, candidate.proof.commands[0].movement)
        self.assertTrue(first.submittable_as_verified_command)
        executor.register_submission(0, control_sequence=20,
                                     requested_movement_tick=11)

        waiting = executor.decide(anchor, ledger)
        self.assertIsNone(waiting.movement)
        self.assertEqual(waiting.reason, "awaiting_application")
        self.assertFalse(waiting.submittable_as_verified_command)

        self.applied(ledger, anchor, 20, 11,
                     candidate.proof.commands[0].movement)
        next_anchor = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=11,
            physics_state=candidate.proof.trajectory[1],
        )
        second = executor.decide(next_anchor, ledger)
        self.assertEqual(second.command_index, 1)
        self.assertEqual(second.movement, candidate.proof.commands[1].movement)

    def test_late_application_is_not_treated_as_verified_progress(self):
        anchor, candidate = self.admitted()
        executor = VerifiedMotionExecutor()
        executor.start(candidate)
        ledger = InputApplicationLedger(max_records=64)
        executor.register_submission(0, control_sequence=20,
                                     requested_movement_tick=11)
        self.applied(ledger, anchor, 20, 13,
                     candidate.proof.commands[0].movement, requested_tick=11)

        decision = executor.decide(anchor, ledger)

        self.assertIs(decision.state, VerifiedMotionExecutorState.INPUT_LOST)
        self.assertEqual(decision.reason, "input_applied_outside_window")

    def test_first_command_rebases_to_actual_tick_within_start_window(self):
        anchor, candidate = self.admitted()
        executor = VerifiedMotionExecutor()
        executor.start(candidate)
        ledger = InputApplicationLedger(max_records=64)

        first = executor.decide(anchor, ledger)
        self.assertEqual(first.expected_movement_tick, 11)
        self.assertEqual(first.latest_movement_tick, 12)
        executor.register_submission(
            0, control_sequence=20, requested_movement_tick=11,
            requested_latest_movement_tick=12,
        )
        self.applied(
            ledger, anchor, 20, 12, candidate.proof.commands[0].movement,
            requested_tick=11, requested_latest_tick=12,
        )
        observed = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=12,
            physics_state=replace(
                candidate.proof.trajectory[1], movement_tick_id=12,
            ),
        )

        second = executor.decide(observed, ledger)

        self.assertEqual(second.command_index, 1)
        self.assertEqual(second.expected_movement_tick, 13)
        self.assertEqual(second.latest_movement_tick, 13)

    def test_delayed_first_command_completes_against_its_verified_start_variant(self):
        anchor, candidate = self.admitted()
        delayed = candidate.proof.start_variant(12)
        self.assertIsNotNone(delayed)
        executor = VerifiedMotionExecutor()
        executor.start(candidate)
        ledger = InputApplicationLedger(max_records=64)

        for index, command in enumerate(candidate.proof.commands):
            decision = executor.decide(anchor, ledger)
            self.assertEqual(decision.command_index, index)
            tick = 12 + index
            executor.register_submission(
                index,
                control_sequence=200 + index,
                requested_movement_tick=decision.expected_movement_tick,
                requested_latest_movement_tick=decision.latest_movement_tick,
            )
            self.applied(
                ledger, anchor, 200 + index, tick, command.movement,
                requested_tick=decision.expected_movement_tick,
                requested_latest_tick=decision.latest_movement_tick,
            )
            state = delayed.trajectory[index + 1]
            anchor = replace(
                anchor,
                observation_sequence_id=anchor.observation_sequence_id + 1,
                movement_tick_id=tick,
                physics_state=state,
            )

        completed = executor.decide(anchor, ledger)

        self.assertIs(completed.state, VerifiedMotionExecutorState.COMPLETE)
        self.assertEqual(completed.reason, "verified_motion_complete")
        self.assertFalse(completed.submittable_as_verified_command)

    def test_cancel_in_air_retains_landing_responsibility(self):
        anchor, candidate = self.admitted()
        executor = VerifiedMotionExecutor()
        executor.start(candidate)
        airborne = replace(
            anchor,
            movement_tick_id=12,
            physics_state=replace(candidate.proof.trajectory[2], on_ground=False),
        )

        executor.cancel(airborne)
        recovering = executor.decide(airborne, InputApplicationLedger())
        self.assertIs(recovering.state, VerifiedMotionExecutorState.RECOVERING)
        self.assertEqual(recovering.movement,
                         candidate.proof.commands[0].movement)
        self.assertEqual(recovering.reason,
                         "complete_verified_landing_after_cancel")

        landed = replace(
            airborne,
            movement_tick_id=candidate.proof.exit_state.movement_tick_id,
            physics_state=candidate.proof.exit_state,
        )
        cancelled = executor.decide(landed, InputApplicationLedger())
        self.assertIs(cancelled.state, VerifiedMotionExecutorState.CANCELLED)

    def test_world_dependency_change_stops_ground_execution(self):
        anchor, candidate = self.admitted()
        executor = VerifiedMotionExecutor()
        executor.start(candidate)

        decision = executor.decide(
            anchor, InputApplicationLedger(),
            changed_cells=(candidate.proof.world_dependencies[0],),
        )

        self.assertIs(decision.state, VerifiedMotionExecutorState.FAILED)
        self.assertEqual(decision.reason, "world_dependency_changed_during_execution")
        self.assertEqual(decision.movement, MovementV1())

    def test_world_dependency_change_in_air_retains_landing_responsibility(self):
        anchor, candidate = self.admitted()
        executor = VerifiedMotionExecutor()
        executor.start(candidate)
        airborne = replace(
            anchor,
            movement_tick_id=12,
            physics_state=replace(candidate.proof.trajectory[2], on_ground=False),
        )

        decision = executor.decide(
            airborne, InputApplicationLedger(),
            changed_cells=(candidate.proof.world_dependencies[0],),
        )

        self.assertIs(decision.state, VerifiedMotionExecutorState.RECOVERING)
        self.assertEqual(decision.reason, "world_dependency_changed_retain_landing")
        self.assertFalse(decision.submittable_as_verified_command)

    def test_world_dependency_change_after_air_cancel_discards_verified_remainder(self):
        anchor, candidate = self.admitted()
        executor = VerifiedMotionExecutor()
        executor.start(candidate)
        ledger = InputApplicationLedger()
        airborne = replace(
            anchor,
            movement_tick_id=12,
            physics_state=replace(candidate.proof.trajectory[2], on_ground=False),
        )
        executor.cancel(airborne)
        changed = candidate.proof.world_dependencies[0]

        decision = executor.decide(airborne, ledger, changed_cells=(changed,))

        self.assertEqual(decision.state, VerifiedMotionExecutorState.RECOVERING)
        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason, "world_dependency_changed_retain_landing")
        landed = replace(airborne, physics_state=replace(airborne.physics_state, on_ground=True))
        terminal = executor.decide(landed, ledger)
        self.assertEqual(terminal.state, VerifiedMotionExecutorState.FAILED)
        self.assertEqual(decision.movement, MovementV1())


class VerifiedMotionRouteIntegrationTests(unittest.TestCase):
    @staticmethod
    def frame(world, state, sequence):
        x, y, z = state.position
        stamp = ObservationStamp(
            state.session, sequence, sequence, "test", sequence * 50_000_000,
        )
        body = BodyState(
            state.session, sequence, stamp, state.position,
            tuple(value * 20.0 for value in state.velocity_blocks_per_tick),
            state.yaw_radians, state.pitch_radians, state.pose,
            Aabb(x - state.body_width / 2.0, y, z - state.body_width / 2.0,
                 x + state.body_width / 2.0, y + state.body_height,
                 z + state.body_width / 2.0),
            state.on_ground, state.horizontal_collision,
            state.vertical_collision, is_sprinting=state.sprinting,
            is_sneaking=state.sneaking, food_points=state.food_points,
            saturation_points=state.saturation_points,
        )
        return NavigationFrame(state.session, body, world, "fabric")

    def test_action_route_uses_admitted_proof_and_hands_off_without_extra_frame(self):
        anchor, reusable = solved_candidate()
        reusable = replace(
            reusable, context=replace(reusable.context, action_index=0),
        )
        admitted_result = MotionCandidateAdmitter().admit(
            reusable, anchor,
            planning_request_id="request-1", planning_generation=4,
            goal_id="goal-1", goal_revision=2,
            route_id="route-1", route_revision=3,
            action_index=0, candidate_revision=5,
            risk_policy_id="no_expected_damage", intended_start_tick=11,
            changed_cells=(),
        )
        self.assertIs(admitted_result.status, MotionCandidateStatus.ACCEPTED)
        admitted = admitted_result.candidate
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        edge = JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ())
        route = ActionRoute(
            "route-1",
            (JumpGapSegment(edge, start_surface, end_surface, ()),),
        )
        _, physics_world, _, _ = fixture()
        world = physics_world._world
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        executor.start(
            route, self.frame(world, anchor.physics_state, 1),
            verified_motion=(admitted,),
        )
        ledger = InputApplicationLedger(max_records=64)
        current_anchor = anchor
        final = None
        for index, command in enumerate(admitted.proof.commands):
            decision = executor.decide(
                self.frame(world, current_anchor.physics_state, index + 2),
                state_anchor=current_anchor, input_ledger=ledger,
            )
            self.assertTrue(decision.submit_input)
            self.assertEqual(decision.movement, command.movement)
            tick = admitted.intended_start_tick + index
            executor.register_verified_submission(
                index, control_sequence=100 + index,
                requested_movement_tick=tick,
            )
            waiting = executor.decide(
                self.frame(world, current_anchor.physics_state, index + 50),
                state_anchor=current_anchor, input_ledger=ledger,
            )
            self.assertFalse(waiting.submit_input)
            self.assertEqual(waiting.reason_code, "awaiting_application")
            VerifiedMotionExecutorTests.applied(
                ledger, anchor, 100 + index, tick, command.movement,
            )
            next_state = admitted.proof.trajectory[index + 1]
            current_anchor = replace(
                anchor,
                observation_sequence_id=anchor.observation_sequence_id + index + 1,
                movement_tick_id=tick,
                physics_state=next_state,
            )
            final = executor.decide(
                self.frame(world, next_state, index + 100),
                state_anchor=current_anchor, input_ledger=ledger,
            )
        self.assertIs(final.state, ActionRouteState.COMPLETE)
        self.assertEqual(final.reason_code, "action_route_complete")

    def test_walk_hands_moving_body_to_verified_gap_without_braking_to_rest(self):
        anchor, physics_world, _, _ = fixture()
        anchor = replace(
            anchor,
            physics_state=replace(
                anchor.physics_state,
                velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.1),
            ),
        )
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute("moving-gap-handoff", (
            WalkSegment(
                FixedRoute("moving-gap-approach", (RoutePoint(.5, 64.0, .5),)),
                (start_id,), (),
            ),
            JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),
        ))
        active = ActiveRoute(
            "moving-gap-handoff", 1, "request", "goal", 1,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=2,
        )
        prepared = prepare_planned_gap_motion(
            active, 1, anchor, physics_world,
            candidate_revision=1, intended_start_tick=11,
        )
        self.assertIs(prepared.status, GapPreparationStatus.READY)
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        current_frame = self.frame(
            physics_world._world, anchor.physics_state, 1,
        )
        executor.start(
            action_route, current_frame,
            verified_motion=(prepared.candidate,),
        )

        decision = executor.decide(
            current_frame, state_anchor=anchor,
            input_ledger=InputApplicationLedger(max_records=64),
        )

        self.assertIs(decision.state, ActionRouteState.RUNNING)
        self.assertEqual(decision.action_index, 1)
        self.assertTrue(decision.submit_input)
        self.assertTrue(decision.movement.jump)
        self.assertEqual(decision.verified_command_index, 0)
        self.assertNotIn(decision.reason_code, {"goal_braking", "awaiting_verified_motion"})

    def test_action_route_rejects_raw_or_wrong_route_proofs(self):
        anchor, reusable = solved_candidate()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        surface_a = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        surface_b = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        route = ActionRoute(
            "another-route",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                surface_a, surface_b, (),
            ),),
        )
        _, physics_world, _, _ = fixture()
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        with self.assertRaises(ContractViolation):
            executor.start(
                route, self.frame(physics_world._world, anchor.physics_state, 1),
                verified_motion=(reusable,),
            )

    def test_gap_route_requires_verified_motion_by_default_then_accepts_it(self):
        anchor, physics_world, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute(
            "required-gap",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),),
        )
        active = ActiveRoute(
            "required-gap", 1, "request", "goal", 1,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=2,
        )
        prepared = prepare_planned_gap_motion(
            active, 0, anchor, physics_world,
            candidate_revision=1, intended_start_tick=11,
        )
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        frame = self.frame(physics_world._world, anchor.physics_state, 1)
        executor.start(action_route, frame)

        waiting = executor.decide(
            frame, state_anchor=anchor,
            input_ledger=InputApplicationLedger(max_records=64),
        )
        self.assertIs(waiting.state, ActionRouteState.RUNNING)
        self.assertFalse(waiting.submit_input)
        self.assertEqual(waiting.reason_code, "awaiting_verified_motion")

        cancelled_executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        cancelled_executor.start(action_route, frame)
        cancelled_executor.cancel()
        for _ in range(3):
            cancelled = cancelled_executor.decide(
                frame, state_anchor=anchor,
                input_ledger=InputApplicationLedger(max_records=64),
            )
            self.assertIs(cancelled.state, ActionRouteState.CANCELLED)
            self.assertFalse(cancelled.submit_input)
            self.assertEqual(cancelled.reason_code, "cancelled")

        executor.install_verified_motion(prepared.candidate)
        started = executor.decide(
            frame, state_anchor=anchor,
            input_ledger=InputApplicationLedger(max_records=64),
        )
        self.assertTrue(started.submit_input)
        self.assertEqual(started.movement,
                         prepared.candidate.proof.commands[0].movement)

    def test_online_coordinator_delivers_background_motion_to_waiting_route(self):
        import time
        anchor, physics_world, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute(
            "coordinated-gap",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),),
        )
        active = ActiveRoute(
            "coordinated-gap", 1, "request", "goal", 1,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=2,
        )
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        frame = self.frame(physics_world._world, anchor.physics_state, 1)
        ledger = InputApplicationLedger(max_records=64)
        with MotionSolverWorker(max_pending=4) as worker:
            coordinator = MotionRouteCoordinator(active, executor, worker)
            coordinator.start(frame)
            decision = coordinator.decide(
                frame, anchor, ledger, physics_world, changed_cells=(),
            )
            self.assertFalse(decision.submit_input)
            deadline = time.perf_counter() + 5.0
            while not decision.submit_input and time.perf_counter() < deadline:
                time.sleep(.01)
                decision = coordinator.decide(
                    frame, anchor, ledger, physics_world, changed_cells=(),
                )

        self.assertTrue(decision.submit_input)
        self.assertTrue(decision.movement.jump)
        self.assertEqual(decision.verified_command_index, 0)
        self.assertEqual(
            decision.expected_movement_tick,
            anchor.movement_tick_id + 1,
        )
        self.assertGreaterEqual(
            decision.latest_movement_tick,
            decision.expected_movement_tick,
        )
        self.assertEqual(executor.action_index, 0)

    def test_coordinator_prepares_next_gap_from_the_next_applied_walk_state(self):
        anchor, physics_world, _, _ = fixture()
        anchor = replace(
            anchor,
            physics_state=replace(
                anchor.physics_state,
                position=(.5, 64.0, .2),
                velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.1),
            ),
        )
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute("predicted-gap", (
            WalkSegment(
                FixedRoute("predicted-approach", (
                    RoutePoint(.5, 64.0, .2),
                    RoutePoint(.5, 64.0, .5),
                )),
                (start_id,), (),
            ),
            JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),
        ))
        active = ActiveRoute(
            "predicted-gap", 1, "request", "goal", 1,
            anchor.session.value, None, 2.3, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.3, end_id),
            action_route, planning_generation=2,
        )
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        current_frame = self.frame(
            physics_world._world, anchor.physics_state, 1,
        )
        jobs = []
        with MotionSolverWorker(max_pending=1) as worker:
            coordinator = MotionRouteCoordinator(active, executor, worker)
            coordinator.start(current_frame)
            with (
                patch.object(worker, "is_alive", return_value=True),
                patch.object(worker, "poll_available", return_value=()),
                patch.object(
                    worker, "submit",
                    side_effect=lambda job: jobs.append(job) or True,
                ),
            ):
                decision = coordinator.decide(
                    current_frame, anchor,
                    InputApplicationLedger(max_records=64),
                    physics_world, changed_cells=(),
                )

            self.assertEqual(len(jobs), 1)
            predicted_anchor = jobs[0].anchor
            solved = _execute_job(jobs[0])
            predicted_frame = self.frame(
                physics_world._world, predicted_anchor.physics_state, 2,
            )
            with (
                patch.object(worker, "is_alive", return_value=True),
                patch.object(worker, "poll_available", return_value=(solved,)),
            ):
                handoff = coordinator.decide(
                    predicted_frame, predicted_anchor,
                    InputApplicationLedger(max_records=64),
                    physics_world, changed_cells=(),
                )

        self.assertEqual(decision.action_index, 0)
        self.assertTrue(decision.submit_input)
        self.assertEqual(jobs[0].connection_id, "predicted-gap/action-1")
        self.assertEqual(jobs[0].anchor.movement_tick_id, 11)
        self.assertGreater(jobs[0].anchor.physics_state.position[2], .2)
        self.assertEqual(
            jobs[0].request.policy.maximum_entry_speed_blocks_per_second,
            3.0,
        )
        self.assertEqual(handoff.action_index, 1)
        self.assertTrue(handoff.submit_input)
        self.assertTrue(handoff.movement.jump)
        self.assertNotEqual(handoff.reason_code, "awaiting_verified_motion")

    def test_online_coordinator_stops_waiting_when_solver_worker_dies(self):
        anchor, physics_world, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute(
            "dead-worker-gap",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),),
        )
        active = ActiveRoute(
            "dead-worker-gap", 1, "request", "goal", 1,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=2,
        )
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        frame = self.frame(physics_world._world, anchor.physics_state, 1)
        ledger = InputApplicationLedger(max_records=64)
        worker = MotionSolverWorker(max_pending=4)
        try:
            coordinator = MotionRouteCoordinator(active, executor, worker)
            coordinator.start(frame)
            worker._process.terminate()
            worker._process.join(2)

            decision = coordinator.decide(
                frame, anchor, ledger, physics_world, changed_cells=(),
            )

            self.assertFalse(decision.submit_input)
            self.assertEqual(coordinator.last_failure_reason,
                             "motion_solver_worker_died")
        finally:
            worker.close()

    def test_coordinator_bounds_identical_revalidation_retries(self):
        anchor, physics_world, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute(
            "bounded-retry-gap",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),),
        )
        active = ActiveRoute(
            "bounded-retry-gap", 1, "request", "goal", 1,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=2,
        )
        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        current_frame = self.frame(
            physics_world._world, anchor.physics_state, 1,
        )
        worker = MotionSolverWorker(max_pending=1)
        failure = SolveResult(
            SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
            reasons=("revalidated_commands_failed",),
        )
        try:
            coordinator = MotionRouteCoordinator(active, executor, worker)
            coordinator.start(current_frame)
            connection = coordinator._connection_id(0)
            rejected = GapPreparationResult(
                GapPreparationStatus.ADMISSION_REJECTED,
                solve_result=failure,
                reason="candidate_revalidation_failed",
            )
            with patch(
                "mc2p.motion_nav.motion_coordination.prepare_planned_gap_motion",
                return_value=rejected,
            ):
                for revision in (1, 2):
                    coordinator._pending_connection = connection
                    coordinator._accept_result(
                        GapMotionSolveResult(
                            connection, revision, failure, 0,
                        ),
                        anchor, physics_world, (),
                    )
                    self.assertIs(executor.state, ActionRouteState.RUNNING)

                coordinator._pending_connection = connection
                coordinator._accept_result(
                    GapMotionSolveResult(connection, 3, failure, 0),
                    anchor, physics_world, (),
                )

            self.assertIs(executor.state, ActionRouteState.CANCELLED)
            self.assertEqual(
                coordinator.last_failure_reason,
                "motion_retry_exhausted:candidate_revalidation_failed",
            )
        finally:
            worker.close()

    def test_planned_gap_is_locally_solved_bound_admitted_then_executable(self):
        anchor, _, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute(
            "planned-route",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),),
        )
        active = ActiveRoute(
            "planned-route", 2, "request", "goal", 3,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=7,
        )
        _, physics_world, _, _ = fixture()

        prepared = prepare_planned_gap_motion(
            active, 0, anchor, physics_world,
            candidate_revision=1, intended_start_tick=11,
        )

        self.assertIs(prepared.status, GapPreparationStatus.READY)
        self.assertEqual(prepared.candidate.context.planning_generation, 7)
        self.assertEqual(prepared.candidate.context.route_revision, 2)
        self.assertEqual(prepared.candidate.context.action_index, 0)
        from mc2p.motion_nav.motion_solver import SOLVER_ID
        self.assertEqual(prepared.candidate.proof.solver_id, SOLVER_ID)

        changed = prepare_planned_gap_motion(
            active, 0, anchor, physics_world,
            candidate_revision=2, intended_start_tick=11,
            changed_cells=(prepared.candidate.proof.world_dependencies[0],),
        )
        self.assertIs(changed.status, GapPreparationStatus.ADMISSION_REJECTED)
        self.assertEqual(changed.reason, "world_dependency_changed")

        wrong_connection = prepare_planned_gap_motion(
            active, 0, anchor, physics_world,
            candidate_revision=3, intended_start_tick=11,
            precomputed=SolveResult(
                SolveStatus.SOLVED,
                replace(prepared.solve_result.proof, direction=(1, 0)),
            ),
        )
        self.assertIs(wrong_connection.status, GapPreparationStatus.SOLVE_FAILED)
        self.assertEqual(wrong_connection.reason,
                         "precomputed_connection_mismatch")

    def test_route_admitter_revalidates_a_delayed_background_result(self):
        anchor, physics_world, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute(
            "delayed-route",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),),
        )
        active = ActiveRoute(
            "delayed-route", 1, "request", "goal", 1,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=3,
        )
        prepared = prepare_planned_gap_motion(
            active, 0, anchor, physics_world,
            candidate_revision=1, intended_start_tick=11,
        )
        reusable = prepared.candidate.candidate
        later = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 3,
            movement_tick_id=anchor.movement_tick_id + 3,
            physics_state=replace(
                anchor.physics_state,
                movement_tick_id=anchor.movement_tick_id + 3,
            ),
        )

        admission = RouteAdmitter().admit_verified_motion(
            reusable, active, later,
            candidate_revision=1, intended_start_tick=14,
            changed_cells=(), world=physics_world,
        )

        self.assertIs(admission.status, MotionCandidateStatus.ACCEPTED)
        self.assertEqual(admission.candidate.admitted_movement_tick_id, 13)
        self.assertEqual(admission.candidate.intended_start_tick, 14)
        self.assertEqual(
            admission.candidate.proof.anchor_observation_sequence_id,
            later.observation_sequence_id,
        )

    def test_following_cardinal_walk_is_part_of_the_gap_exit_proof(self):
        anchor, physics_world, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(0, 2, 1, 3),
            1.0, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute("turn-after-gap", (
            JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),
            WalkSegment(
                FixedRoute("turn-right", (
                    RoutePoint(.5, 64.0, 2.5),
                    RoutePoint(1.5, 64.0, 2.5),
                )),
                (end_id,), (),
            ),
        ))
        active = ActiveRoute(
            "turn-after-gap", 1, "request", "goal", 1,
            anchor.session.value, None, 3.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 3.0, end_id),
            action_route, planning_generation=8,
        )

        prepared = prepare_planned_gap_motion(
            active, 0, anchor, physics_world,
            candidate_revision=1, intended_start_tick=11,
        )

        self.assertIs(prepared.status, GapPreparationStatus.READY)
        self.assertEqual(
            prepared.candidate.proof.commands[-1].required_movement_yaw_radians,
            -math.pi / 2,
        )
        self.assertGreater(
            prepared.candidate.proof.exit_state.velocity_blocks_per_tick[0], 0,
        )

        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        executor.start(
            action_route,
            self.frame(physics_world._world, anchor.physics_state, 1),
            verified_motion=(prepared.candidate,),
        )
        ledger = InputApplicationLedger(max_records=64)
        current_anchor = anchor
        handoff = None
        for index, command in enumerate(prepared.candidate.proof.commands):
            decision = executor.decide(
                self.frame(physics_world._world,
                           current_anchor.physics_state, index + 2),
                state_anchor=current_anchor, input_ledger=ledger,
            )
            if index == len(prepared.candidate.proof.commands) - 1:
                self.assertIsNotNone(decision.look)
                self.assertAlmostEqual(decision.look.yaw_delta_degrees, -90.0)
            tick = prepared.candidate.intended_start_tick + index
            executor.register_verified_submission(
                index, control_sequence=200 + index,
                requested_movement_tick=tick,
            )
            VerifiedMotionExecutorTests.applied(
                ledger, anchor, 200 + index, tick, command.movement,
            )
            next_state = prepared.candidate.proof.trajectory[index + 1]
            current_anchor = replace(
                anchor,
                observation_sequence_id=anchor.observation_sequence_id + index + 1,
                movement_tick_id=tick,
                physics_state=next_state,
            )
            handoff = executor.decide(
                self.frame(physics_world._world, next_state, index + 100),
                state_anchor=current_anchor, input_ledger=ledger,
            )
        self.assertIs(handoff.state, ActionRouteState.RUNNING)
        self.assertEqual(handoff.action_index, 1)
        self.assertNotEqual(handoff.movement, MovementV1())

    def test_planned_gap_rejects_landing_region_narrower_than_the_body(self):
        anchor, physics_world, _, _ = fixture()
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 2, 64, 0)
        start_surface = SupportSurface(
            start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end_surface = SupportSurface(
            end_id, (.5, 64.0, 2.5), HorizontalRegion(.4, 2.0, .6, 3.0),
            .2, ("minecraft:grass_block",), (),
        )
        action_route = ActionRoute(
            "narrow-landing",
            (JumpGapSegment(
                JumpGapEdge(start_id, end_id, "test-jump-gap", .9, ()),
                start_surface, end_surface, (),
            ),),
        )
        active = ActiveRoute(
            "narrow-landing", 1, "request", "goal", 1,
            anchor.session.value, None, 2.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 2.0, end_id),
            action_route, planning_generation=1,
        )

        prepared = prepare_planned_gap_motion(
            active, 0, anchor, physics_world,
            candidate_revision=1, intended_start_tick=11,
        )

        self.assertIs(
            prepared.status, GapPreparationStatus.UNSUPPORTED_ROUTE_ACTION,
        )
        self.assertEqual(prepared.reason, "landing_region_narrower_than_body")


if __name__ == "__main__":
    unittest.main()
