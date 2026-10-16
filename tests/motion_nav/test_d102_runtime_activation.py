"""Actual Runtime arbitration, registry admission and binding delivery boundaries."""
from dataclasses import replace
import inspect
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, AttackEntityV1, LookV1, MovementV1, MovementTickWindowV1
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.motion_activation import (
    ActivationRejectReason, ActivationResult, ActivationStatus, FirstStreamApplication,
    PreparedStreamDispatch, StreamApplicationStatus, StreamStartBranch,
)
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.contracts.task import TaskIntentV0
from mc2p.contracts.task import SuccessCriterionV0, ComparisonOperatorV0
from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutor
from mc2p.motion_nav.body_control import StopCause
from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion, solve_one_cell_gap
from mc2p.motion_nav.online_motion import CandidateExecutionWindow, InputApplicationStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.prepared_motion import ProofUse
from mc2p.motion_nav.world_model import CellKnowledge, ObservationStamp, WorldKnowledge, WorldSessionId
from mc2p.motion_nav.world_snapshot import seal_snapshot_version
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from tests.follow_v3_fixtures import follow_snapshot
from tests.motion_nav import test_d102_action_handoff as handoff_fixtures
from tests.test_action_receipt import receipt_value
from tests.test_player_runtime import _RecordingTrace


