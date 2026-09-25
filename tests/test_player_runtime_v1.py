from dataclasses import replace
import unittest

from mc2p.contracts.action import ActionPriorityV0, ActionSnapshotV0
from mc2p.contracts.action_v1 import (
    ActionIntentV1, ActionSnapshotV1, AttackEntityV1, LookV1, MovementV1,
    OpenInventoryV1,
)
from mc2p.contracts.action_receipt import (
    ClientBehaviorReceiptV2, behavior_receipt_from_mapping,
)
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v3 import CollisionShapeV3, ObservedBlockV3
from mc2p.contracts.report import ExecutionStatusV0, FailureCodeV0, FailureV0
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.runtime.backend import BackendStepResultV0
from mc2p.runtime.failure_disposition import FailureDisposition
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.motion_nav.world_model import CellKnowledge
from tests.observation_v2_fixtures import valid_snapshot_v2
from tests.test_action_receipt import receipt_value
from tests.observation_v3_fixtures import valid_snapshot_v3
from tests import test_player_runtime as legacy


class Backend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v2"

    def __init__(self):
        self.actions, self.close_calls, self.sequence = [], 0, 0
        self.receipt_overrides, self.observation_overrides = {}, {}
        self.step_error = self.close_error = None
        self.legacy_result = self.terminated = False
        self.blocking_io_ns_total = 0
        self.blocking_io_increment = 0
        self.after_step = lambda: None

    def reset(self, request):
        self.episode, self.sequence = request.episode_id, 0
        return ResetResultV0(request.request_id, request.episode_id, True,
            observation=valid_snapshot_v2(episode_id=self.episode, sequence_id=0, request_sequence_id=None))

    def step(self, action, deadline):
        if type(action) is not ActionSnapshotV1: raise AssertionError("legacy action reached formal backend")
        self.actions.append(action)
        self.blocking_io_ns_total += self.blocking_io_increment
        if self.step_error: raise self.step_error
        self.sequence += 1
        observation = valid_snapshot_v2(episode_id=self.episode, sequence_id=self.sequence,
                                       request_sequence_id=action.request_sequence_id)
        observation = replace(observation, **self.observation_overrides)
        self.after_step()
        if self.legacy_result: return BackendStepResultV0(observation, 0, False, False)
        from mc2p.runtime.backend_v1 import BackendStepResultV1
        raw = receipt_value(episode_id=action.episode_id, generation_id=self.sequence,
                            request_sequence_id=action.request_sequence_id, world_tick=observation.world_time_ticks.value)
        raw.update(self.receipt_overrides)
        return BackendStepResultV1(observation, 0, self.terminated, False, ClientBehaviorReceiptV2.from_mapping(raw))

    def close(self):
        self.close_calls += 1
        if self.close_error: raise self.close_error


class V3WorldBackend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(self):
        self.sequence = 0
        self.close_calls = 0
        self.first = ObservedBlockV3(
            (1, 63, 0), "minecraft:stone",
            CollisionShapeV3("full_cube"), None, ("first_hit_ray",),
        )
        self.second = ObservedBlockV3(
            (2, 63, 0), "minecraft:dirt",
            CollisionShapeV3("full_cube"), None, ("first_hit_ray",),
        )

    def _observation(self, *, request_sequence_id):
        blocks = (self.first,) if self.sequence == 0 else (self.second,)
        snapshot = valid_snapshot_v3(blocks=blocks, sequence=self.sequence)
        own = replace(
            snapshot.self_state.value,
            movement_tick_id=self.sequence + 1,
        )
        return replace(
            snapshot,
            episode_id="episode-v3-world",
            request_sequence_id=request_sequence_id,
            self_state=replace(snapshot.self_state, value=own),
        )

    def reset(self, request):
        return ResetResultV0(
            request.request_id, request.episode_id, True,
            replace(
                self._observation(request_sequence_id=None),
                episode_id=request.episode_id,
            ),
        )

    def step(self, action, deadline, *, observation_request=None):
        from mc2p.runtime.backend_v1 import BackendStepResultV1
        self.sequence += 1
        observation = replace(
            self._observation(request_sequence_id=action.request_sequence_id),
            episode_id=action.episode_id,
        )
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id,
                generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observation.world_time_ticks.value,
                input_samples=self.sequence + 1,
                leased_input_samples=0,
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": 0,
            "oldest_retained_input_tick": self.sequence + 1,
            "input_applications": [{
                "schema_version": "mc2p.input-application.v1",
                "movement_tick_id": self.sequence + 1,
                "episode_id": action.episode_id,
                "request_sequence_id": action.request_sequence_id,
                "sampled_at_jvm_ns": self.sequence + 1,
                "state": "neutral",
                "forward": 0.0,
                "strafe": 0.0,
                "jump": False,
                "sneak": False,
                "sprint": False,
            }],
        })
        return BackendStepResultV1(
            observation, 0, False, False, receipt,
        )

    def close(self):
        self.close_calls += 1


