"""D113: delivery jitter and grounded neutral contact use the formal chain."""
from dataclasses import replace
import random
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.async_work import (
    AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind,
    AsyncWorkLifecycle, AsyncWorkWindow,
)
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav import motion_solver
from mc2p.motion_nav.motion_solver import (
    MotionCommandTick, SolveResult, SolveStatus, revalidate_air_transition,
    solve_prepared_air_transition,
)
from mc2p.motion_nav.motion_worker import GapMotionSolveResult, MotionJobOperation
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET
from tests.motion_nav.test_action_continuity_formal import _DeliveryWorker
from tests.motion_nav import test_d112_worker_degradation as degradation
from tests.motion_nav.test_d112_worker_degradation import (
    _action_cases,
)


class _SequenceWorker(_DeliveryWorker):
    def __init__(self, delays):
        super().__init__()
        self.delays = tuple(delays)

    def submit(self, job):
        self.delivery_polls = self.delays[min(len(self.jobs), len(self.delays) - 1)]
        return super().submit(job)


class DeliveryJitterTests(unittest.TestCase):
    def run_sequence(self, action, delays):
        worker = _SequenceWorker(delays)
        checks = degradation.D112WorkerDegradationTests()
        result = checks._run(_action_cases()[action], worker)
        checks._assert_bounded_and_safe(result)
        checks._assert_no_blind_strict_start(result, worker)
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertTrue(all(value.solve_result.status is SolveStatus.SOLVED
                            for value in worker.results), worker.results)
        self.assertEqual(worker.jobs[0].operation, MotionJobOperation.SOLVE)
        self.assertTrue(all(job.operation is MotionJobOperation.REVALIDATE
                            for job in worker.jobs[1:]))
        self.assertLessEqual(len(worker.jobs), 3)
        return worker

    def test_frozen_minimal_sequences_complete_without_wasting_a_solve(self):
        for action, delays, prefixes in (
                ("drop", (2, 3, 3), (1, 2, 3)),
                ("gap", (4, 5, 5), (3, 4, 5)),
                ("jump_up", (4, 4, 5), (3, 4)),
                ("jump_up", (4, 5, 5), (3, 4, 5))):
            with self.subTest(action=action, delays=delays):
                worker = self.run_sequence(action, delays)
                self.assertEqual(tuple(len(job.entry_prefix) for job in worker.jobs), prefixes)

    def test_one_hundred_eighty_seeded_jitter_scenarios(self):
        count = 0
        for action in _action_cases():
            for delay in (2, 3, 4):
                for seed in range(20):
                    with self.subTest(action=action, delay=delay, seed=seed):
                        rng = random.Random(seed)
                        delays = tuple(rng.choice((delay, delay + 1)) for _ in range(3))
                        self.run_sequence(action, delays)
                        count += 1
        self.assertEqual(count, 180)