class _HandoffFixture(handoff_fixtures.ActionHandoffTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source, original = cls.snapshot, cls.base
        session = WorldSessionId("fixture:episode:clock")
        cls.base = SimpleNamespace(entry_state=replace(original.entry_state, session=session),
            world_session_id=session.value, ruleset=original.ruleset, stream=original.stream)
        cls.world = WorldKnowledge(session)
        stamp = ObservationStamp(session, 1, 1, "test", 0)
        cls.world.confirm_air(stamp, tuple(p for p, f in source.facts if f.knowledge is CellKnowledge.AIR))
        cls.world.observe_blocks(stamp, {p: f.block for p, f in source.facts if f.block is not None})
        version = cls.world.borrow_snapshot_version(cls.bounds, ruleset=cls.base.ruleset)
        cls.snapshot = seal_snapshot_version(version, cls.base.ruleset)
        cls.world.publish_sealed_snapshot(version, cls.snapshot)
        physics = PhysicsWorldView(cls.world.view(), cls.base.ruleset)
        cls.proofs = []
        state = cls.base.entry_state
        for index, z in enumerate((2, 4)):
            solved = solve_one_cell_gap(cls.anchor(state, index + 1), physics, GapSolveRequest(
                (0, 1), LandingRegion(.3, .7, z + .3, z + .7, 1.),
                CandidateExecutionWindow(state.movement_tick_id + 1, state.movement_tick_id + 2)))
            if solved.proof is None:
                raise AssertionError("the original solver must prepare the fixture's unchanged gaps")
            cls.proofs.append(solved.proof)
            state = solved.proof.exit_state


class _Backend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(self, fixture, clock, events):
        self.fixture, self.clock, self.events = fixture, clock, events
        self.state, self.sequence = fixture.current.physics_state, 0
        self.actions = []
        self.send_error = False
        self.offset = 0
        self.omit = False
        self.changed = False

    def observation(self, action=None):
        state = self.state
        result = follow_snapshot(sequence=self.sequence, received=self.clock[0], episode="episode",
            position=state.position, yaw=math.degrees(state.yaw_radians), self_changes={
                "movement_tick_id": state.movement_tick_id, "is_on_ground": state.on_ground,
                "velocity": dict(zip(("x", "y", "z"), state.velocity_blocks_per_tick))})
        return replace(result, request_sequence_id=None if action is None else action.request_sequence_id,
                       client_sample=replace(result.client_sample, clock_id="clock"))

    def reset(self, request):
        return ResetResultV0(request.request_id, request.episode_id, True, self.observation())

    def step(self, action, deadline, *, observation_request=None):
        self.actions.append(action)
        self.events.append("BACKEND_SEND")
        if self.send_error:
            raise OSError("may have sent the command before transport failed")
        yaw = self.state.yaw_radians + math.radians(action.look.yaw_delta_degrees)
        for _ in range(self.offset):
            hold = project_movement_command(self.state, MovementV1(), movement_yaw_radians=self.state.yaw_radians)
            self.state = step(self.state, hold.tick_input, self.fixture.physics_world,
                              self.fixture.base.ruleset).next_state
        projected = project_movement_command(self.state, action.movement, movement_yaw_radians=yaw)
        self.state = step(self.state, projected.tick_input, self.fixture.physics_world,
                          self.fixture.base.ruleset).next_state
        self.sequence += 1
        self.clock[0] += 1_000_000
        observation = self.observation(action)
        movement = MovementV1() if self.changed else action.movement
        applications = [] if self.omit else [{
            "schema_version": "mc2p.input-application.v1", "episode_id": action.episode_id,
            "request_sequence_id": action.request_sequence_id, "movement_tick_id": self.state.movement_tick_id,
            "sampled_at_jvm_ns": self.state.movement_tick_id, "state": "leased",
            "forward": float(movement.forward), "strafe": float(movement.strafe),
            "jump": movement.jump, "sneak": movement.sneak, "sprint": movement.sprint}]
        raw = receipt_value(episode_id=action.episode_id, generation_id=self.sequence,
            request_sequence_id=action.request_sequence_id, world_tick=observation.world_time_ticks.value,
            input_samples=self.state.movement_tick_id, leased_input_samples=self.state.movement_tick_id)
        raw.update(schema_version="mc2p.client_action_receipt.v3", dropped_input_samples=0,
                   oldest_retained_input_tick=self.state.movement_tick_id, input_applications=applications)
        return BackendStepResultV1(observation, 0, False, False, behavior_receipt_from_mapping(raw))

    def close(self):
        pass


class ActivationTests(unittest.TestCase):
    fixture_type = _HandoffFixture
    @classmethod
    def setUpClass(cls):
        cls.fixture_type.setUpClass()

    def setUp(self):
        # Composition avoids collecting Task 7's suite again in this module.
        self.f = self.fixture_type("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.clock, self.events = [100_000_000], []
        self.backend = _Backend(self.f, self.clock, self.events)
        self.runtime = PlayerRuntimeV1(self.backend, _RecordingTrace(), clock_ns=lambda: self.clock[0])
        self.runtime.reset(ResetRequestV0("reset", "episode", "activation-fixture", 1, 2_000_000_000))
        # The Runtime's existing observed-world owner supplies the actual lease.
        world = self.runtime.snapshot_world_port
        stamp = ObservationStamp(world.session, 1, 1, "test", 0)
        world.confirm_air(stamp, tuple(p for p, f in self.f.snapshot.facts if f.knowledge is CellKnowledge.AIR))
        world.observe_blocks(stamp, {p: f.block for p, f in self.f.snapshot.facts if f.block is not None})
        version = world.borrow_snapshot_version(self.f.bounds, ruleset=self.f.base.ruleset)
        self.f.snapshot = seal_snapshot_version(version, self.f.base.ruleset)
        self.assertTrue(world.publish_sealed_snapshot(version, self.f.snapshot))
        self.f.world, self.f.physics_world = world, PhysicsWorldView(world.view(), self.f.base.ruleset)
        self.f.inbox.bind_world(world)
        self.f.ledger = self.runtime.input_ledger
        self.f.ledger_id = getattr(self.f.ledger, "ledger_identity", "ledger-before-task8")
        self.incumbent = self.f.prepared_incumbent()
        self.handle = None

    def intent(self, identity, *, movement=None, look=None, operation=None, priority=ActionPriorityV0.TASK,
               window=None):
        obs = self.runtime.observation
        return ActionIntentV1(identity, identity + "-source", "episode", obs.sequence_id, priority,
            self.clock[0], self.clock[0] + 1_000_000_000, movement=movement, look=look,
            operation=operation, movement_tick_window=window)

    def frame(self, **kwargs):
        deadline = self.clock[0] + 1_000_000_000
        task = TaskIntentV0("task", "navigate", "{}", (SuccessCriterionV0("done", ComparisonOperatorV0.EQUAL, 1., "bool"),), 1, deadline, True, 0.)
        return self.runtime.control_frame(task, BehaviorProfileV0(), deadline, **kwargs)

    def arrive(self, *, prepare_during=False):
        for index, command in enumerate(self.f.proofs[0].commands):
            decision = self.f.executor.decide(self.f.frame(self.f.current), state_anchor=self.f.current,
                                             input_ledger=self.f.ledger)
            if decision.verified_command_index is not None:
                self.f.executor.register_verified_submission(index, control_sequence=len(self.backend.actions),
                    requested_movement_tick=decision.expected_movement_tick,
                    requested_latest_movement_tick=decision.latest_movement_tick)
            self.runtime.submit_intent(self.intent("incumbent-" + str(index), movement=command.movement))
            self.frame()
            self.f.current = self.f.anchor(self.backend.state, self.runtime.observation.sequence_id)
            if index == 0:
                self.f.executor.decide(self.f.frame(self.f.current), state_anchor=self.f.current, input_ledger=self.f.ledger)
                self.handle = self.f.ready_next()
        self.f.executor.decide(self.f.frame(self.f.current), state_anchor=self.f.current, input_ledger=self.f.ledger)
        if self.handle is None:
            self.handle = self.f.ready_next()
        # Refresh original current-facts publication at the real boundary.
        self.f.prepare()
        staged = self.f.executor.stage_prepared_action(self.f.frame(self.f.current), self.f.current)
        self.approval = self.f.supervisor.approve_action_handoff(self.f.control, staged,
            self.f.frame(self.f.current), self.f.ledger)
        self.proposal = self.f.control.prepared_action_proposal(self.approval)
        self.assertIsNotNone(self.proposal)

    def dispatch(self, *, proposal=None, port=None):
        self.assertIn("prepared_streams", inspect.signature(PlayerRuntimeV1.control_frame).parameters,
                      "Runtime has no post-arbitration activation boundary")
        self.assertTrue(callable(getattr(self.f.supervisor, "prepare_activation", None)),
                        "the existing supervisor cannot accept/deliver/commit activation")
        proposal = proposal or self.proposal
        self.runtime.submit_intent(self.intent(proposal.movement_intent_id,
            movement=proposal.expected_movement, window=proposal.requested_window))
        return self.frame(prepared_streams=(PreparedStreamDispatch(proposal, port or self.f.supervisor),))

    def test_global_sequences_continue_during_preparation(self):
        self.arrive(prepare_during=True)
        self.assertEqual([a.request_sequence_id for a in self.backend.actions], list(range(16)))
        result = self.dispatch()
        self.assertEqual(self.f.executor.prepared_controller.stream_tracker.activation_binding.first_control_sequence, 16)
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)

    def test_not_selected_keeps_candidate_without_activation(self):
        self.arrive()
        self.runtime.submit_intent(self.intent("safety", movement=MovementV1(strafe=1), priority=ActionPriorityV0.SAFETY))
        before = self.f.unchanged(self.incumbent)
        result = self.dispatch()
        self.assertIs(result.stream_activation.status, ActivationStatus.NOT_SELECTED)
        self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.CANDIDATE))
        self.assertEqual(self.f.unchanged(self.incumbent), before)

    def test_composite_motion_projection_must_match(self):
        self.arrive()
        result = self.dispatch(proposal=replace(self.proposal, expected_movement=MovementV1(strafe=1)))
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATION_MISMATCH)
        self.assertEqual(len(self.backend.actions), 16)
        self.assertEqual(self.f.executor.action_index, 0)

    def test_final_yaw_changes_projection_is_rejected(self):
        self.arrive()
        self.runtime.submit_intent(self.intent("combat-look", look=LookV1(90, 0), priority=ActionPriorityV0.SAFETY))
        result = self.dispatch()
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATION_MISMATCH)
        self.assertEqual(len(self.backend.actions), 16)

    def test_same_frame_attack_preserves_motion(self):
        self.arrive()
        attack = AttackEntityV1("entity-7")
        self.runtime.submit_intent(self.intent("attack", operation=attack))
        result = self.dispatch()
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)
        self.assertEqual(self.backend.actions[-1].operation, attack)

    def test_activation_registered_and_delivered_before_backend_send(self):
        self.arrive()
        for method, event in (("accept_activation", "REGISTRY_ACCEPTED"),):
            original = getattr(self.f.inbox, method)
            def wrapped(*args, _original=original, _event=event, **kwargs):
                result = _original(*args, **kwargs)
                self.events.append(_event)
                return result
            self.enterContext(patch.object(self.f.inbox, method, side_effect=wrapped))
        for method, event in (("prepare_activation", "BINDING_DELIVERED"), ("commit_activation", "ACTION_AND_USAGE_COMMITTED")):
            self.assertTrue(callable(getattr(VerifiedMotionExecutor, method, None)), "executor lacks binding delivery/commit")
            original = getattr(VerifiedMotionExecutor, method)
            def wrapped(executor, *args, _original=original, _event=event, **kwargs):
                result = _original(executor, *args, **kwargs)
                self.events.append(_event)
                return result
            self.enterContext(patch.object(VerifiedMotionExecutor, method, wrapped))
        self.events.clear()
        self.dispatch()
        self.assertEqual(self.events[:4], ["REGISTRY_ACCEPTED", "BINDING_DELIVERED", "ACTION_AND_USAGE_COMMITTED", "BACKEND_SEND"])

    def test_receipt_is_reported_and_pruned_before_runtime_returns(self):
        self.arrive()
        self.dispatch()
        tracker = self.f.executor.prepared_controller.stream_tracker
        self.assertIs(tracker.start_branch, StreamStartBranch.ON_TIME)
        self.assertEqual(len(tracker.command_facts), 1)
        self.assertEqual(len(self.f.executor.prepared_controller.remaining_start_branches), 1)

    def test_real_first_late_application_selects_held_branch_before_return(self):
        self.arrive()
        self.backend.offset = 1
        result = self.dispatch()
        controller = self.f.executor.prepared_controller
        self.assertIs(controller.stream_tracker.start_branch, StreamStartBranch.LATE_ONE)
        self.assertEqual(controller.remaining_start_branches[0].trajectory[1], self.backend.state)
        self.assertEqual(self.f.ledger.record(result.decision.action.request_sequence_id).applied_ticks, (28,))

    def test_runtime_midstream_late_or_missing_retains_owner_without_reselecting_start(self):
        for failure in ("late", "missing"):
            with self.subTest(failure=failure):
                if failure == "missing":
                    self.f.doCleanups()
                    self.setUp()
                self.arrive()
                self.dispatch()
                controller = self.f.executor.prepared_controller
                command = self.handle.hot_view.commands[1]
                proposal = replace(self.proposal, movement_intent_id="midstream-"+failure,
                    expected_movement=command.movement, requested_window=MovementTickWindowV1(28, 28))
                self.backend.offset, self.backend.omit = int(failure == "late"), failure == "missing"
                self.dispatch(proposal=proposal)
                self.assertIs(controller._application_result.status, StreamApplicationStatus.DEVIATION)
                self.assertIs(controller.stream_tracker.start_branch, StreamStartBranch.ON_TIME)
                self.assertIs(self.f.supervisor.route, self.f.control)
                self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))

    def test_wrong_token_owner_ledger_window_or_generation_sends_zero_bundle_input(self):
        self.arrive()
        variants = [replace(self.proposal, **{field: "wrong"}) for field in
                    ("proof_token", "owner_id", "ledger_id", "generation_id", "world_session_id")]
        variants.append(replace(self.proposal, requested_window=replace(self.proposal.requested_window, latest_tick=99)))
        for index, proposal in enumerate(variants):
            with self.subTest(proposal=proposal):
                result = self.dispatch(proposal=replace(proposal, movement_intent_id=proposal.movement_intent_id + "/" + str(index)))
                self.assertIn(result.stream_activation.status, (ActivationStatus.ACTIVATION_MISMATCH, ActivationStatus.STALE_GENERATION))
                self.assertEqual(len(self.backend.actions), 16)
                self.assertEqual(self.f.executor.action_index, 0)

    def test_activation_hot_path_does_not_walk_immutable_graphs(self):
        from mc2p.contracts import motion_activation
        self.arrive()
        # Dispatch construction is a caller boundary, outside control_frame.
        dispatch = PreparedStreamDispatch(self.proposal, self.f.supervisor)
        self.runtime.submit_intent(self.intent(self.proposal.movement_intent_id,
            movement=self.proposal.expected_movement, window=self.proposal.requested_window))
        with patch.object(motion_activation, "require_immutable_graph", wraps=motion_activation.require_immutable_graph) as walked:
            self.frame(prepared_streams=(dispatch,))
        self.assertEqual(walked.call_count, 0)

    def test_driver_exposes_prepared_dispatch_without_duplicating_first_submission(self):
        from mc2p.motion_nav.navigation_session import NavigationSessionProposal
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
        self.arrive()
        self.assertIn("prepared_streams", inspect.signature(NavigationSessionProposal).parameters,
                      "the Session proposal loses the approved Runtime dispatch")
        dispatches = (PreparedStreamDispatch(self.proposal, self.f.supervisor),)
        proposal = NavigationSessionProposal(None, None, prepared_streams=dispatches)
        driver = object.__new__(RuntimeNavigationDriver)
        driver._prepared_proposal = proposal
        self.assertEqual(driver.prepared_streams, dispatches)

    def test_runtime_binds_actual_midstream_sequence_and_reports_deviation(self):
        self.arrive()
        self.dispatch()
        controller = self.f.executor.prepared_controller
        command = self.handle.hot_view.commands[1]
        proposal = replace(self.proposal, movement_intent_id="second-prepared-input",
            expected_movement=command.movement, expected_yaw_radians=command.required_movement_yaw_radians,
            requested_window=replace(self.proposal.requested_window, earliest_tick=28, latest_tick=28))
        self.backend.changed = True
        result = self.dispatch(proposal=proposal)
        self.assertIsNotNone(self.f.ledger.stream_command(result.decision.action.request_sequence_id),
                             "Runtime discarded the actual midstream sequence-to-generation fact")
        self.assertEqual(self.f.ledger.stream_command(result.decision.action.request_sequence_id).command_index, 1)
        self.assertIs(controller._application_result.status, StreamApplicationStatus.DEVIATION)
        self.assertIs(controller.stream_tracker.start_branch, StreamStartBranch.ON_TIME)

    def test_g1_payload_records_real_before_input_entry_and_string_generation(self):
        from tests.motion_nav.d102_contract_cases import canonical_prepared_payloads
        from scripts import verify_prepared_motion_d102 as verifier
        self.arrive()
        registration = self.f.inbox._registry[self.handle.proof_token]
        raw = canonical_prepared_payloads(registration.bundle.request, registration.bundle, registration.lease.snapshot)
        errors = verifier._payload_errors(raw, "first_receipt_branch_pruning", stage="G1", frozen=verifier._load(verifier.MANIFEST_PATH))
        self.assertEqual(errors, [], "the G1 schema rejects the unchanged native prepared proof")
        import copy
        frozen = verifier._load(verifier.MANIFEST_PATH)
        for value in ("", True, "wrong-generation", "1", "x" * 129, "bad generation"):
            changed = copy.deepcopy(raw)
            changed["proof"]["identity"]["stream_generation"] = value
            self.assertTrue(verifier._payload_errors(changed, "first_receipt_branch_pruning", stage="G1", frozen=frozen))
        for field in ("movement_tick", "physics_state"):
            changed = copy.deepcopy(raw)
            changed["proof"]["branches"][0]["trajectory"][0][field] = 99 if field == "movement_tick" else {}
            self.assertTrue(verifier._payload_errors(changed, "first_receipt_branch_pruning", stage="G1", frozen=frozen))
        for key in ("native_request", "native_proof"):
            changed = copy.deepcopy(raw)
            changed["input" if key == "native_request" else "proof"][key]["stream"]["generation_id"] = True
            self.assertTrue(verifier._payload_errors(changed, "first_receipt_branch_pruning", stage="G1", frozen=frozen),
                            "native generation value/type changed while the outer identity stayed unchanged")

    def test_g1_keeps_the_independent_parent_request_source(self):
        from tests.motion_nav.d102_contract_cases import fixed_input_binding
        raw = fixed_input_binding()
        self.assertIs(raw.get("parent_input_reference_is_independent"), True)

    def test_g1_registry_commands_all_come_from_the_real_runtime_ledger(self):
        from tests.motion_nav.d102_contract_cases import runtime_post_arbitration_activation
        raw = runtime_post_arbitration_activation()
        self.assertIn(raw["activation_events"][0]["first_control_sequence"], raw["registry_command_sequences"])
        self.assertLessEqual(set(raw["registry_command_sequences"]), set(raw["actual_ledger_sequences"]))

    def test_no_conflicting_generation_for_same_owner_tick(self):
        self.arrive()
        self.assertIn("prepared_streams", inspect.signature(PlayerRuntimeV1.control_frame).parameters,
                      "Runtime has no conflict admission")
        self.assertTrue(callable(getattr(self.f.supervisor, "prepare_activation", None)))
        self.runtime.submit_intent(self.intent(self.proposal.movement_intent_id, movement=self.proposal.expected_movement))
        other = replace(self.proposal, generation_id="conflicting-generation")
        result = self.frame(prepared_streams=tuple(PreparedStreamDispatch(p, self.f.supervisor) for p in (self.proposal, other)))
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATION_MISMATCH)
        self.assertEqual(len(self.backend.actions), 16)

    def test_cancel_after_supervisor_permission_before_registry_accept_keeps_incumbent(self):
        self.arrive()
        before = self.f.unchanged(self.incumbent)
        original = self.f.inbox.accept_activation
        def cancel_at_accept(*args, **kwargs):
            self.f.control.request_stop(StopCause.CANCELLED)
            return original(*args, **kwargs)
        with patch.object(self.f.inbox, "accept_activation", side_effect=cancel_at_accept):
            result = self.dispatch()
        self.assertIsNot(result.stream_activation.status, ActivationStatus.ACTIVATED)
        self.assertEqual(self.f.unchanged(self.incumbent), before)
        self.assertEqual(len(self.backend.actions), 16)

    def test_delivery_failure_keeps_incumbent(self):
        self.arrive()
        self.assertTrue(callable(getattr(VerifiedMotionExecutor, "prepare_activation", None)), "no delivery boundary")
        before = self.f.unchanged(self.incumbent)
        rejected = ActivationResult(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.TRANSACTION)
        with patch.object(VerifiedMotionExecutor, "prepare_activation", return_value=rejected):
            result = self.dispatch()
        self.assertEqual(result.stream_activation, rejected)
        self.assertEqual(self.f.unchanged(self.incumbent), before)
        self.assertEqual(self.f.inbox._close_report().activation_references, 0)
        self.assertEqual(len(self.backend.actions), 16)

    def test_delivery_exception_aborts_acceptance_and_keeps_incumbent(self):
        self.arrive()
        before = self.f.unchanged(self.incumbent)
        with patch.object(VerifiedMotionExecutor, "prepare_activation", side_effect=RuntimeError("delivery failed")):
            self.dispatch()
        self.assertEqual(self.f.unchanged(self.incumbent), before)
        self.assertEqual(self.f.inbox._close_report().activation_references, 0)
        self.assertEqual(len(self.backend.actions), 16)

    def test_driver_retains_source_when_committed_send_is_uncertain(self):
        from unittest.mock import Mock
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver, RuntimeNavigationDriverState
        from mc2p.motion_nav.navigation_session import NavigationSessionProposal
        self.arrive()
        source = self.runtime.register_ordered_source("uncertain-prepared-body")
        driver = object.__new__(RuntimeNavigationDriver)
        driver.runtime, driver.source = self.runtime, source
        driver.session = SimpleNamespace(report=SimpleNamespace(terminal=False), handle_control_unavailable=Mock())
        driver._prepared_deadline_ns = self.clock[0] + 1_000_000_000
        driver._prepared_proposal = NavigationSessionProposal(None, None,
            prepared_streams=(PreparedStreamDispatch(self.proposal, self.f.supervisor),))
        driver._state = RuntimeNavigationDriverState.RUNNING
        driver._sync_report, driver._release_source = Mock(), Mock()
        self.backend.send_error = True
        result = self.dispatch()
        driver.adopt_result(result)
        self.assertEqual(driver._release_source.call_count, 0, "uncertain send was treated as safe source release")
        self.assertIs(driver.source, source)
        self.assertIs(driver.state, RuntimeNavigationDriverState.STOPPING)
        self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))

    def _driver_for_prepared_owner(self):
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
        from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles, NavigationSessionProposal
        from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
        from tests.motion_nav.test_b07_step_route import step_profile
        session = NavigationSession("task8-review", NavigationSessionProfiles(
            handoff_fixtures.ground_profile(), handoff_fixtures.jump_profile(), step_profile()),
            planner_worker=_InlinePlanner(), owns_planner_worker=False,
            motion_worker=self.f.worker, motion_result_inbox=self.f.inbox, clock_ns=lambda: self.clock[0])
        driver = RuntimeNavigationDriver(self.runtime, session, clock_ns=lambda: self.clock[0])
        driver.start("goal", 1, _goal((.5, 1., 4.5)), self.clock[0])
        session._supervisor = self.f.supervisor
        session._control_ledger, session._control_anchor = self.f.ledger, self.f.current
        session.execution_anchor(self.runtime.observation, self.f.ledger)
        driver._prepared_deadline_ns = self.clock[0] + 1_000_000_000
        driver._prepared_proposal = NavigationSessionProposal(None, None,
            prepared_streams=(PreparedStreamDispatch(self.proposal, self.f.supervisor),))
        return driver

    def test_uncertain_send_blocks_later_release_stop_and_close(self):
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriverState
        for operation in ("release", "stop", "close", "failed_session"):
            with self.subTest(operation=operation):
                if operation != "release":
                    self.f.doCleanups()
                    self.setUp()
                self.arrive()
                driver = self._driver_for_prepared_owner()
                source = driver.source
                self.backend.send_error = True
                result = self.dispatch()
                driver.adopt_result(result)
                self.assertIs(driver.state, RuntimeNavigationDriverState.STOPPING)
                if operation == "stop":
                    try:
                        stopped = driver.stop(BehaviorProfileV0(), "caller_cleanup")
                    except ContractViolation as error:
                        self.assertIn("BODY_RESPONSIBILITY_UNRESOLVED", str(error))
                    else:
                        self.assertEqual(stopped.report.phase, "BODY_RESPONSIBILITY_UNRESOLVED")
                else:
                    if operation == "close":
                        driver.session.close()
                        self.runtime.close()
                    elif operation == "failed_session":
                        driver.session.handle_control_unavailable()
                    self.assertFalse(driver.release("caller_cleanup"), "uncertain command reported released")
                    self.assertFalse(driver.release("repeat_cleanup"))
                self.assertIs(driver.source, source)
                self.assertIs(driver.session._source, source)
                self.assertIs(driver.state, RuntimeNavigationDriverState.STOPPING)
                self.assertEqual(driver.session.report.terminal, operation == "failed_session",
                                 "cleanup invented a business terminal")
                self.assertIs(self.f.ledger.record(16).status, InputApplicationStatus.IN_FLIGHT)
                self.assertIs(self.f.supervisor.route, self.f.control)
                self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.EXECUTING))
                self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))

    def test_terminal_command_still_requires_original_supervisor_release(self):
        from mc2p.contracts.action_receipt import ClientInputApplicationV1
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriverState
        self.arrive()
        driver = self._driver_for_prepared_owner()
        source = driver.source
        self.backend.send_error = True
        result = self.dispatch()
        driver.adopt_result(result)
        record = self.f.ledger.observe_sample(ClientInputApplicationV1("mc2p.input-application.v1",
            27, "episode", 16, 27, "expired", 0., 0., False, False, False))
        self.assertIs(record.status, InputApplicationStatus.EXPIRED)
        self.assertFalse(driver.release("terminal_without_body_exit"))
        self.assertIs(driver.source, source)
        self.assertIs(driver.state, RuntimeNavigationDriverState.STOPPING)
        self.assertIs(self.f.supervisor.route, self.f.control)
        self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))

    def test_complete_stream_with_native_release_evidence_releases_source(self):
        from tests.motion_nav.d102_contract_cases import _record_complete_release
        from mc2p.motion_nav.body_control import HandoffDisposition
        self.arrive()
        driver = self._driver_for_prepared_owner()
        source = driver.source
        result = self.dispatch()
        driver.adopt_result(result)
        raw = {name: [] for name in ("command_terminal_events", "supervisor_events", "reference_events")}
        raw["input"] = {"identity": {"action_id": "task8-review/action-1"}}
        _record_complete_release(self, self.handle, raw, lambda operation, **fields: dict(operation=operation, **fields))
        self.assertIs(self.f.supervisor.last_handoff.disposition, HandoffDisposition.QUIESCENT)
        self.assertFalse(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))
        driver.session.handle_control_unavailable()
        self.assertTrue(driver.release("proved_body_exit"))
        self.assertIsNone(driver.source)
        self.assertIsNone(driver.session._source)
        self.assertFalse(self.runtime.has_ordered_source(source))

    def test_nonapplication_neutral_sample_does_not_advance_stream(self):
        for sample_state in ("expired", "disallowed", "lease_exhausted", "neutral"):
            with self.subTest(sample_state=sample_state):
                if sample_state != "expired":
                    self.f.doCleanups()
                    self.setUp()
                self.arrive()
                self.dispatch()
                controller = self.f.executor.prepared_controller
                for index in (1, 2):
                    command = self.handle.hot_view.commands[index]
                    tick = controller.stream_tracker.expected_effect_tick(index)
                    proposal = replace(self.proposal, movement_intent_id=f"review-neutral/{index}",
                        expected_movement=command.movement, expected_yaw_radians=command.required_movement_yaw_radians,
                        requested_window=MovementTickWindowV1(tick, tick))
                    if index == 1:
                        self.dispatch(proposal=proposal)
                    else:
                        self.assertEqual(command.movement, MovementV1())
                        original = self.backend.step
                        def sample_return(*args, **kwargs):
                            result = original(*args, **kwargs)
                            samples = tuple(replace(sample, state=sample_state) for sample in result.receipt.input_applications)
                            return replace(result, receipt=replace(result.receipt, input_applications=samples))
                        with patch.object(self.backend, "step", side_effect=sample_return):
                            result = self.dispatch(proposal=proposal)
                record = self.f.ledger.record(result.decision.action.request_sequence_id)
                applied = sample_state == "neutral"
                self.assertIs(record.status, InputApplicationStatus.APPLIED if applied else
                              InputApplicationStatus.REJECTED if sample_state == "disallowed" else InputApplicationStatus.EXPIRED)
                self.assertEqual(len(controller.stream_tracker.command_facts), 3 if applied else 2,
                                 "nonapplication sample consumed a prepared command index")
                self.assertEqual(controller._command_index, 3 if applied else 2)
                self.assertIs(controller._application_result.status,
                              StreamApplicationStatus.APPLIED if applied else StreamApplicationStatus.DEVIATION)
                self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))

    def test_return_boundary_timeout_keeps_first_application_and_pruning(self):
        from mc2p.contracts.report import ExecutionStatusV0
        for offset in (0, 1):
            with self.subTest(offset=offset):
                if offset:
                    self.f.doCleanups()
                    self.setUp()
                self.arrive()
                self.backend.offset = offset
                original = self.backend.step
                def late_return(action, deadline, **kwargs):
                    result = original(action, deadline, **kwargs)
                    self.clock[0] = deadline + 1
                    return result
                with patch.object(self.backend, "step", side_effect=late_return):
                    result = self.dispatch()
                controller = self.f.executor.prepared_controller
                self.assertIs(result.report.status, ExecutionStatusV0.TIMED_OUT)
                self.assertEqual(result.report.phase, "BODY_RESPONSIBILITY_UNRESOLVED")
                self.assertIs(controller.stream_tracker.start_branch,
                              StreamStartBranch.LATE_ONE if offset else StreamStartBranch.ON_TIME)
                self.assertEqual(len(controller.stream_tracker.command_facts), 1)
                self.assertEqual(len(controller.remaining_start_branches), 1)
                record = self.f.ledger.record(result.decision.action.request_sequence_id)
                self.assertIs(record.status, InputApplicationStatus.APPLIED)
                self.assertEqual(record.applied_ticks, (27 + offset,))

    def test_invalid_return_boundary_timeout_does_not_admit_application(self):
        from mc2p.contracts.report import FailureCodeV0
        self.arrive()
        original = self.backend.step
        def wrong_return(action, deadline, **kwargs):
            result = original(action, deadline, **kwargs)
            self.clock[0] = deadline + 1
            return replace(result, observation=replace(result.observation, request_sequence_id=999))
        with patch.object(self.backend, "step", side_effect=wrong_return):
            result = self.dispatch()
        self.assertIs(result.report.failure.code, FailureCodeV0.OBSERVATION_INVARIANT)
        self.assertIsNone(self.f.executor.prepared_controller.stream_tracker.start_branch)
        self.assertEqual(self.f.ledger.record(16).applied_ticks, ())

    def test_session_proposal_carries_boundary_and_each_active_command(self):
        from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
        from tests.motion_nav.test_navigation_session import _InlinePlanner
        from tests.motion_nav.test_b07_step_route import step_profile
        self.arrive()
        session = NavigationSession("prepared-proposal", NavigationSessionProfiles(
            handoff_fixtures.ground_profile(), handoff_fixtures.jump_profile(), step_profile()),
            planner_worker=_InlinePlanner(), owns_planner_worker=False,
            motion_worker=self.f.worker, motion_result_inbox=self.f.inbox, clock_ns=lambda: self.clock[0])
        session._supervisor = self.f.supervisor
        session._frame = self.f.frame(self.f.current)
        session.bind_source(self.runtime.register_ordered_source("prepared-session"))
        boundary = self.f.executor.decide(session._frame, state_anchor=self.f.current, input_ledger=self.f.ledger)
        try:
            proposal = session._proposal(boundary.movement, boundary.look, boundary.input_lease_ticks,
                self.clock[0] + 1_000_000_000, route_decision=boundary, route_control=self.f.control)
        except Exception as error:
            self.fail(f"the real Session cannot expose the prepared boundary: {error!r}")
        self.assertEqual(len(proposal.prepared_streams), 1)
        self.runtime.control_frame(TaskIntentV0("task", "navigate", "{}",
            (SuccessCriterionV0("done", ComparisonOperatorV0.EQUAL, 1., "bool"),), 1,
            self.clock[0] + 1_000_000_000, True, 0.), BehaviorProfileV0(), self.clock[0] + 1_000_000_000,
            proposals=(proposal.control_frame,), prepared_streams=proposal.prepared_streams)
        anchor = self.f.anchor(self.backend.state, self.runtime.observation.sequence_id)
        session._frame = self.f.frame(anchor)
        decision = self.f.executor.decide(session._frame, state_anchor=anchor, input_ledger=self.f.ledger)
        proposal = session._proposal(decision.movement, decision.look, decision.input_lease_ticks,
            self.clock[0] + 1_000_000_000, route_decision=decision, route_control=self.f.control)
        self.assertEqual(len(proposal.prepared_streams), 1, "active input lost its real sequence binding port")
        self.assertEqual(proposal.prepared_streams[0].proposal.requested_window, MovementTickWindowV1(28, 28))

    def test_replayed_exact_receipt_does_not_rewind_progress(self):
        self.arrive()
        self.dispatch()
        controller = self.f.executor.prepared_controller
        application = controller.stream_tracker._first_application
        replay = self.f.supervisor.record_application(application)
        self.assertIs(replay.status, StreamApplicationStatus.APPLIED)
        self.assertEqual(controller._command_index, 1)
        changed = replace(application, effect_tick=application.effect_tick+1)
        result = self.f.supervisor.record_application(changed)
        self.assertIs(result.status, StreamApplicationStatus.DEVIATION)

    def test_missing_anchor_retains_committed_prepared_body(self):
        self.arrive()
        self.dispatch()
        controller = self.f.executor.prepared_controller
        decision = controller.recover_without_anchor()
        self.assertEqual(decision.state.value, "recovering")
        self.assertIsNone(decision.movement)
        self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))

    def test_cancel_after_delivery_before_commit_keeps_incumbent(self):
        self.arrive()
        self.assertTrue(callable(getattr(VerifiedMotionExecutor, "prepare_activation", None)), "no delivery boundary")
        before = self.f.unchanged(self.incumbent)
        original = VerifiedMotionExecutor.prepare_activation
        def cancel_after_delivery(executor, *args):
            result = original(executor, *args)
            self.f.control.request_stop(StopCause.CANCELLED)
            return result
        with patch.object(VerifiedMotionExecutor, "prepare_activation", cancel_after_delivery):
            result = self.dispatch()
        self.assertIsNot(result.stream_activation.status, ActivationStatus.ACTIVATED)
        self.assertEqual(self.f.unchanged(self.incumbent), before)
        self.assertEqual(len(self.backend.actions), 16)

    def test_backend_send_exception_after_activation_keeps_uncertain_input_tail(self):
        self.arrive()
        self.backend.send_error = True
        result = self.dispatch()
        self.assertIs(result.stream_activation.status, ActivationStatus.ACTIVATED)
        sequence = result.decision.action.request_sequence_id
        self.assertIs(self.f.ledger.record(sequence).status, InputApplicationStatus.IN_FLIGHT)
        self.assertEqual(self.f.executor.action_index, 1)
        self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.EXECUTING))
        self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))
        report = self.f.inbox.close(timeout_seconds=.1)
        self.assertGreater(report.lease_count, 0)
        self.assertEqual(result.report.phase, "BODY_RESPONSIBILITY_UNRESOLVED")

    def test_first_sequence_is_real_global_sequence_not_command_index(self):
        self.arrive()
        result = self.dispatch()
        fact = self.f.ledger.stream_command(result.decision.action.request_sequence_id)
        self.assertEqual((fact.sequence, fact.command_index), (16, 0))

    def test_stage_or_approval_cannot_bypass_activation(self):
        self.arrive()
        before = self.f.unchanged(self.incumbent)
        self.assertTrue(callable(getattr(VerifiedMotionExecutor, "commit_activation", None)), "no checked commit boundary")
        executor = VerifiedMotionExecutor()
        with self.assertRaises(ContractViolation):
            executor.commit_activation(self.approval)
        self.assertEqual(self.f.unchanged(self.incumbent), before)

    def test_commit_is_exactly_once(self):
        self.arrive()
        self.dispatch()
        controller = self.f.executor.prepared_controller
        with self.assertRaises(ContractViolation):
            controller.commit_activation(controller.activation_delivery)
        self.assertEqual(self.f.executor.action_index, 1)

    def test_midstream_late_missing_or_changed_input_is_deviation(self):
        for failure in ("late", "missing", "changed"):
            with self.subTest(failure=failure):
                if failure != "late":
                    self.f.doCleanups()
                    self.setUp()
                self.arrive()
                self.dispatch()
                controller = self.f.executor.prepared_controller
                tracker = controller.stream_tracker
                command = self.handle.hot_view.commands[1]
                tick = tracker.expected_effect_tick(1) + (failure == "late")
                applied = FirstStreamApplication(self.handle.generation_id, 73, tick,
                    MovementV1() if failure == "changed" else command.movement, command.required_movement_yaw_radians)
                if failure == "missing":
                    result = controller.record_missing_application(command_index=1)
                else:
                    result = controller.record_application(applied, command_index=1)
                self.assertIs(result.status, StreamApplicationStatus.DEVIATION)
                self.assertIs(tracker.start_branch, StreamStartBranch.ON_TIME)

    def test_superseded_is_not_a_release_or_revocation_fact(self):
        self.arrive()
        self.dispatch()
        actual = self.f.ledger.record(16)
        self.assertIs(actual.status, InputApplicationStatus.APPLIED)
        self.assertTrue(self.f.inbox.usage_active(self.incumbent, ProofUse.SAFE_TAIL))
        self.assertTrue(self.f.inbox.usage_active(self.handle, ProofUse.SAFE_TAIL))


# The helper inherits old TestCase methods solely as fixture helpers.
del _HandoffFixture
