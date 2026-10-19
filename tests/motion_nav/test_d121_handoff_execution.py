"""D121 receipt-driven committed suffix, exact fallback and activation choice."""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import ExitStack, contextmanager
import sys
import pickle
import inspect
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav import motion_solver
from mc2p.motion_nav.motion_candidate import (
    MotionCandidateAdmitter, MotionCandidateContext, MotionCandidateStatus,
    VerifiedMotionCandidate, VerifiedMotionExecutor, VerifiedMotionExecutorState,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.online_motion import StateAnchor, MotionTickPhase, InputApplicationLedger
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from tests.motion_nav.test_d121_successor_binding import fixed_case
from tests.motion_nav import test_d121_successor_binding as preparation_fixtures
from tests.motion_nav import test_b10_motion_candidate as candidate_fixtures
from mc2p.motion_nav.body_control import HandoffDisposition, StopCause
from mc2p.motion_nav.motion_worker import _execute_job
from mc2p.contracts.motion_activation import ActivationResult, ActivationStatus, ActivationRejectReason
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor


def anchor_for(state):
    return StateAnchor(state.session, state.movement_tick_id, state.movement_tick_id,
        MotionTickPhase.AFTER_MOVEMENT, None, None, state.ruleset_id,
        state.state_schema, "mc2p.input-projection.v1", state)


def current_case():
    request, fixture, proof, spec, snapshot = fixed_case()
    bound = motion_solver.bind_fixed_successor(proof, request, spec, snapshot,
        damage_remaining_points=0.).proof
    context = MotionCandidateContext("request", 0, "goal", 1, "route", 1, 0, 1,
        TaskDamageBudget(), bound.resource_incomplete_reasons)
    candidate = VerifiedMotionCandidate(bound, context)
    arguments = asdict(context)
    arguments.pop("accepted_resource_incomplete_reasons")
    arguments["damage_budget"] = context.damage_budget
    arguments.update(intended_start_tick=1, changed_cells=())
    return fixture, candidate, arguments


@contextmanager
def forbid_control_rebuilds():
    from mc2p.motion_nav import physics_1_21, prepared_motion_proof
    prohibited = {physics_1_21.step: "physics", prepared_motion_proof.canonical_motion_digest: "digest",
        prepared_motion_proof.canonical_motion_bytes: "canonical",
        prepared_motion_proof.canonical_bundle_bytes: "canonical_bundle"}
    counts = {name: 0 for name in (*set(prohibited.values()), "variant", "pickle")}
    residual_ticks = set()
    def fail(name):
        def called(*args, **kwargs):
            if name == "physics":
                caller = inspect.currentframe().f_back
                if (caller.f_globals.get("__name__") == "mc2p.motion_nav.motion_residual"
                        and caller.f_code.co_name == "calculate_motion_residual"):
                    movement_tick = args[0].movement_tick_id + 1
                    if movement_tick in residual_ticks:
                        raise AssertionError("repeated residual physics for the same real tick")
                    residual_ticks.add(movement_tick)
                    return physics_1_21_original(*args, **kwargs)
            counts[name] += 1
            raise AssertionError("forbidden control rebuild: " + name)
        return called
    physics_1_21_original = physics_1_21.step
    with ExitStack() as stack:
        for name, module in tuple(sys.modules.items()):
            if not name.startswith("mc2p.") or module is None:
                continue
            for attribute, value in tuple(vars(module).items()):
                if callable(value):
                    for function, kind in prohibited.items():
                        if value is function:
                            stack.enter_context(patch.object(module, attribute, fail(kind)))
        stack.enter_context(patch.object(motion_solver.VerifiedMotionResult, "start_variant", fail("variant")))
        stack.enter_context(patch.object(pickle, "dumps", fail("pickle")))
        stack.enter_context(patch.object(pickle, "loads", fail("pickle")))
        yield counts


class CommittedExecutorTests(unittest.TestCase):
    def test_default_admission_stays_closed(self):
        _, candidate, arguments = current_case()
        admission = MotionCandidateAdmitter().admit(candidate,
            anchor_for(candidate.proof.entry_state), **arguments)
        self.assertIs(admission.status, MotionCandidateStatus.REJECTED)
        self.assertEqual(admission.reason, "committed_main_suffix_execution_not_enabled")

    def run_current(self, *, cancel_tick=None, pending_cancel=False):
        fixture, candidate, arguments = current_case()
        executor = VerifiedMotionExecutor()
        initial = anchor_for(candidate.proof.entry_state)
        admission = MotionCandidateAdmitter().admit_committed(candidate, initial,
            executor=executor, **arguments)
        self.assertIs(admission.status, MotionCandidateStatus.ACCEPTED)
        executor.start(admission.candidate)
        ledger = InputApplicationLedger(max_records=128)
        state, sent = candidate.proof.entry_state, []
        for index in range(70):
            anchor = anchor_for(state)
            if state.movement_tick_id == cancel_tick and not pending_cancel:
                executor.cancel(anchor)
            decision = executor.decide(anchor, ledger)
            if decision.state in {VerifiedMotionExecutorState.CANCELLED,
                                  VerifiedMotionExecutorState.INPUT_LOST}:
                self.assertTrue(executor.exact_release_ready(anchor, ledger))
                self.last_executor, self.last_ledger = executor, ledger
                return candidate.proof, state, sent
            self.assertTrue(decision.submittable_as_verified_command, decision)
            self.assertEqual(decision.expected_movement_tick, state.movement_tick_id + 1)
            self.assertEqual(decision.latest_movement_tick, decision.expected_movement_tick)
            executor.register_submission(decision.command_index, control_sequence=index + 1,
                requested_movement_tick=decision.expected_movement_tick,
                requested_latest_movement_tick=decision.latest_movement_tick)
            if state.movement_tick_id == cancel_tick and pending_cancel:
                executor.cancel(anchor)
            movement = decision.movement
            control = TickInput(float(movement.forward), float(movement.strafe),
                movement.jump, movement.sneak, movement.sprint, decision.movement_yaw_radians)
            state = step(state, control, fixture.context.world, JAVA_1_21_RULESET).next_state
            candidate_fixtures.VerifiedMotionExecutorTests.applied(ledger, anchor, index + 1,
                state.movement_tick_id, movement)
            sent.append((state.movement_tick_id, movement, decision.command_index))
        self.fail("committed fallback did not reach its observed release witness")

    def test_single_executor_consumes_main_fallback_and_actual_witness(self):
        proof, state, sent = self.run_current()
        self.assertEqual([tick for tick, _, _ in sent], list(range(1, len(sent) + 1)))
        self.assertEqual([movement for _, movement, _ in sent[:26]],
                         [command.movement for command in proof.commands])
        self.assertFalse(any(movement.sneak for _, movement, _ in sent[:26]))
        self.assertTrue(sent[26][1].sneak)
        self.assertEqual(sent[-1][1], MovementV1())
        self.assertEqual(state, proof.fallback_tails[-1].release_witness_state)

    def test_cancel_with_pending_consumes_main_suffix_before_fallback(self):
        for tick in (15, 23, 24, 25, 26, 28):
            for pending in (False, True):
                with self.subTest(tick=tick, pending=pending):
                    proof, state, sent = self.run_current(cancel_tick=tick,
                        pending_cancel=pending)
                    self.assertEqual([movement for _, movement, _ in sent[:26]],
                        [command.movement for command in proof.commands])
                    self.assertEqual(state, proof.fallback_tails[-1].release_witness_state)

    def test_release_witness_rejects_foreign_ledger_body_and_dependencies(self):
        proof, state, _ = self.run_current()
        anchor = anchor_for(state)
        executor, ledger = self.last_executor, self.last_ledger
        self.assertFalse(executor.exact_release_ready(anchor, InputApplicationLedger()))
        shifted = replace(state, position=(state.position[0]+.001, *state.position[1:]))
        self.assertFalse(executor.exact_release_ready(anchor_for(shifted), ledger))
        self.assertFalse(executor.exact_release_ready(anchor, ledger,
            changed_cells=(proof.world_dependencies[0],)))

    def test_cached_tail_views_preserve_sneak_and_negative_input_projection(self):
        from mc2p.motion_nav.online_motion import project_movement_command
        _, candidate, arguments = current_case()
        executor = VerifiedMotionExecutor()
        admission = MotionCandidateAdmitter().admit_committed(candidate,
            anchor_for(candidate.proof.entry_state), executor=executor, **arguments)
        executor.start(admission.candidate)
        for tail in candidate.proof.fallback_tails:
            values = (*tail.inputs, *((tail.release_witness_input,)
                if tail.release_witness_input is not None else ()))
            views = executor._tail_command_views[id(tail)]
            for expected, view in zip(values, views):
                projected = project_movement_command(candidate.proof.entry_state, view.movement,
                    movement_yaw_radians=view.required_movement_yaw_radians)
                self.assertEqual(projected.tick_input, expected)
        # Runtime's integer movement domain represents the full negative/back
        # candidate exactly. Fractional proof controls are rejected on admission.
        for forward in (-1., 0., 1.):
            for strafe in (-1., 0., 1.):
                expected = TickInput(forward, strafe, False, False, False,
                    candidate.proof.entry_state.yaw_radians)
                projected = project_movement_command(candidate.proof.entry_state,
                    MovementV1(int(forward), int(strafe), False, False, False),
                    movement_yaw_radians=expected.movement_yaw_radians)
                self.assertEqual(projected.tick_input, expected)

    def test_fractional_fallback_controls_never_truncate_to_runtime_movement(self):
        from tests.motion_nav.test_d102_receiver import unchecked_replace
        _, candidate, arguments = current_case()
        tail = candidate.proof.fallback_tails[-1]
        fractional = replace(tail.inputs[0], forward=.5)
        altered_tail = unchecked_replace(tail, inputs=(fractional, *tail.inputs[1:]))
        altered = unchecked_replace(candidate.proof,
            fallback_tails=(*candidate.proof.fallback_tails[:-1], altered_tail))
        candidate = unchecked_replace(candidate, proof=altered)
        result = MotionCandidateAdmitter().admit_committed(candidate,
            anchor_for(candidate.proof.entry_state), executor=VerifiedMotionExecutor(), **arguments)
        self.assertIs(result.status, MotionCandidateStatus.REJECTED)
        self.assertEqual(result.reason, "committed_input_projection_unsupported")

    def test_mixed_sneak_input_cannot_silently_change_runtime_projection(self):
        from tests.motion_nav.test_d102_receiver import unchecked_replace
        _, candidate, arguments = current_case()
        tail = candidate.proof.fallback_tails[-1]
        changed = replace(tail.inputs[0], forward=1.)
        altered_tail = unchecked_replace(tail, inputs=(changed, *tail.inputs[1:]))
        altered = unchecked_replace(candidate.proof,
            fallback_tails=(*candidate.proof.fallback_tails[:-1], altered_tail))
        candidate = unchecked_replace(candidate, proof=altered)
        result = MotionCandidateAdmitter().admit_committed(candidate,
            anchor_for(candidate.proof.entry_state), executor=VerifiedMotionExecutor(), **arguments)
        self.assertIs(result.status, MotionCandidateStatus.REJECTED)
        self.assertEqual(result.reason, "committed_input_projection_unsupported")

    def test_tail_receipt_identity_and_duplicate_do_not_advance_twice(self):
        from tests.motion_nav.test_d102_receiver import unchecked_replace
        for mutation in ("duplicate", "sequence", "movement", "window"):
            with self.subTest(mutation=mutation):
                fixture, candidate, arguments = current_case()
                executor = VerifiedMotionExecutor()
                admitted = MotionCandidateAdmitter().admit_committed(candidate,
                    anchor_for(candidate.proof.entry_state), executor=executor, **arguments)
                executor.start(admitted.candidate)
                ledger = InputApplicationLedger(max_records=128)
                state = candidate.proof.entry_state
                for index in range(27):
                    anchor = anchor_for(state)
                    decision = executor.decide(anchor, ledger)
                    executor.register_submission(decision.command_index, control_sequence=index + 1,
                        requested_movement_tick=decision.expected_movement_tick,
                        requested_latest_movement_tick=decision.latest_movement_tick)
                    movement = decision.movement
                    state = step(state, TickInput(float(movement.forward), float(movement.strafe),
                        movement.jump, movement.sneak, movement.sprint, decision.movement_yaw_radians),
                        fixture.context.world, JAVA_1_21_RULESET).next_state
                    candidate_fixtures.VerifiedMotionExecutorTests.applied(ledger, anchor,
                        index + 1 if index != 26 or mutation != "sequence" else 28,
                        state.movement_tick_id, movement)
                if mutation == "movement":
                    record = ledger.record(27)
                    ledger._records[27] = unchecked_replace(record,
                        action=replace(record.action, movement=MovementV1()))
                if mutation == "window":
                    ledger._records[27] = unchecked_replace(ledger.record(27), latest_allowed_first_tick=28)
                decision = executor.decide(anchor_for(state), ledger)
                if mutation == "duplicate":
                    self.assertEqual(executor._tail_cursor, 1)
                    duplicate = executor.decide(anchor_for(state), ledger)
                    self.assertEqual(executor._tail_cursor, 1)
                    self.assertEqual(duplicate.movement, decision.movement)
                else:
                    self.assertEqual(executor._tail_cursor, 0)
                    self.assertFalse(decision.submittable_as_verified_command)
                    self.assertFalse(executor.exact_release_ready(anchor_for(state), ledger))


def install_current(test):
    case = preparation_fixtures.FixedPreparationTests("runTest")
    case.setUp()
    test.addCleanup(case.doCleanups)
    bound = motion_solver.bind_fixed_successor(case.proof, case.request, case.spec,
        case.snapshot, damage_remaining_points=0.).proof
    candidate = VerifiedMotionCandidate(bound, case.context)
    arguments = asdict(case.context)
    arguments.pop("accepted_resource_incomplete_reasons")
    arguments["damage_budget"] = case.context.damage_budget
    arguments.update(intended_start_tick=1, changed_cells=())
    controller = VerifiedMotionExecutor()
    admission = MotionCandidateAdmitter().admit_committed(candidate, case.anchor,
        executor=controller, **arguments)
    test.assertIs(admission.status, MotionCandidateStatus.ACCEPTED)
    case.executor.install_verified_motion(admission.candidate)
    case.current_controller = controller
    return case


class RouteTailTests(unittest.TestCase):
    def test_installed_fixed_candidate_outlives_worker_wait_but_not_exact_window(self):
        case = install_current(self)
        job = case.submit()
        case.worker.put(_execute_job(job))
        self.assertTrue(case.inbox.wait_for_receiver(timeout=10))
        self.assertTrue(case.coordinator.apply_fixed_successor_preparation(
            movement_tick=0, current_scope=case.coordinator.computation_scope))
        fields = case.coordinator._prepared_fields
        self.assertIsNone(case.coordinator._work_identity)
        for tick, expected in ((19, False), (26, False), (28, True)):
            with self.subTest(tick=tick):
                self.assertIs(case.coordinator._prepared_requirement_expired(fields,
                    anchor_for(replace(case.proof.exit_state, movement_tick_id=tick))), expected)

    def test_L_publishes_prepared_boundary_with_submittable_default_fallback(self):
        case = install_current(self)
        ledger = InputApplicationLedger(max_records=128)
        from mc2p.motion_nav.motion_worker import GapMotionSolveResult
        submitted = case.coordinator.prepare_fixed_successor(
            GapMotionSolveResult("fixed-current", 1,
                motion_solver.SolveResult(motion_solver.SolveStatus.SOLVED,
                    case.current_controller._candidate.proof), 0, case.identity),
            case.request, case.context, case.anchor, case.fixture.context.world,
            ledger_identity=ledger.ledger_identity, expected_work_identity=case.identity)
        self.assertEqual(submitted.status.value, "accepted")
        self.assertTrue(case.worker.submitted.wait(10))
        job = case.worker.jobs[-1]
        case.worker.put(_execute_job(job))
        self.assertTrue(case.inbox.wait_for_receiver(timeout=10))
        state = case.proof.entry_state
        for index in range(27):
            anchor = anchor_for(state)
            frame = candidate_fixtures.VerifiedMotionRouteIntegrationTests.frame(
                case.source.view(), state, anchor.observation_sequence_id)
            frame = replace(frame, body=replace(frame.body, movement_tick_id=state.movement_tick_id))
            decision = case.coordinator.decide(frame, anchor, ledger,
                case.fixture.context.world, changed_cells=(),
                current_scope=case.coordinator.computation_scope)
            if state.movement_tick_id == 26:
                self.assertEqual(decision.reason_code, "prepared_action_boundary")
                self.assertTrue(decision.submit_input)
                self.assertTrue(decision.movement.sneak)
                self.assertEqual(decision.verified_command_index, 26)
                self.assertEqual((decision.expected_movement_tick,
                                  decision.latest_movement_tick), (27, 27))
                self.assertEqual(case.executor.action_index, 0)
                self.assertIs(case.current_controller.state, VerifiedMotionExecutorState.RUNNING)
                return
            case.executor.register_verified_submission(decision.verified_command_index,
                control_sequence=index + 1, requested_movement_tick=decision.expected_movement_tick,
                requested_latest_movement_tick=decision.latest_movement_tick)
            movement = decision.movement
            state = step(state, TickInput(float(movement.forward), float(movement.strafe),
                movement.jump, movement.sneak, movement.sprint, state.yaw_radians),
                case.fixture.context.world, JAVA_1_21_RULESET).next_state
            candidate_fixtures.VerifiedMotionExecutorTests.applied(ledger, anchor, index + 1,
                state.movement_tick_id, movement)


class FormalDefaultTests(unittest.TestCase):
    def test_east_READY_accepts_existing_numeric_body_equivalence(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        case = ExecutionCase(direction_index=1)
        self.addCleanup(case.close)
        case.prepare_successor()
        for _ in range(26):
            case.tick()
        result = case.tick()
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)
        self.assertTrue(result.decision.action.movement.jump)

    def test_ordinary_D117_completion_releases_existing_registry_uses(self):
        from scripts.d116_handoff_timeline import run_representative_timeline
        from mc2p.motion_nav.navigation_session import NavigationSession
        original = NavigationSession.close
        remaining = []
        def close(session):
            original(session)
            remaining.append(len(session._motion_result_inbox.registered_handles))
        with patch.object(NavigationSession, "close", close):
            run_representative_timeline(max_ticks=45)
        self.assertEqual(remaining, [0])

    def test_new_READY_completion_releases_registry_with_real_terminals(self):
        case = self.ready_at_L()
        for _ in range(22):
            result = case.tick()
            self.assertIsNone(result.report.failure)
        self.assertTrue(case.driver.release("completed-component-route"))
        self.assertIsNone(case.driver.source)
        self.assertEqual(len(case.session._motion_result_inbox.registered_handles), 0)

    def ready_at_L(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        case = ExecutionCase()
        self.addCleanup(case.close)
        case.prepare_successor()
        for _ in range(26):
            result = case.tick()
            self.assertIsNone(result.report.failure)
        return case

    def assert_fallback(self, case, result, before):
        self.assertIsNone(result.report.failure)
        self.assertEqual(len(case.backend.actions), before + 1)
        self.assertEqual(case.backend.movement_tick, 27)
        self.assertTrue(result.decision.action.movement.sneak)
        self.assertFalse(result.decision.action.movement.jump)
        self.assertIs(case.session._executor._controller, case.controller)
        self.assertEqual(case.session._executor.action_index, 0)
        record = case.runtime.input_ledger.record(result.decision.action.request_sequence_id)
        self.assertEqual(record.applied_ticks, (27,))
        self.assertIsNone(record.stream_command)
        self.assertEqual(case.controller._pending.control_sequence, record.control_sequence)
        next_result = case.tick()
        self.assertIsNone(next_result.report.failure)
        self.assertEqual(case.controller._tail_cursor, 1)

    def test_activation_refusals_before_commit_share_the_same_single_fallback_send(self):
        reject = ActivationResult._from_native_references(
            ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        for method in ("prepare_activation", "accept_activation", "deliver_activation", "commit_activation"):
            with self.subTest(method=method):
                case = self.ready_at_L()
                before = len(case.backend.actions)
                with patch.object(ExecutionSupervisor, method, return_value=reject) as refused:
                    result = case.tick()
                self.assertEqual(refused.call_count, 1)
                self.assert_fallback(case, result, before)

    def test_early_dispatch_rejections_use_current_owner_and_actual_ledger(self):
        from mc2p.contracts.action_v1 import MovementTickWindowV1
        for mutation in ({"ledger_id": "foreign-ledger"}, {"generation_id": "retired-generation"},
                         {"owner_id": "foreign-owner"},
                         {"requested_window": MovementTickWindowV1(28,28)}):
            with self.subTest(mutation=tuple(mutation)):
                case = self.ready_at_L()
                before = len(case.backend.actions)
                def change(dispatches):
                    self.assertEqual(len(dispatches), 1)
                    return (replace(dispatches[0], proposal=replace(dispatches[0].proposal, **mutation)),)
                result = case.tick(dispatch_transform=change)
                self.assert_fallback(case, result, before)

    def test_transaction_old_action_is_this_unsent_fallback_and_cancel_keeps_it(self):
        case = self.ready_at_L()
        original = ExecutionSupervisor.accept_activation
        def accept_and_cancel(supervisor, transaction):
            self.assertEqual(transaction.old_action.request_sequence_id,
                             transaction.new_action.request_sequence_id)
            self.assertTrue(transaction.old_action.movement.sneak)
            self.assertFalse(transaction.old_action.movement.jump)
            accepted = original(supervisor, transaction)
            case.session._executor.request_stop(StopCause.CANCELLED)
            return accepted
        before = len(case.backend.actions)
        with patch.object(ExecutionSupervisor, "accept_activation", accept_and_cancel):
            result = case.tick()
        self.assert_fallback(case, result, before)

    def test_timely_ready_commits_once_and_only_applies_successor_at_L_plus_one(self):
        case = self.ready_at_L()
        before = len(case.backend.actions)
        result = case.tick()
        self.assertIsNone(result.report.failure)
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)
        self.assertEqual(len(case.backend.actions), before + 1)
        self.assertEqual(case.backend.movement_tick, 27)
        self.assertTrue(result.decision.action.movement.jump)
        self.assertFalse(result.decision.action.movement.sneak)
        self.assertEqual(case.session._executor.action_index, 1)
        self.assertEqual(case.session._supervisor.route.tracker.identity.action_index, 1)
        successor = case.session._executor.prepared_controller
        self.assertIsNotNone(successor)
        self.assertIsNot(successor, case.controller)
        self.assertEqual(case.controller._tail_cursor, 0)
        for _ in range(len(case.proof.successor_binding.spec.commands) - 1):
            continued = case.tick()
            self.assertIsNone(continued.report.failure)
            self.assertFalse(continued.decision.action.movement.sneak)
        self.assertTrue(case.backend.state.on_ground)
        self.assertEqual(case.backend.damage_taken, 0.)
        self.assertEqual(case.backend.movement_tick, case.proof.successor_binding.exit_state.movement_tick_id)

    def test_after_commit_backend_error_does_not_send_or_register_old_fallback(self):
        case = self.ready_at_L()
        before = len(case.backend.actions)
        with patch.object(case.backend, "step", side_effect=OSError("uncertain backend send")):
            result = case.tick()
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)
        self.assertEqual(result.report.phase, "BODY_RESPONSIBILITY_UNRESOLVED")
        self.assertEqual(case.session._executor.action_index, 1)
        self.assertEqual(len(case.backend.actions), before)
        self.assertIsNone(case.controller._pending)
        self.assertEqual(case.controller._tail_cursor, 0)

    def test_tiny_support_uses_actual_witness_and_releases_without_generic_rollout(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        case = ExecutionCase()
        self.addCleanup(case.close)
        for _ in range(36):
            result = case.tick()
            self.assertIsNone(result.report.failure)
        self.assertEqual(case.backend.movement_tick, 36)
        self.assertFalse(case.backend.state.sneaking)
        with patch("mc2p.motion_nav.motion_coordination.verified_ground_rollout",
                   side_effect=AssertionError("typed witness must not reprove ground support")), \
             patch("mc2p.motion_nav.action_route_executor.verified_ground_rollout",
                   side_effect=AssertionError("typed witness must not reprove ground support")), \
             patch("mc2p.motion_nav.route_body_controller.verified_ground_rollout",
                   side_effect=AssertionError("typed witness must not reprove ground support")):
            result = case.tick()
        self.assertIsNone(result.report.failure)
        self.assertIsNone(case.driver.source)
        self.assertEqual(case.backend.damage_taken, 0.)

    def test_retired_fallback_release_is_independent_of_report_reason(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        from mc2p.motion_nav.motion_candidate import CommittedExecutionPhase
        retire_route = ExecutionSupervisor.retire_route
        for reason in ("renamed_for_display", "observed_exact_fallback_release_witness_v2",
                       "consumed_exact_fallback_source_release"):
            with self.subTest(reason=reason):
                case = ExecutionCase()
                self.addCleanup(case.close)
                for _ in range(36):
                    self.assertIsNone(case.tick().report.failure)
                mutated = []

                def retire(supervisor, frame, ledger, anchor):
                    route = supervisor._route
                    result = retire_route(supervisor, frame, ledger, anchor)
                    if route is not None and result:
                        self.assertIs(supervisor, case.session._supervisor)
                        self.assertIs(supervisor._last_handoff_controller, route)
                        self.assertIs(route.executor._controller, case.controller)
                        self.assertIs(case.controller._committed_phase, CommittedExecutionPhase.RELEASED)
                        evidence = supervisor.last_handoff
                        self.assertIs(evidence.disposition, HandoffDisposition.QUIESCENT)
                        self.assertEqual(evidence.movement_tick_id, 36)
                        supervisor._last_handoff = replace(evidence, reason=reason)
                        mutated.append(evidence)
                    return result

                with patch.object(ExecutionSupervisor, "retire_route", new=retire), \
                     patch("mc2p.motion_nav.execution_supervisor.verified_ground_rollout",
                           side_effect=AssertionError("report reason must not select a release proof")):
                    result = case.tick()
                self.assertEqual(len(mutated), 1)
                self.assertIsNone(result.report.failure)
                self.assertIsNone(case.driver.source)
                self.assertIsNone(case.session._supervisor._last_handoff_controller)
                self.assertEqual(case.backend.damage_taken, 0.)

    def test_L_and_complete_tail_only_compare_cached_proof_references(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        for branch in ("ready", "activation_reject", "cancel"):
            with self.subTest(branch=branch):
                case = self.ready_at_L()
                if branch == "cancel":
                    case.session.cancel("component_cancel_at_L")
                reject = ActivationResult._from_native_references(
                    ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
                with forbid_control_rebuilds() as counts, ExitStack() as stack:
                    if branch == "activation_reject":
                        stack.enter_context(patch.object(ExecutionSupervisor, "commit_activation", return_value=reject))
                    result = case.tick()
                    self.assertIsNone(result.report.failure)
                    if branch != "ready":
                        for _ in range(10):
                            if case.driver.source is None:
                                break
                            result = case.tick()
                            self.assertIsNone(result.report.failure)
                        self.assertIsNone(case.driver.source)
                self.assertTrue(all(count == 0 for count in counts.values()), counts)

    def test_no_ready_default_L_uses_driver_session_runtime_backend_and_real_registration(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        case = ExecutionCase()
        self.addCleanup(case.close)
        for _ in range(28):
            before = case.backend.movement_tick
            count = len(case.backend.actions)
            result = case.tick()
            self.assertIsNone(result.report.failure)
            self.assertEqual(len(case.backend.actions), count + 1)
            self.assertEqual(case.backend.movement_tick, before + 1)
            self.assertEqual(sum(group == "movement" for group, _ in result.decision.selected_intents), 1)
            if before == 26:
                self.assertTrue(result.decision.action.movement.sneak)
                record = case.runtime.input_ledger.record(result.decision.action.request_sequence_id)
                self.assertEqual(record.applied_ticks, (27,))
                self.assertEqual(case.controller._pending.control_sequence, record.control_sequence)
                self.assertIsNone(record.stream_command)
            if before == 27:
                self.assertEqual(case.controller._tail_cursor, 1)
                self.assertIs(case.session._executor._controller, case.controller)

    def test_runtime_prepare_rejection_sends_incumbent_fallback_once(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        case = ExecutionCase()
        self.addCleanup(case.close)
        case.prepare_successor()
        for _ in range(26):
            result = case.tick()
            self.assertIsNone(result.report.failure)
        self.assertEqual(case.backend.movement_tick, 26)
        before = len(case.backend.actions)
        reject = ActivationResult._from_native_references(
            ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        with patch.object(ExecutionSupervisor, "prepare_activation", return_value=reject) as rejected:
            result = case.tick()
        self.assertEqual(rejected.call_count, 1)
        self.assertEqual(len(case.backend.actions), before + 1)
        self.assertEqual(case.backend.movement_tick, 27)
        self.assertTrue(result.decision.action.movement.sneak)
        self.assertIs(case.session._executor._controller, case.controller)
        self.assertEqual(case.session._executor.action_index, 0)
        record = case.runtime.input_ledger.record(result.decision.action.request_sequence_id)
        self.assertEqual(record.applied_ticks, (27,))
        self.assertIsNone(record.stream_command)
        self.assertEqual(case.controller._pending.control_sequence, record.control_sequence)

    def test_same_frame_cancel_at_L_keeps_default_fallback(self):
        case = install_current(self)
        state = case.proof.entry_state
        ledger = InputApplicationLedger(max_records=128)
        for index in range(27):
            anchor = anchor_for(state)
            frame = candidate_fixtures.VerifiedMotionRouteIntegrationTests.frame(
                case.source.view(), state, anchor.observation_sequence_id)
            decision = case.executor.decide(frame, state_anchor=anchor, input_ledger=ledger)
            if state.movement_tick_id == 26:
                self.assertTrue(decision.movement.sneak)
                case.executor.request_stop(StopCause.CANCELLED)
                protected = case.executor.stop_protection(decision, frame, state_anchor=anchor)
                self.assertEqual(protected.movement, decision.movement)
                self.assertEqual(protected.verified_command_index, 26)
                self.assertTrue(protected.submit_input)
                return
            case.executor.register_verified_submission(decision.verified_command_index,
                control_sequence=index + 1, requested_movement_tick=decision.expected_movement_tick,
                requested_latest_movement_tick=decision.latest_movement_tick)
            movement = decision.movement
            state = step(state, TickInput(float(movement.forward), float(movement.strafe),
                movement.jump, movement.sneak, movement.sprint, state.yaw_radians),
                case.fixture.context.world, JAVA_1_21_RULESET).next_state
            candidate_fixtures.VerifiedMotionExecutorTests.applied(ledger, anchor, index + 1,
                state.movement_tick_id, movement)


if __name__ == "__main__":
    unittest.main()