class DeliveryEstimateTests(unittest.TestCase):
    def setUp(self):
        from tests.motion_nav.test_b10_gap_solver import fixture
        self.anchor = fixture()[0]
        self.scope = AsyncComputationScope(self.anchor.session.value, "delivery-task", 1)
        self.identity = AsyncWorkIdentity(
            self.scope, "delivery-owner", AsyncWorkKind.MOTION_SOLVE, "connection", 1,
        )
        lifecycle = AsyncWorkLifecycle()
        lifecycle.begin(self.identity, AsyncWorkWindow(10, 100, 200))
        self.owner = SimpleNamespace(
            _solve_basis_job=SimpleNamespace(operation=MotionJobOperation.REVALIDATE,
                                             anchor=self.anchor),
            _work=lifecycle, _clock=lambda: 150,
            _pending_connection="connection", _candidate_revision=1,
            _delivery_identity=None, _delivery_ticks=4,
        )
        self.result = GapMotionSolveResult(
            "connection", 1, SolveResult(SolveStatus.HARD_CONFLICT), 1, self.identity,
        )

    def sample(self, result=None, delay=5, scope=None):
        MotionRouteCoordinator._sample_solve_delivery(
            self.owner, result or self.result,
            replace(self.anchor, movement_tick_id=self.anchor.movement_tick_id + delay),
            current_scope=scope or self.scope,
        )

    def test_valid_hard_conflict_revalidation_raises_delivery_estimate(self):
        self.sample()
        self.assertEqual(self.owner._delivery_ticks, 5)
        self.assertEqual(self.owner._delivery_identity, self.identity)

    def test_faster_revalidation_never_shrinks_complete_solve_estimate(self):
        self.sample(delay=2)
        self.assertEqual(self.owner._delivery_ticks, 4)
        self.assertEqual(self.owner._delivery_identity, self.identity)

    def test_first_visible_sample_is_not_replaced_by_later_take(self):
        self.sample(delay=2)
        self.sample(delay=5)
        self.assertEqual(self.owner._delivery_ticks, 4)

    def test_later_complete_solve_cannot_shrink_estimate_either(self):
        self.owner._solve_basis_job.operation = MotionJobOperation.SOLVE
        self.sample(delay=2)
        self.assertEqual(self.owner._delivery_ticks, 4)

    def test_invalid_or_expired_delivery_cannot_change_estimate(self):
        for invalid in ("identity", "scope", "connection", "revision", "expired", "missing"):
            with self.subTest(invalid=invalid):
                result, scope = self.result, self.scope
                self.owner._clock = lambda: 150
                if invalid == "identity":
                    result = replace(result, work_identity=replace(self.identity, revision=2))
                elif invalid == "scope":
                    scope = replace(self.scope, generation=2)
                elif invalid == "connection":
                    result = replace(result, connection_id="other-connection")
                elif invalid == "revision":
                    result = replace(result, candidate_revision=2)
                elif invalid == "expired":
                    self.owner._clock = lambda: 200
                else:
                    result = replace(result, work_identity=None)
                self.sample(result, scope=scope)
                self.assertEqual(self.owner._delivery_ticks, 4)
                self.assertIsNone(self.owner._delivery_identity)


class NeutralStepPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Capture the real moving t8 source, original command proof and world.
        # Keeping the old failing delivery here makes the fixture independent
        # of whether the repaired coordinator finishes in two or three jobs.
        checks = degradation.D112WorkerDegradationTests()
        worker = _SequenceWorker((4, 4, 5))
        checks._run(_action_cases()["jump_up"], worker)
        cls.job = worker.jobs[1]
        assert cls.job.operation is MotionJobOperation.REVALIDATE

    def replay_prefix(self):
        current = self.job.anchor.physics_state
        trajectory, inputs, dependencies, resource_reasons = [current], [], set(), set()
        for command in self.job.entry_prefix:
            projected = motion_solver.project_movement_command(
                current, command.movement,
                movement_yaw_radians=command.required_movement_yaw_radians,
            )
            calculated = step(current, projected.tick_input, self.job.world, JAVA_1_21_RULESET)
            self.assertIs(calculated.status, CalculationStatus.OK)
            current = calculated.next_state
            trajectory.append(current)
            inputs.append(projected.tick_input)
            dependencies.update(calculated.dependencies)
            resource_reasons.update(calculated.resource_update.incomplete_reasons)
        return tuple(trajectory), tuple(inputs), tuple(sorted(dependencies)), tuple(sorted(resource_reasons))

    def test_neutral_side_contact_replays_all_ticks_then_revalidates_final_state(self):
        job = self.job
        trajectory, inputs, dependencies, resource_reasons = self.replay_prefix()
        self.assertTrue(trajectory[1].horizontal_collision)
        self.assertTrue(all(state.on_ground for state in trajectory))
        self.assertEqual(trajectory[1].velocity_blocks_per_tick[::2], (0., 0.))
        self.assertEqual(len(trajectory), 5)
        replayed = revalidate_air_transition(
            job.proof, job.anchor, job.world, job.request.execution_window,
            entry_prefix=job.entry_prefix,
        )
        self.assertIs(replayed.status, SolveStatus.SOLVED, replayed.reasons)
        self.assertEqual(replayed.candidates_evaluated, 0)
        self.assertEqual(replayed.proof.commands, job.proof.commands)
        preparation = replayed.proof.preparation
        self.assertEqual(preparation.source_anchor, job.anchor)
        self.assertEqual(preparation.commands, job.entry_prefix)
        self.assertEqual(preparation.trajectory, trajectory)
        self.assertEqual(preparation.tick_inputs, inputs)
        self.assertEqual(preparation.world_dependencies, dependencies)
        self.assertEqual(preparation.resource_incomplete_reasons, resource_reasons)
        self.assertEqual(replayed.proof.entry_state, trajectory[-1])
        self.assertEqual(replayed.proof.anchor_observation_sequence_id,
                         job.anchor.observation_sequence_id)
        searched = solve_prepared_air_transition(
            job.anchor, job.world, job.request, job.entry_prefix,
        )
        self.assertIs(searched.status, SolveStatus.SOLVED, searched.reasons)
        self.assertEqual(searched.proof.preparation, preparation)
        self.assertTrue(set(dependencies) <= set(replayed.proof.world_dependencies))
        self.assertTrue(set(resource_reasons) <= set(replayed.proof.resource_incomplete_reasons))

    def test_active_or_later_active_prefix_cannot_use_neutral_collision_exception(self):
        for active_index in (0, 3):
            with self.subTest(active_index=active_index):
                prefix = list(self.job.entry_prefix)
                prefix[active_index] = replace(prefix[active_index], movement=MovementV1(forward=1))
                result = revalidate_air_transition(
                    self.job.proof, self.job.anchor, self.job.world,
                    self.job.request.execution_window, entry_prefix=tuple(prefix),
                )
                self.assertIs(result.status, SolveStatus.HARD_CONFLICT)
                self.assertIn("preparation_horizontal_collision", result.reasons)
                self.assertIsNone(result.proof)

    def test_neutral_collision_with_remaining_motion_is_rejected(self):
        original = motion_solver.step
        def sliding(*args):
            calculated = original(*args)
            if calculated.next_state and calculated.next_state.horizontal_collision:
                calculated = replace(calculated, next_state=replace(
                    calculated.next_state, velocity_blocks_per_tick=(.001, -.0784, 0.),
                ))
            return calculated
        with patch.object(motion_solver, "step", sliding):
            result = revalidate_air_transition(
                self.job.proof, self.job.anchor, self.job.world,
                self.job.request.execution_window, entry_prefix=self.job.entry_prefix,
            )
        self.assertIs(result.status, SolveStatus.HARD_CONFLICT)
        self.assertEqual(result.reasons, ("preparation_horizontal_collision",))

    def test_neutral_preparation_retains_ground_world_support_and_look_gates(self):
        original = motion_solver.step
        for failure, expected in (("ground", SolveStatus.NEEDS_STATE),
                                  ("unknown", SolveStatus.NEEDS_WORLD),
                                  ("unsupported", SolveStatus.UNSUPPORTED),
                                  ("look", SolveStatus.INVALID_INPUT)):
            with self.subTest(failure=failure):
                def rejected_step(*args):
                    calculated = original(*args)
                    if failure == "ground":
                        return replace(calculated, next_state=replace(calculated.next_state, on_ground=False))
                    if failure == "unknown":
                        return type(calculated)(
                            CalculationStatus.NEEDS_WORLD, missing_cells=((0, 64, 2),),
                        )
                    return type(calculated)(CalculationStatus.UNSUPPORTED, unsupported_reasons=("test-unsupported",))
                prefix = self.job.entry_prefix
                if failure == "look":
                    prefix = (replace(prefix[0], required_movement_yaw_radians=.1), *prefix[1:])
                with patch.object(motion_solver, "step", rejected_step if failure != "look" else original):
                    result = revalidate_air_transition(
                        self.job.proof, self.job.anchor, self.job.world,
                        self.job.request.execution_window, entry_prefix=prefix,
                    )
                self.assertIs(result.status, expected, result.reasons)
                self.assertIsNone(result.proof)


if __name__ == "__main__":
    unittest.main()
