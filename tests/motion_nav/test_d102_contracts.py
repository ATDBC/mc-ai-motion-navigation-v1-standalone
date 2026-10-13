"""D102 pure values and actual input-stream timing boundaries."""
from dataclasses import FrozenInstanceError, dataclass, replace
from enum import Enum
from types import MappingProxyType
import unittest

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementTickWindowV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.motion_activation import (
    ActivationResult, ActivationStatus, FirstStreamApplication, PreparedActivationAcceptance,
    PreparedActivationDelivery, PreparedActivationTransaction, PreparedStreamDispatch,
    StreamActivationBinding, StreamActivationProposal, StreamApplicationStatus, StreamStartBranch,
)
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.input_stream import InputStreamTracker
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_solver import MotionCommandTick
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.prepared_motion import (
    PreparedAdmissionResult, PreparedAdmissionStatus, PreparedBranchProof, PreparedComputeCounts, PreparedComputeResult,
    PreparedComputeStatus, PreparedCurrentFacts, PreparedMotionBundle, PreparedMotionHandle, PreparedMotionHotView,
    PreparedMotionProof, PreparedMotionRequest, PreparedQueryResult, PreparedQueryStatus,
    PreparedSafetyTail, PreparedScanBudget, PreparedSubmitResult, PreparedSubmitStatus,
    SnapshotIdentity, SnapshotLease, SnapshotLeaseResult, SnapshotLeaseStatus,
    StreamGeneration, WorldSnapshotVersion,
)
from mc2p.motion_nav.world_model import (
    Aabb, CellFact, CellKnowledge, ImmutableWorldSection, ObservationStamp, WorldSessionId,
)
from mc2p.motion_nav.world_snapshot import seal_snapshot_version
from tests.motion_nav.test_trajectory_proto_contracts import physics_state


def prepared_request():
    state = physics_state()
    inputs = (TickInput(1., 0., False, False, False, 0.),) * 3
    stream = StreamGeneration("stream-1", "body-1", "request-1", "route-1", 2,
                              inputs, (state,), CandidateExecutionWindow(11, 12), ("suffix-1",),
                              TickInput(0., 0., False, False, False, 0.))
    snapshot = SnapshotIdentity(state.session.value, 3, 4, 5, "a" * 64,
                                JAVA_1_21_RULESET, Aabb(-2, -2, -2, 3, 5, 4))
    return PreparedMotionRequest(
        "job-1", AsyncWorkIdentity(AsyncComputationScope(state.session.value, "task-1", 1),
                                  "compute-1", AsyncWorkKind.MOTION_SOLVE, "request-1", 1),
        stream, 1, 1, 1, tuple(MotionCommandTick(MovementV1(forward=1), 0.) for _ in inputs),
        inputs, "b" * 64, state, replace(state, movement_tick_id=13), stream.requested_window,
        11, "ledger-1", state.session.value, (), snapshot, "lease-1", (), JAVA_1_21_RULESET,
        TaskDamageBudget(), 0., PreparedScanBudget(20, 100, 200), ("suffix-1",))


def prepared_bundle():
    request = prepared_request()
    branches = []
    for branch, offset in ((StreamStartBranch.ON_TIME, 0), (StreamStartBranch.LATE_ONE, 1)):
        trajectory = tuple(request.entry_state if offset == i == 0 else
                           replace(request.entry_state, movement_tick_id=10 + offset + i)
                           for i in range(4))
        tails = tuple(PreparedSafetyTail(i, branch, (), (trajectory[i],), (), (), 0.,
                                        CandidateExecutionWindow(11 + offset + i, 11 + offset + i))
                      for i in range(4))
        branches.append(PreparedBranchProof(branch, 11 + offset, request.expected_inputs,
                                           trajectory, tails, 1, 0, (), (),
                                           JAVA_1_21_RULESET, 3, 6, stopping_tail_steps=3))
    proof = PreparedMotionProof(request.stream, tuple(branches), JAVA_1_21_RULESET, (), (), 6, 12,
                                stopping_tail_steps=6)
    return PreparedMotionBundle(request, request.expected_inputs, request.expected_input_digest,
                                proof, "c" * 64, request.snapshot_identity,
                                JAVA_1_21_RULESET, b"sealed-fixture")


def binding(stream, **changes):
    result = StreamActivationBinding("proof-1", stream.generation_id, stream.owner_id,
                                     "ledger-1", "p0-test", 100,
                                     MovementTickWindowV1(11, 12), 10,
                                     ActionSnapshotV1("episode-1", 100, 8, 1000,
                                                      movement=MovementV1(forward=1)))
    return replace(result, **changes) if changes else result


