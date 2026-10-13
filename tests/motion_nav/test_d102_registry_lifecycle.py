"""Proof uses survive computation retirement and bounded service shutdown.

Activation and ledger values are exercised directly. These tests never advance
an action or send movement input; Runtime integration belongs to Tasks 7/8.
"""
from dataclasses import replace
from contextlib import redirect_stderr
from types import SimpleNamespace
import errno
import io
import time
import threading
import unittest
from unittest.mock import Mock, patch

from mc2p.contracts.action_v1 import ActionSnapshotV1, LookV1, MovementTickWindowV1, MovementV1
from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.motion_activation import (
    ActivationResult, ActivationStatus, PreparedActivationAcceptance,
    PreparedActivationDelivery, PreparedActivationTransaction, StreamActivationBinding,
)
from mc2p.motion_nav.body_control import HandoffDisposition, HandoffEvidence
from mc2p.motion_nav.motion_worker import MotionResultInbox, MotionSolverWorker
from mc2p.motion_nav.online_motion import InputApplicationLedger, InputApplicationStatus
from mc2p.motion_nav import prepared_motion as values
from mc2p.motion_nav.prepared_motion import PreparedAdmissionStatus, PreparedComputeStatus, PreparedSubmitStatus
from mc2p.motion_nav.prepared_motion_proof import seal_bundle
from mc2p.motion_nav.world_model import WorldSessionId
from tests.motion_nav import test_d102_receiver as receiver_fixtures

