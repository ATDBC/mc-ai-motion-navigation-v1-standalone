"""D121 fixed successor preparation: feasibility never grants execution."""
from __future__ import annotations

import unittest
from dataclasses import replace
import math
import pickle
import time
from dataclasses import fields, is_dataclass
from unittest.mock import patch

from mc2p.motion_nav import motion_chain, motion_solver
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.motion_solver import MotionCommandTick, MotionSolveKind, LandingRegion, SolveStatus
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.prepared_motion import PreparedScanBudget
from mc2p.motion_nav.world_snapshot import seal_snapshot_version
from tests.motion_nav.test_d121_momentum_handoff import _d120_first_jump_proof
from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.motion_candidate import MotionCandidateContext
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_worker import GapMotionSolveResult, MotionResultInbox, MotionSolverWorker, PreparedMotionJob, _execute_job
from mc2p.motion_nav.online_motion import StateAnchor, MotionTickPhase, InputApplicationLedger
from mc2p.motion_nav.prepared_motion import PreparedSubmitStatus, PreparedQueryStatus
from mc2p.motion_nav.retry_ledger import RetryLedger
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.support_surfaces import SurfaceNodeId, SupportSurface, HorizontalRegion
from tests.motion_nav.test_d102_receiver import QueuedWorker
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav import test_b10_motion_candidate as candidate_fixtures


class RecordingProcessWorker(MotionSolverWorker):
    """Observe the existing worker boundary; submit/poll still use real IPC."""
    def __init__(self):
        self.jobs, self.messages = [], []
        super().__init__()

    def submit(self, job):
        accepted = super().submit(job)
        if accepted:
            self.jobs.append(job)
        return accepted

    def poll_available(self):
        messages = super().poll_available()
        self.messages.extend(messages)
        return messages


def fixed_case(direction_index=0):
    from scripts import d120_momentum_entry_calibration as calibration
    request, fixture, result = _d120_first_jump_proof(direction_index=direction_index)
    proof = result.proof
    current, commands, landed = proof.exit_state, [], False
    for index in range(40):
        control = TickInput(0. if landed else 1., 0., index == 0,
                            False, not landed, current.yaw_radians)
        calculated = step(current, control, fixture.context.world, JAVA_1_21_RULESET)
        current = calculated.next_state
        commands.append(MotionCommandTick(MovementV1(
            int(control.forward), 0, control.jump, False, control.sprint), control.movement_yaw_radians))
        landed |= "landed" in calculated.events
        if landed and math.hypot(current.velocity_blocks_per_tick[0], current.velocity_blocks_per_tick[2]) <= 1.e-9:
            break
    source = fixture.context.world._world._owner
    corners = tuple(calibration.frozen._local_to_world(fixture.context.origin, direction_index, x, z)
                    for x in (.3, .7) for z in (6.3, 11.7))
    landing = LandingRegion(min(x for x, _ in corners), max(x for x, _ in corners),
                            min(z for _, z in corners), max(z for _, z in corners), 64.)
    snapshot = seal_snapshot_version(source.borrow_snapshot_version(
        PhysicsWorldBounds(-14, 14, 60, 70, -14, 14), ruleset=JAVA_1_21_RULESET), JAVA_1_21_RULESET)
    spec = motion_chain.FixedSuccessorSpec(tuple(commands), MotionSolveKind.JUMP_GAP,
        request.direction, landing,
        request.fallback_candidates, 40, PreparedScanBudget(65536, 65536, 1))
    return replace(request, fixed_successor_spec=spec), fixture, proof, spec, snapshot


class SuccessorBindingContractTests(unittest.TestCase):
    def test_fixed_successor_has_a_typed_declaration_and_binding(self):
        self.assertTrue(hasattr(motion_chain, "FixedSuccessorSpec"),
                        "fixed successor input declaration is missing")
        self.assertTrue(hasattr(motion_chain, "SuccessorFeasibilityBinding"),
                        "compact successor feasibility binding is missing")

    def test_fixed_binding_generation_uses_a_production_entry(self):
        self.assertTrue(callable(getattr(motion_solver, "bind_fixed_successor", None)),
                        "fixed replay cannot produce a compact binding")

    def test_preparation_can_register_fixed_inputs_without_starting_incumbent(self):
        self.assertTrue(callable(getattr(MotionRouteCoordinator,
                                       "prepare_fixed_successor", None)),
                        "preparation still requires an executed incumbent or successor SOLVE")