def application(stream, *, tick=11, sequence=100, movement=None):
    return FirstStreamApplication(stream.generation_id, sequence, tick,
                                  MovementV1(forward=1) if movement is None else movement, 0.)


class ContractsTests(unittest.TestCase):
    def test_prepared_ascii_ids_accept_127_and_128_utf8_bytes(self):
        request = prepared_request()
        for size in (127, 128):
            token = "i" * size
            with self.subTest(size=size):
                self.assertEqual(replace(request, job_token=token).job_token, token)
                self.assertEqual(replace(request, ledger_id=token).ledger_id, token)
                self.assertEqual(replace(request, snapshot_lease_token=token).snapshot_lease_token, token)
                for field in ("generation_id", "owner_id", "request_id", "route_id"):
                    self.assertEqual(getattr(replace(request.stream, **{field: token}), field), token)
                result = PreparedComputeResult(PreparedComputeStatus.MISSING_INFORMATION, token,
                                                PreparedComputeCounts())
                self.assertEqual(result.job_token, token)

    def test_all_prepared_identity_fields_reject_empty_or_oversized_ids(self):
        bundle = prepared_bundle()
        request, snapshot = bundle.request, bundle.snapshot_identity
        hot = PreparedMotionHotView.from_bundle(bundle)
        handle = PreparedMotionHandle("proof-1", "request-1", "stream-1", "body-1", "route-1", 2,
                                      bundle.proof_digest, (), hot)
        version = WorldSnapshotVersion(snapshot.world_session_id, 3, 4, 5, snapshot.bounds,
                                       JAVA_1_21_RULESET, ())
        lease = SnapshotLease("lease-1", seal_snapshot_version(version, JAVA_1_21_RULESET))
        facts = PreparedCurrentFacts(request.async_identity.scope, request.stream, request.stream.owner_id,
            request.world_session_id, request.ruleset, (), request.ledger_id, snapshot, request.entry_state,
            0., request.entry_state.movement_tick_id, request.requested_window)
        groups = (
            (request, ("job_token", "ledger_id", "world_session_id", "snapshot_lease_token")),
            (request.stream, ("generation_id", "owner_id", "request_id", "route_id")),
            (snapshot, ("world_session_id",)),
            (version, ("world_session_id",)),
            (lease, ("lease_token",)),
            (hot, ("ledger_id", "world_session_id")),
            (handle, ("proof_token", "request_id", "generation_id", "owner_id", "route_id")),
            (facts, ("body_owner_id", "world_session_id", "ledger_id")),
            (PreparedSubmitResult(PreparedSubmitStatus.ACCEPTED, "job-1"), ("job_token",)),
            (PreparedComputeResult(PreparedComputeStatus.MISSING_INFORMATION, "job-1", PreparedComputeCounts()),
             ("job_token",)),
        )
        for original, names in groups:
            for name in names:
                with self.subTest(type=type(original).__name__, field=name, size=0), \
                     self.assertRaises(ContractViolation):
                    replace(original, **{name: ""})
                for size in (129, 2 * 1024 * 1024):
                    with self.subTest(type=type(original).__name__, field=name, size=size), \
                         self.assertRaisesRegex(ContractViolation, "128 UTF-8 bytes"):
                        replace(original, **{name: "i" * size})

    def test_prepared_identity_length_counts_utf8_bytes_and_keeps_ascii_syntax(self):
        request = prepared_request()
        for at_limit, oversized in (("é" * 64, "é" * 64 + "a"),
                                    ("中" * 42 + "aa", "中" * 43),
                                    ("🙂" * 32, "🙂" * 32 + "a")):
            self.assertEqual(len(at_limit.encode("utf-8")), 128)
            self.assertEqual(len(oversized.encode("utf-8")), 129)
            self.assertLess(len(oversized), 128)
            with self.subTest(value=at_limit), self.assertRaisesRegex(ContractViolation, "non-empty identifier"):
                replace(request, job_token=at_limit)
            with self.subTest(value=oversized), self.assertRaisesRegex(ContractViolation, "128 UTF-8 bytes"):
                replace(request, job_token=oversized)

    def test_preparation_has_no_actual_sequence(self):
        request = prepared_request()
        for value in (request, request.stream, prepared_bundle().proof):
            self.assertFalse(hasattr(value, "first_control_sequence"))
        self.assertNotEqual(request.async_identity.owner_instance_id, request.stream.owner_id)

    def test_activation_does_not_mutate_bundle(self):
        bundle = prepared_bundle()
        before_activation = prepared_bundle()
        tracker = InputStreamTracker.start(bundle.request.stream)
        self.assertEqual(tracker.bind_activation(binding(tracker.generation)).status,
                         ActivationStatus.ACTIVATED)
        tracker.record_first_application(application(tracker.generation))
        self.assertEqual(bundle, before_activation)
        with self.assertRaises(FrozenInstanceError):
            bundle.input_digest = "changed"

    def test_nested_mapping_or_list_is_rejected(self):
        stream = prepared_request().stream
        @dataclass(frozen=True)
        class Nested:
            payload: object
        for graph in (([],), ({"key": 1},), (Nested([]),),
                      (MappingProxyType({"key": []}),)):
            with self.subTest(graph=graph), self.assertRaises(ContractViolation):
                replace(stream, suffix_identity=graph)
        with self.assertRaises(ContractViolation):
            replace(prepared_request(), expected_inputs=list(stream.expected_inputs))

    def test_first_receipt_selects_only_start_branch(self):
        stream = prepared_request().stream
        for tick, branch in ((11, StreamStartBranch.ON_TIME), (12, StreamStartBranch.LATE_ONE)):
            tracker = InputStreamTracker.start(stream)
            tracker.bind_activation(binding(stream))
            result = tracker.record_first_application(application(stream, tick=tick))
            self.assertEqual(result.status, StreamApplicationStatus.APPLIED)
            self.assertEqual(result.start_branch, branch)
            self.assertEqual(tracker.expected_effect_tick(1), tick + 1)

    def test_second_command_cannot_reselect_late_branch(self):
        stream = prepared_request().stream
        tracker = InputStreamTracker.start(stream)
        tracker.bind_activation(binding(stream))
        tracker.record_first_application(application(stream))
        result = tracker.record_application(application(stream, sequence=104, tick=13), command_index=1)
        self.assertEqual(result.status, StreamApplicationStatus.DEVIATION)
        self.assertEqual(result.start_branch, StreamStartBranch.ON_TIME)
        self.assertEqual(tracker.expected_effect_tick(1), 12)

    def test_duplicate_or_late_two_receipt_is_rejected(self):
        stream = prepared_request().stream
        tracker = InputStreamTracker.start(stream)
        tracker.bind_activation(binding(stream))
        self.assertEqual(tracker.record_first_application(application(stream, tick=13)).status,
                         StreamApplicationStatus.REJECTED)
        with self.assertRaises(ContractViolation):
            tracker.expected_effect_tick(0)
        tracker.record_first_application(application(stream))
        self.assertEqual(tracker.record_first_application(application(stream)).status,
                         StreamApplicationStatus.REJECTED)

    def test_unbound_wrong_generation_or_command_receipt_is_rejected(self):
        stream = prepared_request().stream
        self.assertEqual(InputStreamTracker.start(stream).record_first_application(application(stream)).status,
                         StreamApplicationStatus.REJECTED)
        for changes in ({"generation_id": "old"}, {"control_sequence": 101},
                        {"movement": MovementV1()}, {"movement_yaw_radians": 1.}):
            tracker = InputStreamTracker.start(stream)
            tracker.bind_activation(binding(stream))
            self.assertEqual(tracker.record_first_application(replace(application(stream), **changes)).status,
                             StreamApplicationStatus.REJECTED)

    def test_activation_must_match_actual_sequence_movement_owner_and_window(self):
        stream = prepared_request().stream
        with self.assertRaises(ContractViolation):
            binding(stream, first_control_sequence=101)
        for changes in ({"generation_id": "old"}, {"owner_id": "other"},
                        {"world_session_id": "other"},
                        {"requested_window": MovementTickWindowV1(12, 13)},
                        {"compound_action": ActionSnapshotV1("episode-1", 100, 8, 1000)}):
            tracker = InputStreamTracker.start(stream)
            self.assertNotEqual(tracker.bind_activation(binding(stream, **changes)).status,
                                ActivationStatus.ACTIVATED)

    def test_actual_sequence_can_skip_other_runtime_commands_but_cannot_repeat(self):
        stream = prepared_request().stream
        tracker = InputStreamTracker.start(stream)
        tracker.bind_activation(binding(stream))
        tracker.record_first_application(application(stream))
        result = tracker.record_application(application(stream, sequence=104, tick=12), command_index=1)
        self.assertEqual(result.status, StreamApplicationStatus.APPLIED)
        self.assertEqual(tracker.command_facts[1].sequence, 104)
        self.assertEqual(tracker.record_application(application(stream, sequence=104, tick=13),
                                                    command_index=2).status,
                         StreamApplicationStatus.DEVIATION)

    def test_request_bundle_and_snapshot_contradictions_are_rejected(self):
        request = prepared_request()
        for changes in ({"world_session_id": "other"}, {"expected_inputs": ()},
                        {"ruleset": replace(JAVA_1_21_RULESET, ruleset_id="other")},
                        {"entry_state": replace(request.entry_state, yaw_radians=1.)},
                        {"requested_window": CandidateExecutionWindow(12, 13)}):
            with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                replace(request, **changes)
        with self.assertRaises(ContractViolation):
            replace(prepared_bundle(), inputs=())
        with self.assertRaises(ContractViolation):
            replace(request.snapshot_identity, digest="")
        version = WorldSnapshotVersion("p0-test", 3, 4, 5, request.snapshot_identity.bounds,
                                       JAVA_1_21_RULESET, ())
        self.assertFalse(hasattr(version, "digest"))
        lease = SnapshotLease("lease-1", seal_snapshot_version(version, JAVA_1_21_RULESET))
        with self.assertRaises(FrozenInstanceError):
            lease.lease_token = "other"

    def test_proof_exit_counters_and_hot_view_cannot_substitute_parent_facts(self):
        bundle = prepared_bundle()
        with self.assertRaises(ContractViolation):
            replace(bundle, request=replace(bundle.request, exit_state=replace(bundle.request.exit_state,
                                                                             yaw_radians=1.)))
        with self.assertRaises(ContractViolation):
            replace(bundle.proof, physics_steps=0)
        with self.assertRaises(ContractViolation):
            replace(bundle.proof, completed_candidates=1)
        hot = PreparedMotionHotView.from_bundle(bundle)
        handle = PreparedMotionHandle("proof-1", "request-1", "stream-1", "body-1", "route-1", 2,
                                      bundle.proof_digest, (), hot)
        self.assertIs(hot.inputs, bundle.inputs)
        self.assertIs(hot.branches, bundle.proof.branches)
        clone = replace(handle)
        self.assertEqual(clone, handle)
        self.assertIsNot(clone, handle)
        self.assertFalse(hasattr(clone, "registered"))
        with self.assertRaises(ContractViolation):
            replace(handle, owner_id="other")
        with self.assertRaises(ContractViolation):
            replace(hot, branches=())
        with self.assertRaises(ContractViolation):
            replace(hot, key_nodes=(replace(hot.entry_state, yaw_radians=1.),))
        with self.assertRaises(ContractViolation):
            replace(hot, safety_tails=())

    def test_results_reject_strings_and_contradictory_success_payloads(self):
        cases = (
            lambda: ActivationResult("activated"),
            lambda: PreparedSubmitResult(PreparedSubmitStatus.ACCEPTED),
            lambda: PreparedSubmitResult(PreparedSubmitStatus.CLOSED, "job-1"),
            lambda: PreparedComputeResult(PreparedComputeStatus.PREPARED, "job-1", PreparedComputeCounts()),
            lambda: PreparedAdmissionResult(PreparedAdmissionStatus.REGISTERED),
            lambda: PreparedAdmissionResult(PreparedAdmissionStatus.REJECTED),
            lambda: PreparedQueryResult(PreparedQueryStatus.READY_MATCH),
            lambda: SnapshotLeaseResult(SnapshotLeaseStatus.READY),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ContractViolation):
                case()

    def test_compute_counts_and_missing_cells_preserve_typed_failure_work(self):
        counts = PreparedComputeCounts(4, 6, 0, 3)
        failed = PreparedComputeResult(PreparedComputeStatus.MISSING_INFORMATION, "job-1", counts,
                                       missing_cells=((0, 0, 0),))
        self.assertEqual(failed.counts, counts)
        self.assertEqual(failed.missing_cells, ((0, 0, 0),))
        for changes in ({"nodes_scanned": -1}, {"physics_steps": -1},
                        {"completed_candidates": -1}, {"stopping_tail_steps": -1},
                        {"stopping_tail_steps": 7}):
            with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                replace(counts, **changes)
        with self.assertRaises(ContractViolation):
            replace(failed, status=PreparedComputeStatus.BLOCKED)
        bundle = prepared_bundle()
        good = PreparedComputeCounts(bundle.proof.nodes_scanned, bundle.proof.physics_steps, 0,
                                     bundle.proof.stopping_tail_steps)
        result = PreparedComputeResult(PreparedComputeStatus.PREPARED, "job-1", good, bundle)
        self.assertEqual(result.counts.physics_steps, bundle.proof.physics_steps)
        with self.assertRaises(ContractViolation):
            replace(result, counts=replace(good, physics_steps=good.physics_steps + 1))
        with self.assertRaises(ContractViolation):
            replace(result, counts=replace(good, stopping_tail_steps=good.stopping_tail_steps - 1))

    def test_proof_stopping_work_is_included_once_and_sums_all_branches(self):
        proof = prepared_bundle().proof
        self.assertEqual(proof.stopping_tail_steps, sum(branch.stopping_tail_steps for branch in proof.branches))
        for branch in proof.branches:
            with self.assertRaises(ContractViolation):
                replace(branch, stopping_tail_steps=branch.physics_steps + 1)
            with self.assertRaises(ContractViolation):
                replace(branch, stopping_tail_steps=-1)
        for value in (-1, proof.stopping_tail_steps - 1, proof.physics_steps + 1):
            with self.subTest(value=value), self.assertRaises(ContractViolation):
                replace(proof, stopping_tail_steps=value)

    def test_hold_input_is_explicit_and_part_of_stream_identity(self):
        stream = prepared_request().stream
        changed = replace(stream, generation_id="stream-2", late_start_hold_input=stream.expected_inputs[0])
        self.assertNotEqual(changed, stream)
        with self.assertRaises(ContractViolation):
            replace(stream, late_start_hold_input="neutral")

    def test_activation_transactions_retain_final_command_and_dispatch_is_local(self):
        import pickle
        stream = prepared_request().stream
        final = binding(stream)
        tx = PreparedActivationTransaction("tx-1", final, None, final.compound_action,
                                           "permit-1", "handoff-1")
        accepted = PreparedActivationAcceptance(tx.transaction_token, "accept-1", final, "permit-1")
        delivered = PreparedActivationDelivery(tx.transaction_token, accepted.acceptance_token,
                                               final, "delivery-1", final.compound_action)
        self.assertEqual(delivered.binding, tx.binding)
        with self.assertRaises(ContractViolation):
            replace(tx, new_action=replace(final.compound_action, movement=MovementV1()))
        with self.assertRaises(ContractViolation):
            replace(delivered, delivered_action=replace(final.compound_action, request_sequence_id=101))
        class BorrowedPort:
            def prepare_activation(self, binding): pass
            def accept_activation(self, transaction): pass
            def deliver_activation(self, transaction, accepted): pass
            def commit_activation(self, transaction, accepted, delivered): pass
            def record_application(self, application): pass
        proposal = StreamActivationProposal("proof-1", "stream-1", "body-1", "ledger-1", "p0-test",
                                             "intent-1", MovementV1(forward=1), 0.,
                                             MovementTickWindowV1(11, 12), "handoff-1")
        dispatch = PreparedStreamDispatch(proposal, BorrowedPort())
        with self.assertRaises(TypeError):
            pickle.dumps(dispatch)
        with self.assertRaises(ContractViolation):
            replace(stream, suffix_identity=(dispatch,))

    def test_no_rebind_missing_middle_command_or_late_receipt_recovers_the_branch(self):
        stream = prepared_request().stream
        tracker = InputStreamTracker.start(stream)
        tracker.bind_activation(binding(stream))
        self.assertEqual(tracker.bind_activation(binding(stream)).status, ActivationStatus.ACTIVATION_MISMATCH)
        tracker.record_first_application(application(stream, tick=12))
        self.assertEqual(tracker.record_application(application(stream, tick=14, sequence=107), command_index=2).status,
                         StreamApplicationStatus.DEVIATION)
        self.assertEqual(tracker.record_application(application(stream, tick=14, sequence=107), command_index=1).status,
                         StreamApplicationStatus.DEVIATION)
        self.assertEqual(tracker.start_branch, StreamStartBranch.LATE_ONE)
        self.assertEqual(tracker.expected_effect_tick(1), 13)

    def test_snapshot_preserves_unknown_and_rejects_wrong_section_or_semantic_version(self):
        identity = prepared_request().snapshot_identity
        facts = (((0, 0, 0), CellFact(CellKnowledge.UNKNOWN)),)
        versions = (((0, 0, 0), 0),)
        section = ImmutableWorldSection((0, 0, 0), facts, versions)
        version = WorldSnapshotVersion("p0-test", 3, 4, 5, identity.bounds, JAVA_1_21_RULESET,
                                       (section,))
        self.assertIs(version.section_references[0].facts, facts)
        self.assertFalse(hasattr(version, "facts"))
        lease = SnapshotLease("lease-unknown", seal_snapshot_version(version, JAVA_1_21_RULESET))
        self.assertEqual(lease.facts[0][1].knowledge, CellKnowledge.UNKNOWN)
        with self.assertRaises(ContractViolation):
            ImmutableWorldSection((1, 0, 0), facts, versions)
        with self.assertRaises(ContractViolation):
            ImmutableWorldSection((0, 0, 0), facts, (((0, 0, 0), True),))

    def test_proof_dependency_summary_cannot_disagree_with_branch_or_tail(self):
        proof = prepared_bundle().proof
        facts = (((0, 0, 0), CellFact(CellKnowledge.UNKNOWN)),)
        versions = (((0, 0, 0), 1),)
        branch = replace(proof.branches[0], dependency_facts=facts, dependency_versions=versions)
        with self.assertRaises(ContractViolation):
            replace(proof, branches=(branch, proof.branches[1]))
        tail = replace(proof.branches[0].safety_tails[0], dependency_facts=facts, dependency_versions=versions)
        branch_with_tail = replace(proof.branches[0], safety_tails=(tail,) + proof.branches[0].safety_tails[1:])
        with self.assertRaises(ContractViolation):
            replace(proof, branches=(branch_with_tail, proof.branches[1]), dependency_facts=facts,
                    dependency_versions=(((0, 0, 0), 2),))


