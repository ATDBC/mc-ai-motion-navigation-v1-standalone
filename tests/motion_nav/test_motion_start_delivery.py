"""Preparation consumes real time without consuming first-command slack."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, LandingRegion, MotionCommandTick, SolveStatus,
    revalidate_air_transition, solve_one_cell_gap, solve_prepared_air_transition,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveJob, MotionJobOperation, _execute_job,
)
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav.test_b10_gap_solver import fixture
from tests.motion_nav import test_motion_baseline_recovery as recovery_cases
from tests.motion_nav import test_action_continuity_formal as continuity
from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutor
from tests.sim.runner import run
from mc2p.contracts.behavior import BehaviorProfileV0


class PreparedRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.anchor, self.world, target, _ = fixture()
        self.request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12),
        )
        result = solve_one_cell_gap(self.anchor, self.world, self.request)
        self.assertIs(result.status, SolveStatus.SOLVED)
        self.proof = result.proof
        self.prefix = (MotionCommandTick(MovementV1(), self.anchor.physics_state.yaw_radians),)

    def test_revalidation_predicts_prefix_and_keeps_original_commands(self):
        window = CandidateExecutionWindow(12, 13)
        result = revalidate_air_transition(
            self.proof, self.anchor, self.world, window, entry_prefix=self.prefix,
        )
        self.assertIs(result.status, SolveStatus.SOLVED, result.reasons)
        self.assertEqual(result.candidates_evaluated, 0)
        self.assertEqual(result.proof.commands, self.proof.commands)
        self.assertEqual(result.proof.preparation.source_anchor, self.anchor)
        self.assertEqual(result.proof.preparation.commands, self.prefix)
        self.assertEqual(result.proof.anchor_movement_tick_id, 11)
        self.assertEqual(result.proof.execution_window, window)
        self.assertTrue(set(result.proof.preparation.world_dependencies)
                        <= set(result.proof.world_dependencies))
        searched = solve_prepared_air_transition(
            self.anchor, self.world, replace(self.request, execution_window=window), self.prefix,
        )
        self.assertIs(searched.status, SolveStatus.SOLVED)
        self.assertEqual(result.proof.preparation, searched.proof.preparation)

    def test_worker_consumes_revalidation_prefix(self):
        request = replace(self.request, execution_window=CandidateExecutionWindow(12, 13))
        result = _execute_job(GapMotionSolveJob(
            "prepared-gap", 2, self.anchor, self.world, request,
            operation=MotionJobOperation.REVALIDATE, proof=self.proof,
            entry_prefix=self.prefix,
        )).solve_result
        self.assertIs(result.status, SolveStatus.SOLVED, result.reasons)
        self.assertEqual(result.proof.preparation.source_anchor, self.anchor)
        self.assertEqual(result.proof.anchor_movement_tick_id, 11)
        self.assertEqual(result.proof.commands, self.proof.commands)

    def test_revalidation_cannot_detach_window_or_invent_ground_preparation(self):
        for prefix, window in (
                (self.prefix, CandidateExecutionWindow(11, 12)),
                ((MotionCommandTick(MovementV1(jump=True), 0.),), CandidateExecutionWindow(12, 13))):
            with self.subTest(prefix=prefix):
                result = revalidate_air_transition(
                    self.proof, self.anchor, self.world, window, entry_prefix=prefix,
                )
                self.assertIs(result.status, SolveStatus.INVALID_INPUT)
                self.assertIsNone(result.proof)

    def test_look_confirmed_before_source_does_not_contaminate_future_prefix(self):
        from mc2p.contracts.action_v1 import ActionSnapshotV1, LookV1
        from mc2p.contracts.action_receipt import ClientInputApplicationV1
        from mc2p.motion_nav.online_motion import InputApplicationLedger
        from tests.motion_nav import test_b10_motion_candidate as candidates
        from tests.motion_nav.test_action_continuity_admission import admit
        prepared = solve_prepared_air_transition(
            self.anchor, self.world,
            replace(self.request, execution_window=CandidateExecutionWindow(12, 13)), self.prefix,
        )
        _, candidate = candidates.solved_candidate()
        candidate = replace(candidate, proof=prepared.proof)
        current = replace(self.anchor, movement_tick_id=11,
                          physics_state=prepared.proof.entry_state,
                          observation_sequence_id=self.anchor.observation_sequence_id+1)
        for applied_at in (10, 11, None):
            with self.subTest(applied_at=applied_at):
                ledger = InputApplicationLedger()
                look = ActionSnapshotV1('episode', 50, 2, 1000000,
                    look=LookV1(15.,0.), valid_for_ticks=1)
                ledger.submit(self.anchor.session, look, requested_first_tick=10,
                              latest_allowed_first_tick=11)
                if applied_at is not None:
                    ledger.observe_sample(ClientInputApplicationV1(
                        'mc2p.input-application.v1', applied_at, 'episode', 50, applied_at,
                        'neutral', 0., 0., False, False, False))
                if applied_at != 11:
                    candidates.VerifiedMotionExecutorTests.applied(
                        ledger, self.anchor, 51, 11, MovementV1())
                result = admit(candidate, current, input_ledger=ledger)
                self.assertEqual(result.status.value,
                    'accepted' if applied_at == 10 else 'needs_revalidation')


class StartDeliveryFormalTests(unittest.TestCase):
    def test_complete_solve_samples_once_at_delivery_before_entry_wait(self):
        from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
        original = MotionRouteCoordinator._sample_solve_delivery
        for delay in (1, 2, 4, 7):
            with self.subTest(delay=delay):
                samples = []
                worker = continuity._DeliveryWorker(delivery_polls=delay)
                case = recovery_cases.MotionBaselineRecoveryTests().scenario(72)
                case = replace(case, perturbations=replace(case.perturbations, late_ticks=frozenset()))

                def record_sample(owner, result, anchor):
                    before = owner._delivery_identity, owner._delivery_ticks
                    original(owner, result, anchor)
                    after = owner._delivery_identity, owner._delivery_ticks
                    if before != after:
                        samples.append((result.work_identity, owner._delivery_ticks,
                                        owner._solve_basis_job.operation, result.solve_result.status))

                with patch.object(MotionRouteCoordinator, '_sample_solve_delivery', record_sample):
                    result = run(case, motion_factory=lambda: worker)
                self.assertTrue(samples)
                self.assertEqual(samples[0][1], min(4, delay))
                self.assertEqual(len({sample[0] for sample in samples}), len(samples))
                self.assertTrue(all(sample[2:] == (MotionJobOperation.SOLVE, SolveStatus.SOLVED)
                                    for sample in samples))
                self.assertFalse(result.violations)

    def test_fast_alignment_revalidation_keeps_complete_solve_preparation(self):
        from mc2p.contracts.action import ActionPriorityV0
        from mc2p.contracts.action_v1 import ActionIntentV1, LookV1
        from mc2p.contracts.intent_source import ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id

        class AlignmentDeliveryWorker(continuity._DeliveryWorker):
            def submit(self, job):
                self.delivery_polls = (1 if job.operation is MotionJobOperation.REVALIDATE else 3)
                return super().submit(job)

        worker = AlignmentDeliveryWorker()
        case = recovery_cases.MotionBaselineRecoveryTests().scenario(72)
        case = replace(case, perturbations=replace(case.perturbations, late_ticks=frozenset()))
        stolen = []
        source = None

        def turn_before_delivery(context):
            nonlocal source
            runtime, driver = context.driver.runtime, context.driver
            deadline = context.clock[0] + 500_000_000
            if source is not None:
                runtime.cancel_source(source.source_id)
            proposals = driver.prepare_proposals(deadline)
            external = ()
            if (len(worker.jobs) == 1 and worker._pending
                    and worker.poll_count == worker._pending[0][0] - 1 and not stolen):
                source = runtime.register_ordered_source('delivery-alignment-look')
                intent = ActionIntentV1(ordered_intent_id(source, 1), source.source_id,
                    source.episode_id, runtime.observation.sequence_id, ActionPriorityV0.SAFETY,
                    context.clock[0], deadline, look=LookV1(15., 0.))
                proposals += (ControlFrameProposalV1((OrderedIntentV1(source, 1, intent),)),)
                stolen.append(context.backend.movement_tick + 1)
                external = (intent.intent_id,)
            result = runtime.control_frame(driver._task(deadline), BehaviorProfileV0(),
                                           deadline, proposals=proposals)
            driver.adopt_result(result)
            return external

        result = run(case, motion_factory=lambda: worker, control_step=turn_before_delivery)
        self.assertTrue(stolen)
        self.assertGreaterEqual(len(worker.jobs), 3)
        self.assertEqual([job.operation for job in worker.jobs[:3]],
                         [MotionJobOperation.SOLVE, MotionJobOperation.REVALIDATE,
                          MotionJobOperation.SOLVE])
        self.assertIs(worker.results[0].solve_result.status, SolveStatus.SOLVED)
        self.assertIs(worker.results[1].solve_result.status, SolveStatus.NEEDS_STATE)
        self.assertEqual([len(job.entry_prefix) for job in worker.jobs[:3]], [1, 3, 3])
        self.assertFalse(result.violations)
        self.assertFalse(result.trace[-1]['source_bound'])

    def test_changed_look_during_preparation_cannot_authorize_predicted_entry(self):
        from mc2p.contracts.action import ActionPriorityV0
        from mc2p.contracts.action_v1 import ActionIntentV1, LookV1
        from mc2p.contracts.intent_source import ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id
        from tests.test_player_runtime import _task
        case = recovery_cases.MotionBaselineRecoveryTests().scenario(72)
        case = replace(case, perturbations=replace(case.perturbations, late_ticks=frozenset()))
        worker = continuity._DeliveryWorker()
        stolen = []
        source = None
        def steal_preparation_look(context):
            nonlocal source
            runtime, driver = context.driver.runtime, context.driver
            deadline = context.clock[0] + 500_000_000
            if source is not None:
                runtime.cancel_source(source.source_id)
            proposals = driver.prepare_proposals(deadline)
            external = ()
            if worker.jobs and not stolen:
                source = runtime.register_ordered_source('preparation-look-loss')
                intent = ActionIntentV1(ordered_intent_id(source, 1), source.source_id,
                    source.episode_id, runtime.observation.sequence_id, ActionPriorityV0.SAFETY,
                    context.clock[0], deadline, look=LookV1(15., 0.))
                proposals += (ControlFrameProposalV1((OrderedIntentV1(source, 1, intent),)),)
                stolen.append(context.backend.movement_tick + 1)
                external = (intent.intent_id,)
            result = runtime.control_frame(_task(deadline), BehaviorProfileV0(), deadline, proposals=proposals)
            driver.adopt_result(result)
            return external
        grants = []
        original_start = VerifiedMotionExecutor.start
        def record_grant(executor, candidate):
            grants.append(candidate)
            return original_start(executor, candidate)
        with patch.object(VerifiedMotionExecutor, 'start', record_grant):
            result = run(case, motion_factory=lambda: worker, control_step=steal_preparation_look)
        self.assertTrue(stolen)
        self.assertIs(worker.results[0].solve_result.status, SolveStatus.SOLVED)
        self.assertTrue(all(candidate.proof.preparation.source_anchor != worker.jobs[0].anchor
                            for candidate in grants))
        self.assertFalse(result.violations)
        self.assertIn(result.outcome, {'success', 'failed'})
        self.assertFalse(result.trace[-1]['source_bound'])
        self.assertTrue(all(row['runtime_failure'] is None for row in result.trace))

    def test_first_verified_command_can_apply_one_tick_late(self):
        for case in (continuity._gap_case(), recovery_cases.MotionBaselineRecoveryTests().scenario(14)):
            with self.subTest(case=case.name):
                case = replace(case, perturbations=replace(
                    case.perturbations, late_ticks=frozenset()))
                late_at = []
                def late_first(context):
                    driver = context.driver
                    deadline = context.clock[0] + 500_000_000
                    proposals = driver.prepare_proposals(deadline)
                    windows = [envelope.intent.movement_tick_window
                        for proposal in proposals for envelope in proposal.intents
                        if envelope.intent.movement_tick_window is not None]
                    if windows and not late_at:
                        self.assertEqual(windows[0].latest_tick - windows[0].earliest_tick, 1)
                        late_at.append(windows[0].earliest_tick)
                        context.backend.perturbations.late_ticks = frozenset(late_at)
                    result = driver.runtime.control_frame(
                        driver._task(deadline), BehaviorProfileV0(), deadline, proposals=proposals)
                    driver.adopt_result(result)
                    return ()
                result = run(case, control_step=late_first)
                self.assertTrue(late_at)
                self.assertEqual(result.outcome, "success", result.reason)
                self.assertFalse(result.violations)

    def test_delivery_beyond_prefix_bound_is_bounded_without_unsafe_start(self):
        worker = continuity._DeliveryWorker(delivery_polls=7)
        case = recovery_cases.MotionBaselineRecoveryTests().scenario(72)
        case = replace(case, perturbations=replace(case.perturbations, late_ticks=frozenset()))
        result = run(case, motion_factory=lambda: worker)
        self.assertEqual(result.outcome, "failed")
        self.assertFalse(result.violations)
        self.assertEqual(result.damage, 0.)
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(all(len(job.entry_prefix) <= 4 for job in worker.jobs))

    def test_current_drop_first_grants_keep_slack_and_actual_prefix(self):
        factory = recovery_cases.MotionBaselineRecoveryTests()
        original = VerifiedMotionExecutor.start
        grants = []
        def recorded_start(executor, candidate):
            grants.append(candidate)
            return original(executor, candidate)
        for delay in (0, 1, 2, 4):
            with self.subTest(delay=delay):
                grants.clear()
                worker = continuity._DeliveryWorker(delivery_polls=delay)
                case = factory.scenario(72)
                case = replace(case, perturbations=replace(
                    case.perturbations, late_ticks=frozenset()))
                with patch.object(VerifiedMotionExecutor, "start", recorded_start):
                    result = run(case, motion_factory=lambda: worker)
                self.assertEqual(result.outcome, "success", result.reason)
                self.assertFalse(result.violations)
                self.assertTrue(grants)
                for candidate in grants:
                    self.assertEqual(candidate.proof.execution_window.latest_start_tick
                                     - candidate.intended_start_tick, 1)
                    self.assertIsNotNone(candidate.proof.preparation)
                self.assertTrue(all(1 <= len(job.entry_prefix) <= 4 for job in worker.jobs))

    def test_original_six_late_drop_tasks_complete_with_same_inputs(self):
        factory = recovery_cases.MotionBaselineRecoveryTests()
        for seed in (1, 9, 14, 56, 72, 97, 130, 146):
            with self.subTest(seed=seed):
                result = run(factory.scenario(seed))
                self.assertEqual(result.outcome, "success", result.reason)
                self.assertFalse(result.violations)
                self.assertFalse(result.trace[-1]["source_bound"])


if __name__ == "__main__":
    unittest.main()
