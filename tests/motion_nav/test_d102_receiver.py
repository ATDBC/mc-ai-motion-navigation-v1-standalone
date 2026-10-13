"""Parent admission and the single background consumer of the motion service."""
from dataclasses import fields, replace
import hashlib
import importlib
import importlib.util
import pickle
import threading
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.motion_solver import MotionCommandTick
from mc2p.motion_nav.motion_worker import MotionResultInbox
from mc2p.motion_nav.prepared_motion import (
    PreparedAdmissionStatus, PreparedComputeCounts, PreparedComputeResult, PreparedComputeStatus,
    PreparedCurrentFacts, PreparedQueryStatus, PreparedRejectReason, PreparedSubmitStatus,
    SnapshotLease, SnapshotLeaseStatus,
)
from mc2p.motion_nav.prepared_motion_proof import (
    PREPARED_BUNDLE_SCHEMA, PreparedWireMessage, canonical_bundle_bytes,
    canonical_motion_digest, prepare_fixed_motion, seal_bundle, seal_compute_failure,
)
from tests.motion_nav import test_d102_prepared_proof as proof_fixtures


def raw_message(bundle, **changes):
    """Restore transport fields without invoking the worker-side constructors."""
    candidate = PreparedWireMessage.__new__(PreparedWireMessage)
    payload = pickle.dumps(bundle, protocol=5)
    values = dict(job_token=bundle.request.job_token, schema=PREPARED_BUNDLE_SCHEMA,
                  payload=payload, payload_digest=hashlib.sha256(payload).hexdigest())
    values.update(changes)
    for name, value in values.items():
        object.__setattr__(candidate, name, value)
    return candidate


def unchecked_replace(value, **changes):
    restored = type(value).__new__(type(value))
    for field in fields(value):
        object.__setattr__(restored, field.name, changes.get(field.name, getattr(value, field.name)))
    return restored


def reseal(bundle):
    proof_digest = canonical_motion_digest(bundle.proof)
    bundle = unchecked_replace(bundle, proof_digest=proof_digest)
    return unchecked_replace(bundle, sealed_bytes=canonical_bundle_bytes(bundle))


class QueuedWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.results = []
        self.jobs = []
        self.consumer_threads = set()
        self.submitted = threading.Event()
        self.alive = True

    def submit(self, job):
        with self.lock:
            self.jobs.append(job)
        self.submitted.set()
        return True

    def poll_available(self):
        with self.lock:
            self.consumer_threads.add(threading.get_ident())
            result, self.results = tuple(self.results), []
            return result

    def put(self, result):
        with self.lock:
            self.results.append(result)

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False


class ReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proof_fixtures.ProofTests.setUpClass()
        cls.request, cls.snapshot = proof_fixtures.ProofTests.request, proof_fixtures.ProofTests.snapshot
        result = prepare_fixed_motion(cls.request, cls.snapshot)
        if result.status is not PreparedComputeStatus.PREPARED:
            raise AssertionError("the existing frozen fixed-input producer must prepare this fixture")
        cls.bundle, cls.message = result.bundle, seal_bundle(result.bundle)
        cls.current = PreparedCurrentFacts(
            cls.request.async_identity.scope, cls.request.stream, cls.request.stream.owner_id,
            cls.request.world_session_id, cls.request.ruleset, cls.bundle.proof.dependency_versions,
            cls.request.ledger_id, cls.snapshot.identity, cls.request.entry_state,
            cls.request.damage_remaining_points, cls.request.entry_state.movement_tick_id,
            cls.request.requested_window)
        cls.lease = SnapshotLease(cls.request.snapshot_lease_token, cls.snapshot)

    def validator(self):
        name = "mc2p.motion_nav.prepared_motion_validation"
        self.assertIsNotNone(importlib.util.find_spec(name),
                             "the independent parent-side prepared admission is missing")
        return importlib.import_module(name).validate_prepared_message

    def validate(self, message=None, *, current=None):
        return self.validator()(message or self.message, self.request, self.snapshot,
                                current or self.current)

    def inbox(self):
        self.assertTrue(callable(getattr(MotionResultInbox, "register_preparation", None)),
                        "the shared inbox cannot independently register prepared requests")
        worker = QueuedWorker()
        inbox = MotionResultInbox(worker=worker, current_facts=self.current)
        self.addCleanup(inbox.close)
        result = inbox.register_preparation(self.request, self.lease)
        self.assertIs(result.status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(worker.submitted.wait(10), "the one receiver must submit the sealed parent request")
        return inbox, worker

    def deliver(self, inbox, worker, message=None):
        worker.put(message or self.message)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        return inbox.prepared_admission(self.request.async_identity)

    def test_other_legal_inputs_resealed_are_rejected(self):
        # Sprint on the final neutral tick is legal and leaves this exact exit unchanged.
        inputs = self.request.expected_inputs[:-1] + (
            replace(self.request.expected_inputs[-1], sprint=not self.request.expected_inputs[-1].sprint),)
        commands = tuple(MotionCommandTick(MovementV1(
            forward=int(item.forward), strafe=int(item.strafe), jump=item.jump,
            sneak=item.sneak, sprint=item.sprint), item.movement_yaw_radians) for item in inputs)
        stream = replace(self.request.stream, expected_inputs=inputs)
        other = replace(self.request, stream=stream, commands=commands, expected_inputs=inputs,
                        expected_input_digest=canonical_motion_digest(inputs))
        prepared = prepare_fixed_motion(other, self.snapshot)
        self.assertIs(prepared.status, PreparedComputeStatus.PREPARED)
        result = self.validate(seal_bundle(prepared.bundle))
        self.assertIs(result.reason, PreparedRejectReason.INPUTS)
        self.assertIsNone(result.handle)

    def test_three_way_ruleset_after_reseal_is_rejected(self):
        different = replace(self.request.ruleset, ruleset_id="different-complete-rules")
        forged = unchecked_replace(self.bundle, ruleset=different)
        result = self.validate(raw_message(reseal(forged)))
        self.assertIs(result.reason, PreparedRejectReason.RULESET)
        self.assertIsNone(result.handle)
        result = self.validate(current=replace(self.current, ruleset=different,
            snapshot_identity=replace(self.snapshot.identity, ruleset=different),
            current_state=replace(self.current.current_state, ruleset_id=different.ruleset_id)))
        self.assertIs(result.reason, PreparedRejectReason.RULESET)

    def test_all_binding_fields_are_independently_checked(self):
        cases = [
            ({"job_token": "other-token"}, PreparedRejectReason.REQUEST),
            ({"async_identity": replace(self.request.async_identity, revision=2)}, PreparedRejectReason.REQUEST),
            ({"stream": replace(self.request.stream, generation_id="other-stream")}, PreparedRejectReason.GENERATION),
            ({"stream": replace(self.request.stream, owner_id="other-body")}, PreparedRejectReason.OWNER),
            ({"stream": replace(self.request.stream, route_id="other-route")}, PreparedRejectReason.ROUTE_ACTION),
            ({"stream": replace(self.request.stream, action_index=1)}, PreparedRejectReason.ROUTE_ACTION),
            ({"route_revision": 2}, PreparedRejectReason.ROUTE_ACTION),
            ({"action_revision": 2}, PreparedRejectReason.ROUTE_ACTION),
            ({"goal_revision": 2}, PreparedRejectReason.REQUEST),
            ({"ledger_id": "other-ledger"}, PreparedRejectReason.LEDGER),
            ({"boundary_applications": ("invented-existing-application",)}, PreparedRejectReason.LEDGER),
            ({"snapshot_lease_token": "other-lease"}, PreparedRejectReason.WORLD_SNAPSHOT),
            ({"snapshot_identity": replace(self.snapshot.identity, local_revision=2)}, PreparedRejectReason.WORLD_SNAPSHOT),
            ({"deadline_tick": self.request.deadline_tick - 1}, PreparedRejectReason.REQUEST),
            ({"exit_state": replace(self.request.exit_state, yaw_radians=.1)}, PreparedRejectReason.REQUEST),
            ({"damage_remaining_points": self.request.damage_remaining_points + 1}, PreparedRejectReason.REQUEST),
        ]
        for changed, reason in cases:
            with self.subTest(changed=tuple(changed)):
                request = unchecked_replace(self.request, **changed)
                # Re-seal all changed request metadata so transport self-consistency is insufficient.
                forged = reseal(unchecked_replace(self.bundle, request=request))
                result = self.validate(raw_message(forged, job_token=self.request.job_token))
                self.assertIs(result.reason, reason)
                self.assertIsNone(result.handle)

    def test_mutable_graph_is_rejected(self):
        forged = unchecked_replace(self.bundle, inputs=list(self.bundle.inputs))
        result = self.validate(raw_message(forged))
        self.assertIs(result.reason, PreparedRejectReason.MUTABLE_GRAPH)
        self.assertIsNone(result.handle)

    def test_payload_hash_precedes_deserialization(self):
        message = raw_message(self.bundle, payload_digest="0" * 64)
        with patch("pickle.loads", side_effect=AssertionError("hash must win before unpickle")):
            result = self.validate(message)
        self.assertIs(result.reason, PreparedRejectReason.MESSAGE)

    def test_full_proof_digest_and_actual_dependencies_are_checked(self):
        result = self.validate(raw_message(unchecked_replace(self.bundle, proof_digest="0" * 64)))
        self.assertIs(result.reason, PreparedRejectReason.PROOF)
        versions = self.current.dependency_versions
        changed = replace(self.current, dependency_versions=((versions[0][0], versions[0][1] + 1),) + versions[1:])
        result = self.validate(current=changed)
        self.assertIs(result.reason, PreparedRejectReason.DEPENDENCY)

    def test_resealed_tail_without_a_safe_terminal_is_rejected(self):
        branch = self.bundle.proof.branches[0]
        tail = branch.safety_tails[0]
        end = replace(tail.trajectory[-1], on_ground=False,
                      velocity_blocks_per_tick=(0., -.1, 0.))
        broken = replace(tail, trajectory=tail.trajectory[:-1] + (end,))
        branches = (replace(branch, safety_tails=(broken,) + branch.safety_tails[1:]),) + self.bundle.proof.branches[1:]
        proof = replace(self.bundle.proof, branches=branches)
        forged = reseal(unchecked_replace(self.bundle, proof=proof))
        self.assertIs(self.validate(raw_message(forged)).reason, PreparedRejectReason.PROOF)

    def test_cancel_wins_at_publish_barrier(self):
        inbox, worker = self.inbox()
        entered, release = threading.Event(), threading.Event()
        publish = inbox._publish_prepared
        def paused(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return publish(*args)
        with patch.object(inbox, "_publish_prepared", side_effect=paused):
            worker.put(self.message)
            inbox.wake_receiver()
            self.assertTrue(entered.wait(10))
            inbox.retire(self.request.async_identity)
            release.set()
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
        self.assertIs(inbox.prepared_admission(self.request.async_identity).status,
                      PreparedAdmissionStatus.CANCELLED)
        self.assertEqual(inbox.registered_handles, ())
        self.assertIs(inbox.query_prepared(self.request.async_identity,
            movement_tick=self.current.movement_tick).status, PreparedQueryStatus.NOT_READY)

    def test_body_update_at_publish_does_not_replace_scope_or_stream_checks(self):
        for change in ("body", "scope"):
            with self.subTest(change=change):
                inbox, worker = self.inbox()
                entered, release = threading.Event(), threading.Event()
                publish = inbox._publish_prepared
                def paused(*args):
                    entered.set()
                    self.assertTrue(release.wait(10))
                    return publish(*args)
                with patch.object(inbox, "_publish_prepared", side_effect=paused):
                    worker.put(self.message)
                    inbox.wake_receiver()
                    self.assertTrue(entered.wait(10))
                    if change == "body":
                        moved = replace(self.current.current_state, position=(.6, self.current.current_state.position[1], .5))
                        current = replace(self.current, current_state=moved)
                    else:
                        current = replace(self.current, scope=replace(self.current.scope,
                                          generation=self.current.scope.generation + 1))
                    inbox.publish_current_facts(current)
                    release.set()
                    self.assertTrue(inbox.wait_for_receiver(timeout=10))
                result = inbox.prepared_admission(self.request.async_identity)
                self.assertIs(result.status, PreparedAdmissionStatus.REGISTERED if change == "body"
                              else PreparedAdmissionStatus.STALE)
                query = inbox.query_prepared(self.request.async_identity, movement_tick=current.movement_tick)
                self.assertIs(query.status, PreparedQueryStatus.READY_MATCH if change == "body"
                              else PreparedQueryStatus.NOT_READY)

    def test_transport_and_decoded_bytes_remain_charged_until_atomic_publish(self):
        inbox, worker = self.inbox()
        entered, release = threading.Event(), threading.Event()
        publish = inbox._publish_prepared
        def paused(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return publish(*args)
        with patch.object(inbox, "_publish_prepared", side_effect=paused):
            worker.put(self.message)
            inbox.wake_receiver()
            self.assertTrue(entered.wait(10))
            record = inbox._active[self.request.async_identity]
            expected = (len(self.snapshot.canonical_bytes) + len(record.canonical_request)
                        + len(record.sealed_request) + len(self.message.payload) + len(self.bundle.sealed_bytes))
            self.assertEqual(inbox.retained_bytes, expected)
            release.set()
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
        self.assertEqual(inbox.retained_bytes,
                         len(self.snapshot.canonical_bytes) + len(self.bundle.sealed_bytes))

    def test_publish_then_cancel_releases_only_candidate(self):
        inbox, worker = self.inbox()
        result = self.deliver(inbox, worker)
        self.assertIs(result.status, PreparedAdmissionStatus.REGISTERED)
        handle = result.handle
        inbox.retire(self.request.async_identity)
        self.assertTrue(inbox.has_registered_handle(handle), "compute retirement must preserve the candidate")
        self.assertTrue(inbox.release_candidate(handle))
        self.assertFalse(inbox.has_registered_handle(handle))
        self.assertFalse(inbox.release_candidate(handle))

    def test_slow_validation_does_not_block_control_query(self):
        inbox, worker = self.inbox()
        import mc2p.motion_nav.prepared_motion_validation as validation
        entered, release, queried = threading.Event(), threading.Event(), threading.Event()
        original = validation._validate_for_registration
        def slow(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args)
        with patch.object(validation, "_validate_for_registration", side_effect=slow):
            worker.put(self.message)
            inbox.wake_receiver()
            self.assertTrue(entered.wait(10))
            results = []
            def query():
                with patch("pickle.loads", side_effect=AssertionError("control unpickle")), \
                        patch("mc2p.motion_nav.prepared_motion_proof.canonical_motion_digest",
                              side_effect=AssertionError("control full hash")):
                    results.append(inbox.query_prepared(self.request.async_identity,
                                                       movement_tick=self.current.movement_tick))
                queried.set()
            thread = threading.Thread(target=query)
            thread.start()
            self.assertTrue(queried.wait(2), "query must finish while validation is held at the event")
            thread.join(2)
            self.assertIs(results[0].status, PreparedQueryStatus.NOT_READY)
            release.set()
            self.assertTrue(inbox.wait_for_receiver(timeout=10))

    def test_one_consumer_for_shared_worker(self):
        inbox, worker = self.inbox()
        self.deliver(inbox, worker)
        main = threading.get_ident()
        for sequence in range(4):
            inbox.drain_once(worker, sequence)
            inbox.query_prepared(self.request.async_identity, movement_tick=self.current.movement_tick)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        self.assertEqual(worker.consumer_threads, {inbox.receiver_thread_id})
        self.assertNotIn(main, worker.consumer_threads)
        other = MotionResultInbox()
        self.addCleanup(other.close)
        with self.assertRaises(ContractViolation):
            other.attach_worker(worker)

    def test_duplicate_old_or_foreign_token_is_rejected(self):
        inbox, worker = self.inbox()
        result = self.deliver(inbox, worker)
        self.deliver(inbox, worker)
        foreign = raw_message(self.bundle, job_token="foreign-unregistered-token")
        self.deliver(inbox, worker, foreign)
        self.assertEqual(inbox.registered_handles, (result.handle,))
        self.assertGreaterEqual(inbox.discarded_results, 2)
        inbox.retire(self.request.async_identity)
        self.deliver(inbox, worker)
        self.assertEqual(inbox.registered_handles, (result.handle,))

    def test_third_unfinished_job_is_typed_rejection_without_replacement(self):
        inbox, worker = self.inbox()
        request = replace(self.request, job_token="second-token", async_identity=replace(
            self.request.async_identity, revision=2))
        self.assertIs(inbox.register_preparation(request, self.lease).status, PreparedSubmitStatus.ACCEPTED)
        third = replace(request, job_token="third-token", async_identity=replace(request.async_identity, revision=3))
        self.assertIs(inbox.register_preparation(third, self.lease).status, PreparedSubmitStatus.QUEUE_FULL)
        self.assertIn(self.request.async_identity, inbox.active_identities)
        self.assertIn(request.async_identity, inbox.active_identities)
        self.assertNotIn(third.async_identity, inbox.active_identities)

    def test_compute_failure_schema_is_typed_and_never_publishes_handle(self):
        inbox, worker = self.inbox()
        failure = PreparedComputeResult(PreparedComputeStatus.BLOCKED,
                                        self.request.job_token, PreparedComputeCounts())
        self.deliver(inbox, worker, seal_compute_failure(failure))
        self.assertIs(inbox.prepared_compute_result(self.request.async_identity).status,
                      PreparedComputeStatus.BLOCKED)
        self.assertEqual(inbox.registered_handles, ())

    def test_worker_death_and_expired_result_do_not_publish(self):
        inbox, worker = self.inbox()
        worker.alive = False
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        self.assertIs(inbox.prepared_compute_result(self.request.async_identity).status,
                      PreparedComputeStatus.WORKER_FAILED)
        self.deliver(inbox, worker)
        self.assertEqual(inbox.registered_handles, ())

    def test_ready_query_uses_cached_typed_value_without_walking_proof(self):
        inbox, worker = self.inbox()
        self.deliver(inbox, worker)
        with patch("mc2p.contracts.motion_activation.require_immutable_graph",
                   side_effect=AssertionError("hot query traversed the proof")), \
                patch("pickle.loads", side_effect=AssertionError("hot query unpickled")):
            first = inbox.query_prepared(self.request.async_identity, movement_tick=self.current.movement_tick)
            second = inbox.query_prepared(self.request.async_identity, movement_tick=self.current.movement_tick)
        self.assertIs(first.status, PreparedQueryStatus.READY_MATCH)
        self.assertIs(first, second)

    def test_snapshot_sealing_is_background_and_releases_the_original_world_port(self):
        from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
        from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId
        old = WorldKnowledge(WorldSessionId(self.request.world_session_id))
        old.observe_blocks(ObservationStamp(old.session, 1, 1, "receiver-clock", 50_000_000),
                           {(0, 0, 0): BlockGeometry.full_cube("minecraft:stone")})
        new = WorldKnowledge(WorldSessionId("receiver-new-world"))
        inbox = MotionResultInbox(world=old)
        self.addCleanup(inbox.close)
        bounds = PhysicsWorldBounds(0, 0, 0, 0, 0, 0)
        threads, original = [], old.publish_sealed_snapshot
        def publication(*args):
            threads.append(threading.get_ident())
            return original(*args)
        with patch.object(old, "publish_sealed_snapshot", side_effect=publication):
            self.assertIs(inbox.prepare_snapshot(bounds, ruleset=self.request.ruleset,
                identity=self.request.async_identity).status, SnapshotLeaseStatus.NOT_READY)
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
        lease = inbox.prepare_snapshot(bounds, ruleset=self.request.ruleset,
                                       identity=self.request.async_identity).lease
        self.assertIsNotNone(lease)
        self.assertEqual(threads, [inbox.receiver_thread_id])
        inbox.bind_world(new)
        with patch.object(new, "release_snapshot", side_effect=AssertionError("released through replacement world")):
            inbox.retire(self.request.async_identity)
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
        self.assertFalse(old.release_snapshot(lease.lease_token), "the captured original port already returned the lease")

    def test_bundle_and_lease_capacity_keep_eight_existing_candidates(self):
        self.assertTrue(callable(getattr(MotionResultInbox, "register_preparation", None)))
        worker = QueuedWorker()
        inbox = MotionResultInbox(worker=worker, current_facts=self.current)
        self.addCleanup(inbox.close)
        for index in range(8):
            lease = SnapshotLease(f"capacity-lease-{index}", self.snapshot)
            request = replace(self.request, job_token=f"capacity-job-{index}", snapshot_lease_token=lease.lease_token,
                              async_identity=replace(self.request.async_identity, revision=index + 1))
            self.assertIs(inbox.register_preparation(request, lease).status, PreparedSubmitStatus.ACCEPTED)
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
            bundle = reseal(replace(self.bundle, request=request))
            worker.put(raw_message(bundle))
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
            self.assertIs(inbox.prepared_admission(request.async_identity).status, PreparedAdmissionStatus.REGISTERED)
            inbox.retire(request.async_identity)
        handles = inbox.registered_handles
        self.assertEqual(len(handles), 8)
        ninth = replace(self.request, job_token="capacity-job-9",
                        async_identity=replace(self.request.async_identity, revision=9))
        self.assertIs(inbox.register_preparation(ninth, self.lease).status, PreparedSubmitStatus.QUEUE_FULL)
        self.assertEqual(inbox.registered_handles, handles)
        self.assertLessEqual(inbox.retained_bytes, MotionResultInbox.MAX_BYTES)

    def test_byte_capacity_rejects_without_overwriting_pending(self):
        from mc2p.motion_nav.world_model import BlockGeometry
        from mc2p.motion_nav.world_snapshot import SealedSnapshot, canonical_snapshot_bytes
        inbox, worker = self.inbox()
        first, fact = next((position, fact) for position, fact in self.snapshot.facts if fact.block is not None)
        # A formally typed material identifier makes a genuinely large immutable
        # snapshot. The byte boundary is decided before calculation/submission.
        material = BlockGeometry.full_cube("minecraft:" + "x" * (12 * 1024 * 1024))
        facts = tuple((position, replace(value, block=material) if position == first else value)
                      for position, value in self.snapshot.facts)
        identity = replace(self.snapshot.identity, digest="0" * 64)
        data = canonical_snapshot_bytes(identity, facts, self.snapshot.dependency_versions)
        snapshot = SealedSnapshot(replace(identity, digest=hashlib.sha256(data).hexdigest()),
                                  data, facts, self.snapshot.dependency_versions)
        lease = SnapshotLease("large-real-snapshot", snapshot)
        request = replace(self.request, job_token="large-snapshot-job", snapshot_lease_token=lease.lease_token,
                          snapshot_identity=snapshot.identity,
                          async_identity=replace(self.request.async_identity, revision=2))
        self.assertIs(inbox.register_preparation(request, lease).status, PreparedSubmitStatus.QUEUE_FULL)
        self.assertEqual(inbox.active_identities, (self.request.async_identity,))
        self.assertLessEqual(inbox.retained_bytes, MotionResultInbox.MAX_BYTES)

    def test_inline_submit_during_poll_is_not_lost(self):
        from types import SimpleNamespace
        from tests.sim.runner import InlineMotionWorker
        worker = InlineMotionWorker()
        first = SimpleNamespace(work_identity=self.request.async_identity)
        second = SimpleNamespace(work_identity=replace(self.request.async_identity, revision=2))
        worker.submit(first)
        entered, release = threading.Event(), threading.Event()
        def execute(job):
            if job is first:
                entered.set()
                self.assertTrue(release.wait(10))
            return job
        results = []
        with patch("tests.sim.runner.motion_worker._execute_job", side_effect=execute):
            thread = threading.Thread(target=lambda: results.extend(worker.poll_available()))
            thread.start()
            self.assertTrue(entered.wait(10))
            worker.submit(second)
            release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [first])
            self.assertEqual(worker.poll_available(), (second,))
            self.assertEqual(worker.poll_available(), ())

    def test_successor_transfers_same_inbox_receiver_and_close_is_idempotent(self):
        from mc2p.motion_nav.navigation_session import NavigationSession
        from mc2p.motion_nav.navigation_lifecycle import NavigationTransitionAction
        from tests.motion_nav import test_navigation_session as session_fixtures
        worker = QueuedWorker()
        planner = session_fixtures._InlinePlanner()
        session = NavigationSession("receiver-original", session_fixtures.NavigationSessionTests().profiles(),
                                    planner_worker=planner, motion_worker=worker, clock_ns=lambda: 1_000_000_000)
        self.addCleanup(session._motion_result_inbox.close)
        old = session._motion_result_inbox
        old.attach_worker(worker)
        self.assertTrue(old.wait_for_receiver(timeout=10))
        receiver_id = old.receiver_thread_id
        session._transition(NavigationTransitionAction.MARK_FAILED, "test_receiver_terminal")
        successor = session.spawn_successor("receiver-successor", task_id="receiver-new-task")
        self.addCleanup(successor.close)
        self.addCleanup(successor._motion_result_inbox.close)
        self.assertTrue(old._receiver.is_alive(), "the same service consumer survives shell transfer")
        inbox = successor._motion_result_inbox
        self.assertIs(inbox, old)
        self.assertEqual(inbox.receiver_thread_id, receiver_id)
        inbox.publish_current_facts(self.current)
        inbox.attach_worker(worker)
        self.assertIs(inbox.register_preparation(self.request, self.lease).status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        worker.consumer_threads.clear()
        worker.put(self.message)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        self.assertIs(inbox.prepared_admission(self.request.async_identity).status, PreparedAdmissionStatus.REGISTERED)
        self.assertEqual(worker.consumer_threads, {inbox.receiver_thread_id})
        inbox.close()
        inbox.close()
        self.assertFalse(inbox._receiver.is_alive())

    def test_timed_out_close_returns_claim_only_when_receiver_really_exits(self):
        inbox, worker = self.inbox()
        successor = MotionResultInbox(current_facts=self.current)
        self.addCleanup(successor.close)
        entered, release = threading.Event(), threading.Event()
        from mc2p.motion_nav import prepared_motion_validation
        original = prepared_motion_validation._validate_for_registration

        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args)

        with patch.object(prepared_motion_validation, "_validate_for_registration", side_effect=blocked):
            worker.put(self.message)
            inbox.wake_receiver()
            try:
                self.assertTrue(entered.wait(10))
                inbox.close(timeout_seconds=.05, close_worker=False)
                self.assertTrue(inbox._receiver.is_alive(), "the validation barrier outlives bounded close")
                with self.assertRaises(ContractViolation):
                    successor.attach_worker(worker)
            finally:
                release.set()
                inbox._receiver.join(10)
        self.assertFalse(inbox._receiver.is_alive())
        try:
            successor.attach_worker(worker)
        except ContractViolation as error:
            self.fail(f"the exited receiver still owns the worker: {error}")
        # Repeated old cleanup must not remove the new consumer's claim.
        inbox.close(close_worker=False)
        inbox.close(close_worker=False)
        third = MotionResultInbox()
        self.addCleanup(third.close)
        with self.assertRaises(ContractViolation):
            third.attach_worker(worker)
        self.assertIs(successor.register_preparation(self.request, self.lease).status,
                      PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(successor.wait_for_receiver(timeout=10))
        worker.consumer_threads.clear()
        worker.put(self.message)
        self.assertTrue(successor.wait_for_receiver(timeout=10))
        self.assertIs(successor.prepared_admission(self.request.async_identity).status,
                      PreparedAdmissionStatus.REGISTERED)
        self.assertEqual(worker.consumer_threads, {successor.receiver_thread_id})

    def test_live_candidate_token_stays_reserved_after_seventy_history_entries(self):
        inbox, worker = self.inbox()
        handle = self.deliver(inbox, worker).handle
        inbox.retire(self.request.async_identity)
        for index in range(70):
            request = replace(self.request, job_token=f"rolling-history-{index}",
                              async_identity=replace(self.request.async_identity, revision=index + 2))
            self.assertIs(inbox.register_preparation(request, self.lease).status, PreparedSubmitStatus.ACCEPTED)
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
            worker.put(seal_compute_failure(PreparedComputeResult(
                PreparedComputeStatus.BLOCKED, request.job_token, PreparedComputeCounts())))
            self.assertTrue(inbox.wait_for_receiver(timeout=10))
            self.assertIs(inbox.prepared_compute_result(request.async_identity).status,
                          PreparedComputeStatus.BLOCKED)
            inbox.retire(request.async_identity)
        self.assertEqual(len(inbox._token_history), MotionResultInbox.MAX_HISTORY)
        self.assertNotIn(self.request.job_token, inbox._token_history)
        reused = replace(self.request, async_identity=replace(self.request.async_identity, revision=100))
        with self.subTest(check="refuse_live_candidate_token"):
            self.assertIs(inbox.register_preparation(reused, self.lease).status, PreparedSubmitStatus.QUEUE_FULL)
        worker.put(seal_compute_failure(PreparedComputeResult(
            PreparedComputeStatus.BLOCKED, self.request.job_token, PreparedComputeCounts())))
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        with self.subTest(check="old_wire_cannot_finish_new_identity"):
            self.assertNotIn(reused.async_identity, inbox.active_identities)
            self.assertIsNone(inbox.prepared_compute_result(reused.async_identity))
        self.assertEqual(inbox.registered_handles, (handle,))
        self.assertIs(inbox.query_prepared(self.request.async_identity,
                                          movement_tick=self.current.movement_tick).status,
                      PreparedQueryStatus.READY_MATCH)

    def test_unrelated_current_revision_does_not_reject_same_dependencies(self):
        current = replace(self.current, snapshot_identity=replace(self.snapshot.identity,
                          geometry_revision=self.snapshot.identity.geometry_revision + 1))
        self.assertIs(self.validate(current=current).status, PreparedAdmissionStatus.REGISTERED)

    def native_preparation(self):
        from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
        from mc2p.motion_nav.world_model import ObservationStamp, WorldKnowledge, WorldSessionId
        world = WorldKnowledge(WorldSessionId(self.request.world_session_id), max_known_cells=16_384)
        stamp = ObservationStamp(world.session, 1, 1, "native-receiver-clock", 50_000_000)
        source_facts = self.bundle.proof.dependency_facts
        world.observe_blocks(stamp, {position: fact.block for position, fact in source_facts
                                    if fact.block is not None})
        world.confirm_air(stamp, tuple(position for position, fact in source_facts if fact.block is None))
        worker = QueuedWorker()
        inbox = MotionResultInbox(worker=worker, world=world)
        self.addCleanup(inbox.close)
        region = self.snapshot.identity.bounds
        bounds = PhysicsWorldBounds(int(region.min_x), int(region.max_x) - 1,
                                    int(region.min_y), int(region.max_y) - 1,
                                    int(region.min_z), int(region.max_z) - 1)
        inbox.prepare_snapshot(bounds, ruleset=self.request.ruleset, identity=self.request.async_identity)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        lease = inbox.prepare_snapshot(bounds, ruleset=self.request.ruleset, identity=self.request.async_identity).lease
        self.assertIsNotNone(lease)
        request = replace(self.request, snapshot_identity=lease.identity, snapshot_lease_token=lease.lease_token)
        prepared = prepare_fixed_motion(request, lease.snapshot)
        self.assertIs(prepared.status, PreparedComputeStatus.PREPARED)
        current = replace(self.current, snapshot_identity=lease.identity,
                          dependency_versions=prepared.bundle.proof.dependency_versions)
        inbox.publish_current_facts(current)
        self.assertIs(inbox.register_preparation(request, lease).status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        self.assertTrue(worker.submitted.is_set(), inbox.prepared_compute_result(request.async_identity))
        return inbox, worker, world, lease, request, prepared.bundle, current

    def test_native_lease_liveness_rejects_foreign_owner_and_same_token_copy(self):
        inbox, worker, world, lease, request, bundle, current = self.native_preparation()
        self.assertTrue(callable(getattr(world, "snapshot_lease_is_live", None)),
                        "the formal world cannot confirm its own sealed lease identity")
        self.assertTrue(world.snapshot_lease_is_live(lease))
        from mc2p.motion_nav.world_model import WorldKnowledge
        foreign = WorldKnowledge(world.session)
        self.assertFalse(foreign.snapshot_lease_is_live(lease))
        restored = pickle.loads(pickle.dumps(lease.snapshot, protocol=5))
        forged = SnapshotLease(lease.lease_token, restored)
        self.assertFalse(world.snapshot_lease_is_live(forged))
        self.assertFalse(world.release_snapshot(lease.lease_token, expected_snapshot=restored))
        self.assertFalse(world.release_snapshot(lease.lease_token, expected_snapshot=restored))
        self.assertTrue(world.snapshot_lease_is_live(lease))
        self.assertEqual(lease.snapshot.cell(bundle.proof.dependency_facts[0][0]),
                         bundle.proof.dependency_facts[0][1])

    def test_receiver_rejects_released_native_lease(self):
        inbox, worker, world, lease, request, bundle, current = self.native_preparation()
        self.assertTrue(world.release_snapshot(lease.lease_token))
        worker.put(seal_bundle(bundle))
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        result = inbox.prepared_admission(request.async_identity)
        self.assertIsNotNone(result, "the native message must reach the existing admission record")
        self.assertIs(result.reason, PreparedRejectReason.WORLD_SNAPSHOT)
        self.assertEqual(inbox.registered_handles, ())

    def test_receiver_reads_real_dependency_changes_and_rejects_incomplete_history(self):
        from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp
        from mc2p.motion_nav.world_snapshot import DependencyCheck, DependencyStatus
        for mode in ("changed", "incomplete"):
            with self.subTest(mode=mode):
                inbox, worker, world, lease, request, bundle, current = self.native_preparation()
                if mode == "changed":
                    position = bundle.proof.dependency_facts[0][0]
                    world.observe_blocks(ObservationStamp(world.session, 2, 2, "native-receiver-clock", 100_000_000),
                                         {position: BlockGeometry.full_cube("minecraft:stone")})
                    worker.put(seal_bundle(bundle))
                    self.assertTrue(inbox.wait_for_receiver(timeout=10))
                else:
                    with patch.object(world, "dependency_changes", return_value=DependencyCheck(
                            DependencyStatus.HISTORY_INCOMPLETE)):
                        worker.put(seal_bundle(bundle))
                        self.assertTrue(inbox.wait_for_receiver(timeout=10))
                result = inbox.prepared_admission(request.async_identity)
                self.assertIsNotNone(result, "the native message must reach the existing admission record")
                self.assertIs(result.reason, PreparedRejectReason.DEPENDENCY)
                self.assertEqual(inbox.registered_handles, ())

    def test_forged_lease_rejection_and_cleanup_leave_the_real_lease_live(self):
        inbox, worker, world, lease, request, bundle, current = self.native_preparation()
        forged = SnapshotLease(lease.lease_token, pickle.loads(pickle.dumps(lease.snapshot, protocol=5)))
        other_worker = QueuedWorker()
        other = MotionResultInbox(worker=other_worker, world=world, current_facts=current)
        self.addCleanup(other.close)
        self.assertIs(other.register_preparation(request, forged).status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(other.wait_for_receiver(timeout=10))
        other_worker.put(seal_bundle(bundle))
        self.assertTrue(other.wait_for_receiver(timeout=10))
        self.assertIs(other.prepared_admission(request.async_identity).reason, PreparedRejectReason.WORLD_SNAPSHOT)
        other.retire(request.async_identity)
        self.assertTrue(other.wait_for_receiver(timeout=10))
        other.close()
        other.close()
        self.assertTrue(world.snapshot_lease_is_live(lease), "foreign cleanup must not consume the real owner's lease")
        self.assertTrue(world.release_snapshot(lease.lease_token, expected_snapshot=lease.snapshot))

    def test_copied_lease_wrapper_cannot_submit_or_release_the_real_lease(self):
        inbox, worker, world, lease, request, bundle, current = self.native_preparation()
        alias = SnapshotLease(lease.lease_token, lease.snapshot)
        self.assertIsNot(alias, lease)
        self.assertIs(alias.snapshot, lease.snapshot)
        with self.subTest(check="source_identity"):
            self.assertFalse(world.snapshot_lease_is_live(alias))
        other_worker = QueuedWorker()
        other = MotionResultInbox(worker=other_worker, world=world, current_facts=current)
        self.addCleanup(other.close)
        self.assertIs(other.register_preparation(request, alias).status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(other.wait_for_receiver(timeout=10))
        with self.subTest(check="no_worker_submission"):
            self.assertEqual(len(other_worker.jobs), 0)
            result = other.prepared_admission(request.async_identity)
            self.assertIsNotNone(result)
            self.assertIs(result.reason, PreparedRejectReason.WORLD_SNAPSHOT)
        other.retire(request.async_identity)
        self.assertTrue(other.wait_for_receiver(timeout=10))
        other.close()
        other.close()
        with self.subTest(check="real_owner_survives_cleanup"):
            self.assertTrue(world.snapshot_lease_is_live(lease))
            self.assertEqual(lease.snapshot.cell(bundle.proof.dependency_facts[0][0]),
                             bundle.proof.dependency_facts[0][1])
        with self.subTest(check="atomic_exact_lease_release"):
            self.assertFalse(world.release_snapshot(lease.lease_token, expected_snapshot=lease.snapshot,
                                                   expected_lease=alias))
            self.assertFalse(world.release_snapshot(lease.lease_token, expected_snapshot=lease.snapshot,
                                                   expected_lease=alias))
            self.assertTrue(world.snapshot_lease_is_live(lease))
            self.assertTrue(world.release_snapshot(lease.lease_token, expected_snapshot=lease.snapshot,
                                                  expected_lease=lease))
            self.assertFalse(world.release_snapshot(lease.lease_token, expected_snapshot=lease.snapshot,
                                                   expected_lease=lease))


if __name__ == "__main__":
    unittest.main()