class FixedReplayTests(unittest.TestCase):
    def test_fixed_replay_creates_a_binding_without_successor_search_or_full_proof(self):
        request, fixture, proof, spec, snapshot = fixed_case()
        with patch.object(motion_solver, "_candidate_templates", side_effect=AssertionError("candidate search")), \
             patch.object(motion_solver, "_release_recovery_evidence", side_effect=AssertionError("full successor proof")):
            result = motion_solver.bind_fixed_successor(proof, request, spec, snapshot,
                                                        damage_remaining_points=0.)
        self.assertIs(result.status, SolveStatus.SOLVED)
        binding = result.proof.successor_binding
        self.assertEqual(binding.entry_state, proof.exit_state)
        self.assertEqual(binding.requested_window.earliest_start_tick, 27)
        self.assertEqual(binding.requested_window.latest_start_tick, 27)
        self.assertEqual(binding.physics_steps, len(spec.commands))
        self.assertIs(binding.spec, spec)
        self.assertEqual(result.proof.fallback_tails, proof.fallback_tails)
        self.assertEqual(result.proof.world_dependencies, proof.world_dependencies)
        self.assertFalse(hasattr(binding, "trajectory"))

    def test_budget_and_bad_fixed_target_do_not_create_a_binding(self):
        request, _, proof, spec, snapshot = fixed_case()
        for changed, status in ((replace(spec, maximum_replay_steps=1), SolveStatus.BUDGET_EXHAUSTED),
                                (replace(spec, landing=LandingRegion(.3, .7, 20.3, 21.7, 64.)), SolveStatus.NEEDS_STATE)):
            with self.subTest(status=status):
                result = motion_solver.bind_fixed_successor(proof,
                    replace(request, fixed_successor_spec=changed), changed, snapshot,
                    damage_remaining_points=0.)
                self.assertIs(result.status, status)
                self.assertIsNone(result.proof)

    def test_parent_and_binding_mutations_cannot_be_attached(self):
        request, _, proof, spec, snapshot = fixed_case()
        result = motion_solver.bind_fixed_successor(proof, request, spec, snapshot,
            damage_remaining_points=0.)
        binding = result.proof.successor_binding
        for changes in ({"parent_main_digest": "0" * 64}, {"exit_fallback_digest": "0" * 64},
                        {"first_start_tick": 2}, {"exit_boundary_index": 25},
                        {"entry_state": replace(binding.entry_state, position=(.5, 64., 1.5))},
                        {"input_projection_version": "wrong-projection"}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(ContractViolation):
                replace(proof, successor_binding=replace(binding, **changes))
        for changed_request in (replace(request, direction=(1, 0)),
                                replace(request, fixed_successor_spec=replace(spec, maximum_replay_steps=39))):
            with self.assertRaises(ContractViolation):
                motion_solver.validate_fixed_successor_parent(result.proof, changed_request,
                                                              changed_request.fixed_successor_spec)

    def test_changed_fixed_input_digest_is_rejected(self):
        request, _, proof, spec, snapshot = fixed_case()
        result = motion_solver.bind_fixed_successor(proof, request, spec, snapshot,
            damage_remaining_points=0.)
        binding = result.proof.successor_binding
        with self.assertRaises(ContractViolation):
            replace(binding, input_digest="0" * 64)

    def test_independent_parent_exit_and_damage_declarations_cannot_change(self):
        request, _, proof, spec, snapshot = fixed_case()
        for changed in (replace(request, exit_direction=(0, 1), exit_motion_ticks=1),
                        replace(request, recovery_horizon_ticks=39),
                        replace(request, damage_budget=TaskDamageBudget(maximum_expected_damage_points=1.))):
            with self.subTest(request=changed.exit_direction), self.assertRaises(ContractViolation):
                motion_solver.bind_fixed_successor(proof, changed, spec, snapshot,
                    damage_remaining_points=0.)

    def test_parent_dependency_versions_cannot_be_deleted_added_or_replaced(self):
        from tests.motion_nav.test_d102_receiver import unchecked_replace
        request, _, proof, spec, snapshot = fixed_case()
        bound = motion_solver.bind_fixed_successor(proof, request, spec, snapshot,
            damage_remaining_points=0.).proof
        binding = bound.successor_binding
        versions = binding.parent_dependency_versions
        self.assertEqual(versions, bound.world_dependency_versions)
        self.assertEqual(versions, tuple((pos, dict(snapshot.dependency_versions)[pos])
                                        for pos in proof.world_dependencies))
        mutations = {
            "empty": (), "missing_cell": versions[1:],
            "extra_cell": tuple(sorted((*versions, ((99, 99, 99), 0)))),
            "changed_version": ((versions[0][0], versions[0][1] + 1), *versions[1:]),
        }
        for name, changed in mutations.items():
            altered = replace(binding, parent_dependency_versions=changed)
            with self.subTest(mutation=name, gate="parent_constructor"), self.assertRaises(ContractViolation):
                replace(bound, successor_binding=altered)
            with self.subTest(mutation=name, gate="direct_parent_check"), self.assertRaises(ContractViolation):
                motion_solver._validate_bound_parent(bound, altered)
            with self.subTest(mutation=name, gate="independent_request_check"), self.assertRaises(ContractViolation):
                motion_solver.validate_fixed_successor_parent(
                    unchecked_replace(bound, successor_binding=altered), request, spec)
        changed = mutations["changed_version"]
        with self.assertRaises(ContractViolation):
            replace(bound, world_dependency_versions=changed,
                    successor_binding=replace(binding, parent_dependency_versions=changed))

    def test_feasibility_uses_dependency_versions_without_an_unchecked_snapshot_digest(self):
        request, _, proof, spec, snapshot = fixed_case()
        bound = motion_solver.bind_fixed_successor(proof, request, spec, snapshot,
            damage_remaining_points=0.).proof
        self.assertNotIn("snapshot_digest", {field.name for field in fields(bound.successor_binding)})
        with self.assertRaises(ContractViolation):
            replace(bound.successor_binding, dependency_digest="0" * 64)

    def test_unknown_current_support_and_successor_only_support_are_rejected(self):
        from mc2p.motion_nav.world_model import UNKNOWN_CELL
        from tests.motion_nav.test_d121_momentum_handoff import _snapshot_with_change
        request, _, proof, spec, snapshot = fixed_case()
        blocks = dict(snapshot.facts)
        current = next(pos for pos in proof.world_dependencies if blocks[pos].block is not None)
        successor = (0, 63, 6)
        self.assertNotIn(successor, proof.world_dependencies)
        for position in (current, successor):
            with self.subTest(position=position):
                changed = _snapshot_with_change(snapshot, position, UNKNOWN_CELL)
                denied = motion_solver.bind_fixed_successor(proof, request, spec, changed,
                    damage_remaining_points=0.)
                self.assertIs(denied.status, SolveStatus.NEEDS_WORLD)


class FixedPreparationTests(unittest.TestCase):
    direction_index = 0
    def setUp(self):
        self.request, self.fixture, self.proof, self.spec, self.snapshot = fixed_case(self.direction_index)
        scope = AsyncComputationScope(self.proof.entry_state.session.value, "fixed-task", 1)
        self.identity = AsyncWorkIdentity(scope, "fixed-owner", AsyncWorkKind.MOTION_SOLVE, "fixed-current", 1)
        self.anchor = StateAnchor(self.proof.entry_state.session, 0, 0, MotionTickPhase.AFTER_MOVEMENT,
            None, None, self.proof.ruleset_id, self.proof.entry_state.state_schema,
            self.proof.input_projection_version, self.proof.entry_state)
        surfaces = tuple(SupportSurface(SurfaceNodeId(0, z, 64, 0), (.5, 64., z + .5),
            HorizontalRegion(0, z, 1, z + 1), 1., ("minecraft:grass_block",), ()) for z in (-1, 1, 6))
        actions = tuple(JumpGapSegment(JumpGapEdge(a.node_id, b.node_id, "fixed-gap", .9, ()),
            a, b, ()) for a, b in zip(surfaces, surfaces[1:]))
        self.route = ActiveRoute("fixed-route", 1, "fixed-request", "fixed-goal", 1,
            scope.world_session_id, None, 6., 0., (), ExecutableCorridor(
                tuple(s.node_id for s in surfaces), (), 6., surfaces[-1].node_id), ActionRoute("fixed-route", actions))
        self.context = MotionCandidateContext("fixed-request", 0, "fixed-goal", 1,
            "fixed-route", 1, 0, 1, TaskDamageBudget(), self.proof.resource_incomplete_reasons)
        self.worker = getattr(self, "worker_factory", QueuedWorker)()
        self.source = self.fixture.context.world._world._owner
        self.inbox = MotionResultInbox(worker=self.worker, world=self.source)
        self.addCleanup(self.inbox.close)
        self.executor = ActionRouteExecutor(ground_profile(), jump_profile())
        self.coordinator = MotionRouteCoordinator(self.route, self.executor, self.worker,
            result_inbox=self.inbox, retry_ledger=RetryLedger("fixed-task"), computation_scope=scope,
            owner_instance_id="fixed-owner", clock_ns=lambda: 1)
        frame = candidate_fixtures.VerifiedMotionRouteIntegrationTests.frame(self.source.view(), self.proof.entry_state, 0)
        self.coordinator.start(frame)

    def submit(self):
        bound = motion_solver.bind_fixed_successor(self.proof, self.request, self.spec,
                                                   self.snapshot, damage_remaining_points=0.)
        result = GapMotionSolveResult("fixed-current", 1, bound, 0, self.identity)
        submitted = self.coordinator.prepare_fixed_successor(result, self.request, self.context,
            self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
            expected_work_identity=self.identity)
        self.assertIs(submitted.status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(self.worker.submitted.wait(10))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        return self.worker.jobs[-1]

    def test_prepared_suffix_rejects_a_widened_window_or_changed_fallbacks(self):
        from mc2p.motion_nav.online_motion import CandidateExecutionWindow
        job = self.submit()
        window = CandidateExecutionWindow(27, 28)
        with self.assertRaises(ContractViolation):
            replace(job.request, stream=replace(job.request.stream, requested_window=window),
                    requested_window=window)
        with self.assertRaises(ContractViolation):
            replace(job.request, fallback_candidates=(motion_chain.preserve_current_neutral_fallback(),))

    def test_restart_and_missed_landing_remove_fixed_preparation_context(self):
        self.submit()
        late = replace(self.anchor, movement_tick_id=28,
                       physics_state=replace(self.proof.exit_state, movement_tick_id=28))
        self.coordinator.fixed_successor_eligibility(late, self.fixture.context.world,
            current_scope=self.identity.scope, ledger_identity="fixed-ledger", owner_id="route/fixed-route")
        self.assertIsNone(self.coordinator.generation_for(1))

    def test_nonzero_balance_is_reserved_once_and_prior_spend_is_respected(self):
        budget = TaskDamageBudget(maximum_expected_damage_points=3.)
        # Parent proof reserves one point; one point was already spent before it.
        proof = replace(self.proof, damage_budget=budget, maximum_expected_damage_points=1.)
        request = replace(self.request, damage_budget=budget)
        result = motion_solver.bind_fixed_successor(proof, request, self.spec,
            self.snapshot, damage_remaining_points=2.)
        self.assertIs(result.status, SolveStatus.SOLVED)
        self.assertEqual(result.proof.successor_binding.damage_remaining_points, 1.)
        denied = motion_solver.bind_fixed_successor(proof, request, self.spec,
            self.snapshot, damage_remaining_points=.5)
        self.assertIs(denied.status, SolveStatus.HARD_CONFLICT)

    def test_formal_messages_fit_and_neither_direction_duplicates_a_full_proof(self):
        from mc2p.motion_nav.prepared_motion_proof import canonical_motion_bytes, canonical_bundle_bytes
        from mc2p.motion_nav.motion_solver import VerifiedMotionResult
        from mc2p.motion_nav.prepared_motion import PreparedMotionProof
        job = self.submit()
        message = _execute_job(job)
        bundle = pickle.loads(message.payload)
        sizes = {"job_canonical": len(canonical_motion_bytes(job)),
                 "job_pickle5": len(pickle.dumps(job, protocol=5)),
                 "bundle_canonical": len(canonical_bundle_bytes(bundle)),
                 "wire_payload": len(message.payload)}
        for name, size in sizes.items():
            with self.subTest(name=name):
                self.assertLessEqual(size, 1048576)
        def contains(value, forbidden):
            if isinstance(value, forbidden):
                return True
            if is_dataclass(value):
                return any(contains(getattr(value, field.name), forbidden) for field in fields(value))
            return type(value) is tuple and any(contains(item, forbidden) for item in value)
        self.assertFalse(contains(job, VerifiedMotionResult))
        self.assertFalse(contains(bundle, VerifiedMotionResult))
        bound = motion_solver.bind_fixed_successor(self.proof, self.request, self.spec,
            self.snapshot, damage_remaining_points=0.)
        self.assertFalse(contains(bound.proof.successor_binding, PreparedMotionProof))

    def landing_anchor(self, tick=26):
        return replace(self.anchor, movement_tick_id=tick,
                       physics_state=replace(self.proof.exit_state, movement_tick_id=tick))

    def eligibility(self, tick=26, **changes):
        arguments = dict(current_scope=self.identity.scope, ledger_identity="fixed-ledger",
                         owner_id="route/fixed-route")
        arguments.update(changes)
        return self.coordinator.fixed_successor_eligibility(self.landing_anchor(tick),
            self.fixture.context.world, **arguments)

    def test_ready_before_l_is_eligible_only_at_the_exact_l_without_authority(self):
        job = self.submit()
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertTrue(self.coordinator.apply_fixed_successor_preparation(
            movement_tick=10, current_scope=self.identity.scope))
        self.assertIs(self.eligibility(25), PreparedQueryStatus.TIMING_MISMATCH)
        self.assertIs(self.eligibility(), PreparedQueryStatus.READY_MATCH)
        self.assertEqual(self.executor.action_index, 0)
        self.assertIsNone(self.executor.prepared_controller)
        self.assertIsNotNone(self.coordinator.generation_for(1))

    def test_no_ready_at_l_and_late_results_cannot_restore_this_opportunity(self):
        for lateness in (2, 5, 10):
            with self.subTest(lateness=lateness):
                case = FixedPreparationTests()
                try:
                    case.setUp()
                    job = case.submit()
                    message = _execute_job(job)
                    self.assertIs(case.eligibility(), PreparedQueryStatus.NOT_READY)
                    case.inbox.publish_diagnostic_movement_tick(26 + lateness)
                    case.worker.put(message)
                    self.assertTrue(case.inbox.wait_for_receiver(timeout=10))
                    self.assertFalse(case.coordinator.apply_fixed_successor_preparation(
                        movement_tick=26 + lateness, current_scope=case.identity.scope))
                    self.assertIs(case.eligibility(26 + lateness), PreparedQueryStatus.NOT_READY)
                    self.assertEqual(case.inbox.registered_handles, ())
                    self.assertEqual(case.executor.action_index, 0)
                    self.assertIsNone(case.executor.prepared_controller)
                finally:
                    case.doCleanups()

    def test_never_return_and_worker_death_remain_non_eligible(self):
        self.submit()
        self.worker.alive = False
        self.assertFalse(self.coordinator.apply_fixed_successor_preparation(
            movement_tick=5, current_scope=self.identity.scope))
        self.assertIs(self.eligibility(), PreparedQueryStatus.NOT_READY)
        self.assertEqual(self.executor.action_index, 0)

    def test_live_worker_that_never_returns_loses_eligibility_at_l(self):
        self.submit()
        self.assertTrue(self.worker.is_alive())
        self.assertIs(self.eligibility(), PreparedQueryStatus.NOT_READY)
        self.assertIsNone(self.coordinator.generation_for(1))
        self.assertEqual(self.inbox.registered_handles, ())
        self.assertEqual(self.executor.action_index, 0)

    def test_actual_receiver_capacity_rejection_cleans_the_same_lifecycle(self):
        bound = motion_solver.bind_fixed_successor(self.proof, self.request, self.spec,
            self.snapshot, damage_remaining_points=0.)
        result = GapMotionSolveResult("fixed-current", 1, bound, 0, self.identity)
        with patch.object(self.inbox, "MAX_UNFINISHED", 0):
            submitted = self.coordinator.prepare_fixed_successor(result, self.request, self.context,
                self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
                expected_work_identity=self.identity)
        self.assertIs(submitted.status, PreparedSubmitStatus.QUEUE_FULL)
        self.assertIsNone(self.coordinator.generation_for(1))
        self.assertIsNone(self.coordinator._work_identity)
        self.assertEqual(self.worker.jobs, [])

    def test_query_and_apply_do_not_hash_or_rebuild_proofs(self):
        job = self.submit()
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        with patch("mc2p.motion_nav.prepared_motion_proof.canonical_motion_digest", side_effect=AssertionError("hot digest")), \
             patch.object(motion_solver.VerifiedMotionResult, "__post_init__", side_effect=AssertionError("hot proof")), \
             patch.object(motion_solver, "main_suffix_digest", side_effect=AssertionError("hot suffix")):
            self.assertTrue(self.coordinator.apply_fixed_successor_preparation(
                movement_tick=5, current_scope=self.identity.scope))
            self.assertIs(self.eligibility(), PreparedQueryStatus.READY_MATCH)

    def test_identity_and_candidate_mutations_are_rejected_before_preparation(self):
        bound = motion_solver.bind_fixed_successor(self.proof, self.request, self.spec,
            self.snapshot, damage_remaining_points=0.)
        result = GapMotionSolveResult("fixed-current", 1, bound, 0, self.identity)
        for context in (replace(self.context, candidate_revision=2), replace(self.context, route_revision=2),
                        replace(self.context, action_index=1), replace(self.context, planning_generation=1)):
            with self.subTest(context=context), self.assertRaises(ContractViolation):
                self.coordinator.prepare_fixed_successor(result, self.request, context,
                    self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
                    expected_work_identity=self.identity)
        for identity in (replace(self.identity, owner_instance_id="wrong-owner"),
                         replace(self.identity, scope=replace(self.identity.scope, generation=2))):
            with self.subTest(identity=identity), self.assertRaises(ContractViolation):
                self.coordinator.prepare_fixed_successor(replace(result, work_identity=identity), self.request, self.context,
                    self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
                    expected_work_identity=identity)
        self.assertEqual(self.worker.jobs, [])

    def test_complete_prepare_budget_failure_does_not_register_or_authorize(self):
        self.spec = replace(self.spec, scan_budget=PreparedScanBudget(1, 1, 1))
        self.request = replace(self.request, fixed_successor_spec=self.spec)
        job = self.submit()
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertEqual(self.inbox.prepared_compute_result(job.request.async_identity).status.value,
                         "budget_exhausted")
        self.assertIs(self.eligibility(), PreparedQueryStatus.NOT_READY)
        self.assertEqual(self.inbox.registered_handles, ())

    def test_wrong_ledger_at_l_revokes_the_candidate_without_touching_parent_tail(self):
        original_tail = self.proof.fallback_tails[-1]
        job = self.submit()
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertTrue(self.coordinator.apply_fixed_successor_preparation(
            movement_tick=5, current_scope=self.identity.scope))
        self.assertIs(self.eligibility(ledger_identity="wrong-ledger"), PreparedQueryStatus.ENTRY_MISMATCH)
        self.assertEqual(self.inbox.registered_handles, ())
        self.assertIs(self.proof.fallback_tails[-1], original_tail)
        self.assertEqual(self.executor.action_index, 0)

    def test_stamp_only_refresh_keeps_semantic_dependencies_eligible(self):
        from scripts import d120_momentum_entry_calibration as calibration
        from mc2p.motion_nav.world_model import ObservationStamp
        # The D120 builder caches an immutable view of its source sections.
        # Do not let this source-refresh case leak an expired view to others.
        self.addCleanup(calibration.build_fixture.cache_clear)
        job = self.submit()
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertTrue(self.coordinator.apply_fixed_successor_preparation(
            movement_tick=5, current_scope=self.identity.scope))
        position = next(pos for pos, _ in job.request.inherited_dependencies
                        if self.source.view().cell(pos).block is None)
        self.source.confirm_air(ObservationStamp(self.proof.entry_state.session,
            100, 100, "d121-stamp-only", 100), (position,))
        self.assertIs(self.eligibility(), PreparedQueryStatus.READY_MATCH)

    def test_successor_only_change_revokes_eligibility_with_parent_dependencies_intact(self):
        from scripts import d120_momentum_entry_calibration as calibration
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        self.addCleanup(calibration.build_fixture.cache_clear)
        job = self.submit()
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertTrue(self.coordinator.apply_fixed_successor_preparation(
            movement_tick=5, current_scope=self.identity.scope))
        reference = job.request.stream.suffix_identity[0]
        position = (0, 63, 6)
        self.assertNotIn(position, self.proof.world_dependencies)
        self.source.observe_blocks(ObservationStamp(self.proof.entry_state.session,
            101, 101, "d121-successor-only-change", 101),
            {position: BlockGeometry.full_cube("minecraft:stone")})
        self.assertIs(self.source.dependency_changes(reference.parent_dependency_versions).status,
                      DependencyStatus.UNCHANGED)
        self.assertIs(self.eligibility(), PreparedQueryStatus.ENTRY_MISMATCH)
        self.assertEqual(self.executor.action_index, 0)

    def ready_job(self):
        job = self.submit()
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertTrue(self.coordinator.apply_fixed_successor_preparation(
            movement_tick=5, current_scope=self.identity.scope))
        return job

    def tail_only_dependency(self, job):
        handle = self.inbox.query_prepared(job.request.async_identity, movement_tick=5).handle
        self.assertIsNotNone(handle)
        reference = job.request.stream.suffix_identity[0]
        inherited = {pos for pos, _ in reference.dependency_versions}
        parent = {pos for pos, _ in reference.parent_dependency_versions}
        complete = {pos for pos, _ in handle.hot_view.dependency_versions}
        candidates = sorted(complete - inherited - parent)
        self.assertTrue(candidates, "fixture must have a dependency used only by a complete safety tail")
        return next(pos for pos in candidates if self.source.view().cell(pos).block is None), handle

    def assert_tail_only_change_rejected(self, *, unknown):
        from scripts import d120_momentum_entry_calibration as calibration
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        self.addCleanup(calibration.build_fixture.cache_clear)
        job = self.ready_job()
        position, handle = self.tail_only_dependency(job)
        stamp = ObservationStamp(self.proof.entry_state.session, 102, 102, "d121-tail-only-change", 102)
        if unknown:
            self.source.invalidate(stamp, (position,))
        else:
            self.source.observe_blocks(stamp, {position: BlockGeometry.full_cube("minecraft:stone")})
        self.assertIs(self.source.dependency_changes(job.request.inherited_dependencies).status,
                      DependencyStatus.UNCHANGED)
        self.assertIs(self.source.dependency_changes(handle.hot_view.dependency_versions).status,
                      DependencyStatus.CHANGED)
        self.assertIs(self.eligibility(), PreparedQueryStatus.DEPENDENCY_CHANGED)
        self.assertIsNone(self.coordinator.generation_for(1))
        self.assertEqual(self.inbox.registered_handles, ())
        self.assertEqual(self.executor.action_index, 0)
        self.assertIsNone(self.executor.prepared_controller)

    def test_tail_only_changed_dependency_revokes_full_candidate(self):
        self.assert_tail_only_change_rejected(unknown=False)

    def test_tail_only_unknown_dependency_revokes_full_candidate(self):
        self.assert_tail_only_change_rejected(unknown=True)

    def test_tail_only_stamp_refresh_keeps_full_candidate_eligible(self):
        from scripts import d120_momentum_entry_calibration as calibration
        from mc2p.motion_nav.world_model import ObservationStamp
        self.addCleanup(calibration.build_fixture.cache_clear)
        job = self.ready_job()
        position, _ = self.tail_only_dependency(job)
        self.source.confirm_air(ObservationStamp(self.proof.entry_state.session,
            103, 103, "d121-tail-only-stamp", 103), (position,))
        self.assertIs(self.eligibility(), PreparedQueryStatus.READY_MATCH)

    def assert_anchor_identity_rejected(self, name, value):
        self.ready_job()
        anchor = replace(self.landing_anchor(), **{name: value})
        status = self.coordinator.fixed_successor_eligibility(anchor, self.fixture.context.world,
            current_scope=self.identity.scope, ledger_identity="fixed-ledger", owner_id="route/fixed-route")
        self.assertIs(status, PreparedQueryStatus.ENTRY_MISMATCH)
        self.assertIsNone(self.coordinator.generation_for(1))
        self.assertEqual(self.inbox.registered_handles, ())

    def test_l_anchor_projection_identity_is_required(self):
        self.assert_anchor_identity_rejected("input_projection_version", "wrong-projection")

    def test_l_anchor_ruleset_identity_is_required(self):
        self.assert_anchor_identity_rejected("ruleset_id", "wrong-ruleset")

    def test_l_anchor_state_schema_is_required(self):
        self.assert_anchor_identity_rejected("state_schema", "wrong-schema")

    def test_l_anchor_session_is_required(self):
        from mc2p.motion_nav.world_model import WorldSessionId
        self.assert_anchor_identity_rejected("session", WorldSessionId("wrong-session"))

    def test_l_world_ruleset_identity_is_required(self):
        from mc2p.motion_nav.physics_adapter import PhysicsWorldView
        self.ready_job()
        world = PhysicsWorldView(self.source.view(), replace(JAVA_1_21_RULESET, ruleset_id="wrong-world-ruleset"))
        status = self.coordinator.fixed_successor_eligibility(self.landing_anchor(), world,
            current_scope=self.identity.scope, ledger_identity="fixed-ledger", owner_id="route/fixed-route")
        self.assertIs(status, PreparedQueryStatus.ENTRY_MISMATCH)

    def test_parent_only_dependency_change_is_rejected_before_prepare(self):
        from scripts import d120_momentum_entry_calibration as calibration
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        self.addCleanup(calibration.build_fixture.cache_clear)
        bound = motion_solver.bind_fixed_successor(self.proof, self.request, self.spec,
            self.snapshot, damage_remaining_points=0.)
        binding = bound.proof.successor_binding
        successor = {pos for pos, _ in binding.dependency_versions}
        position = next(pos for pos, _ in binding.parent_dependency_versions
                        if pos not in successor and self.source.view().cell(pos).block is None)
        self.source.observe_blocks(ObservationStamp(self.proof.entry_state.session,
            104, 104, "d121-parent-only-change", 104), {position: BlockGeometry.full_cube("minecraft:stone")})
        result = GapMotionSolveResult("fixed-current", 1, bound, 0, self.identity)
        with self.assertRaises(ContractViolation):
            self.coordinator.prepare_fixed_successor(result, self.request, self.context,
                self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
                expected_work_identity=self.identity)
        self.assertEqual(self.worker.jobs, [])

    def parent_only_bound_and_position(self):
        bound = motion_solver.bind_fixed_successor(self.proof, self.request, self.spec,
            self.snapshot, damage_remaining_points=0.)
        binding = bound.proof.successor_binding
        successor = {pos for pos, _ in binding.dependency_versions}
        position = next(pos for pos, _ in binding.parent_dependency_versions
                        if pos not in successor and self.source.view().cell(pos).block is None)
        return bound, position

    def test_parent_only_unknown_dependency_is_rejected_before_prepare(self):
        from scripts import d120_momentum_entry_calibration as calibration
        from mc2p.motion_nav.world_model import ObservationStamp
        self.addCleanup(calibration.build_fixture.cache_clear)
        bound, position = self.parent_only_bound_and_position()
        self.source.invalidate(ObservationStamp(self.proof.entry_state.session,
            105, 105, "d124-parent-only-unknown", 105), (position,))
        result = GapMotionSolveResult("fixed-current", 1, bound, 0, self.identity)
        with self.assertRaises(ContractViolation):
            self.coordinator.prepare_fixed_successor(result, self.request, self.context,
                self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
                expected_work_identity=self.identity)
        self.assertEqual(self.worker.jobs, [])

    def test_parent_only_stamp_refresh_remains_eligible_before_prepare(self):
        from scripts import d120_momentum_entry_calibration as calibration
        from mc2p.motion_nav.world_model import ObservationStamp
        self.addCleanup(calibration.build_fixture.cache_clear)
        bound, position = self.parent_only_bound_and_position()
        self.source.confirm_air(ObservationStamp(self.proof.entry_state.session,
            106, 106, "d124-parent-only-stamp", 106), (position,))
        result = GapMotionSolveResult("fixed-current", 1, bound, 0, self.identity)
        submitted = self.coordinator.prepare_fixed_successor(result, self.request, self.context,
            self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
            expected_work_identity=self.identity)
        self.assertIs(submitted.status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(self.worker.submitted.wait(10))
        self.assertEqual(len(self.worker.jobs), 1)

    def test_worker_death_does_not_invalidate_an_already_applied_local_proof(self):
        self.ready_job()
        self.worker.alive = False
        self.assertIs(self.eligibility(), PreparedQueryStatus.READY_MATCH)

    def test_actual_process_worker_crosses_request_result_and_registry(self):
        case = FixedPreparationTests()
        case.worker_factory = RecordingProcessWorker
        try:
            case.setUp()
            case.addCleanup(case.worker.close)
            case.inbox.publish_diagnostic_movement_tick(0)
            bound = motion_solver.bind_fixed_successor(case.proof, case.request, case.spec,
                case.snapshot, damage_remaining_points=0.)
            result = GapMotionSolveResult("fixed-current", 1, bound, 0, case.identity)
            submitted = case.coordinator.prepare_fixed_successor(result, case.request, case.context,
                case.anchor, case.fixture.context.world, ledger_identity="fixed-ledger",
                expected_work_identity=case.identity)
            self.assertIs(submitted.status, PreparedSubmitStatus.ACCEPTED)
            self.assertTrue(self.wait_ready(case))
            self.assertTrue(case.coordinator.apply_fixed_successor_preparation(
                movement_tick=5, current_scope=case.identity.scope))
            self.assertIs(case.eligibility(), PreparedQueryStatus.READY_MATCH)
            self.assertEqual(len(case.worker.jobs), 1)
            self.assertEqual(len(case.worker.messages), 1)
        finally:
            case.doCleanups()

    @staticmethod
    def wait_ready(case):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            fields = case.coordinator._prepared_fields
            if fields is not None and case.inbox.query_prepared(fields.async_identity, movement_tick=0).handle is not None:
                return True
            time.sleep(.01)
        return False

    def test_real_preparation_registers_and_applies_without_starting_either_action(self):
        bound = motion_solver.bind_fixed_successor(self.proof, self.request, self.spec,
                                                   self.snapshot, damage_remaining_points=0.)
        result = GapMotionSolveResult("fixed-current", 1, bound, 0, self.identity)
        submitted = self.coordinator.prepare_fixed_successor(result, self.request, self.context,
            self.anchor, self.fixture.context.world, ledger_identity="fixed-ledger",
            expected_work_identity=self.identity)
        self.assertIs(submitted.status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(self.worker.submitted.wait(10))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        job = self.worker.jobs[-1]
        self.assertIs(type(job), PreparedMotionJob)
        self.assertEqual(job.request.requested_window.earliest_start_tick, 27)
        self.assertEqual(job.request.requested_window.latest_start_tick, 27)
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertIs(self.inbox.query_prepared(job.request.async_identity,
            movement_tick=0).status, PreparedQueryStatus.READY_MATCH)
        self.assertTrue(self.coordinator.apply_fixed_successor_preparation(
            movement_tick=0, current_scope=self.identity.scope))
        self.assertEqual(self.executor.action_index, 0)
        self.assertIsNone(self.executor.prepared_controller)
        self.assertEqual(self.executor.completed_movement_damage_points, 0.)
        self.assertEqual(self.executor.selected_motion(0), None)


class FourDirectionPreparationTests(unittest.TestCase):
    def test_fixed_generation_real_prepare_and_exact_windows_in_four_directions(self):
        for direction_index in range(4):
            with self.subTest(direction_index=direction_index):
                case = FixedPreparationTests("test_real_preparation_registers_and_applies_without_starting_either_action")
                case.direction_index = direction_index
                try:
                    case.setUp()
                    case.test_real_preparation_registers_and_applies_without_starting_either_action()
                finally:
                    case.doCleanups()


if __name__ == "__main__":
    unittest.main()
