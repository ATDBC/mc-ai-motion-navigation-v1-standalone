"""Same-owner preparation and approval never activate the next action.

The existing solver, fixed-input producer, ledger and registry are real. Only
the worker transport is queued so every handoff boundary is inspectable.
"""
from dataclasses import replace
import math
import unittest
import threading
from unittest.mock import patch

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementTickWindowV1, MovementV1
from mc2p.contracts.motion_activation import (
    ActivationResult, ActivationStatus, PreparedActivationDelivery,
    PreparedActivationTransaction, StreamActivationBinding,
)
from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.body_control import HandoffDisposition, HandoffEvidence
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, RoutePoint
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.motion_candidate import (
    AdmittedMotionCandidate, MotionCandidateContext, VerifiedMotionCandidate,
)
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion, solve_one_cell_gap
from mc2p.motion_nav.motion_worker import GapMotionSolveJob, MotionResultInbox, PreparedMotionJob, _execute_job
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, InputApplicationLedger, InputApplicationStatus,
    InputLedgerCapacityExceeded, MotionTickPhase, StateAnchor,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView
from mc2p.motion_nav.prepared_motion import (
    PreparedComputeStatus, PreparedCurrentFacts, PreparedMotionRequest,
    PreparedScanBudget, PreparedSubmitStatus, ProofUse, StreamGeneration,
)
from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest, prepare_fixed_motion, seal_bundle
from mc2p.motion_nav.retry_ledger import RetryLedger
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.support_surfaces import HorizontalRegion, SupportSurface, SurfaceNodeId
from mc2p.motion_nav.world_model import CellKnowledge, ObservationStamp, WorldKnowledge, WorldSessionId, WorldView
from mc2p.motion_nav.world_snapshot import seal_snapshot_version
from tests.motion_nav import test_b10_motion_candidate as candidate_fixtures
from tests.motion_nav.test_b10_online_motion import sample
from tests.motion_nav import test_d102_receiver as receiver_fixtures
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile
from mc2p.motion_nav import prepared_motion as values


class ActionHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        receiver_fixtures.ReceiverTests.setUpClass()
        base = receiver_fixtures.ReceiverTests.request
        cls.world = WorldKnowledge(base.entry_state.session)
        stamp = ObservationStamp(base.entry_state.session, 1, 1, "test", 0)
        cls.world.confirm_air(stamp, tuple(pos for pos, fact in receiver_fixtures.ReceiverTests.snapshot.facts
                                         if fact.knowledge is CellKnowledge.AIR))
        cls.world.observe_blocks(stamp, {pos: fact.block for pos, fact in receiver_fixtures.ReceiverTests.snapshot.facts
                                        if fact.block is not None})
        cls.bounds = PhysicsWorldBounds(-2, 2, -2, 6, -2, 7)
        version = cls.world.borrow_snapshot_version(cls.bounds, ruleset=base.ruleset)
        cls.snapshot = seal_snapshot_version(version, base.ruleset)
        if not cls.world.publish_sealed_snapshot(version, cls.snapshot):
            raise AssertionError("the formal world must publish the selected source version")
        cls.physics_world = PhysicsWorldView(WorldView.from_sealed_snapshot(cls.snapshot), base.ruleset)
        cls.proofs = []
        state = base.entry_state
        for index, z in enumerate((2, 4)):
            anchor = cls.anchor(state, index + 1)
            solved = solve_one_cell_gap(anchor, cls.physics_world, GapSolveRequest(
                (0, 1), LandingRegion(.3, .7, z + .3, z + .7, 1.),
                CandidateExecutionWindow(state.movement_tick_id + 1, state.movement_tick_id + 2)))
            if solved.proof is None:
                raise AssertionError("the existing solver must select each fixed input sequence")
            cls.proofs.append(solved.proof)
            state = solved.proof.exit_state
        cls.base = base

    @staticmethod
    def anchor(state, observation):
        return StateAnchor(state.session, observation, state.movement_tick_id,
                           MotionTickPhase.AFTER_MOVEMENT, None, None, state.ruleset_id,
                           state.state_schema, "mc2p.input-projection.v1", state)

    def frame(self, anchor):
        result = candidate_fixtures.VerifiedMotionRouteIntegrationTests.frame(self.world.view(), anchor.physics_state,
                                                         anchor.observation_sequence_id)
        return replace(result, body=replace(result.body, movement_tick_id=anchor.movement_tick_id))

    def setUp(self):
        self.assertTrue(callable(getattr(MotionRouteCoordinator, "prepare_action", None)),
                        "the coordinator has no independent fixed-input preparation boundary")
        # Each case owns its world/leases. close deliberately preserves body
        # responsibilities, so sharing a lease owner across cases is invalid.
        source = self.snapshot
        self.world = WorldKnowledge(self.base.entry_state.session)
        stamp = ObservationStamp(self.base.entry_state.session, 1, 1, "test", 0)
        self.world.confirm_air(stamp, tuple(pos for pos, fact in source.facts if fact.knowledge is CellKnowledge.AIR))
        self.world.observe_blocks(stamp, {pos: fact.block for pos, fact in source.facts if fact.block is not None})
        version = self.world.borrow_snapshot_version(self.bounds, ruleset=self.base.ruleset)
        self.snapshot = seal_snapshot_version(version, self.base.ruleset)
        self.assertTrue(self.world.publish_sealed_snapshot(version, self.snapshot))
        self.physics_world = PhysicsWorldView(self.world.view(), self.base.ruleset)
        self.worker = receiver_fixtures.QueuedWorker()
        self.inbox = MotionResultInbox(worker=self.worker, world=self.world)
        self.addCleanup(self.inbox.close)
        surfaces = []
        for z in (0, 2, 4):
            node = SurfaceNodeId(0, z, 1, 0)
            surfaces.append(SupportSurface(node, (.5, 1., z + .5),
                HorizontalRegion(0, z, 1, z + 1), 1., ("minecraft:stone",), ()))
        actions = tuple(JumpGapSegment(JumpGapEdge(a.node_id, b.node_id, "test-gap", .9, ()),
                        a, b, ()) for a, b in zip(surfaces, surfaces[1:]))
        route_id = "d102-handoff"
        self.route = ActiveRoute(route_id, 1, "request-handoff", "goal-handoff", 1,
            self.base.world_session_id, None, 4., 0., (),
            ExecutableCorridor(tuple(s.node_id for s in surfaces), (), 4., surfaces[-1].node_id),
            ActionRoute(route_id, actions))
        self.executor = ActionRouteExecutor(ground_profile(), jump_profile())
        self.coordinator = MotionRouteCoordinator(self.route, self.executor, self.worker,
            result_inbox=self.inbox, retry_ledger=RetryLedger("task-handoff"),
            computation_scope=AsyncComputationScope(self.route.world_session, "task-handoff", 1))
        self.current = self.anchor(self.proofs[0].entry_state, 1)
        self.ledger = InputApplicationLedger()
        self.ledger_id = "ledger-handoff"
        self.ledger.submit(self.current.session, ActionSnapshotV1("episode", 10, 1, 1_000_000),
                           requested_first_tick=self.current.movement_tick_id)
        self.ledger.observe_sample(sample(10, self.current.movement_tick_id, forward=0., sample_state="neutral"))
        self.coordinator.start(self.frame(self.current))
        for index, proof in enumerate(self.proofs):
            context = MotionCandidateContext(self.route.source_request_id, 0, self.route.goal_id, 1,
                route_id, 1, index, index + 1, TaskDamageBudget(), proof.resource_incomplete_reasons)
            self.executor.install_verified_motion(AdmittedMotionCandidate(
                VerifiedMotionCandidate(proof, context), proof.anchor_observation_sequence_id,
                proof.anchor_movement_tick_id, proof.execution_window.earliest_start_tick))
        self.control = RouteControl(self.route, self.executor, self.coordinator)
        self.supervisor = ExecutionSupervisor()
        self.supervisor.offer_route(self.control, self.frame(self.current), self.ledger, self.current)
        self.executor.decide(self.frame(self.current), state_anchor=self.current, input_ledger=self.ledger)

    def prepared_incumbent(self):
        proof = self.proofs[0]
        lease = self.world.acquire_published_snapshot(self.bounds, ruleset=self.base.ruleset).lease
        stream = StreamGeneration("stream-incumbent", self.control.owner_id, "request-incumbent",
            self.route.route_id, 0, proof.tick_inputs, (proof.entry_state,), proof.execution_window,
            ("known-stop-or-proved-landing",), self.base.stream.late_start_hold_input)
        request = PreparedMotionRequest("job-incumbent",
            AsyncWorkIdentity(self.coordinator.computation_scope, "computation-incumbent",
                              AsyncWorkKind.MOTION_SOLVE, stream.request_id, 1),
            stream, 1, 1, 1, proof.commands, proof.tick_inputs, canonical_motion_digest(proof.tick_inputs),
            proof.entry_state, proof.exit_state, proof.execution_window,
            proof.execution_window.earliest_start_tick, self.ledger_id, self.route.world_session, (),
            lease.identity, lease.lease_token, (), self.base.ruleset, TaskDamageBudget(), 0.,
            PreparedScanBudget(65_536, 65_536, 1), stream.suffix_identity)
        produced = prepare_fixed_motion(request, lease.snapshot)
        self.assertIs(produced.status, PreparedComputeStatus.PREPARED)
        facts = PreparedCurrentFacts(request.async_identity.scope, stream, stream.owner_id,
            request.world_session_id, request.ruleset, produced.bundle.proof.dependency_versions,
            self.ledger_id, lease.identity, self.current.physics_state, 0., self.current.movement_tick_id,
            request.requested_window)
        self.inbox.publish_current_facts(facts)
        self.inbox.register_preparation(request, lease)
        self.inbox.wait_for_receiver(timeout=10)
        self.worker.put(seal_bundle(produced.bundle))
        self.inbox.wait_for_receiver(timeout=10)
        handle = self.inbox.prepared_admission(request.async_identity).handle
        action = ActionSnapshotV1("episode", 1000, 1, 1_000_000,
                                  movement=proof.commands[0].movement)
        binding = StreamActivationBinding(handle.proof_token, handle.generation_id, handle.owner_id,
            self.ledger_id, request.world_session_id, 1000,
            MovementTickWindowV1(11, 12), 10, action)
        transaction = PreparedActivationTransaction("tx-incumbent", binding, None, action,
                                                    "permission-incumbent", "handoff-incumbent")
        handoff = HandoffEvidence(handle.owner_id, self.current.session, HandoffDisposition.TRANSFERABLE,
            1, 10, action.movement, "selected", handle.owner_id, 1000, 1, 0)
        accepted = self.inbox.accept_activation(handle, transaction, handoff)
        delivered = PreparedActivationDelivery(transaction.transaction_token, accepted.acceptance_token,
                                                binding, "delivery-incumbent", action)
        self.assertIs(self.inbox.commit_activation(accepted, delivered).status, ActivationStatus.ACTIVATED)
        self.inbox.retain_safe_tail(handle, action_index=0)
        self.inbox.retire(request.async_identity)
        return handle

    def prepare(self, *, commands=None, anchor=None):
        proof = self.executor.selected_motion(1) or self.proofs[1]
        entry = anchor or self.anchor(proof.entry_state, self.current.observation_sequence_id)
        result = self.coordinator.prepare_action(1, commands or proof.commands, entry,
                                                ledger_identity=self.ledger_id)
        self.assertIs(result.status, PreparedSubmitStatus.ACCEPTED)
        return result

    def wait_for_current_prepared_job(self, *, action_index, existing_job_ids=()):
        fields = self.coordinator._prepared_fields
        stream = self.coordinator.generation_for(action_index)
        self.assertIsNotNone(fields, "the coordinator did not retain the current preparation identity")
        self.assertIsNotNone(stream, "the coordinator did not retain the current stream generation")
        expected_identity = fields.async_identity
        expected_generation = stream.generation_id
        existing_job_ids = frozenset(existing_job_ids)
        for _ in range(5):
            self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
            matching = [job for job in self.worker.jobs
                        if isinstance(job, PreparedMotionJob)
                        and id(job) not in existing_job_ids
                        and job.request.async_identity == expected_identity
                        and job.request.stream.generation_id == expected_generation
                        and job.request.stream.action_index == action_index]
            if matching:
                self.assertEqual(len(matching), 1,
                                 "one preparation identity must submit exactly one worker job")
                return matching[0]
        observed = tuple((job.request.async_identity.key, job.request.stream.generation_id,
                          job.request.stream.action_index) for job in self.worker.jobs
                         if isinstance(job, PreparedMotionJob))
        self.fail("the receiver did not submit the current preparation within five cycles: "
                  f"expected={(expected_identity.key, expected_generation, action_index)!r}, "
                  f"observed={observed!r}")

    def ready_next(self):
        existing_job_ids = {id(job) for job in self.worker.jobs}
        self.prepare()
        job = self.wait_for_current_prepared_job(action_index=1, existing_job_ids=existing_job_ids)
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        admission = self.inbox.prepared_admission(job.request.async_identity)
        self.assertIsNotNone(admission)
        self.assertIsNotNone(admission.handle)
        self.executor.install_prepared_motion(admission.handle)
        return admission.handle

    def observe_first(self, *, stop_airborne=False):
        proof = self.proofs[0]
        for index, command in enumerate(proof.commands):
            decision = self.executor.decide(self.frame(self.current), state_anchor=self.current,
                                            input_ledger=self.ledger)
            tick = proof.execution_window.earliest_start_tick + index
            sequence = 20 + index
            if decision.verified_command_index is not None:
                self.executor.register_verified_submission(index, control_sequence=sequence,
                    requested_movement_tick=decision.expected_movement_tick,
                    requested_latest_movement_tick=decision.latest_movement_tick)
            action = ActionSnapshotV1("episode", sequence, self.current.observation_sequence_id,
                                      1_000_000, movement=command.movement)
            self.ledger.submit(self.current.session, action, requested_first_tick=tick)
            self.ledger.observe_sample(sample(sequence, tick, forward=float(command.movement.forward),
                strafe=float(command.movement.strafe), jump=command.movement.jump,
                sneak=command.movement.sneak, sprint=command.movement.sprint))
            self.current = replace(self.anchor(proof.trajectory[index + 1], index + 2),
                confirmed_control_sequence=sequence, confirmed_control_tick_range=(tick, tick))
            if stop_airborne and not self.current.physics_state.on_ground:
                self.executor.decide(self.frame(self.current), state_anchor=self.current, input_ledger=self.ledger)
                return
        return self.executor.decide(self.frame(self.current), state_anchor=self.current, input_ledger=self.ledger)

    def unchanged(self, handle):
        return (self.executor.action_index, self.control.owner_id,
                self.inbox.usage_active(handle, ProofUse.EXECUTING),
                self.inbox.usage_active(handle, ProofUse.SAFE_TAIL))

    def test_next_jump_prepares_while_first_is_airborne(self):
        self.observe_first(stop_airborne=True)
        self.assertFalse(self.current.physics_state.on_ground)
        self.prepare()
        stream = self.coordinator.generation_for(1)
        self.assertIsNotNone(stream)
        self.assertEqual(stream.entry_identity, (self.proofs[0].exit_state,))
        self.assertEqual(self.executor.action_index, 0)
        self.ready_next()
        self.assertFalse(self.current.physics_state.on_ground)

    def test_target_revision_alone_keeps_stream(self):
        self.prepare()
        original = self.coordinator.generation_for(1)
        self.coordinator.route = replace(self.route, goal_revision=2)
        self.prepare()
        self.assertIs(self.coordinator.generation_for(1), original)

    def test_inputs_entry_window_or_suffix_change_replaces_stream(self):
        for change in ("inputs", "window", "suffix", "dependencies"):
            with self.subTest(change=change):
                self.prepare()
                original = self.coordinator.generation_for(1)
                installed = self.executor._verified_motion[1]
                proof = installed.proof
                if change == "inputs":
                    commands = proof.commands[:-1] + (replace(proof.commands[-1],
                        movement=replace(proof.commands[-1].movement, sprint=True)),)
                    proof = replace(proof, commands=commands, tick_inputs=proof.tick_inputs[:-1] +
                                    (replace(proof.tick_inputs[-1], sprint=True),))
                elif change == "window":
                    proof = replace(proof, execution_window=CandidateExecutionWindow(
                        proof.execution_window.earliest_start_tick, proof.execution_window.earliest_start_tick),
                        delayed_start_variants=())
                elif change == "suffix":
                    proof = replace(proof, recovery_horizon_ticks=proof.recovery_horizon_ticks - 1)
                else:
                    proof = replace(proof, world_dependencies=tuple(sorted(set(proof.world_dependencies) | {(0, 6, 3)})))
                self.executor._verified_motion[1] = replace(installed,
                    candidate=replace(installed.candidate, proof=proof))
                existing_job_ids = {id(job) for job in self.worker.jobs}
                self.prepare(commands=proof.commands)
                self.assertNotEqual(self.coordinator.generation_for(1).generation_id, original.generation_id)
                job = self.wait_for_current_prepared_job(
                    action_index=1, existing_job_ids=existing_job_ids)
                self.worker.put(_execute_job(job))
                self.assertTrue(self.inbox.wait_for_receiver(timeout=10))

    def test_same_route_handoff_keeps_body_owner(self):
        incumbent = self.prepared_incumbent()
        handle = self.ready_next()
        boundary = self.observe_first()
        self.assertFalse(boundary.submit_input)
        before = self.unchanged(incumbent)
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        approval = self.supervisor.approve_action_handoff(self.control, proposal, self.frame(self.current), self.ledger)
        self.assertIs(approval.status, values.PreparedHandoffStatus.APPROVED)
        self.assertEqual(proposal.old_action_index, 0)
        self.assertEqual(proposal.new_action_index, 1)
        self.assertEqual(proposal.proof_token, handle.proof_token)
        self.assertEqual(proposal.owner_id, self.control.owner_id)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertTrue(self.inbox.usage_active(handle, ProofUse.CANDIDATE))
        first = self.control.prepared_action_proposal(approval)
        self.assertEqual(first.generation_id, handle.generation_id)
        self.assertFalse(hasattr(first, "first_control_sequence"))

    def test_supervisor_approval_is_required(self):
        self.ready_next()
        self.observe_first()
        self.assertIsNone(self.control.prepared_action_proposal(None))
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        other = ExecutionSupervisor()
        denied = other.approve_action_handoff(self.control, proposal, self.frame(self.current), self.ledger)
        self.assertIs(denied.status, values.PreparedHandoffStatus.REJECTED)
        self.assertIsNone(self.control.prepared_action_proposal(denied))

    def test_entry_mismatch_keeps_current_safe_tail(self):
        incumbent = self.prepared_incumbent()
        self.ready_next()
        self.observe_first()
        drift = replace(self.current, physics_state=replace(self.current.physics_state,
            position=(.55, *self.current.physics_state.position[1:])))
        before = self.unchanged(incumbent)
        proposal = self.executor.stage_prepared_action(self.frame(drift), drift)
        approval = self.supervisor.approve_action_handoff(self.control, proposal, self.frame(drift), self.ledger)
        self.assertIs(approval.status, values.PreparedHandoffStatus.REJECTED)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertIsNone(self.control.prepared_action_proposal(approval))

    def test_overdue_or_worker_death_does_not_cancel_valid_prefix(self):
        incumbent = self.prepared_incumbent()
        self.observe_first(stop_airborne=True)
        self.prepare()
        before = self.unchanged(incumbent)
        self.worker.alive = False
        decision = self.coordinator.decide(self.frame(self.current), self.current, self.ledger,
            self.physics_world, changed_cells=(), current_scope=self.coordinator.computation_scope)
        self.assertFalse(self.executor._cancel_requested)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertIs(decision.state, ActionRouteState.RUNNING)
        index = decision.verified_command_index
        tick = self.current.movement_tick_id + 1 if index is None else decision.expected_movement_tick
        if index is not None:
            self.executor.register_verified_submission(index, control_sequence=500,
                requested_movement_tick=tick, requested_latest_movement_tick=decision.latest_movement_tick)
        self.ledger.submit(self.current.session, ActionSnapshotV1("episode", 500,
            self.current.observation_sequence_id, 1_000_000, movement=decision.movement), requested_first_tick=tick)
        self.ledger.observe_sample(sample(500, tick, forward=float(decision.movement.forward),
            strafe=float(decision.movement.strafe), jump=decision.movement.jump,
            sneak=decision.movement.sneak, sprint=decision.movement.sprint))
        self.current = replace(self.anchor(self.proofs[0].trajectory[tick - self.proofs[0].anchor_movement_tick_id],
            self.current.observation_sequence_id + 1), confirmed_control_sequence=500,
            confirmed_control_tick_range=(tick, tick))
        self.coordinator.decide(self.frame(self.current), self.current, self.ledger,
            self.physics_world, changed_cells=(), current_scope=self.coordinator.computation_scope)
        self.assertFalse(self.executor._cancel_requested, "worker death remains a next-action failure on later frames")
        self.assertEqual(before, self.unchanged(incumbent))

    def test_overdue_does_not_release_incumbent(self):
        incumbent = self.prepared_incumbent()
        self.ready_next()
        self.observe_first()
        tick = self.proofs[1].execution_window.latest_start_tick + 1
        overdue = replace(self.current, movement_tick_id=tick,
                          physics_state=replace(self.current.physics_state, movement_tick_id=tick))
        before = self.unchanged(incumbent)
        proposal = self.executor.stage_prepared_action(self.frame(overdue), overdue)
        self.assertIs(proposal.entry_result, values.PreparedQueryStatus.OVERDUE)
        self.assertEqual(before, self.unchanged(incumbent))

    def test_late_hold_is_from_the_actual_owner_ledger(self):
        ledger = InputApplicationLedger()
        action = ActionSnapshotV1("episode", 100, 1, 1_000_000, movement=MovementV1(forward=1))
        ledger.submit(self.current.session, action, requested_first_tick=self.current.movement_tick_id)
        ledger.observe_sample(sample(100, self.current.movement_tick_id))
        self.executor.decide(self.frame(self.current), state_anchor=self.current, input_ledger=ledger)
        self.coordinator.prepare_action(0, self.proofs[0].commands, self.current, ledger_identity=self.ledger_id)
        stream = self.coordinator.generation_for(0)
        actual = ledger.record(100)
        self.assertEqual(stream.late_start_hold_input.forward, float(actual.action.movement.forward))
        self.assertEqual(stream.late_start_hold_input.jump, actual.action.movement.jump)
        self.assertNotEqual(stream.late_start_hold_input, self.base.stream.late_start_hold_input)

    def test_inflight_conflicting_command_blocks_approval(self):
        incumbent = self.prepared_incumbent()
        self.ready_next()
        self.observe_first()
        self.ledger.submit(self.current.session, ActionSnapshotV1("episode", 2000, 2, 1_000_000,
            movement=MovementV1(strafe=1)), requested_first_tick=self.current.movement_tick_id + 1)
        before = self.unchanged(incumbent)
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        denied = self.supervisor.approve_action_handoff(self.control, proposal, self.frame(self.current), self.ledger)
        self.assertIs(denied.status, values.PreparedHandoffStatus.REJECTED)
        self.assertEqual(before, self.unchanged(incumbent))

    def test_preparing_activation_has_no_action_or_usage_side_effect(self):
        incumbent = self.prepared_incumbent()
        handle = self.ready_next()
        self.observe_first()
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        approval = self.supervisor.approve_action_handoff(self.control, proposal, self.frame(self.current), self.ledger)
        first = self.control.prepared_action_proposal(approval)
        action = ActionSnapshotV1("episode", 9999, self.current.observation_sequence_id, 1_000_000,
                                  movement=first.expected_movement)
        binding = StreamActivationBinding(handle.proof_token, handle.generation_id, handle.owner_id,
            self.ledger_id, self.route.world_session, 9999, first.requested_window,
            self.current.movement_tick_id, action)
        before = self.unchanged(incumbent)
        transaction = self.supervisor.prepare_action_activation(self.control, approval, binding)
        self.assertIsInstance(transaction, PreparedActivationTransaction)
        self.assertEqual(transaction.permission_token, approval.approval_token)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertTrue(self.inbox.usage_active(handle, ProofUse.CANDIDATE))
        bad = self.supervisor.prepare_action_activation(self.control, approval,
                                                      replace(binding, generation_id="stale-stream"))
        self.assertIsInstance(bad, ActivationResult)
        self.assertIs(bad.status, ActivationStatus.STALE_GENERATION)
        self.assertEqual(before, self.unchanged(incumbent))

    def test_cancelled_candidate_and_forged_handle_cannot_propose(self):
        incumbent = self.prepared_incumbent()
        handle = self.ready_next()
        before = self.unchanged(incumbent)
        with self.assertRaises(ValueError):
            self.executor.install_prepared_motion(replace(handle))
        self.inbox.release_candidate(handle)
        self.observe_first()
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        denied = self.supervisor.approve_action_handoff(self.control, proposal, self.frame(self.current), self.ledger)
        self.assertIsNone(self.control.prepared_action_proposal(denied))
        self.assertEqual(before, self.unchanged(incumbent))

    def test_ground_path_keeps_fixed_controller(self):
        walk = WalkSegment(FixedRoute("ordinary", (RoutePoint(.5, 1., .5), RoutePoint(.5, 1., 2.5))),
                           (SurfaceNodeId(0, 0, 1, 0), SurfaceNodeId(0, 2, 1, 0)), ())
        executor = ActionRouteExecutor(ground_profile(), jump_profile())
        executor.start(ActionRoute("ordinary", (walk,)), self.frame(self.current))
        self.assertIsInstance(executor._controller, FixedRouteController)
        self.assertFalse(executor.has_verified_motion(0))

    def test_handoff_comparison_does_not_replay_or_hash_proof(self):
        self.ready_next()
        self.observe_first()
        with patch("mc2p.motion_nav.physics_1_21.step", side_effect=AssertionError("hot replay")), \
             patch("mc2p.motion_nav.prepared_motion_proof.canonical_motion_digest",
                   side_effect=AssertionError("hot hashing")):
            proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
            approved = self.supervisor.approve_action_handoff(self.control, proposal, self.frame(self.current), self.ledger)
        self.assertIs(approved.status, values.PreparedHandoffStatus.APPROVED)


    def test_parent_request_building_stays_on_existing_receiver(self):
        import pickle
        import mc2p.motion_nav.prepared_motion_proof as proof_module
        import mc2p.motion_nav.prepared_motion as value_module
        events = []
        control_thread = threading.get_ident()
        def observed(name, original):
            def record(*args, **kwargs):
                events.append((name, threading.get_ident()))
                return original(*args, **kwargs)
            return record
        with patch.object(proof_module, "canonical_motion_digest", side_effect=observed("digest", proof_module.canonical_motion_digest)), \
             patch.object(value_module, "validate_frozen_value", side_effect=observed("graph", value_module.validate_frozen_value)), \
             patch.object(pickle, "dumps", side_effect=observed("serialize", pickle.dumps)):
            existing_job_ids = {id(job) for job in self.worker.jobs}
            self.prepare()
            job = self.wait_for_current_prepared_job(
                action_index=1, existing_job_ids=existing_job_ids)
        self.assertEqual([event for event in events if event[1] == control_thread], [])
        self.assertEqual({event[0] for event in events}, {"digest", "graph", "serialize"})
        self.assertEqual({event[1] for event in events}, {self.inbox.receiver_thread_id})
        self.assertEqual(job.request.async_identity, self.coordinator._prepared_fields.async_identity)

    def test_same_worker_solve_choice_is_frozen_before_prepare(self):
        self.executor._verified_motion.pop(1)
        self.observe_first(stop_airborne=True)
        self.assertTrue(callable(getattr(self.coordinator, "prepare_upcoming_action", None)),
                        "the coordinator cannot select the next action through the same worker")
        self.coordinator.prepare_upcoming_action(self.current, self.physics_world, ledger_identity=self.ledger_id)
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        selection, = [job for job in self.worker.jobs if isinstance(job, GapMotionSolveJob)]
        self.worker.put(_execute_job(selection))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        existing_job_ids = {id(job) for job in self.worker.jobs}
        self.coordinator.decide(self.frame(self.current), self.current, self.ledger, self.physics_world,
                                changed_cells=(), current_scope=self.coordinator.computation_scope)
        # Snapshot sealing and request submission can use separate receiver cycles.
        prepared = self.wait_for_current_prepared_job(
            action_index=1, existing_job_ids=existing_job_ids)
        self.assertEqual(prepared.request.commands, self.inbox._active[prepared.request.async_identity].request.commands)
        self.assertNotEqual(prepared.request.async_identity, selection.work_identity)
        self.assertFalse(self.current.physics_state.on_ground)
        self.assertEqual(self.executor.action_index, 0)

    def test_cancel_during_parent_builder_keeps_inflight_lease_and_body_uses(self):
        incumbent = self.prepared_incumbent()
        before = self.unchanged(incumbent)
        entered, release = threading.Event(), threading.Event()
        original = values.PreparedActionPreparation.build_request
        def blocked(fields, lease):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(fields, lease)
        with patch.object(values.PreparedActionPreparation, "build_request", blocked):
            try:
                self.prepare()
                self.assertTrue(entered.wait(10))
                identity = self.coordinator._prepared_fields.async_identity
                count = self.inbox._close_report().lease_count
                unfinished = self.inbox._close_report().unfinished_computations
                self.inbox.retire(identity)
                self.assertEqual(self.inbox._close_report().unfinished_computations, unfinished,
                                 "retired in-flight parent construction still occupies its bounded slot")
                self.assertEqual(self.inbox._close_report().lease_count, count,
                                 "in-flight parent construction still holds the source lease")
                self.assertEqual(self.unchanged(incumbent), before)
            finally:
                release.set()
                self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertFalse(any(handle.action_index == 1 for handle in self.inbox.registered_handles))
        self.assertEqual(self.unchanged(incumbent), before)

    def test_preparation_rejection_and_queue_full_keep_incumbent_uses(self):
        incumbent = self.prepared_incumbent()
        before = self.unchanged(incumbent)
        bad = self.proofs[1].commands[:-1]
        with self.assertRaises(ValueError):
            self.coordinator.prepare_action(1, bad, self.anchor(self.proofs[1].entry_state, 1),
                                            ledger_identity=self.ledger_id)
        self.assertEqual(self.unchanged(incumbent), before)
        with patch.object(self.inbox, "MAX_UNFINISHED", 0):
            rejected = self.coordinator.prepare_action(1, self.proofs[1].commands,
                self.anchor(self.proofs[1].entry_state, 1), ledger_identity=self.ledger_id)
        self.assertIs(rejected.status, PreparedSubmitStatus.QUEUE_FULL)
        self.assertEqual(self.unchanged(incumbent), before)

    def test_computation_scope_alone_keeps_stream_and_incumbent(self):
        incumbent = self.prepared_incumbent()
        self.ready_next()
        before = self.unchanged(incumbent)
        stream = self.coordinator.generation_for(1)
        self.coordinator.computation_scope = replace(self.coordinator.computation_scope, generation=2)
        self.prepare()
        self.assertIs(self.coordinator.generation_for(1), stream)
        self.assertEqual(self.unchanged(incumbent), before)

    def test_parent_builder_failure_is_typed_and_keeps_receiver_and_body_uses(self):
        incumbent = self.prepared_incumbent()
        before = self.unchanged(incumbent)
        with patch.object(values.PreparedActionPreparation, "build_request", side_effect=RuntimeError("builder fault")):
            self.prepare()
            self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        result = self.inbox.prepared_compute_result(self.coordinator._prepared_fields.async_identity)
        self.assertIsNotNone(result, "a builder exception must become a typed result")
        self.assertIs(result.status, PreparedComputeStatus.WORKER_FAILED)
        self.assertTrue(self.inbox._receiver.is_alive())
        self.assertEqual(before, self.unchanged(incumbent))

    def test_retired_selection_loses_authority_and_keeps_incumbent_uses(self):
        incumbent = self.prepared_incumbent()
        self.observe_first(stop_airborne=True)
        before = self.unchanged(incumbent)
        self.coordinator.prepare_upcoming_action(self.current, self.physics_world,
                                                  ledger_identity=self.ledger_id)
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        selection, = [job for job in self.worker.jobs if isinstance(job, GapMotionSolveJob)]
        self.coordinator._retire_work("upcoming_selection_cancelled")
        self.worker.put(_execute_job(selection))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.assertIsNone(self.coordinator._selection_request,
                          "a retired selection cannot intercept another computation's result")
        self.assertIsNone(self.coordinator._selection_anchor)
        self.assertIsNone(self.coordinator._selection_ledger_id)
        self.assertIsNone(self.coordinator.generation_for(1))
        self.assertEqual(before, self.unchanged(incumbent))

    def test_old_permission_cannot_prepare_new_generation(self):
        incumbent = self.prepared_incumbent()
        self.ready_next()
        self.observe_first()
        original = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        granted = self.supervisor.approve_action_handoff(
            self.control, original, self.frame(self.current), self.ledger)
        for forged in (replace(granted), replace(granted, approval_token="approval-never-issued"),
                       replace(granted, proposal=replace(original, owner_id="route/foreign"))):
            self.assertIsNone(self.control.prepared_action_proposal(forged))
        candidate = self.executor._verified_motion[1]
        proof = replace(candidate.proof, world_dependencies=tuple(sorted(
            set(candidate.proof.world_dependencies) | {(0, 6, 3)})))
        self.executor._verified_motion[1] = replace(
            candidate, candidate=replace(candidate.candidate, proof=proof))
        old_jobs = {id(job) for job in self.worker.jobs}
        self.prepare()
        job = self.wait_for_current_prepared_job(action_index=1, existing_job_ids=old_jobs)
        self.assertEqual(job.request.stream.generation_id,
                         self.coordinator.generation_for(1).generation_id)
        self.worker.put(_execute_job(job))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        fresh = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        self.assertNotEqual(fresh.generation_id, original.generation_id)
        mixed = replace(granted, proposal=fresh)
        action = ActionSnapshotV1("episode", 9999, self.current.observation_sequence_id,
                                  1_000_000, movement=fresh.expected_movement)
        binding = StreamActivationBinding(
            fresh.proof_token, fresh.generation_id, fresh.owner_id,
            self.ledger_id, self.route.world_session, 9999,
            MovementTickWindowV1(fresh.permitted_window.earliest_start_tick,
                                 fresh.permitted_window.latest_start_tick),
            self.current.movement_tick_id, action)
        before = self.unchanged(incumbent)
        result = self.supervisor.prepare_action_activation(self.control, mixed, binding)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertIsInstance(result, ActivationResult,
                              "an old permission must not prepare a new generation")
        self.assertIsNone(self.control.prepared_action_proposal(mixed))
        never_issued = values.PreparedHandoffApproval(values.PreparedHandoffStatus.APPROVED,
                                                       fresh, "approval-never-issued")
        self.assertIsNone(self.control.prepared_action_proposal(never_issued))
        self.assertIsInstance(self.supervisor.prepare_action_activation(self.control, never_issued, binding),
                              ActivationResult)
        fresh_grant = self.supervisor.approve_action_handoff(
            self.control, fresh, self.frame(self.current), self.ledger)
        self.assertIsNotNone(self.control.prepared_action_proposal(fresh_grant))
        self.assertIsInstance(self.supervisor.prepare_action_activation(self.control, fresh_grant, binding),
                              PreparedActivationTransaction)
        self.assertEqual(before, self.unchanged(incumbent))

    def test_unrevoked_old_command_must_block_approval(self):
        incumbent = self.prepared_incumbent()
        self.ready_next()
        self.ledger.submit(self.current.session,
            ActionSnapshotV1("episode", 11, 1, 1_000_000, movement=MovementV1(strafe=1)),
            requested_first_tick=self.current.movement_tick_id + 1)
        self.observe_first()
        self.assertIs(self.ledger.record(11).status, InputApplicationStatus.SUPERSEDED)
        before = self.unchanged(incumbent)
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        approval = self.supervisor.approve_action_handoff(
            self.control, proposal, self.frame(self.current), self.ledger)
        self.ledger.observe_sample(sample(11, proposal.permitted_window.earliest_start_tick,
                                         forward=0., strafe=1.))
        late = self.ledger.record(11)
        self.assertIs(late.status, InputApplicationStatus.APPLIED_OUTSIDE_WINDOW)
        self.assertIs(approval.status, values.PreparedHandoffStatus.REJECTED)
        self.assertIn(11, tuple(item.control_sequence for item in proposal.conflicting_commands))
        self.assertEqual(before, self.unchanged(incumbent))

    def test_hold_must_use_observed_input(self):
        ledger = InputApplicationLedger()
        ledger.submit(self.current.session,
            ActionSnapshotV1("episode", 100, 1, 1_000_000, movement=MovementV1(forward=1)),
            requested_first_tick=self.current.movement_tick_id)
        ledger.observe_sample(sample(100, self.current.movement_tick_id,
                                     forward=0., sample_state="expired"))
        self.executor.decide(self.frame(self.current), state_anchor=self.current, input_ledger=ledger)
        self.coordinator.prepare_action(0, self.proofs[0].commands, self.current,
                                        ledger_identity=self.ledger_id)
        observed = ledger.sample(self.current.movement_tick_id)
        hold = self.coordinator.generation_for(0).late_start_hold_input
        self.assertEqual(hold.forward, observed.forward)
        self.assertEqual(hold.strafe, observed.strafe)
        self.assertEqual(hold.jump, observed.jump)
        self.assertEqual(hold.sneak, observed.sneak)
        self.assertEqual(hold.sprint, observed.sprint)

    def test_complete_control_path_has_zero_recursive_validation(self):
        import mc2p.contracts.motion_activation as activation_contract
        handle = self.ready_next()
        self.observe_first()
        staged = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        granted = self.supervisor.approve_action_handoff(
            self.control, staged, self.frame(self.current), self.ledger)
        first = self.control.prepared_action_proposal(granted)
        action = ActionSnapshotV1("episode", 9999, self.current.observation_sequence_id,
                                  1_000_000, movement=first.expected_movement)
        binding = StreamActivationBinding(handle.proof_token, handle.generation_id,
            handle.owner_id, self.ledger_id, self.route.world_session, 9999,
            first.requested_window, self.current.movement_tick_id, action)
        wrong_binding = replace(binding, generation_id="wrong-generation")
        events = []
        original = activation_contract.require_immutable_graph
        def observed(value):
            events.append((type(value).__name__, threading.get_ident()))
            return original(value)
        with patch.object(activation_contract, "require_immutable_graph", observed):
            proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
            approval = self.supervisor.approve_action_handoff(
                self.control, proposal, self.frame(self.current), self.ledger)
            self.assertIsNotNone(self.control.prepared_action_proposal(approval))
            self.assertIsInstance(self.supervisor.prepare_action_activation(
                self.control, approval, binding), PreparedActivationTransaction)
            unissued = values.PreparedHandoffApproval(values.PreparedHandoffStatus.APPROVED,
                                                       proposal, "approval-never-issued")
            self.assertIsNone(self.control.prepared_action_proposal(unissued))
            self.assertIsInstance(self.supervisor.prepare_action_activation(
                self.control, unissued, binding), ActivationResult)
            self.assertIsInstance(self.supervisor.prepare_action_activation(
                self.control, approval, wrong_binding), ActivationResult)
        self.assertEqual(events, [])

    def test_unconfirmed_hold_does_not_create_stream_or_release_incumbent(self):
        incumbent = self.prepared_incumbent()
        before = self.unchanged(incumbent)
        self.executor.decide(self.frame(self.current), state_anchor=self.current,
                              input_ledger=InputApplicationLedger())
        result = self.coordinator.prepare_action(0, self.proofs[0].commands, self.current,
                                                ledger_identity=self.ledger_id)
        self.assertIs(result.status, PreparedSubmitStatus.INPUT_UNCONFIRMED)
        self.assertIsNone(self.coordinator.generation_for(0))
        self.assertEqual(before, self.unchanged(incumbent))

    def test_external_activation_construction_keeps_full_graph_validation(self):
        import mc2p.contracts.motion_activation as activation_contract
        handle = self.ready_next()
        self.observe_first()
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        approval = self.supervisor.approve_action_handoff(
            self.control, proposal, self.frame(self.current), self.ledger)
        first = self.control.prepared_action_proposal(approval)
        action = ActionSnapshotV1("episode", 9999, self.current.observation_sequence_id,
                                  1_000_000, movement=first.expected_movement)
        events = []
        original = activation_contract.require_immutable_graph
        def observed(value):
            events.append(type(value).__name__)
            return original(value)
        with patch.object(activation_contract, "require_immutable_graph", observed):
            replace(first)
            binding = StreamActivationBinding(handle.proof_token, handle.generation_id,
                handle.owner_id, self.ledger_id, self.route.world_session, 9999,
                first.requested_window, self.current.movement_tick_id, action)
            PreparedActivationTransaction("external-transaction", binding, None, action,
                                          "external-permission", proposal.handoff_token)
        self.assertEqual(events, ["StreamActivationProposal", "StreamActivationBinding",
                                  "PreparedActivationTransaction"])

    def test_arbitrary_predicate_is_not_an_approval_issuer(self):
        incumbent = self.prepared_incumbent()
        self.ready_next()
        self.observe_first()
        staged = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        bind = getattr(self.executor, "bind_prepared_approval_verifier", None)
        if bind is not None:
            try:
                bind(lambda _: True)
            except ValueError:
                pass
        unissued = values.PreparedHandoffApproval(
            values.PreparedHandoffStatus.APPROVED, staged, "approval-never-issued")
        first = self.control.prepared_action_proposal(unissued)
        action = ActionSnapshotV1("episode", 9999, self.current.observation_sequence_id,
                                  1_000_000, movement=staged.expected_movement)
        binding = StreamActivationBinding(
            staged.proof_token, staged.generation_id, staged.owner_id,
            self.ledger_id, self.route.world_session, 9999,
            MovementTickWindowV1(staged.permitted_window.earliest_start_tick,
                                 staged.permitted_window.latest_start_tick),
            self.current.movement_tick_id, action)
        before = self.unchanged(incumbent)
        result = self.supervisor.prepare_action_activation(self.control, unissued, binding)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertIsNone(first)
        self.assertIsInstance(result, ActivationResult)

    def test_unrevoked_command_is_not_forgotten_when_ledger_fills(self):
        incumbent = self.prepared_incumbent()
        self.ledger = InputApplicationLedger(max_records=3)
        self.ledger.submit(self.current.session,
            ActionSnapshotV1("episode", 10, 1, 1_000_000),
            requested_first_tick=self.current.movement_tick_id)
        self.ledger.observe_sample(sample(10, self.current.movement_tick_id,
                                         forward=0., sample_state="neutral"))
        self.executor.decide(self.frame(self.current), state_anchor=self.current, input_ledger=self.ledger)
        self.ready_next()
        self.ledger.submit(self.current.session,
            ActionSnapshotV1("episode", 11, 1, 1_000_000, movement=MovementV1(strafe=1)),
            requested_first_tick=self.current.movement_tick_id + 1)
        self.observe_first()
        before = self.unchanged(incumbent)
        proposal = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        approval = self.supervisor.approve_action_handoff(
            self.control, proposal, self.frame(self.current), self.ledger)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertIs(approval.status, values.PreparedHandoffStatus.REJECTED)
        self.assertIs(self.ledger.record(11).status, InputApplicationStatus.SUPERSEDED)

    def test_actual_expiry_can_finish_a_superseded_command(self):
        self._assert_superseded_termination("expired", InputApplicationStatus.EXPIRED)

    def test_actual_disallowed_sample_can_finish_a_superseded_command(self):
        self._assert_superseded_termination("disallowed", InputApplicationStatus.REJECTED)

    def _assert_superseded_termination(self, sample_state, expected_status):
        from mc2p.motion_nav.physics_1_21 import step
        from mc2p.motion_nav.physics_types import CalculationStatus, TickInput
        self.ready_next()
        self.ledger.submit(self.current.session,
            ActionSnapshotV1("episode", 11, 1, 1_000_000, movement=MovementV1(strafe=1)),
            requested_first_tick=self.current.movement_tick_id + 1)
        self.observe_first()
        blocked = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        denied = self.supervisor.approve_action_handoff(
            self.control, blocked, self.frame(self.current), self.ledger)
        self.assertIs(denied.status, values.PreparedHandoffStatus.REJECTED)
        actual = sample(11, self.current.movement_tick_id + 1, forward=0., sample_state=sample_state)
        self.ledger.observe_sample(actual)
        calculated = step(self.current.physics_state,
            TickInput(actual.forward, actual.strafe, actual.jump, actual.sneak,
                      actual.sprint, self.current.physics_state.yaw_radians),
            self.physics_world, self.base.ruleset)
        self.assertIs(calculated.status, CalculationStatus.OK)
        self.current = self.anchor(calculated.next_state, self.current.observation_sequence_id + 1)
        self.executor.decide(self.frame(self.current), state_anchor=self.current, input_ledger=self.ledger)
        fresh = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        approval = self.supervisor.approve_action_handoff(
            self.control, fresh, self.frame(self.current), self.ledger)
        self.assertIs(fresh.entry_result, values.PreparedQueryStatus.READY_MATCH)
        self.assertIs(approval.status, values.PreparedHandoffStatus.APPROVED)
        self.assertIs(self.ledger.record(11).status, expected_status)

    def test_standalone_helper_rejects_noncurrent_control(self):
        from mc2p.motion_nav.execution_supervisor import prepare_action_activation
        incumbent = self.prepared_incumbent()
        self.ready_next()
        self.observe_first()
        staged = self.executor.stage_prepared_action(self.frame(self.current), self.current)
        issued = self.supervisor.approve_action_handoff(
            self.control, staged, self.frame(self.current), self.ledger)
        self.assertIs(issued.status, values.PreparedHandoffStatus.APPROVED)
        action = ActionSnapshotV1("episode", 9999, self.current.observation_sequence_id,
                                  1_000_000, movement=staged.expected_movement)
        binding = StreamActivationBinding(
            staged.proof_token, staged.generation_id, staged.owner_id,
            self.ledger_id, self.route.world_session, 9999,
            MovementTickWindowV1(staged.permitted_window.earliest_start_tick,
                                 staged.permitted_window.latest_start_tick),
            self.current.movement_tick_id, action)
        other = replace(self.control, route=replace(self.route, route_revision=2),
                        coordinator=None, tracker=None)
        self.assertIsNot(other, self.control)
        self.assertEqual(other.route.route_revision, 2)
        self.assertIs(self.supervisor.incumbent_route, self.control)
        before = self.unchanged(incumbent)
        denied = self.supervisor.approve_action_handoff(other, staged, self.frame(self.current), self.ledger)
        self.assertIs(denied.status, values.PreparedHandoffStatus.REJECTED)
        first = other.prepared_action_proposal(issued)
        guarded = self.supervisor.prepare_action_activation(other, issued, binding)
        result = prepare_action_activation(other, issued, binding)
        self.assertEqual(before, self.unchanged(incumbent))
        self.assertIsInstance(guarded, ActivationResult)
        self.assertEqual((first is None, isinstance(result, ActivationResult)), (True, True))
        copied = replace(self.control, coordinator=None, tracker=None)
        self.assertEqual(copied.route.route_revision, issued.proposal.route_revision)
        self.assertEqual(copied.owner_id, issued.proposal.owner_id)
        self.assertIsNone(copied.prepared_action_proposal(issued))
        self.assertIsInstance(prepare_action_activation(copied, issued, binding), ActivationResult)
        self.assertIsInstance(self.supervisor.prepare_action_activation(copied, issued, binding), ActivationResult)
        self.assertIsNone(self.executor.prepared_action_proposal(issued))
        self.assertIsNotNone(self.control.prepared_action_proposal(issued))
        allowed = prepare_action_activation(self.control, issued, binding)
        self.assertIsInstance(allowed, PreparedActivationTransaction)
        self.assertEqual(allowed.permission_token, issued.approval_token)
        self.assertIsInstance(self.supervisor.prepare_action_activation(self.control, issued, binding),
                              PreparedActivationTransaction)
        self.assertEqual(before, self.unchanged(incumbent))