def with_dependency(bundle, *, position=(0, 0, 0), version=1, branch_index=0, tail_only=False, session="p0-test"):
    facts = ((position, CellFact(CellKnowledge.AIR, ObservationStamp(WorldSessionId(session),
                                                                    0, 0, "fix-round1", 0))),)
    versions = ((position, version),)
    branch = bundle.proof.branches[branch_index]
    if tail_only:
        tail = replace(branch.safety_tails[0], dependency_facts=facts, dependency_versions=versions)
        branch = replace(branch, safety_tails=(tail,) + branch.safety_tails[1:])
    else:
        branch = replace(branch, dependency_facts=facts, dependency_versions=versions)
    branches = tuple(branch if i == branch_index else existing for i, existing in enumerate(bundle.proof.branches))
    return replace(bundle, proof=replace(bundle.proof, branches=branches,
                                         dependency_facts=facts, dependency_versions=versions))


class FixRound1Tests(unittest.TestCase):
    def test_frozen_dataclass_with_writable_instance_dictionary_is_rejected(self):
        @dataclass(frozen=True)
        class Marker:
            value: str
        marker = Marker("original")
        with self.assertRaises(ContractViolation):
            replace(prepared_request().stream, suffix_identity=(marker,))
        marker.__dict__["value"] = "mutated"
        self.assertEqual(marker.value, "mutated")
        @dataclass(frozen=True, slots=True)
        class SlotsMarker:
            value: str
        immutable = SlotsMarker("original")
        stream = replace(prepared_request().stream, suffix_identity=(immutable,))
        self.assertFalse(hasattr(stream.suffix_identity[0], "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            immutable.value = "mutated"

    def test_foreign_world_in_late_branch_or_tail_is_rejected(self):
        bundle = prepared_bundle()
        late = bundle.proof.branches[1]
        foreign = WorldSessionId("other-world")
        states = tuple(replace(state, session=foreign) for state in late.trajectory)
        tails = tuple(replace(tail, trajectory=(states[tail.boundary_index],)) for tail in late.safety_tails)
        with self.assertRaises(ContractViolation):
            changed = replace(late, trajectory=states, safety_tails=tails)
            replace(bundle, proof=replace(bundle.proof, branches=(bundle.proof.branches[0], changed)))

    def test_all_proof_branch_and_tail_dependencies_bind_world_and_snapshot_bounds(self):
        for branch_index in (0, 1):
            for tail_only in (False, True):
                for position, session in (((0, 0, 0), "other-world"), ((99, 99, 99), "p0-test")):
                    with self.subTest(branch=branch_index, tail=tail_only, position=position, session=session):
                        with self.assertRaises(ContractViolation):
                            with_dependency(prepared_bundle(), position=position, session=session,
                                            branch_index=branch_index, tail_only=tail_only)
        # A top-level dependency may be inherited rather than used by a branch.
        for position, session in (((0, 0, 0), "other-world"), ((99, 99, 99), "p0-test")):
            with self.subTest(top_level=True, position=position, session=session), self.assertRaises(ContractViolation):
                bundle = prepared_bundle()
                fact = CellFact(CellKnowledge.AIR, ObservationStamp(WorldSessionId(session), 0, 0, "fix-round1", 0))
                replace(bundle, proof=replace(bundle.proof, dependency_facts=((position, fact),),
                                             dependency_versions=((position, 1),)))

    def test_hot_view_keeps_every_canonical_key_node_in_order_and_by_reference(self):
        hot = PreparedMotionHotView.from_bundle(prepared_bundle())
        cases = ((), hot.key_nodes[1:], hot.key_nodes[:-1], tuple(reversed(hot.key_nodes)),
                 tuple(replace(node) for node in hot.key_nodes))
        for nodes in cases:
            with self.subTest(count=len(nodes)), self.assertRaises(ContractViolation):
                replace(hot, key_nodes=nodes)

    def test_hot_view_and_handle_cannot_drop_branch_tail_or_extra_proof_dependency(self):
        for branch_index in (0, 1):
            for tail_only in (False, True):
                bundle = with_dependency(prepared_bundle(), branch_index=branch_index, tail_only=tail_only)
                hot = PreparedMotionHotView.from_bundle(bundle)
                with self.subTest(branch=branch_index, tail=tail_only), self.assertRaises(ContractViolation):
                    dropped = replace(hot, dependency_facts=(), dependency_versions=())
                    PreparedMotionHandle("proof-1", "request-1", "stream-1", "body-1", "route-1", 2,
                                         bundle.proof_digest, (), dropped)
                with self.assertRaises(ContractViolation):
                    replace(hot, dependency_versions=(((0, 0, 0), 2),))
        bundle = prepared_bundle()
        facts = (((0, 0, 0), CellFact(CellKnowledge.UNKNOWN)),)
        bundle = replace(bundle, proof=replace(bundle.proof, dependency_facts=facts,
                                              dependency_versions=(((0, 0, 0), 9),)))
        hot = PreparedMotionHotView.from_bundle(bundle)
        with self.assertRaises(ContractViolation):
            replace(hot, dependency_facts=(), dependency_versions=())

    def test_bundle_requires_every_inherited_dependency_at_the_same_version(self):
        bundle = prepared_bundle()
        request = replace(bundle.request, inherited_dependencies=(((0, 0, 0), 9),))
        with self.assertRaises(ContractViolation):
            replace(bundle, request=request)
        with self.assertRaises(ContractViolation):
            replace(with_dependency(bundle, version=8), request=request)
        ready = replace(with_dependency(bundle, version=9), request=request)
        self.assertEqual(ready.proof.dependency_versions, request.inherited_dependencies)

    def test_every_executable_tail_damage_is_within_remaining_and_task_cap(self):
        for branch_index in (0, 1):
            for damage, cap, remaining, rejected in ((999., 0., 0., True), (3., 5., 2., True),
                                                     (2., 5., 2., False)):
                with self.subTest(branch=branch_index, damage=damage, remaining=remaining):
                    bundle = prepared_bundle()
                    branch = bundle.proof.branches[branch_index]
                    tail = replace(branch.safety_tails[0], expected_damage_points=damage)
                    branch = replace(branch, safety_tails=(tail,) + branch.safety_tails[1:])
                    branches = tuple(branch if i == branch_index else existing for i, existing in enumerate(bundle.proof.branches))
                    proof = replace(bundle.proof, branches=branches)
                    request = replace(bundle.request, damage_budget=TaskDamageBudget("test-cap", cap),
                                      damage_remaining_points=remaining)
                    if rejected:
                        with self.assertRaises(ContractViolation):
                            replace(bundle, request=request, proof=proof)
                    else:
                        ready = replace(bundle, request=request, proof=proof)
                        self.assertEqual(ready.proof.branches[branch_index].safety_tails[0].expected_damage_points, 2.)

    def test_tail_window_covers_exact_effect_tick_and_deadline_for_both_start_branches(self):
        for branch in prepared_bundle().proof.branches:
            for original in branch.safety_tails:
                for nonempty in (False, True):
                    tick = original.trajectory[0].movement_tick_id + 1
                    trajectory = original.trajectory + (replace(original.trajectory[0], movement_tick_id=tick),) if nonempty else original.trajectory
                    inputs = (branch.inputs[0],) if nonempty else ()
                    for window in (CandidateExecutionWindow(0, 0), CandidateExecutionWindow(tick + 1, tick + 1),
                                   CandidateExecutionWindow(tick, tick + 1)):
                        with self.subTest(branch=branch.start_branch, boundary=original.boundary_index,
                                          nonempty=nonempty, window=window), self.assertRaises(ContractViolation):
                            replace(original, inputs=inputs, trajectory=trajectory, permitted_window=window)
                    ready = replace(original, inputs=inputs, trajectory=trajectory,
                                    permitted_window=CandidateExecutionWindow(tick, tick))
                    self.assertEqual(ready.permitted_window.latest_start_tick, tick)


class FixRound2Tests(unittest.TestCase):
    def test_hidden_mutable_nonfield_slot_from_base_is_rejected(self):
        class CachedBase:
            __slots__ = ("cache",)
            def __init__(self):
                object.__setattr__(self, "cache", [])
        @dataclass(frozen=True, slots=True)
        class Marker(CachedBase):
            value: str
            def __post_init__(self):
                CachedBase.__init__(self)
        marker = Marker("original")
        self.assertFalse(hasattr(marker, "__dict__"))
        with self.assertRaises(ContractViolation):
            replace(prepared_request().stream, suffix_identity=(marker,))
        marker.cache.append("mutated")
        self.assertEqual(marker.cache, ["mutated"])

    def test_enum_constructor_hidden_mutable_attribute_is_rejected(self):
        class MutableTag(Enum):
            SUFFIX = "suffix"
            def __init__(self, value):
                self.cache = []
        with self.assertRaises(ContractViolation):
            replace(prepared_request().stream, suffix_identity=(MutableTag.SUFFIX,))
        MutableTag.SUFFIX.cache.append("normal-list-write")
        self.assertEqual(MutableTag.SUFFIX.cache, ["normal-list-write"])

    def test_project_enums_accept_normal_shape_but_reject_extra_member_storage(self):
        from unittest.mock import patch
        member = StreamStartBranch.ON_TIME
        stream = prepared_request().stream
        self.assertEqual(replace(stream, suffix_identity=(member,)).suffix_identity, (member,))
        for extra in ([], "extra-immutable-storage"):
            with patch.object(member, "cache", extra, create=True):
                with self.subTest(extra=extra), self.assertRaises(ContractViolation):
                    replace(stream, suffix_identity=(member,))

    def test_private_nonfield_slot_is_rejected_and_inherited_declared_field_is_valid(self):
        class PrivateBase:
            __slots__ = ("__cache",)
            def initialize(self):
                object.__setattr__(self, "_PrivateBase__cache", [])
        @dataclass(frozen=True, slots=True)
        class Hidden(PrivateBase):
            value: str
            def __post_init__(self):
                self.initialize()
        with self.assertRaises(ContractViolation):
            replace(prepared_request().stream, suffix_identity=(Hidden("original"),))
        @dataclass(frozen=True, slots=True)
        class ParentValue:
            parent: str
        @dataclass(frozen=True, slots=True)
        class ChildValue(ParentValue):
            child: str
        value = ChildValue("parent", "child")
        stream = replace(prepared_request().stream, suffix_identity=(value,))
        self.assertIs(stream.suffix_identity[0], value)


class FixRound3Tests(unittest.TestCase):
    def test_frozen_dataclass_with_list_storage_is_rejected(self):
        @dataclass(frozen=True, slots=True)
        class ListValue(list):
            label: str
            def __post_init__(self):
                list.__init__(self, ["original"])
        node = ListValue("suffix")
        self.assertFalse(hasattr(node, "__dict__"))
        with self.assertRaises(ContractViolation):
            replace(prepared_request().stream, suffix_identity=(node,))
        node.append("mutated")
        self.assertEqual(list(node), ["original", "mutated"])

    def test_frozen_dataclass_with_dict_storage_is_rejected(self):
        @dataclass(frozen=True, slots=True)
        class DictValue(dict):
            label: str
            def __post_init__(self):
                dict.__init__(self, original=1)
        node = DictValue("suffix")
        self.assertFalse(hasattr(node, "__dict__"))
        with self.assertRaises(ContractViolation):
            replace(prepared_request().stream, suffix_identity=(node,))
        node["mutated"] = 2
        self.assertEqual(dict(node), {"original": 1, "mutated": 2})

    def test_declared_frozen_slots_dataclass_inheritance_remains_valid(self):
        @dataclass(frozen=True, slots=True)
        class FrozenParent:
            parent_label: str
        @dataclass(frozen=True, slots=True)
        class FrozenChild(FrozenParent):
            child_label: str
        node = FrozenChild("parent", "child")
        stream = replace(prepared_request().stream, suffix_identity=(node,))
        self.assertIs(stream.suffix_identity[0], node)
        with self.assertRaises(FrozenInstanceError):
            node.parent_label = "changed"


if __name__ == "__main__":
    unittest.main()