QueuedWorker = receiver_fixtures.QueuedWorker
reseal = receiver_fixtures.reseal


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        receiver_fixtures.ReceiverTests.setUpClass()
        for name in ("request", "snapshot", "bundle", "message", "current", "lease"):
            setattr(cls, name, getattr(receiver_fixtures.ReceiverTests, name))

    def uses(self):
        self.assertTrue(hasattr(values, "ProofUse"), "the shared registry lacks the three proof uses")
        return values.ProofUse

    def ready(self, *, inbox=None, worker=None, request=None, bundle=None, lease=None):
        worker = worker or QueuedWorker()
        inbox = inbox or MotionResultInbox(worker=worker, current_facts=self.current)
        self.addCleanup(inbox.close)
        request, bundle, lease = request or self.request, bundle or self.bundle, lease or self.lease
        self.assertIs(inbox.register_preparation(request, lease).status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        worker.put(seal_bundle(bundle))
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        admission = inbox.prepared_admission(request.async_identity)
        self.assertIs(admission.status, PreparedAdmissionStatus.REGISTERED)
        return inbox, worker, admission.handle

    def transaction(self, handle, *, token="transaction-first", sequence=101):
        window = handle.hot_view.requested_window
        action = ActionSnapshotV1("lifecycle-episode", sequence, 1, 1_000_000,
                                  movement=self.request.commands[0].movement)
        binding = StreamActivationBinding(
            handle.proof_token, handle.generation_id, handle.owner_id,
            handle.hot_view.ledger_id, handle.hot_view.world_session_id, sequence,
            MovementTickWindowV1(window.earliest_start_tick, window.latest_start_tick),
            self.current.movement_tick, action)
        transaction = PreparedActivationTransaction(token, binding, None, action,
                                                    "permission-first", "handoff-first")
        handoff = HandoffEvidence(handle.owner_id, WorldSessionId(handle.hot_view.world_session_id),
                                  HandoffDisposition.TRANSFERABLE, 1, self.current.movement_tick,
                                  action.movement, "supervisor_selected_action", handle.owner_id,
                                  sequence, self.request.route_revision, handle.action_index)
        return transaction, handoff

    def accept(self, inbox, handle, **changes):
        self.uses()
        transaction, handoff = self.transaction(handle, **changes)
        accepted = inbox.accept_activation(handle, transaction, handoff)
        self.assertIsInstance(accepted, PreparedActivationAcceptance)
        return transaction, accepted

    def activate(self, inbox, handle, **changes):
        transaction, accepted = self.accept(inbox, handle, **changes)
        delivered = PreparedActivationDelivery(transaction.transaction_token, accepted.acceptance_token,
                                                transaction.binding, "delivery-first", transaction.new_action)
        self.assertIs(inbox.commit_activation(accepted, delivered).status, ActivationStatus.ACTIVATED)
        return transaction.binding

    def evidence(self, handle, use, *, commands=None, sequence=101):
        consumer = values.ProofConsumerIdentity.from_handle(handle, use)
        action = self.transaction(handle, sequence=sequence)[0].new_action
        ledger = InputApplicationLedger()
        ledger.submit(WorldSessionId(handle.hot_view.world_session_id), action,
                      requested_first_tick=handle.hot_view.requested_window.earliest_start_tick,
                      latest_allowed_first_tick=handle.hot_view.requested_window.latest_start_tick)
        terminal = ledger.reject(sequence, at_tick=self.current.movement_tick + 1)
        handoff = HandoffEvidence(handle.owner_id, WorldSessionId(handle.hot_view.world_session_id),
                                  HandoffDisposition.QUIESCENT, 2, self.current.movement_tick + 2,
                                  MovementV1(), "safe_action_finished")
        exit_id = f"exit/{handle.proof_token}/{use.value}"
        action_exit = values.ProofActionExitEvidence(consumer, exit_id, handoff)
        confirmed = values.ProofSupervisorReleaseEvidence(consumer, exit_id, handoff)
        return values.ProofReleaseEvidence(consumer, (terminal,) if commands is None else commands,
                                          action_exit, confirmed)

    def test_candidate_is_retained_before_job_retirement(self):
        inbox, _, handle = self.ready()
        uses = self.uses()
        self.assertTrue(inbox.usage_active(handle, uses.CANDIDATE))
        self.assertTrue(inbox.job_active(self.request.async_identity))
        inbox.retire(self.request.async_identity)
        self.assertFalse(inbox.job_active(self.request.async_identity))
        self.assertTrue(inbox.usage_active(handle, uses.CANDIDATE))
        self.activate(inbox, handle)
        self.assertFalse(inbox.usage_active(handle, uses.CANDIDATE))
        self.assertTrue(inbox.usage_active(handle, uses.EXECUTING))

    def test_next_preparation_keeps_current_execution_ref(self):
        inbox, worker, handle = self.ready()
        uses = self.uses()
        self.activate(inbox, handle)
        inbox.retire(self.request.async_identity)
        stream = replace(self.request.stream, generation_id="next-stream", request_id="next-request", action_index=1)
        request = replace(self.request, job_token="next-job", stream=stream,
                          async_identity=replace(self.request.async_identity, subject_id=stream.request_id, revision=2))
        bundle = reseal(replace(self.bundle, request=request, proof=replace(self.bundle.proof, stream=stream)))
        inbox.publish_current_facts(replace(self.current, stream=stream))
        self.ready(inbox=inbox, worker=worker, request=request, bundle=bundle)
        self.assertFalse(inbox.job_active(self.request.async_identity))
        self.assertTrue(inbox.usage_active(handle, uses.EXECUTING))
        self.assertTrue(handle.hot_view.safety_tails)

    def test_executor_candidate_discard_does_not_release_execution_or_tail(self):
        from types import SimpleNamespace
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
        inbox, _, handle = self.ready()
        uses = self.uses()
        executor = ActionRouteExecutor.__new__(ActionRouteExecutor)
        executor.route = SimpleNamespace(route_id=handle.route_id)
        self.assertTrue(callable(getattr(executor, "bind_prepared_result_inbox", None)),
                        "the existing executor cannot publish candidate use retirement")
        executor.bind_prepared_result_inbox(inbox, owner_id=handle.owner_id)
        self.assertEqual(executor.discard_prepared_candidates(action_index=handle.action_index), 1)
        self.assertFalse(inbox.usage_active(handle, uses.CANDIDATE))
        # Install another independently registered candidate after retirement.
        inbox.retire(self.request.async_identity)
        request = replace(self.request, job_token="executor-next-token",
                          async_identity=replace(self.request.async_identity, revision=2))
        worker = inbox._worker
        _, _, executing = self.ready(inbox=inbox, worker=worker, request=request,
                                     bundle=reseal(replace(self.bundle, request=request)))
        self.activate(inbox, executing)
        inbox.retain_safe_tail(executing, action_index=executing.action_index)
        self.assertEqual(executor.discard_prepared_candidates(action_index=executing.action_index), 0)
        self.assertTrue(inbox.usage_active(executing, uses.EXECUTING))
        self.assertTrue(inbox.usage_active(executing, uses.SAFE_TAIL))

    def test_stale_scope_keeps_valid_tail(self):
        inbox, _, handle = self.ready()
        uses = self.uses()
        self.activate(inbox, handle)
        self.assertIs(inbox.retain_safe_tail(handle, action_index=handle.action_index).status,
                      values.ReferenceStatus.RETAINED)
        inbox.retire(self.request.async_identity)
        inbox.publish_current_facts(replace(self.current,
            scope=replace(self.current.scope, generation=self.current.scope.generation + 1),
            stream=replace(self.current.stream, generation_id="stale-scope-replacement")))
        self.assertTrue(inbox.usage_active(handle, uses.SAFE_TAIL))
        self.assertTrue(inbox.has_registered_handle(handle))
        report = inbox.close(timeout_seconds=.5)
        self.assertTrue(inbox.usage_active(handle, uses.SAFE_TAIL))
        self.assertEqual(report.safe_tail_references, 1)
        self.assertEqual(report.live_workers + report.live_receivers, 0)
        self.assertFalse(report.fully_released)

    def test_each_missing_release_fact_keeps_lease(self):
        uses = self.uses()
        for use in (uses.EXECUTING, uses.SAFE_TAIL):
            for field in ("command_terminals", "action_handoff", "supervisor_confirmation", "consumer"):
                with self.subTest(use=use, missing=field):
                    inbox, _, handle = self.ready()
                    self.activate(inbox, handle)
                    inbox.retire(self.request.async_identity)
                    inbox.retain_safe_tail(handle, action_index=handle.action_index)
                    evidence = replace(self.evidence(handle, use), **{field: None})
                    before = inbox.retained_bytes
                    self.assertIs(inbox.release_usage(handle, use=use, evidence=evidence).status,
                                  values.ReferenceStatus.REJECTED)
                    self.assertTrue(inbox.usage_active(handle, use))
                    self.assertEqual(inbox.retained_bytes, before)

    def test_wrong_consumer_nonterminal_and_foreign_commands_are_rejected(self):
        inbox, _, handle = self.ready()
        use = self.uses().EXECUTING
        self.activate(inbox, handle)
        evidence = self.evidence(handle, use)
        terminal = evidence.command_terminals[0]
        invalid = (
            replace(evidence, consumer=replace(evidence.consumer, action_index=1)),
            replace(evidence, consumer=replace(evidence.consumer, use=self.uses().SAFE_TAIL)),
            replace(evidence, command_terminals=()),
            replace(evidence, command_terminals=(replace(terminal, status=InputApplicationStatus.IN_FLIGHT),)),
            replace(evidence, command_terminals=(replace(terminal, status=InputApplicationStatus.AMBIGUOUS),)),
            replace(evidence, command_terminals=(replace(terminal, session=WorldSessionId("foreign-world")),)),
            replace(evidence, action_handoff=replace(evidence.action_handoff,
                handoff=replace(evidence.action_handoff.handoff, disposition=HandoffDisposition.RETAIN))),
            replace(evidence, supervisor_confirmation=replace(evidence.supervisor_confirmation,
                handoff=replace(evidence.supervisor_confirmation.handoff, owner_id="foreign-owner"))),
        )
        for case in invalid:
            with self.subTest(evidence=case):
                self.assertIs(inbox.release_usage(handle, use=use, evidence=case).status,
                              values.ReferenceStatus.REJECTED)
                self.assertTrue(inbox.usage_active(handle, use))

    def test_superseded_without_client_revocation_keeps_both_body_uses(self):
        inbox, _, handle = self.ready()
        self.activate(inbox, handle)
        inbox.retire(self.request.async_identity)
        inbox.retain_safe_tail(handle, action_index=handle.action_index)
        first = self.transaction(handle)[0].new_action
        later = replace(first, request_sequence_id=102, movement=MovementV1())
        world = WorldSessionId(handle.hot_view.world_session_id)
        window = handle.hot_view.requested_window
        ledger = InputApplicationLedger()
        ledger.submit(world, first, requested_first_tick=window.earliest_start_tick,
                      latest_allowed_first_tick=window.latest_start_tick)
        ledger.submit(world, later, requested_first_tick=window.earliest_start_tick)
        movement = later.movement
        ledger.observe_sample(ClientInputApplicationV1("mc2p.input-application.v1",
            window.earliest_start_tick, later.episode_id, later.request_sequence_id, 1, "leased",
            movement.forward, movement.strafe, movement.jump, movement.sneak, movement.sprint))
        superseded = ledger.record(first.request_sequence_id)
        self.assertIs(superseded.status, InputApplicationStatus.SUPERSEDED)
        for use in (self.uses().EXECUTING, self.uses().SAFE_TAIL):
            evidence = self.evidence(handle, use, commands=(superseded,))
            result = inbox.release_usage(handle, use=use, evidence=evidence)
            self.assertIs(result.status, values.ReferenceStatus.REJECTED)
            self.assertIs(result.reason, values.ReferenceRejectReason.COMMAND_REVOCATION_UNCONFIRMED)
            self.assertTrue(inbox.usage_active(handle, use))
        movement = first.movement
        applied = ledger.observe_sample(ClientInputApplicationV1("mc2p.input-application.v1",
            window.latest_start_tick, first.episode_id, first.request_sequence_id, 1, "leased",
            movement.forward, movement.strafe, movement.jump, movement.sneak, movement.sprint))
        self.assertIs(applied.status, InputApplicationStatus.APPLIED)
        self.assertEqual(inbox.close().lease_count, 1)

    def test_entering_current_action_cannot_be_its_exit(self):
        inbox, _, handle = self.ready()
        self.activate(inbox, handle)
        evidence = self.evidence(handle, self.uses().EXECUTING)
        entering = replace(evidence.action_handoff.handoff, disposition=HandoffDisposition.TRANSFERABLE,
            successor_id=handle.owner_id, successor_action_index=handle.action_index,
            successor_route_revision=self.request.route_revision, control_sequence=103,
            movement=self.request.commands[0].movement,
            reason="permission_to_start_current_action")
        result = inbox.release_usage(handle, use=self.uses().EXECUTING,
            evidence=replace(evidence,
                action_handoff=replace(evidence.action_handoff, handoff=entering),
                supervisor_confirmation=replace(evidence.supervisor_confirmation, handoff=entering)))
        self.assertIs(result.status, values.ReferenceStatus.REJECTED)
        self.assertTrue(inbox.usage_active(handle, self.uses().EXECUTING))

    def test_exit_and_supervisor_each_bind_exact_action_stream_and_exit_event(self):
        inbox, _, handle = self.ready()
        use = self.uses().EXECUTING
        self.activate(inbox, handle)
        evidence = self.evidence(handle, use)
        other_action = replace(evidence.action_handoff.consumer, action_index=handle.action_index + 1)
        other_generation = replace(evidence.supervisor_confirmation.consumer,
                                   generation_id="prior-stream-generation")
        old_proof = replace(evidence.supervisor_confirmation.consumer, proof_token="previous-proof-token")
        cases = (
            replace(evidence, action_handoff=replace(evidence.action_handoff, consumer=other_action)),
            replace(evidence, action_handoff=replace(evidence.action_handoff,
                consumer=replace(evidence.action_handoff.consumer, generation_id="prior-stream-generation"))),
            replace(evidence, action_handoff=replace(evidence.action_handoff,
                consumer=replace(evidence.action_handoff.consumer, route_id="other-route"))),
            replace(evidence, action_handoff=replace(evidence.action_handoff,
                consumer=replace(evidence.action_handoff.consumer, use=self.uses().SAFE_TAIL))),
            replace(evidence, supervisor_confirmation=replace(evidence.supervisor_confirmation,
                consumer=replace(evidence.supervisor_confirmation.consumer, action_index=handle.action_index + 1))),
            replace(evidence, supervisor_confirmation=replace(evidence.supervisor_confirmation,
                consumer=other_generation)),
            replace(evidence, supervisor_confirmation=replace(evidence.supervisor_confirmation,
                consumer=replace(evidence.supervisor_confirmation.consumer, route_id="other-route"))),
            replace(evidence, supervisor_confirmation=replace(evidence.supervisor_confirmation,
                consumer=replace(evidence.supervisor_confirmation.consumer, use=self.uses().SAFE_TAIL))),
            replace(evidence, supervisor_confirmation=replace(evidence.supervisor_confirmation,
                consumer=old_proof)),
            replace(evidence, supervisor_confirmation=replace(evidence.supervisor_confirmation,
                confirmed_exit_id="previous-action-exit")),
        )
        for case in cases:
            with self.subTest(case=case):
                result = inbox.release_usage(handle, use=use, evidence=case)
                self.assertIs(result.status, values.ReferenceStatus.REJECTED)
                self.assertTrue(inbox.usage_active(handle, use))
        with self.assertRaises(ContractViolation):
            replace(evidence, action_handoff=evidence.action_handoff.handoff)

    def test_executor_accepts_a_narrow_proof_use_port(self):
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
        inbox, _, handle = self.ready()
        class Port:
            registered_handles = (handle,)
            def release_usage(self, observed, *, use, evidence):
                return inbox.release_usage(observed, use=use, evidence=evidence)
        executor = ActionRouteExecutor.__new__(ActionRouteExecutor)
        executor.route = SimpleNamespace(route_id=handle.route_id)
        executor.bind_prepared_result_inbox(Port(), owner_id=handle.owner_id)
        self.assertEqual(executor.discard_prepared_candidates(action_index=handle.action_index), 1)
        self.assertFalse(inbox.usage_active(handle, self.uses().CANDIDATE))

    def test_release_covers_every_actual_command_not_just_first(self):
        inbox, _, handle = self.ready()
        use = self.uses().EXECUTING
        self.activate(inbox, handle)
        next_action = replace(self.transaction(handle)[0].new_action, request_sequence_id=104,
                              movement=self.request.commands[1].movement)
        self.assertIs(inbox.record_command(handle, command_index=1, action=next_action).status,
                      values.ReferenceStatus.RETAINED)
        evidence = self.evidence(handle, use)
        self.assertIs(inbox.release_usage(handle, use=use, evidence=evidence).status, values.ReferenceStatus.REJECTED)
        ledger = InputApplicationLedger()
        ledger.submit(WorldSessionId(handle.hot_view.world_session_id), next_action,
                      requested_first_tick=self.current.movement_tick + 2)
        final = ledger.reject(104, at_tick=self.current.movement_tick + 3)
        ended = replace(evidence.action_handoff.handoff, movement_tick_id=self.current.movement_tick + 4)
        self.assertIs(inbox.release_usage(handle, use=use,
            evidence=replace(evidence, command_terminals=evidence.command_terminals + (final,),
                             action_handoff=replace(evidence.action_handoff, handoff=ended),
                             supervisor_confirmation=replace(evidence.supervisor_confirmation, handoff=ended))).status,
            values.ReferenceStatus.RELEASED)

    def test_close_keeps_tail_until_all_release_facts(self):
        inbox, _, handle = self.ready()
        uses = self.uses()
        self.activate(inbox, handle)
        inbox.retire(self.request.async_identity)
        inbox.retain_safe_tail(handle, action_index=handle.action_index)
        initial = inbox.close(timeout_seconds=.5)
        self.assertEqual((initial.executing_references, initial.safe_tail_references, initial.lease_count), (1, 1, 1))
        self.assertGreater(initial.retained_bytes, 0)
        self.assertFalse(initial.fully_released)
        self.assertTrue(handle.hot_view.safety_tails)
        self.assertTrue(handle.hot_view.inputs)
        for use in (uses.EXECUTING, uses.SAFE_TAIL):
            self.assertIs(inbox.release_usage(handle, use=use, evidence=self.evidence(handle, use)).status,
                          values.ReferenceStatus.RELEASED)
        final = inbox.close(timeout_seconds=.5)
        self.assertTrue(final.fully_released)
        self.assertEqual(final.live_workers + final.live_receivers + final.lease_count, 0)
        self.assertEqual(final.retained_bytes, 0)

    def test_last_reference_reclaims_bytes_and_lease(self):
        inbox, _, handle = self.ready()
        uses = self.uses()
        self.activate(inbox, handle)
        inbox.retire(self.request.async_identity)
        inbox.retain_safe_tail(handle, action_index=handle.action_index)
        initial = inbox.retained_bytes
        inbox.release_usage(handle, use=uses.EXECUTING, evidence=self.evidence(handle, uses.EXECUTING))
        self.assertTrue(inbox.has_registered_handle(handle))
        self.assertEqual(inbox.retained_bytes, initial)
        inbox.release_usage(handle, use=uses.SAFE_TAIL, evidence=self.evidence(handle, uses.SAFE_TAIL))
        self.assertFalse(inbox.has_registered_handle(handle))
        self.assertEqual(inbox.retained_bytes, 0)

    def test_acceptance_is_only_a_temporary_reference_and_abort_keeps_old_action(self):
        inbox, _, handle = self.ready()
        uses = self.uses()
        _, accepted = self.accept(inbox, handle)
        self.assertTrue(inbox.usage_active(handle, uses.CANDIDATE))
        self.assertFalse(inbox.usage_active(handle, uses.EXECUTING))
        before = inbox.retained_bytes
        self.assertIs(inbox.abort_activation(accepted).status, values.ReferenceStatus.RELEASED)
        self.assertTrue(inbox.usage_active(handle, uses.CANDIDATE))
        self.assertEqual(inbox.retained_bytes, before)
        self.activate(inbox, handle, token="retry-transaction")
        # An unrelated failed/aborted accept cannot release the old execution.
        self.assertIs(inbox.abort_activation(accepted).status, values.ReferenceStatus.REJECTED)
        self.assertTrue(inbox.usage_active(handle, uses.EXECUTING))

    def test_commit_rechecks_cancel_scope_delivery_and_exact_acceptance(self):
        self.uses()
        for changed in ("cancel", "scope", "delivery", "acceptance_copy", "world", "damage", "body"):
            with self.subTest(changed=changed):
                inbox, _, handle = self.ready()
                transaction, accepted = self.accept(inbox, handle)
                delivered = PreparedActivationDelivery(transaction.transaction_token, accepted.acceptance_token,
                    transaction.binding, "delivery-first", transaction.new_action)
                if changed == "cancel":
                    self.assertTrue(inbox.release_candidate(handle))
                elif changed == "scope":
                    inbox.publish_current_facts(replace(self.current,
                        scope=replace(self.current.scope, generation=self.current.scope.generation + 1)))
                elif changed == "delivery":
                    delivered = replace(delivered, acceptance_token="other-acceptance")
                elif changed == "acceptance_copy":
                    accepted = replace(accepted)
                elif changed == "world":
                    versions = self.current.dependency_versions
                    inbox.publish_current_facts(replace(self.current,
                        dependency_versions=((versions[0][0], versions[0][1] + 1),) + versions[1:]))
                elif changed == "damage":
                    inbox.publish_current_facts(replace(self.current, damage_remaining_points=0.))
                elif changed == "body":
                    inbox.publish_current_facts(replace(self.current,
                        current_state=replace(self.current.current_state, yaw_radians=.1)))
                result = inbox.commit_activation(accepted, delivered)
                if changed == "damage" and self.request.damage_remaining_points == 0:
                    self.assertIs(result.status, ActivationStatus.ACTIVATED)
                else:
                    self.assertIsInstance(result, ActivationResult)
                    self.assertIsNot(result.status, ActivationStatus.ACTIVATED)
                    self.assertFalse(inbox.usage_active(handle, self.uses().EXECUTING))

    def test_binding_handoff_and_foreign_handle_cannot_grant_execution(self):
        inbox, _, handle = self.ready()
        self.uses()
        transaction, handoff = self.transaction(handle)
        cases = (
            (replace(handle), transaction, handoff),
            (handle, transaction, replace(handoff, control_sequence=104)),
            (handle, transaction, replace(handoff, successor_action_index=1)),
            (handle, transaction, replace(handoff, owner_id="foreign-owner")),
            (handle, replace(transaction, binding=replace(transaction.binding, proof_token="foreign-proof")), handoff),
            (handle, replace(transaction, binding=replace(transaction.binding, ledger_id="foreign-ledger")), handoff),
        )
        for h, t, evidence in cases:
            with self.subTest(transaction=t):
                result = inbox.accept_activation(h, t, evidence)
                self.assertIsInstance(result, ActivationResult)
                self.assertIsNot(result.status, ActivationStatus.ACTIVATED)
        self.assertTrue(inbox.usage_active(handle, self.uses().CANDIDATE))

    def test_inflight_acceptance_keeps_bytes_until_abort(self):
        inbox, _, handle = self.ready()
        transaction, accepted = self.accept(inbox, handle)
        inbox.retire(self.request.async_identity)
        before = inbox.retained_bytes
        self.assertTrue(inbox.release_candidate(handle))
        self.assertFalse(inbox.usage_active(handle, self.uses().CANDIDATE))
        self.assertTrue(inbox.has_registered_handle(handle))
        self.assertEqual(inbox.retained_bytes, before)
        self.assertIs(inbox.abort_activation(accepted).status, values.ReferenceStatus.RELEASED)
        self.assertFalse(inbox.has_registered_handle(handle))
        self.assertEqual(inbox.retained_bytes, 0)

    def test_close_during_validation_reports_live_receiver_and_lease_then_finishes(self):
        worker = QueuedWorker()
        inbox = MotionResultInbox(worker=worker, current_facts=self.current)
        self.addCleanup(inbox.close)
        self.assertIs(inbox.register_preparation(self.request, self.lease).status, PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        entered, release = threading.Event(), threading.Event()
        from mc2p.motion_nav import prepared_motion_validation
        validate = prepared_motion_validation._validate_for_registration
        def blocked(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError("validation test barrier was not released")
            return validate(*args)
        with patch.object(prepared_motion_validation, "_validate_for_registration", side_effect=blocked):
            worker.put(self.message)
            inbox.wake_receiver()
            self.assertTrue(entered.wait(10))
            try:
                started = time.monotonic()
                report = inbox.close(timeout_seconds=.05)
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(report.timed_out)
                self.assertEqual(report.live_receivers, 1)
                self.assertEqual(report.lease_count, 1)
                self.assertGreater(report.retained_bytes, 0)
            finally:
                release.set()
                inbox._receiver.join(10)
        final = inbox.close(timeout_seconds=.5)
        self.assertTrue(final.fully_released)
        self.assertEqual(inbox.registered_handles, ())

    def test_constructor_exception_hard_exit_full_queue_and_repeat_close_are_bounded(self):
        context = Mock()
        context.Process.return_value.start.side_effect = RuntimeError("start refused")
        first, second = Mock(), Mock()
        context.Queue.side_effect = (first, second)
        with patch("mc2p.motion_nav.motion_worker.multiprocessing.get_context", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "start refused"):
                MotionSolverWorker()
        for channel in (first, second):
            self.assertTrue(channel.close.called, "constructor failure leaked a queue")
            self.assertTrue(channel.cancel_join_thread.called)
            self.assertFalse(channel.join_thread.called)
        worker = MotionSolverWorker(max_pending=2)
        self.addCleanup(worker.close)
        inbox = MotionResultInbox(worker=worker, current_facts=self.current)
        self.addCleanup(inbox.close)
        worker._process.terminate()
        worker._process.join(3)
        started = time.monotonic()
        final = inbox.close(timeout_seconds=.5)
        self.assertLess(time.monotonic() - started, 1.)
        self.assertIsNotNone(final.worker_exit_code)
        self.assertNotEqual(final.worker_exit_code, 0, "hard exit must retain its real exit code")
        self.assertTrue(final.fully_released)
        self.assertTrue(inbox.close(timeout_seconds=.5).fully_released)
        queued = QueuedWorker()
        inbox = MotionResultInbox(worker=queued, current_facts=self.current)
        self.addCleanup(inbox.close)
        self.assertIs(inbox.register_preparation(self.request, self.lease).status, PreparedSubmitStatus.ACCEPTED)
        request2 = replace(self.request, job_token="queue-second", async_identity=replace(self.request.async_identity, revision=2))
        self.assertIs(inbox.register_preparation(request2, self.lease).status, PreparedSubmitStatus.ACCEPTED)
        request3 = replace(request2, job_token="queue-third", async_identity=replace(request2.async_identity, revision=3))
        self.assertIs(inbox.register_preparation(request3, self.lease).status, PreparedSubmitStatus.QUEUE_FULL)
        self.assertEqual(set(inbox.active_identities), {self.request.async_identity, request2.async_identity})
        final = inbox.close(timeout_seconds=.5)
        self.assertTrue(final.fully_released)
        self.assertEqual(final.retained_bytes + final.lease_count, 0)

    def test_worker_transfer_keeps_one_receiver(self):
        from mc2p.motion_nav.navigation_session import NavigationSession
        from mc2p.motion_nav.navigation_lifecycle import NavigationTransitionAction
        from tests.motion_nav import test_navigation_session as fixtures
        worker, planner = QueuedWorker(), fixtures._InlinePlanner()
        session = NavigationSession("lifecycle-original", fixtures.NavigationSessionTests().profiles(),
                                    planner_worker=planner, motion_worker=worker, clock_ns=lambda: 1_000_000_000)
        inbox = session._motion_result_inbox
        self.addCleanup(inbox.close)
        inbox.attach_worker(worker)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        receiver = inbox.receiver_thread_id
        session._transition(NavigationTransitionAction.MARK_FAILED, "terminal_for_transfer")
        successor = session.spawn_successor("lifecycle-successor", task_id="successor-task")
        self.addCleanup(successor._motion_result_inbox.close)
        self.addCleanup(successor.close)
        self.assertIs(successor._motion_result_inbox, inbox)
        self.assertEqual(inbox.receiver_thread_id, receiver)
        self.assertTrue(inbox._receiver.is_alive())
        self.assertTrue(worker.is_alive())
        session.close()
        self.assertTrue(inbox._receiver.is_alive(), "old shell cannot close the transferred service")
        other = MotionResultInbox()
        self.addCleanup(other.close)
        with self.assertRaises(ContractViolation):
            other.attach_worker(worker)

    def test_full_native_request_queue_and_feeder_close_are_bounded(self):
        from mc2p.motion_nav.motion_worker import PreparedMotionJob
        worker = MotionSolverWorker(max_pending=2)
        self.addCleanup(worker.close)
        worker._process.terminate()
        worker._process.join(3)
        job = PreparedMotionJob(self.request, self.snapshot)
        self.assertTrue(worker.submit(job))
        self.assertTrue(worker.submit(job))
        self.assertFalse(worker.submit(job), "a full native FIFO cannot overwrite old jobs")
        started = time.monotonic()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            report = worker.close()
        self.assertLess(time.monotonic() - started, 5.1)
        self.assertEqual(report.live_workers + report.live_queue_feeders, 0)
        self.assertNotEqual(report.worker_exit_code, 0)
        self.assertEqual(stderr.getvalue(), "", "queue teardown raised an unhandled exception")
        self.assertLessEqual(report.expected_close_feeder_errors, 2)
        self.assertEqual(report.unexpected_feeder_errors, 0)

    def test_windows_send_published_after_pipe_close_is_cancelled(self):
        import sys
        if sys.platform != "win32":
            self.skipTest("the send-overlapped publication race is Windows specific")
        feeder = Mock(spec=threading.Thread)
        feeder.is_alive.return_value = True
        writer = Mock()
        writer._send_ov = None
        pending = Mock()
        # The first endpoint close sees no overlapped send. Its subsequent
        # bounded join observes the send published by the existing feeder.
        def joined(_timeout):
            writer._send_ov = pending
        feeder.join.side_effect = joined
        pending.cancel.side_effect = lambda: setattr(feeder.is_alive, "return_value", False)
        channel = Mock()
        channel._writer, channel._thread = writer, feeder
        worker = MotionSolverWorker.__new__(MotionSolverWorker)
        worker._closed = False
        worker._process = worker._results = None
        worker._requests = channel
        report = worker.close()
        self.assertTrue(pending.cancel.called, "the send published after close still blocks the feeder")
        self.assertEqual(report.live_queue_feeders, 0)
        self.assertFalse(report.timed_out)

    def test_windows_feeder_cancel_precedes_writer_handle_close(self):
        import sys
        if sys.platform != "win32":
            self.skipTest("the overlapped write handle is Windows specific")
        closed = threading.Event()
        feeder = Mock(spec=threading.Thread)
        feeder.is_alive.return_value = True
        writer = Mock()
        writer._send_ov = None
        writer.close.side_effect = closed.set
        pending = Mock()
        def joined(_timeout):
            writer._send_ov = pending
        def cancelled():
            if closed.is_set():
                raise OSError(6, "the write handle is already closed")
            writer.close()  # The existing feeder consumes its sentinel.
            feeder.is_alive.return_value = False
        feeder.join.side_effect = joined
        pending.cancel.side_effect = cancelled
        channel = Mock()
        channel._writer, channel._thread = writer, feeder
        worker = MotionSolverWorker.__new__(MotionSolverWorker)
        worker._closed = False
        worker._process = worker._results = None
        worker._requests = channel
        report = worker.close(timeout_seconds=.05)
        self.assertEqual(report.live_queue_feeders, 0, "cancelling after CloseHandle loses the event wakeup")
        self.assertTrue(closed.is_set(), "the writer must still close after feeder cancellation")

    def test_windows_feeder_consumes_sentinel_and_closes_both_ends_once(self):
        import sys
        if sys.platform != "win32":
            self.skipTest("the overlapped write handle is Windows specific")
        feeder = Mock(spec=threading.Thread)
        feeder.is_alive.return_value = True
        writer, reader, pending = Mock(), Mock(), Mock()
        writer._send_ov = None
        cancelled = threading.Event()
        pending.cancel.side_effect = cancelled.set
        def joined(_timeout):
            if not cancelled.is_set():
                writer._send_ov = pending
                return
            reader.close()
            writer.close()
            feeder.is_alive.return_value = False
        feeder.join.side_effect = joined
        channel = Mock()
        channel._writer, channel._reader, channel._thread = writer, reader, feeder
        worker = MotionSolverWorker.__new__(MotionSolverWorker)
        worker._closed = False
        worker._process = worker._results = None
        worker._requests = channel
        report = worker.close(timeout_seconds=.05)
        self.assertEqual(report.live_queue_feeders, 0)
        self.assertEqual(reader.close.call_count, 1, "the main thread raced the sentinel's reader close")
        self.assertEqual(writer.close.call_count, 1, "the main thread raced the sentinel's writer close")

    def test_expected_close_feeder_errors_are_counted_and_other_errors_are_logged(self):
        context = Mock()
        context.Process.return_value.pid = None
        context.Process.return_value.is_alive.return_value = False
        first, second = Mock(), Mock()
        context.Queue.side_effect = (first, second)
        logger = first._on_queue_feeder_error
        with patch("mc2p.motion_nav.motion_worker.multiprocessing.get_context", return_value=context):
            worker = MotionSolverWorker()
        try:
            first._on_queue_feeder_error(OSError(errno.EPIPE, "outside close"), b"first")
            self.assertEqual(logger.call_count, 1)
            worker._closed = True
            first._on_queue_feeder_error(OSError(errno.EPIPE, "expected close cancellation"), b"second")
            self.assertEqual(logger.call_count, 1, "only the expected close error is kept as a diagnostic")
            first._on_queue_feeder_error(OSError(6, "unexpected handle error"), b"third")
            self.assertEqual(logger.call_count, 2, "an unexpected teardown error must retain the original handler")
            report = worker.close()
            self.assertEqual(report.expected_close_feeder_errors, 1)
            self.assertEqual(report.unexpected_feeder_errors, 2)
        finally:
            worker.close()

    def test_close_with_blocked_port_uses_the_existing_receiver_and_returns(self):
        entered, release = threading.Event(), threading.Event()
        worker = QueuedWorker()
        original = worker.close
        def blocked_close():
            entered.set()
            if not release.wait(10):
                raise AssertionError("worker close test barrier was not released")
            original()
        worker.close = blocked_close
        inbox = MotionResultInbox(worker=worker)
        self.addCleanup(inbox.close)
        started = time.monotonic()
        try:
            report = inbox.close(timeout_seconds=.05)
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(entered.is_set())
            self.assertTrue(report.timed_out)
            self.assertEqual((report.live_workers, report.live_receivers), (1, 1))
        finally:
            release.set()
            inbox._receiver.join(10)
        self.assertTrue(inbox.close(timeout_seconds=.5).fully_released)

    def test_failed_snapshot_return_retries_off_caller_and_preserves_counts(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        class Port:
            def __init__(self):
                self.allow_return = threading.Event()
                self.entered = threading.Event()
                self.calls = []
            def snapshot_lease_is_live(self, lease):
                return not self.allow_return.is_set()
            def dependency_changes(self, versions):
                return SimpleNamespace(status=DependencyStatus.UNCHANGED)
            def release_snapshot(self, *args, **kwargs):
                self.calls.append(threading.current_thread().name)
                if threading.current_thread().name == "mc2p-motion-receiver":
                    return False
                self.entered.set()
                self.allow_return.wait(10)
                return True
        port = Port()
        worker = QueuedWorker()
        inbox = MotionResultInbox(worker=worker, world=port, current_facts=self.current)
        self.addCleanup(inbox.close)
        self.ready(inbox=inbox, worker=worker)
        reports = []
        closer = threading.Thread(name="test-close-caller", target=lambda: reports.append(inbox.close(timeout_seconds=.05)),
                                  daemon=True)
        started = time.monotonic()
        try:
            closer.start()
            self.assertTrue(port.entered.wait(2))
            closer.join(.2)
            self.assertFalse(closer.is_alive(), "snapshot source callback blocked close's caller")
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].lease_count, 1)
            self.assertGreater(reports[0].retained_bytes, 0)
            self.assertTrue(reports[0].timed_out)
            self.assertEqual(reports[0].live_lease_returners, 1)
            self.assertNotIn("test-close-caller", port.calls)
            repeated = inbox.close(timeout_seconds=.05)
            self.assertEqual(repeated.lease_count, 1)
            self.assertTrue(repeated.timed_out)
        finally:
            port.allow_return.set()
            closer.join(2)
            if inbox._lease_returner is not None:
                inbox._lease_returner.join(2)
        final = inbox.close(timeout_seconds=.5)
        self.assertTrue(final.fully_released, (final, port.calls))

    def test_last_use_release_after_close_never_calls_source_on_caller(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        class Port:
            def __init__(self):
                self.allow_return = threading.Event()
                self.entered = threading.Event()
                self.calls = []
            def snapshot_lease_is_live(self, lease):
                return not self.allow_return.is_set()
            def dependency_changes(self, versions):
                return SimpleNamespace(status=DependencyStatus.UNCHANGED)
            def release_snapshot(self, *args, **kwargs):
                self.calls.append(threading.current_thread().name)
                self.entered.set()
                self.allow_return.wait(10)
                return True
        port = Port()
        worker = QueuedWorker()
        inbox = MotionResultInbox(worker=worker, world=port, current_facts=self.current)
        self.addCleanup(inbox.close)
        _, _, handle = self.ready(inbox=inbox, worker=worker)
        self.activate(inbox, handle)
        inbox.retain_safe_tail(handle, action_index=handle.action_index)
        retained = inbox.close(timeout_seconds=.5)
        self.assertEqual((retained.executing_references, retained.safe_tail_references), (1, 1))
        try:
            self.assertIs(inbox.release_usage(handle, use=self.uses().EXECUTING,
                evidence=self.evidence(handle, self.uses().EXECUTING)).status, values.ReferenceStatus.RELEASED)
            started = time.monotonic()
            self.assertIs(inbox.release_usage(handle, use=self.uses().SAFE_TAIL,
                evidence=self.evidence(handle, self.uses().SAFE_TAIL)).status, values.ReferenceStatus.RELEASED)
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(port.entered.wait(2))
            self.assertNotIn(threading.current_thread().name, port.calls)
            blocked = inbox.close(timeout_seconds=.05)
            self.assertEqual(blocked.lease_count, 1)
            self.assertGreater(blocked.retained_bytes, 0)
        finally:
            port.allow_return.set()
            if inbox._lease_returner is not None:
                inbox._lease_returner.join(2)
        self.assertTrue(inbox.close(timeout_seconds=.5).fully_released)

    def test_close_report_sees_returner_started_by_concurrent_last_release(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        class Port:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
            def snapshot_lease_is_live(self, lease):
                return not self.release.is_set()
            def dependency_changes(self, versions):
                return SimpleNamespace(status=DependencyStatus.UNCHANGED)
            def release_snapshot(self, *args, **kwargs):
                self.entered.set()
                self.release.wait(10)
                return True
        port, worker = Port(), QueuedWorker()
        inbox = MotionResultInbox(worker=worker, world=port, current_facts=self.current)
        self.addCleanup(inbox.close)
        _, _, handle = self.ready(inbox=inbox, worker=worker)
        self.activate(inbox, handle)
        inbox.retain_safe_tail(handle, action_index=handle.action_index)
        self.assertEqual(inbox.close(timeout_seconds=.5).safe_tail_references, 1)
        inbox.release_usage(handle, use=self.uses().EXECUTING,
                            evidence=self.evidence(handle, self.uses().EXECUTING))
        report_entered, resume_report = threading.Event(), threading.Event()
        original_alive = worker.is_alive
        def paused_alive():
            if threading.current_thread().name == "test-report-close":
                report_entered.set()
                self.assertTrue(resume_report.wait(5))
            return original_alive()
        reports = []
        closer = threading.Thread(name="test-report-close",
            target=lambda: reports.append(inbox.close(timeout_seconds=.2)), daemon=True)
        try:
            with patch.object(worker, "is_alive", side_effect=paused_alive):
                closer.start()
                self.assertTrue(report_entered.wait(2))
                self.assertIs(inbox.release_usage(handle, use=self.uses().SAFE_TAIL,
                    evidence=self.evidence(handle, self.uses().SAFE_TAIL)).status,
                    values.ReferenceStatus.RELEASED)
                self.assertTrue(port.entered.wait(2))
                self.assertTrue(inbox._lease_returner.is_alive())
                resume_report.set()
                closer.join(2)
            self.assertFalse(closer.is_alive())
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].live_lease_returners, 1)
            self.assertTrue(reports[0].timed_out)
            self.assertEqual(reports[0].lease_count, 1)
            self.assertEqual(reports[0].queued_returns, 0)
            self.assertGreater(reports[0].retained_bytes, 0)
        finally:
            resume_report.set()
            port.release.set()
            closer.join(2)
            if inbox._lease_returner is not None:
                inbox._lease_returner.join(2)
        self.assertTrue(inbox.close(timeout_seconds=.5).fully_released)

    def test_returner_drains_second_lease_released_during_first_callback(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        class Port:
            def __init__(self):
                self.first_entered = threading.Event()
                self.release_first = threading.Event()
                self.calls = []
            def snapshot_lease_is_live(self, lease):
                return True
            def dependency_changes(self, versions):
                return SimpleNamespace(status=DependencyStatus.UNCHANGED)
            def release_snapshot(self, token, **kwargs):
                self.calls.append(token)
                if token == self.lease_token:
                    self.first_entered.set()
                    self.release_first.wait(10)
                return True
        port, worker = Port(), QueuedWorker()
        port.lease_token = self.lease.lease_token
        inbox = MotionResultInbox(worker=worker, world=port, current_facts=self.current)
        self.addCleanup(inbox.close)
        _, _, first = self.ready(inbox=inbox, worker=worker)
        self.activate(inbox, first)
        second_lease = replace(self.lease, lease_token="second-return-lease")
        second_request = replace(self.request, job_token="second-return-job",
            snapshot_lease_token=second_lease.lease_token,
            async_identity=replace(self.request.async_identity, revision=2))
        second_bundle = reseal(replace(self.bundle, request=second_request))
        _, _, second = self.ready(inbox=inbox, worker=worker, request=second_request,
                                  bundle=second_bundle, lease=second_lease)
        self.activate(inbox, second, sequence=201)
        retained = inbox.close(timeout_seconds=.5)
        self.assertEqual((retained.executing_references, retained.lease_count), (2, 2))
        try:
            self.assertIs(inbox.release_usage(first, use=self.uses().EXECUTING,
                evidence=self.evidence(first, self.uses().EXECUTING)).status,
                values.ReferenceStatus.RELEASED)
            self.assertTrue(port.first_entered.wait(2))
            returner = inbox._lease_returner
            self.assertIs(inbox.release_usage(second, use=self.uses().EXECUTING,
                evidence=self.evidence(second, self.uses().EXECUTING, sequence=201)).status,
                values.ReferenceStatus.RELEASED)
            self.assertIs(inbox._lease_returner, returner)
            pending = inbox._close_report()
            self.assertEqual((pending.live_lease_returners, pending.lease_count,
                              pending.queued_returns), (1, 2, 1))
            self.assertGreater(pending.retained_bytes, 0)
            started = time.monotonic()
            repeated = inbox.close(timeout_seconds=.05)
            self.assertLess(time.monotonic() - started, .5)
            self.assertIs(inbox._lease_returner, returner)
            self.assertEqual(repeated.queued_returns, 1)
            self.assertTrue(repeated.timed_out)
        finally:
            port.release_first.set()
            if inbox._lease_returner is not None:
                inbox._lease_returner.join(2)
        # Read a report without close(): another close would mask a stranded
        # queue by starting a new returner.
        report = inbox._close_report()
        self.assertTrue(report.fully_released, report)
        self.assertEqual(report.lease_count, 0)
        self.assertEqual(report.queued_returns, 0)
        self.assertEqual(port.calls, [self.lease.lease_token, second_lease.lease_token])

    def test_failed_source_return_waits_for_explicit_retry_without_spinning(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        class Port:
            def __init__(self):
                self.calls = 0
                self.succeed = False
            def snapshot_lease_is_live(self, lease):
                return not self.succeed
            def dependency_changes(self, versions):
                return SimpleNamespace(status=DependencyStatus.UNCHANGED)
            def release_snapshot(self, *args, **kwargs):
                self.calls += 1
                return self.succeed
        port, worker = Port(), QueuedWorker()
        inbox = MotionResultInbox(worker=worker, world=port, current_facts=self.current)
        self.addCleanup(inbox.close)
        self.ready(inbox=inbox, worker=worker)
        first = inbox.close(timeout_seconds=.2)
        calls_after_first = port.calls
        self.assertGreater(calls_after_first, 0)
        self.assertEqual((first.lease_count, first.queued_returns), (1, 1))
        self.assertEqual(first.live_lease_returners, 1)
        returner = inbox._lease_returner
        threading.Event().wait(.05)
        self.assertEqual(port.calls, calls_after_first, "failed source return spun without a new trigger")
        repeated = inbox.close(timeout_seconds=.2)
        self.assertEqual(port.calls, calls_after_first + 1)
        self.assertEqual((repeated.lease_count, repeated.queued_returns), (1, 1))
        self.assertIs(inbox._lease_returner, returner)
        port.succeed = True
        self.assertTrue(inbox.close(timeout_seconds=.5).fully_released)

    def test_explicit_close_retry_between_failed_batch_and_wait_is_not_lost(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        class Port:
            def __init__(self):
                self.succeed = False
                self.calls = []
                self.retried = threading.Event()
            def snapshot_lease_is_live(self, lease):
                return not self.succeed
            def dependency_changes(self, versions):
                return SimpleNamespace(status=DependencyStatus.UNCHANGED)
            def release_snapshot(self, token, **kwargs):
                self.calls.append((token, self.succeed))
                if token == self.first_token and self.succeed:
                    self.retried.set()
                return self.succeed
        port, worker = Port(), QueuedWorker()
        port.first_token = self.lease.lease_token
        inbox = MotionResultInbox(worker=worker, world=port, current_facts=self.current)
        self.addCleanup(inbox.close)
        _, _, first = self.ready(inbox=inbox, worker=worker)
        self.activate(inbox, first)
        second_lease = replace(self.lease, lease_token="retry-held-second-lease")
        second_request = replace(self.request, job_token="retry-held-second-job",
            snapshot_lease_token=second_lease.lease_token,
            async_identity=replace(self.request.async_identity, revision=2))
        second_bundle = reseal(replace(self.bundle, request=second_request))
        _, _, second = self.ready(inbox=inbox, worker=worker, request=second_request,
                                  bundle=second_bundle, lease=second_lease)
        self.activate(inbox, second, sequence=201)
        self.assertEqual(inbox.close(timeout_seconds=.5).executing_references, 2)
        batch_done, resume_batch = threading.Event(), threading.Event()
        original_batch = inbox._return_leases
        def paused_batch(*args, **kwargs):
            result = original_batch(*args, **kwargs)
            if (threading.current_thread().name == "mc2p-motion-lease-return"
                    and not batch_done.is_set()):
                batch_done.set()
                self.assertTrue(resume_batch.wait(10))
            return result
        try:
            with patch.object(inbox, "_return_leases", side_effect=paused_batch):
                self.assertIs(inbox.release_usage(first, use=self.uses().EXECUTING,
                    evidence=self.evidence(first, self.uses().EXECUTING)).status,
                    values.ReferenceStatus.RELEASED)
                self.assertTrue(batch_done.wait(2))
                self.assertEqual(port.calls, [(self.lease.lease_token, False)])
                port.succeed = True
                repeated = inbox.close(timeout_seconds=.05)
                self.assertEqual((repeated.live_lease_returners, repeated.lease_count,
                                  repeated.queued_returns), (1, 2, 1))
                resume_batch.set()
                self.assertTrue(port.retried.wait(2), "explicit close retry was lost before the wait")
                self.assertEqual(port.calls[:2], [(self.lease.lease_token, False),
                                                 (self.lease.lease_token, True)])
                self.assertIs(inbox.release_usage(second, use=self.uses().EXECUTING,
                    evidence=self.evidence(second, self.uses().EXECUTING, sequence=201)).status,
                    values.ReferenceStatus.RELEASED)
                inbox._lease_returner.join(2)
        finally:
            port.succeed = True
            resume_batch.set()
            if inbox.usage_active(second, self.uses().EXECUTING):
                inbox.release_usage(second, use=self.uses().EXECUTING,
                    evidence=self.evidence(second, self.uses().EXECUTING, sequence=201))
            if inbox._lease_returner is not None:
                inbox._lease_returner.join(2)
        self.assertTrue(inbox._close_report().fully_released)

    def test_close_retry_while_returner_thread_is_finalizing_is_not_lost(self):
        from mc2p.motion_nav.world_snapshot import DependencyStatus
        class Port:
            def __init__(self):
                self.succeed = False
                self.calls = []
                self.entered = threading.Event()
                self.finish_callback = threading.Event()
                self.retried = threading.Event()
            def snapshot_lease_is_live(self, lease):
                return not self.succeed
            def dependency_changes(self, versions):
                return SimpleNamespace(status=DependencyStatus.UNCHANGED)
            def release_snapshot(self, token, **kwargs):
                self.calls.append((token, self.succeed))
                if threading.current_thread().name == "mc2p-motion-lease-return" and not self.succeed:
                    self.entered.set()
                    self.finish_callback.wait(10)
                if self.succeed:
                    self.retried.set()
                return self.succeed
        port, worker = Port(), QueuedWorker()
        inbox = MotionResultInbox(worker=worker, world=port, current_facts=self.current)
        self.addCleanup(inbox.close)
        self.ready(inbox=inbox, worker=worker)
        phase_ready = threading.Event()
        phases = []
        original_wait = inbox._lease_return_condition.wait
        def observed_wait(*args, **kwargs):
            if threading.current_thread() is inbox._lease_returner and not phase_ready.is_set():
                phases.append("condition_wait")
                phase_ready.set()
            return original_wait(*args, **kwargs)
        def observe_native_exit(frame, event, arg):
            returner = inbox._lease_returner
            if (event == "call" and frame.f_code.co_name == "_delete"
                    and returner is not None and threading.get_ident() == returner.ident
                    and not phase_ready.is_set()):
                phases.append("native_delete")
                phase_ready.set()
            return observe_native_exit
        previous_trace = threading.gettrace()
        try:
            threading.settrace(observe_native_exit)
            with patch.object(inbox._lease_return_condition, "wait", side_effect=observed_wait):
                self.assertEqual(inbox.close(timeout_seconds=.05).lease_count, 1)
                self.assertTrue(port.entered.wait(2))
                returner = inbox._lease_returner
                # This is the real Python thread finalization lock. Old code
                # reaches Thread._delete while still is_alive; the repaired
                # returner keeps its target alive while work remains.
                with threading._active_limbo_lock:
                    port.finish_callback.set()
                    self.assertTrue(phase_ready.wait(2))
                    self.assertEqual(phases[0], "condition_wait",
                        "a returner with queued work must not enter Thread._delete")
                    self.assertTrue(returner.is_alive())
                    before = len(port.calls)
                    port.succeed = True
                    started = time.monotonic()
                    report = inbox.close(timeout_seconds=.05)
                    self.assertLess(time.monotonic() - started, .5)
                    self.assertTrue(report.timed_out)
                    self.assertTrue(port.retried.wait(2),
                        "explicit close was delivered to a returner that could no longer consume it")
                    self.assertEqual(len(port.calls), before + 1)
        finally:
            threading.settrace(previous_trace)
            port.succeed = True
            port.finish_callback.set()
            if inbox._lease_returner is not None:
                inbox._lease_returner.join(2)
        self.assertTrue(inbox.close(timeout_seconds=.5).fully_released)

    def test_inbox_constructor_exception_returns_worker_claim(self):
        worker = QueuedWorker()
        with patch("mc2p.motion_nav.motion_worker.threading.Thread.start", side_effect=RuntimeError("receiver refused")):
            with self.assertRaisesRegex(RuntimeError, "receiver refused"):
                MotionResultInbox(worker=worker)
        self.assertTrue(worker.is_alive(), "failed construction cannot close an explicitly borrowed worker")
        inbox = MotionResultInbox(worker=worker)
        self.addCleanup(inbox.close)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))


if __name__ == "__main__":
    unittest.main()