class LedgerResponsibilityChecks(unittest.TestCase):
    def test_full_ledger_rejects_new_submission_without_losing_superseded_record(self):
        ledger = InputApplicationLedger(max_records=2)
        session = WorldSessionId("d102-ledger-responsibility")
        for sequence in (11, 20):
            ledger.submit(session, ActionSnapshotV1("episode", sequence, 1, 1_000_000),
                          requested_first_tick=11)
        ledger.observe_sample(sample(20, 11, forward=0., sample_state="neutral"))
        superseded = ledger.record(11)
        self.assertIs(superseded.status, InputApplicationStatus.SUPERSEDED)
        ledger.submit(session, ActionSnapshotV1("episode", 21, 1, 1_000_000),
                      requested_first_tick=12)
        self.assertIsNone(ledger.record(20))
        before = ledger.snapshot()
        with self.assertRaises(InputLedgerCapacityExceeded):
            ledger.submit(session, ActionSnapshotV1("episode", 22, 1, 1_000_000),
                          requested_first_tick=12)
        self.assertEqual(ledger.snapshot(), before)
        self.assertIs(ledger.record(11), superseded)
        self.assertIsNone(ledger.record(22))

    def test_partial_late_application_retains_responsibility_until_actual_expiry(self):
        ledger = InputApplicationLedger(max_records=1)
        session = WorldSessionId("d102-ledger-responsibility")
        ledger.submit(session, ActionSnapshotV1("episode", 11, 1, 1_000_000,
                                               movement=MovementV1(forward=1), valid_for_ticks=2),
                      requested_first_tick=11)
        partial = ledger.observe_sample(sample(11, 13))
        self.assertIs(partial.status, InputApplicationStatus.APPLIED_OUTSIDE_WINDOW)
        with self.assertRaises(InputLedgerCapacityExceeded):
            ledger.submit(session, ActionSnapshotV1("episode", 12, 1, 1_000_000),
                          requested_first_tick=14)
        finished = ledger.observe_sample(sample(11, 14, forward=0., sample_state="expired"))
        self.assertIs(finished.status, InputApplicationStatus.EXPIRED)
        ledger.submit(session, ActionSnapshotV1("episode", 12, 1, 1_000_000),
                      requested_first_tick=15)
        self.assertEqual(tuple(r.control_sequence for r in ledger.snapshot()), (12,))


