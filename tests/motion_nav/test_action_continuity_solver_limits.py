"""Recovery evidence keeps its own horizon across short moving air actions."""
from dataclasses import replace
from pathlib import Path
import pickle
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.motion_solver import (
    AirTransitionSolveRequest, GapSolveRequest, LandingRegion, MotionCommandTick,
    MotionSolveKind,
    SolveStatus, load_air_transition_solver_policies, revalidate_air_transition,
    revalidate_gap_motion, solve_air_transition, solve_one_cell_gap,
)
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav.test_air_transition_solver import fixture as air_fixture
from tests.motion_nav.test_b10_gap_solver import fixture as gap_fixture


class ActionContinuitySolverLimitsTests(unittest.TestCase):
    @staticmethod
    def moving_gap():
        anchor, world, target, _ = gap_fixture()
        anchor = replace(
            anchor,
            physics_state=replace(
                anchor.physics_state,
                velocity_blocks_per_tick=(
                    0.0, anchor.physics_state.velocity_blocks_per_tick[1], .065,
                ),
            ),
        )
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12),
            exit_direction=(0, 1), exit_motion_ticks=1,
        )
        return anchor, world, request

    def test_short_gap_followed_by_walk_revalidates_without_changing_trajectory(self):
        anchor, world, request = self.moving_gap()
        solved = solve_one_cell_gap(anchor, world, request)
        self.assertIs(solved.status, SolveStatus.SOLVED)
        proof = solved.proof
        self.assertLess(len(proof.commands), request.max_ticks)
        self.assertGreater(proof.exit_state.velocity_blocks_per_tick[2], .01)

        replayed = revalidate_gap_motion(
            proof, anchor, world, request.execution_window,
        )

        self.assertIs(replayed.status, SolveStatus.SOLVED, replayed.reasons)
        self.assertEqual(replayed.proof.commands, proof.commands)
        self.assertEqual(replayed.proof.trajectory, proof.trajectory)
        self.assertEqual(replayed.proof.delayed_start_variants,
                         proof.delayed_start_variants)

    def test_one_tick_later_entry_retains_the_published_recovery_horizon(self):
        anchor, world, request = self.moving_gap()
        proof = solve_one_cell_gap(anchor, world, request).proof
        delayed = proof.start_variant(12)
        later_anchor = replace(
            anchor, observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=delayed.entry_state.movement_tick_id,
            physics_state=delayed.entry_state,
        )

        replayed = revalidate_gap_motion(
            proof, later_anchor, world, CandidateExecutionWindow(12, 13),
        )

        self.assertIs(replayed.status, SolveStatus.SOLVED, replayed.reasons)
        self.assertEqual(replayed.proof.recovery_horizon_ticks, 20)
        self.assertEqual(replayed.proof.commands, proof.commands)
        self.assertEqual(replayed.proof.trajectory, delayed.trajectory)
        self.assertEqual(len(replayed.proof.start_variant(13).tick_inputs),
                         len(proof.commands))

    def test_rollout_and_recovery_limits_are_independent(self):
        anchor, world, request = self.moving_gap()
        request = replace(request, max_ticks=13, recovery_horizon_ticks=20)

        solved = solve_one_cell_gap(anchor, world, request)

        self.assertIs(solved.status, SolveStatus.SOLVED, solved.reasons)
        self.assertLessEqual(len(solved.proof.commands), 13)
        self.assertEqual(solved.proof.recovery_horizon_ticks, 20)
        self.assertEqual(request.as_air_transition().max_ticks, 13)
        self.assertEqual(request.as_air_transition().recovery_horizon_ticks, 20)

    def test_insufficient_recovery_time_is_rejected_in_solve_and_revalidation(self):
        anchor, world, request = self.moving_gap()
        proof = solve_one_cell_gap(anchor, world, request).proof

        solved = solve_one_cell_gap(
            anchor, world, replace(request, recovery_horizon_ticks=1),
        )
        replayed = revalidate_gap_motion(
            replace(proof, recovery_horizon_ticks=1),
            anchor, world, request.execution_window,
        )

        self.assertIs(solved.status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH)
        self.assertIn("release_recovery_not_safe", solved.reasons)
        self.assertIsNone(replayed.proof)
        self.assertIn("start_window_release_recovery_not_safe", replayed.reasons)

    def test_each_request_keeps_its_original_recovery_limit_when_omitted(self):
        anchor, world, gap_request = self.moving_gap()
        self.assertEqual(gap_request.recovery_horizon_ticks, 20)
        self.assertEqual(gap_request.as_air_transition().recovery_horizon_ticks, 20)
        policies = load_air_transition_solver_policies(Path(
            "config/motion-navigation/verified-height-transitions-v1.json",
        ))
        for kind, ticks in (
                (MotionSolveKind.JUMP_UP, 40),
                (MotionSolveKind.CONTROLLED_DROP, 80)):
            with self.subTest(kind=kind):
                anchor, world, target = air_fixture(kind)
                request = AirTransitionSolveRequest(
                    kind, (0, 1), LandingRegion(*target),
                    CandidateExecutionWindow(11, 12), max_ticks=ticks,
                    policy=policies[kind],
                )
                self.assertEqual(request.recovery_horizon_ticks, ticks)
                proof = solve_air_transition(anchor, world, request).proof
                self.assertIsNotNone(proof)
                self.assertLess(len(proof.commands), ticks)
                replayed = revalidate_air_transition(
                    proof, anchor, world, request.execution_window,
                )
                self.assertIs(replayed.status, SolveStatus.SOLVED, replayed.reasons)
                self.assertEqual(proof.recovery_horizon_ticks, ticks)
                self.assertEqual(replayed.proof.recovery_horizon_ticks, ticks)
                self.assertEqual(replayed.proof.trajectory, proof.trajectory)

    def test_proof_serialization_retains_the_recovery_limit(self):
        anchor, world, request = self.moving_gap()
        proof = solve_one_cell_gap(anchor, world, request).proof

        restored = pickle.loads(pickle.dumps(proof))

        self.assertEqual(restored, proof)
        self.assertEqual(restored.recovery_horizon_ticks, 20)
        replayed = revalidate_gap_motion(
            restored, anchor, world, request.execution_window,
        )
        self.assertIs(replayed.status, SolveStatus.SOLVED, replayed.reasons)

    def test_invalid_limits_and_missing_proof_limit_are_not_guessed(self):
        anchor, world, request = self.moving_gap()
        proof = solve_one_cell_gap(anchor, world, request).proof
        for invalid in (0, -1, True, 1.5, 81):
            with self.subTest(limit=invalid):
                with self.assertRaises(ContractViolation):
                    replace(request, recovery_horizon_ticks=invalid)
                with self.assertRaises(ContractViolation):
                    replace(request.as_air_transition(),
                            recovery_horizon_ticks=invalid)
                with self.assertRaises(ContractViolation):
                    replace(proof, recovery_horizon_ticks=invalid)
        with self.assertRaises(ContractViolation):
            replace(proof, recovery_horizon_ticks=None)

    def test_preparation_advances_from_real_noncentral_source_without_new_observation(self):
        from mc2p.motion_nav import motion_solver
        self.assertTrue(callable(getattr(
            motion_solver, "solve_prepared_air_transition", None,
        )), "solver must bind predicted entry to its real source and chosen prefix")
        anchor, world, request = self.moving_gap()
        source = replace(
            anchor, physics_state=replace(
                anchor.physics_state, position=(.55, 64.0, .3),
            ),
        )
        command = MotionCommandTick(MovementV1(forward=1), 0.0)
        request = replace(request, execution_window=CandidateExecutionWindow(12, 13))

        solved = motion_solver.solve_prepared_air_transition(
            source, world, request, (command,),
        )

        self.assertIs(solved.status, SolveStatus.SOLVED, solved.reasons)
        proof = solved.proof
        preparation = proof.preparation
        projected = project_movement_command(
            source.physics_state, command.movement, movement_yaw_radians=0.0,
        )
        expected_entry = step(
            source.physics_state, projected.tick_input, world, JAVA_1_21_RULESET,
        ).next_state
        self.assertEqual(preparation.source_anchor, source)
        self.assertEqual(preparation.commands, (command,))
        self.assertEqual(preparation.tick_inputs, (projected.tick_input,))
        self.assertEqual(preparation.trajectory, (source.physics_state, expected_entry))
        self.assertEqual(proof.entry_state, expected_entry)
        self.assertNotEqual(proof.entry_state.position, (.5, 64.0, .5))
        self.assertEqual(proof.anchor_observation_sequence_id,
                         source.observation_sequence_id)
        self.assertEqual(proof.anchor_movement_tick_id, source.movement_tick_id + 1)
        self.assertEqual(proof.recovery_horizon_ticks, 20)
        self.assertLessEqual(solved.candidates_evaluated, request.max_candidates)
        self.assertTrue(set(preparation.world_dependencies).issubset(proof.world_dependencies))
        self.assertTrue(set(preparation.resource_incomplete_reasons).issubset(
            proof.resource_incomplete_reasons,
        ))
        self.assertEqual(pickle.loads(pickle.dumps(proof)), proof)

        actual = replace(
            source, physics_state=expected_entry,
            movement_tick_id=expected_entry.movement_tick_id,
            observation_sequence_id=source.observation_sequence_id + 1,
        )
        refreshed = revalidate_gap_motion(
            proof, actual, world, request.execution_window,
        )
        self.assertIs(refreshed.status, SolveStatus.SOLVED, refreshed.reasons)
        self.assertIsNone(refreshed.proof.preparation)
        self.assertEqual(refreshed.proof.anchor_observation_sequence_id,
                         actual.observation_sequence_id)
        self.assertEqual(refreshed.proof.recovery_horizon_ticks, 20)

    def test_preparation_unknown_space_is_reported_before_air_search(self):
        from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
        anchor, world, target, _ = gap_fixture(missing=((0, 65, 0),))
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(12, 13),
        )

        result = solve_prepared_air_transition(
            anchor, world, request, (MotionCommandTick(MovementV1(forward=1), 0.0),),
        )

        self.assertIs(result.status, SolveStatus.NEEDS_WORLD)
        self.assertIn((0, 65, 0), result.missing_cells)
        self.assertEqual(result.candidates_evaluated, 0)
        self.assertIsNone(result.proof)

    def test_preparation_wall_contact_and_unsupported_state_do_not_create_proof(self):
        from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
        from mc2p.motion_nav.physics_adapter import PhysicsWorldView
        from mc2p.motion_nav.world_model import (
            BlockGeometry, ObservationStamp, WorldKnowledge,
        )
        anchor, world, request = self.moving_gap()
        knowledge = WorldKnowledge(anchor.session)
        stamp = ObservationStamp(anchor.session, 1, 1, "test", 1)
        knowledge.confirm_air(stamp, tuple(
            (x, y, z) for x in range(-1, 2) for y in range(61, 68)
            for z in range(-1, 4)
        ))
        knowledge.observe_blocks(
            stamp, {
                (0, 63, 0): BlockGeometry.full_cube("minecraft:grass_block"),
                (0, 64, 1): BlockGeometry.full_cube("minecraft:stone"),
            },
        )
        world = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)
        forward = MotionCommandTick(MovementV1(forward=1), 0.0)
        rejected = solve_prepared_air_transition(
            anchor, world,
            replace(request, execution_window=CandidateExecutionWindow(13, 14)),
            (forward, forward),
        )
        self.assertIs(rejected.status, SolveStatus.HARD_CONFLICT)
        self.assertIn("preparation_horizontal_collision", rejected.reasons)
        self.assertEqual(rejected.candidates_evaluated, 0)
        self.assertIsNone(rejected.proof)

        anchor, world, request = self.moving_gap()
        using_item = replace(
            anchor, physics_state=replace(anchor.physics_state, is_using_item=True),
        )
        rejected = solve_prepared_air_transition(
            using_item, world,
            replace(request, execution_window=CandidateExecutionWindow(12, 13)),
            (forward,),
        )
        self.assertIs(rejected.status, SolveStatus.UNSUPPORTED)
        self.assertIsNone(rejected.proof)

    def test_preparation_cannot_be_rebound_to_a_different_source_or_entry(self):
        from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
        anchor, world, request = self.moving_gap()
        request = replace(request, execution_window=CandidateExecutionWindow(12, 13))
        solved = solve_prepared_air_transition(
            anchor, world, request,
            (MotionCommandTick(MovementV1(forward=1), 0.0),),
        )
        self.assertIs(solved.status, SolveStatus.SOLVED, solved.reasons)
        preparation = solved.proof.preparation
        with self.assertRaises(ContractViolation):
            replace(preparation, source_anchor=replace(
                anchor, physics_state=replace(
                    anchor.physics_state, position=(.55, 64.0, .5),
                ),
            ))
        changed_observation = replace(
            preparation, source_anchor=replace(
                anchor, observation_sequence_id=anchor.observation_sequence_id + 1,
            ),
        )
        with self.assertRaises(ContractViolation):
            replace(solved.proof, preparation=changed_observation)

    def test_preparation_must_not_jump_change_look_or_leave_support(self):
        from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
        anchor, world, request = self.moving_gap()
        request = replace(request, execution_window=CandidateExecutionWindow(12, 13))
        for command in (
                MotionCommandTick(MovementV1(forward=1, jump=True), 0.0),
                MotionCommandTick(MovementV1(forward=1), .2)):
            with self.subTest(command=command):
                rejected = solve_prepared_air_transition(anchor, world, request, (command,))
                self.assertIs(rejected.status, SolveStatus.INVALID_INPUT)
                self.assertIsNone(rejected.proof)
                self.assertEqual(rejected.candidates_evaluated, 0)
        edge = replace(
            anchor, physics_state=replace(
                anchor.physics_state, position=(.5, 64.0, 1.24),
            ),
        )
        rejected = solve_prepared_air_transition(
            edge, world,
            replace(request, execution_window=CandidateExecutionWindow(13, 14)),
            (MotionCommandTick(MovementV1(forward=1), 0.0),) * 2,
        )
        self.assertIs(rejected.status, SolveStatus.NEEDS_STATE)
        self.assertIn("preparation_left_ground_support", rejected.reasons)
        self.assertIsNone(rejected.proof)

    def test_preparation_prefix_and_window_are_bounded(self):
        from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
        anchor, world, request = self.moving_gap()
        command = MotionCommandTick(MovementV1(forward=1), 0.0)
        rejected = solve_prepared_air_transition(anchor, world, request, (command,))
        self.assertIs(rejected.status, SolveStatus.INVALID_INPUT)
        self.assertIn("preparation_execution_window_detached", rejected.reasons)
        with self.assertRaises(ContractViolation):
            solve_prepared_air_transition(anchor, world, request, (command,) * 5)

        old = solve_one_cell_gap(anchor, world, request)
        no_prefix = solve_prepared_air_transition(anchor, world, request, ())
        self.assertEqual(no_prefix, old)
        self.assertIsNone(no_prefix.proof.preparation)

    def test_four_tick_prefix_still_uses_one_bounded_air_search(self):
        from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
        anchor, world, request = self.moving_gap()
        source = replace(
            anchor, physics_state=replace(
                anchor.physics_state, position=(.55, 64.0, -.3),
            ),
        )
        request = replace(request, execution_window=CandidateExecutionWindow(15, 16))
        forward = MotionCommandTick(MovementV1(forward=1), 0.0)

        solved = solve_prepared_air_transition(source, world, request, (forward,) * 4)

        self.assertIs(solved.status, SolveStatus.SOLVED, solved.reasons)
        self.assertEqual(len(solved.proof.preparation.commands), 4)
        self.assertEqual(solved.proof.anchor_movement_tick_id, source.movement_tick_id + 4)
        self.assertEqual(solved.proof.anchor_observation_sequence_id,
                         source.observation_sequence_id)
        self.assertEqual(solved.proof.recovery_horizon_ticks, 20)
        self.assertLessEqual(solved.candidates_evaluated, 12)

    def test_coordinator_checks_proof_fact_changed_before_the_delivery_frame(self):
        """The latest frame's empty change set cannot revive an old landing."""
        from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
        from mc2p.motion_nav.motion_worker import _execute_job
        from mc2p.motion_nav.online_motion import InputApplicationLedger
        from mc2p.motion_nav.physics_adapter import PhysicsWorldView
        from mc2p.motion_nav.world_model import ObservationStamp
        from tests.motion_nav.test_b10_motion_candidate import (
            VerifiedMotionRouteIntegrationTests,
        )

        class HeldWorker:
            def __init__(self):
                self.jobs = []
                self.ready = ()

            def submit(self, job):
                self.jobs.append(job)
                return True

            def poll_available(self):
                ready, self.ready = self.ready, ()
                return ready

            def is_alive(self):
                return True

            def close(self):
                pass

        fixture = VerifiedMotionRouteIntegrationTests()
        anchor, old_world, route, executor = fixture.coordinator_fixture(
            "proof-dependent-landing-changed",
        )
        worker = HeldWorker()
        coordinator = MotionRouteCoordinator(
            route, executor, worker, clock_ns=lambda: 1,
        )
        first_frame = fixture.frame(old_world._world, anchor.physics_state, 1)
        coordinator.start(first_frame)
        ledger = InputApplicationLedger()
        coordinator.decide(first_frame, anchor, ledger, old_world, changed_cells=())
        result = _execute_job(worker.jobs[0])
        self.assertIs(result.solve_result.status, SolveStatus.SOLVED)
        changed = (0, 63, 2)
        self.assertIn(changed, result.solve_result.proof.world_dependencies)
        self.assertNotIn(changed, route.action_route.dependencies)

        # An observation changes the solver-only fact while transport still
        # holds the result. A second observation has no new geometry changes.
        knowledge = old_world._world._owner
        knowledge.confirm_air(ObservationStamp(anchor.session, 2, 2, "test", 2),
                              (changed,))
        current = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)
        coordinator.decide(
            fixture.frame(current._world, anchor.physics_state, 2),
            replace(anchor, observation_sequence_id=4), ledger, current,
            changed_cells=(changed,),
        )
        worker.ready = (result,)
        decision = coordinator.decide(
            fixture.frame(current._world, anchor.physics_state, 3),
            replace(anchor, observation_sequence_id=5), ledger, current,
            changed_cells=(),
        )

        self.assertFalse(executor.has_verified_motion(0))
        self.assertFalse(decision.movement.jump)
        self.assertFalse(any(record.disposition.value == "applied"
                             for record in coordinator.admission_records))


if __name__ == "__main__":
    unittest.main()