class RuntimeV1Tests(unittest.TestCase):
    def setUp(self):
        self.clock = [10]
        self.backend, self.trace = Backend(), legacy._RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, clock_ns=lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(legacy._reset_request()).succeeded)

    def intent(self, name="move", **kwargs):
        return ActionIntentV1(name, kwargs.pop("source_id", name), "episode-1", self.runtime.observation.sequence_id,
            ActionPriorityV0.TASK, 10, 900, **kwargs)

    def step(self, deadline=1000):
        return self.runtime.step(legacy._task(), BehaviorProfileV0(), deadline)

    def ordered_source(self):
        self.assertTrue(hasattr(self.runtime, 'register_ordered_source'), 'ordered Runtime API is missing')
        return self.runtime.register_ordered_source('follow')

    def test_runtime_owns_one_navigation_world_adapter_for_all_tasks(self):
        owner = self.runtime.navigation_observation_adapter
        self.assertIs(owner, self.runtime.navigation_observation_adapter)

    def test_v3_runtime_updates_world_without_a_navigation_task(self):
        backend = V3WorldBackend()
        runtime = PlayerRuntimeV1(
            backend, legacy._RecordingTrace(), clock_ns=lambda: 10,
        )
        reset = runtime.reset(ResetRequestV0(
            "reset-v3-world", "episode-v3-world", "test", 1, 1_000,
        ))

        first = runtime.navigation_observation_adapter.latest_frame
        self.assertTrue(reset.succeeded)
        self.assertIsNotNone(first)
        self.assertIs(
            first.world.cell((1, 63, 0)).knowledge, CellKnowledge.BLOCK,
        )

        result = runtime.step(legacy._task(), BehaviorProfileV0(), 1_000)
        second = runtime.navigation_observation_adapter.latest_frame

        self.assertIs(result.report.status, ExecutionStatusV0.RUNNING)
        self.assertEqual(second.body.sequence_id, 1)
        self.assertIs(
            second.world.cell((1, 63, 0)).knowledge, CellKnowledge.BLOCK,
        )
        self.assertIs(
            second.world.cell((2, 63, 0)).knowledge, CellKnowledge.BLOCK,
        )

    def test_async_evidence_failure_keeps_current_control_and_backend(self):
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))

        decision = self.runtime.apply_failure(FailureV0(
            FailureCodeV0.TRACE_IO, "diagnostic writer failed", True,
            "async_trace",
        ))
        result = self.step()

        self.assertIs(
            decision.disposition,
            FailureDisposition.CONTINUE_WITH_INCOMPLETE_EVIDENCE,
        )
        self.assertFalse(decision.evidence_complete)
        self.assertEqual(result.decision.action.movement, MovementV1(forward=1))
        self.assertEqual(self.runtime.state.value, "ready")
        self.assertEqual(self.backend.close_calls, 0)

    def test_task_retry_and_cancel_release_old_inputs_without_closing_runtime(self):
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))
        timeout = FailureV0(
            FailureCodeV0.DEADLINE_EXCEEDED, "task search expired", True,
            "task",
        )

        first = self.runtime.apply_failure(timeout)
        released = self.step()
        second = self.runtime.apply_failure(timeout)
        exhausted = self.runtime.apply_failure(timeout)

        self.assertIs(first.disposition, FailureDisposition.RETRY_TASK_BOUNDED)
        self.assertIs(second.disposition, FailureDisposition.RETRY_TASK_BOUNDED)
        self.assertIs(exhausted.disposition, FailureDisposition.CANCEL_TASK)
        self.assertEqual(released.decision.action.movement, MovementV1())
        self.assertEqual(self.runtime.state.value, "ready")
        self.assertEqual(self.backend.close_calls, 0)

    def test_nonrecoverable_backend_failure_ends_episode(self):
        decision = self.runtime.apply_failure(FailureV0(
            FailureCodeV0.BACKEND_DISCONNECTED, "server ended", False,
            "runtime",
        ))

        self.assertIs(decision.disposition, FailureDisposition.END_EPISODE)
        self.assertEqual(self.runtime.state.value, "ended")
        self.assertEqual(self.backend.close_calls, 1)

    def test_sync_trace_failure_overrides_async_evidence_downgrade(self):
        self.trace.fail_writes = True

        decision = self.runtime.apply_failure(FailureV0(
            FailureCodeV0.TRACE_IO, "diagnostic writer failed", True,
            "async_trace",
        ))

        self.assertIs(decision.disposition, FailureDisposition.RECREATE_RUNTIME)
        self.assertEqual(self.runtime.state.value, "failed")
        self.assertEqual(self.backend.close_calls, 1)

    def test_runtime_attributes_only_backend_reported_blocking_io(self):
        self.backend.blocking_io_increment = 17
        self.step()
        self.assertEqual(self.runtime.backend_blocking_io_ns_total, 17)
        self.assertGreaterEqual(self.runtime.backend_elapsed_ns_total, 0)

    def test_v3_first_receipt_anchors_ledger_when_reset_has_no_movement_tick(self):
        from mc2p.runtime.backend_v1 import BackendStepResultV1
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1

        class FirstTickBackend:
            action_schema_version = "mc2p.action-snapshot.v1"
            observation_schema_version = "mc2p.client_observation.v3"

            def reset(inner, request):
                observation = valid_snapshot_v3(sequence=0)
                own = replace(
                    observation.self_state.value,
                    hurt_animation_ticks=None,
                    movement_tick_id=None,
                )
                observation = replace(
                    observation, episode_id=request.episode_id,
                    request_sequence_id=None,
                    self_state=replace(observation.self_state, value=own),
                )
                return ResetResultV0(
                    request.request_id, request.episode_id, True, observation,
                )

            def step(inner, action, deadline, *, observation_request=None):
                observation = valid_snapshot_v3(sequence=1)
                own = replace(
                    observation.self_state.value, movement_tick_id=51,
                )
                observation = replace(
                    observation, episode_id=action.episode_id,
                    request_sequence_id=action.request_sequence_id,
                    self_state=replace(observation.self_state, value=own),
                )
                receipt = behavior_receipt_from_mapping({
                    **receipt_value(
                        episode_id=action.episode_id,
                        generation_id=1,
                        request_sequence_id=action.request_sequence_id,
                        world_tick=observation.world_time_ticks.value,
                        input_samples=51,
                        leased_input_samples=1,
                    ),
                    "schema_version": "mc2p.client_action_receipt.v3",
                    "dropped_input_samples": 0,
                    "oldest_retained_input_tick": 49,
                    "input_applications": [
                        {
                            "schema_version": "mc2p.input-application.v1",
                            "movement_tick_id": 49,
                            "episode_id": "old-episode",
                            "request_sequence_id": 999,
                            "sampled_at_jvm_ns": 149,
                            "state": "leased",
                            "forward": 1.0, "strafe": 0.0,
                            "jump": False, "sneak": False, "sprint": False,
                        },
                        {
                            "schema_version": "mc2p.input-application.v1",
                            "movement_tick_id": 51,
                            "episode_id": action.episode_id,
                            "request_sequence_id": action.request_sequence_id,
                            "sampled_at_jvm_ns": 151,
                            "state": "neutral",
                            "forward": 0.0, "strafe": 0.0,
                            "jump": False, "sneak": False, "sprint": False,
                        },
                    ],
                })
                return BackendStepResultV1(observation, 0, False, False, receipt)

            def close(inner):
                pass

        trace = legacy._RecordingTrace()
        runtime = PlayerRuntimeV1(FirstTickBackend(), trace, clock_ns=lambda: 10)
        reset = runtime.reset(ResetRequestV0(
            "reset-v3", "episode-v3", "test", 1, 1_000,
        ))
        self.assertTrue(reset.succeeded)

        result = runtime.step(legacy._task(), BehaviorProfileV0(), 1_000)

        self.assertIs(result.report.status, ExecutionStatusV0.RUNNING)
        self.assertIsNone(runtime.input_ledger.sample(49))
        self.assertIsNotNone(runtime.input_ledger.sample(51))
        self.assertEqual(runtime.input_ledger.record(0).applied_ticks, (51,))

    def test_control_frame_combines_skill_proposals_and_steps_backend_once(self):
        from mc2p.contracts.intent_source import ControlFrameProposalV1

        movement = self.ordered_source()
        combat = self.runtime.register_ordered_source("combat")
        move = self.ordered(
            movement, movement=MovementV1(forward=1), look=LookV1(8, -2),
        )
        attack = self.ordered(
            combat, operation=AttackEntityV1("entity-session-7"),
        )

        result = self.runtime.control_frame(
            legacy._task(), BehaviorProfileV0(), 1000,
            proposals=(
                ControlFrameProposalV1((attack,)),
                ControlFrameProposalV1((move,)),
            ),
        )

        self.assertEqual(len(self.backend.actions), 1)
        self.assertEqual(result.decision.action.movement, MovementV1(forward=1))
        self.assertEqual(result.decision.action.look, LookV1(8, -2))
        self.assertEqual(
            result.decision.action.operation,
            AttackEntityV1("entity-session-7"),
        )
        self.assertEqual(result.observation.sequence_id, 1)

    def test_control_frame_rejects_duplicate_proposals_before_backend_step(self):
        from mc2p.contracts.intent_source import ControlFrameProposalV1

        source = self.ordered_source()
        proposal = ControlFrameProposalV1((self.ordered(source),))
        with self.assertRaises(ContractViolation):
            self.runtime.control_frame(
                legacy._task(), BehaviorProfileV0(), 1000,
                proposals=(proposal, proposal),
            )
        self.assertEqual(self.backend.actions, [])

    def ordered(self, source, sequence=1, **kwargs):
        from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
        return OrderedIntentV1(source, sequence, self.intent(ordered_intent_id(source, sequence),
            source_id=source.source_id, **(kwargs or {'look': LookV1(12, 0)})))

    def test_ordered_runtime_uses_normal_actions_receipts_and_source_release(self):
        source = self.ordered_source()
        first = self.ordered(source, movement=MovementV1(forward=1))
        self.runtime.submit_ordered_intent(first)
        self.assertEqual(self.step().decision.action.movement.forward, 1)
        self.assertEqual(self.runtime.cancel_source(source.source_id), (first.intent.intent_id,))
        with self.assertRaises(ContractViolation): self.runtime.submit_ordered_intent(self.ordered(source))
        self.runtime.submit_ordered_intent(self.ordered(source, 2))
        self.assertEqual(self.step().decision.action.look, LookV1(12, 0))
        self.assertEqual(self.step().decision.action.look, LookV1())
        self.runtime.unregister_ordered_source(source)
        new = self.runtime.register_ordered_source('follow')
        self.assertGreater(new.generation, source.generation)
        self.runtime.submit_ordered_intent(self.ordered(new))
        self.assertEqual(self.step().report.status, ExecutionStatusV0.RUNNING)
        rows = {kind: payload for kind, payload in self.trace.records if kind.startswith('ordered_')}
        self.assertEqual(rows['ordered_source_registered']['source'], new)
        self.assertEqual(rows['ordered_source_registered']['label'], 'follow')
        self.assertEqual(rows['ordered_intent']['envelope'].source, new)
        self.assertEqual(rows['ordered_source_unregistered']['source'], source)

    def test_ordered_bad_observation_or_time_does_not_consume_sequence(self):
        source = self.ordered_source()
        old = self.ordered(source)
        self.step()
        invalid = (old, replace(old, intent=replace(old.intent, observation_sequence_id=1, submitted_at_monotonic_ns=20)),
                   replace(old, intent=replace(old.intent, observation_sequence_id=1,
                                              submitted_at_monotonic_ns=1, expires_at_monotonic_ns=10)))
        for envelope in invalid:
            with self.assertRaises(ContractViolation): self.runtime.submit_ordered_intent(envelope)
        self.runtime.submit_ordered_intent(self.ordered(source))
        self.assertEqual(self.step().decision.action.look, LookV1(12, 0))

    def test_ordered_runtime_crosses_4096_without_reset_or_reopening_source(self):
        source = self.ordered_source()
        for sequence in range(1, 5001):
            self.runtime.submit_ordered_intent(self.ordered(source, sequence))
            result = self.step()
            self.assertEqual(result.report.status, ExecutionStatusV0.RUNNING)
            self.assertEqual(result.observation.sequence_id, sequence)
        self.assertEqual(self.runtime.ordered_source_stats,
            {'retained_slots': 1, 'active_sources': 1, 'active_intents': 0})
        kinds = [kind for kind, payload in self.trace.records]
        self.assertEqual(kinds.count('reset'), 1)
        self.assertEqual(kinds.count('ordered_source_registered'), 1)
        self.assertEqual(kinds.count('ordered_intent'), 5000)
        self.assertEqual(kinds.count('step'), 5000)
        self.assertEqual(self.backend.close_calls, 0)
        with self.assertRaises(ContractViolation): self.runtime.submit_ordered_intent(self.ordered(source, 5000))

    def test_ordered_scope_invalidated_by_reset_cancel_and_close(self):
        for phase in ('reset', 'cancel', 'close'):
            with self.subTest(phase=phase):
                self.setUp()
                source = self.ordered_source()
                old = self.ordered(source)
                self.runtime.submit_ordered_intent(old)
                if phase == 'reset':
                    request = replace(legacy._reset_request(), request_id='reset2', episode_id='episode-2')
                    self.assertTrue(self.runtime.reset(request).succeeded)
                    with self.assertRaises(ContractViolation):
                        self.runtime.submit_ordered_intent(replace(old, intent=replace(old.intent, episode_id='episode-2')))
                    self.assertEqual(self.step().decision.action.look, LookV1())
                elif phase == 'cancel':
                    self.runtime.cancel('stop')
                    self.assertEqual(self.step().report.status, ExecutionStatusV0.CANCELLED)
                else:
                    self.runtime.close()
                with self.assertRaises(ContractViolation): self.runtime.submit_ordered_intent(old)
                with self.assertRaises(ContractViolation): self.runtime.unregister_ordered_source(source)

    def test_ordered_trace_errors_and_interrupts_seal_without_unlogged_execution(self):
        for phase in ('register', 'submit', 'unregister'):
            for error_type in (OSError, ContractViolation, KeyboardInterrupt):
                with self.subTest(phase=phase, error=error_type):
                    self.setUp()
                    source = self.ordered_source()
                    def fail(*args, **kwargs):
                        raise error_type('ordered trace failure')
                    self.trace.write = fail
                    operation = {'register': lambda: self.runtime.register_ordered_source('another'),
                        'submit': lambda: self.runtime.submit_ordered_intent(self.ordered(source)),
                        'unregister': lambda: self.runtime.unregister_ordered_source(source)}[phase]
                    with self.assertRaises(error_type): operation()
                    self.assertEqual(self.runtime.state.value, 'failed')
                    self.assertEqual(self.backend.actions, [])
                    self.assertEqual(self.backend.close_calls, 1)
                    with self.assertRaises(ContractViolation): self.runtime.submit_ordered_intent(self.ordered(source))

    def test_runtime_sends_arbitrated_v1_and_records_receipt_without_task_success(self):
        self.backend.receipt_overrides = {"status": "pending_confirmation", "reason": "selected_locally"}
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))
        result = self.step()
        self.assertEqual(result.decision.action.movement.forward, 1)
        self.assertEqual(result.report.status, ExecutionStatusV0.RUNNING)
        self.assertEqual(result.backend_result.receipt.status, "pending_confirmation")
        record = self.trace.records[-1][1]
        self.assertIs(record["backend_result"], result.backend_result)
        self.assertEqual(result.observation.request_sequence_id, 0)

    def test_normal_rejection_supports_new_request_not_automatic_replay(self):
        self.runtime.submit_intent(self.intent("open", operation=OpenInventoryV1()))
        self.backend.receipt_overrides = {"status": "rejected", "reason": "screen_conflict"}
        rejected = self.step()
        self.assertEqual(rejected.report.status, ExecutionStatusV0.FAILED)
        self.assertEqual(rejected.report.failure.source, "client_behavior")
        self.assertEqual(self.runtime.state.value, "ready")
        self.backend.receipt_overrides = {}
        recovered = self.step()
        self.assertIsNone(recovered.decision.action.operation)
        self.assertEqual(recovered.report.status, ExecutionStatusV0.RUNNING)
        self.assertEqual(self.backend.close_calls, 0)

    def test_operation_rejection_keeps_runtime_ready_for_applied_movement(self):
        self.backend.receipt_overrides = {
            "status": "operation_rejected",
            "reason": "entity_target_mismatch",
        }
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))

        result = self.step()

        self.assertEqual(result.decision.action.movement, MovementV1(forward=1))
        self.assertEqual(result.backend_result.receipt.status, "operation_rejected")
        self.assertEqual(result.report.status, ExecutionStatusV0.RUNNING)
        self.assertEqual(result.report.phase, "operation_rejected")
        self.assertIsNone(result.report.failure)
        self.assertEqual(self.runtime.state.value, "ready")

    def test_cancel_source_then_global_cancel_sends_neutral_not_legacy(self):
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))
        self.assertEqual(self.step().decision.action.movement.forward, 1)
        self.assertEqual(self.runtime.cancel_source("move"), ("move",))
        self.assertEqual(self.step().decision.action.movement, MovementV1())
        self.runtime.cancel("player_stop")
        result = self.step()
        self.assertEqual(result.report.status, ExecutionStatusV0.CANCELLED)
        self.assertEqual(result.decision.action, ActionSnapshotV1("episode-1", 2, 2, 1000))
        with self.assertRaises(ContractViolation): self.step()
        self.runtime.close()
        self.assertEqual(len(self.backend.actions), 3)
        self.assertEqual(self.backend.close_calls, 1)

    def test_rejected_cancel_does_not_claim_controls_released(self):
        self.backend.receipt_overrides = {"status": "rejected", "reason": "no_world"}
        self.runtime.cancel("stop")
        result = self.step()
        self.assertNotEqual(result.report.status, ExecutionStatusV0.CANCELLED)
        self.assertEqual(self.runtime.state.value, "failed")
        self.assertEqual(self.backend.close_calls, 1)

    def test_stale_submission_duplicate_episode_and_legacy_backend_are_rejected(self):
        old = self.intent("old", operation=OpenInventoryV1())
        self.step()
        for intent in (old, replace(old, episode_id="wrong"), ActionSnapshotV0.neutral(0),
                       replace(old, observation_sequence_id=1, submitted_at_monotonic_ns=20)):
            with self.assertRaises(ContractViolation): self.runtime.submit_intent(intent)
        with self.assertRaises(ContractViolation): self.runtime.reset(legacy._reset_request())
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        with self.assertRaises(ContractViolation): PlayerRuntimeV1(legacy._FakeBackend(), self.trace)

    def test_bad_receipts_seal_backend_and_cannot_send_again(self):
        for overrides in ({"episode_id": "wrong"}, {"generation_id": 99}, {"request_sequence_id": 7},
                          {"world_tick": 999}, {"on_client_thread": False}, {"action_mouse_callbacks": 1},
                          {"status": "idle"}):
            with self.subTest(overrides=overrides):
                self.setUp()
                self.backend.receipt_overrides = overrides
                result = self.step()
                self.assertEqual(result.report.failure.code, FailureCodeV0.OBSERVATION_INVARIANT)
                self.assertEqual(self.backend.close_calls, 1)
                with self.assertRaises(ContractViolation): self.step()
                self.runtime.close()
                self.assertEqual(len(self.backend.actions), 1)

    def test_noncontinuous_observation_and_legacy_result_fail_closed(self):
        for overrides, old in (({"sequence_id": 3}, False), ({"episode_id": "wrong"}, False), ({}, True)):
            with self.subTest(overrides=overrides, old=old):
                self.setUp()
                self.backend.observation_overrides, self.backend.legacy_result = overrides, old
                self.assertEqual(self.step().report.failure.code, FailureCodeV0.OBSERVATION_INVARIANT)
                self.assertEqual(self.backend.close_calls, 1)

    def test_transport_timeout_closes_without_resending_and_new_runtime_recovers(self):
        self.backend.step_error = TimeoutError("partial send")
        self.assertEqual(self.step().report.status, ExecutionStatusV0.TIMED_OUT)
        self.assertIs(
            self.runtime.last_failure_disposition.disposition,
            FailureDisposition.RECREATE_RUNTIME,
        )
        self.assertEqual(self.backend.close_calls, 1)
        self.runtime.close()
        self.assertEqual(len(self.backend.actions), 1)
        self.setUp()
        self.assertEqual(self.step().report.status, ExecutionStatusV0.RUNNING)

    def test_expired_or_late_deadline_does_not_leave_active_backend(self):
        self.assertEqual(self.step(deadline=10).report.status, ExecutionStatusV0.TIMED_OUT)
        self.assertEqual(self.backend.actions, [])
        self.assertEqual(self.backend.close_calls, 1)
        self.setUp()
        self.backend.after_step = lambda: self.clock.__setitem__(0, 1001)
        self.assertEqual(self.step().report.status, ExecutionStatusV0.TIMED_OUT)
        self.assertEqual(self.backend.close_calls, 1)

    def test_trace_failure_closes_instead_of_continuing_unlogged_actions(self):
        self.trace.fail_writes = True
        result = self.step()
        self.assertEqual(result.report.failure.code, FailureCodeV0.TRACE_IO)
        self.assertEqual(self.backend.close_calls, 1)
        self.assertLessEqual(len(self.backend.actions), 1)

    def test_reset_clears_old_intents_and_cancel_but_not_episode_reuse_guard(self):
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))
        self.runtime.cancel("stop")
        new = replace(legacy._reset_request(), request_id="reset2", episode_id="episode-2")
        self.assertTrue(self.runtime.reset(new).succeeded)
        result = self.step()
        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(result.decision.action.episode_id, "episode-2")
        self.assertEqual(result.report.status, ExecutionStatusV0.RUNNING)

    def test_termination_prevents_next_action_without_claiming_task_success(self):
        self.backend.terminated = True
        self.assertNotEqual(self.step().report.status, ExecutionStatusV0.SUCCEEDED)
        with self.assertRaises(ContractViolation): self.step()
        self.assertEqual(self.backend.close_calls, 1)

    def test_close_once_uses_v1_neutral_and_preserves_cleanup_errors(self):
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))
        self.step()
        self.backend.step_error = OSError("neutral failure")
        self.backend.close_error = OSError("close failure")
        self.runtime.close()
        self.runtime.close()
        self.assertEqual(len(self.backend.actions), 2)
        self.assertEqual(self.backend.actions[-1].movement, MovementV1())
        self.assertEqual(len(self.runtime.cleanup_failures), 2)
        self.assertEqual(self.backend.close_calls, 1)
        self.assertTrue(self.trace.closed)

    def test_forbidden_operations_cannot_override_allowed_movement_or_replay(self):
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))
        self.runtime.submit_intent(replace(self.intent("open", operation=OpenInventoryV1()),
                                           priority=ActionPriorityV0.PLAYER))
        task = replace(legacy._task(), forbidden_actions=("open_inventory",))
        result = self.runtime.step(task, BehaviorProfileV0(), 1000)
        self.assertIsNone(result.decision.action.operation)
        self.assertEqual(result.decision.action.movement.forward, 1)
        self.assertIn(("open", "task_forbidden_open_inventory"), result.decision.suppressed_intents)
        self.assertEqual(result.report.phase, "constraint_filtered")
        self.assertIsNone(self.step().decision.action.operation)

    def test_forbidden_control_groups_release_without_blocking_global_cancel(self):
        self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1), look=LookV1(12, 0)))
        task = replace(legacy._task(), forbidden_actions=("movement", "look", "operation"))
        result = self.runtime.step(task, BehaviorProfileV0(), 1000)
        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(result.decision.action.look, LookV1())
        self.runtime.cancel("stop")
        self.assertEqual(self.runtime.step(task, BehaviorProfileV0(), 1000).report.status, ExecutionStatusV0.CANCELLED)

    def test_unknown_forbidden_action_fails_closed_before_dispatch(self):
        self.runtime.submit_intent(self.intent(operation=OpenInventoryV1()))
        task = replace(legacy._task(), forbidden_actions=("do_not_damage_my_house",))
        result = self.runtime.step(task, BehaviorProfileV0(), 1000)
        self.assertEqual(result.report.status, ExecutionStatusV0.FAILED)
        self.assertEqual(result.report.failure.code, FailureCodeV0.CONTRACT)
        self.assertEqual(self.backend.actions, [])
        self.assertEqual(self.backend.close_calls, 1)

    def test_combat_task_events_use_the_existing_trace_boundary(self):
        for kind, schema in (
            ("combat_assessment", "mc2p.combat-assessment.v1"),
            ("combat_candidates", "mc2p.combat-candidates.v1"),
            ("combat_selection", "mc2p.combat-selection.v1"),
            ("combat_skill", "mc2p.combat-skill.v1"),
        ):
            with self.subTest(kind=kind):
                self.runtime.record_task_event(kind, {
                    "schema_version": schema,
                    "episode_id": "episode-1",
                })
        self.assertEqual(
            [kind for kind, _ in self.trace.records[-4:]],
            ["combat_assessment", "combat_candidates", "combat_selection", "combat_skill"],
        )

    def test_system_interrupt_seals_inflight_step_reset_and_intent_trace(self):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt("stop during I/O")

        for phase in ("step", "reset", "intent", "source_cancel"):
            with self.subTest(phase=phase):
                self.setUp()
                if phase == "step":
                    self.backend.step_error = KeyboardInterrupt("sent without reply")
                    operation = self.step
                elif phase == "reset":
                    self.backend.reset = interrupted
                    operation = lambda: self.runtime.reset(replace(legacy._reset_request(), episode_id="episode-2"))
                else:
                    self.trace.write = interrupted
                    operation = (lambda: self.runtime.submit_intent(self.intent(movement=MovementV1(forward=1)))) if phase == "intent" else (lambda: self.runtime.cancel_source("move"))
                with self.assertRaises(KeyboardInterrupt): operation()
                self.assertEqual(self.runtime.state.value, "failed")
                self.assertEqual(self.backend.close_calls, 1)
                count = len(self.backend.actions)
                with self.assertRaises(ContractViolation): self.step()
                self.runtime.close()
                self.assertEqual(len(self.backend.actions), count)
                self.assertTrue(self.trace.closed)

    def test_close_interrupt_still_closes_trace_and_seals_state(self):
        for phase in ("neutral", "backend_close", "trace_close"):
            with self.subTest(phase=phase):
                self.setUp()
                if phase == "neutral": self.backend.step_error = KeyboardInterrupt("neutral")
                elif phase == "backend_close": self.backend.close_error = KeyboardInterrupt("close")
                else:
                    original = self.trace.close
                    def interrupted_close():
                        original()
                        raise KeyboardInterrupt("trace close")
                    self.trace.close = interrupted_close
                with self.assertRaises(KeyboardInterrupt): self.runtime.close()
                self.assertEqual(self.runtime.state.value, "closed")
                self.assertTrue(self.trace.closed)
                self.assertEqual(self.backend.close_calls, 1)
                self.runtime.close()
                self.assertEqual(self.backend.close_calls, 1)