class NativeConstructionChecks(unittest.TestCase):
    def test_private_freezer_cannot_return_a_mutable_contract_value(self):
        from mc2p.contracts import motion_activation as native
        from mc2p.contracts.common import ContractViolation
        fields = dict(proof_token="proof", generation_id="generation", owner_id="owner",
            ledger_id="ledger", world_session_id="world", movement_intent_id="intent",
            expected_movement=[], expected_yaw_radians=0.,
            requested_window=MovementTickWindowV1(1, 1), handoff_token="handoff")
        with self.assertRaises(ContractViolation):
            native.StreamActivationProposal(**fields)
        with self.assertRaises(ContractViolation):
            native._freeze_native_fields(native.StreamActivationProposal, fields)

    def test_all_native_factories_check_fields_before_returning(self):
        from mc2p.contracts import motion_activation as native
        from mc2p.contracts.common import ContractViolation
        action = ActionSnapshotV1("episode", 11, 1, 1_000_000)
        binding = StreamActivationBinding("proof", "generation", "owner", "ledger", "world",
                                          11, MovementTickWindowV1(1, 1), 1, action)
        proposals = dict(proof_token="proof", generation_id="generation", owner_id="owner",
            ledger_id="ledger", world_session_id="world", movement_intent_id="intent",
            expected_movement=MovementV1(), expected_yaw_radians=0.,
            requested_window=MovementTickWindowV1(1, 1), handoff_token="handoff")
        transactions = dict(transaction_token="transaction", binding=binding, old_action=None,
                            new_action=action, permission_token="permission", handoff_token="handoff")
        cases = (
            (native.ActivationResult, dict(status=ActivationStatus.NOT_SELECTED, reason=[])),
            (native.StreamActivationProposal, dict(proposals, expected_movement=[])),
            (native.StreamActivationProposal, dict(proposals, requested_window=[])),
            (native.PreparedActivationTransaction, dict(transactions, old_action=[])),
            (native.PreparedActivationTransaction, dict(transactions, new_action=replace(action))),
        )
        for kind, fields in cases:
            with self.subTest(kind=kind.__name__, fields=tuple(fields)):
                with self.assertRaises(ContractViolation):
                    native._freeze_native_fields(kind, fields)
                with self.assertRaises(ContractViolation):
                    kind._from_native_references(**fields)


if __name__ == "__main__":
    unittest.main()
