from dataclasses import replace
import unittest

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1, LookV1, MovementV1
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.follow import RuleFollower
from mc2p.skills.follow_driver import FollowDriver
from mc2p.skills.local_perception import project_follow_view
from tests.follow_v3_fixtures import follow_snapshot, player_value, observed_block
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from tests.test_action_receipt import receipt_value
from tests.test_follow_perception import request
from tests.test_player_runtime import _RecordingTrace, _task


class FollowBackend:
    """Strict transport-boundary double; it does not simulate Minecraft movement."""
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(self, clock):
        self.clock = clock
        self.sequence = 0
        self.actions = []
        self.requests = []
        self.closed = False
        self.receipt_changes = {}
        self.error = None

    def observation(self):
        floors = [(x, 63, z) for x in range(-1, 2) for z in range(-1, 7)]
        return follow_snapshot(sequence=self.sequence, received=self.clock[0], position=(.5, 64, .5),
            entities=[player_value(relative=(0, 0, 4))],
            blocks=[observed_block(block) for block in floors])

    def reset(self, request):
        return ResetResultV0(request.request_id, request.episode_id, True, self.observation())

    def step(self, action, deadline, *, observation_request=None):
        if type(observation_request) is not ObservationRequestV3: raise AssertionError('missing V3 request')
        if observation_request.field_profile!='navigation_v1': raise AssertionError('not a navigation request')
        self.requests.append(observation_request)
        if type(action) is not ActionSnapshotV1: raise AssertionError("wrong action boundary")
        self.actions.append(action)
        if self.error: raise self.error
        self.sequence += 1
        self.clock[0] += 1_000_000
        obs = replace(self.observation(), request_sequence_id=action.request_sequence_id)
        receipt = receipt_value(episode_id=action.episode_id, request_sequence_id=action.request_sequence_id,
                                generation_id=self.sequence,world_tick=obs.world_time_ticks.value)
        receipt.update(self.receipt_changes)
        return BackendStepResultV1(obs, 0, False, False, ClientBehaviorReceiptV2.from_mapping(receipt))

    def close(self): self.closed = True


class FollowDriverTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100_000_000]
        self.backend = FollowBackend(self.clock)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(ResetRequestV0("r", "episode-1", "test", 1, 1_000_000_000)).succeeded)
        follower = RuleFollower(request(), project_follow_view(self.runtime.observation, self.clock[0], "controller-test"))
        self.driver = FollowDriver(self.runtime, follower, lambda: self.clock[0])
        self.task = replace(_task(), deadline_monotonic_ns=120_100_000_000)
        self.profile = BehaviorProfileV0()

    def tearDown(self): self.runtime.close()

    def tick(self):
        return self.driver.tick(self.task, self.profile, self.clock[0]+1_000_000_000)

    def test_one_combined_short_lease_intent_and_own_source_release(self):
        result = self.tick()
        action = result.runtime_result.decision.action
        self.assertEqual(action.valid_for_ticks, 1)
        self.assertEqual(action.movement, MovementV1(forward=1))
        self.assertIsNone(action.operation)
        self.assertTrue(result.movement_selected)
        intents = [p["intent"] for k,p in self.trace.records if k == "intent"]
        self.assertEqual(len(intents), 1)
        self.assertLessEqual(intents[0].expires_at_monotonic_ns-intents[0].submitted_at_monotonic_ns, 250_000_000)
        stopped = self.driver.stop(self.task, self.profile, self.clock[0]+1_000_000_000)
        self.assertEqual(stopped.runtime_result.decision.action.movement, MovementV1())
        self.assertEqual(self.runtime.state.value, "ready")

    def test_higher_priority_owner_is_reported_and_survives_skill_stop(self):
        self.runtime.submit_intent(ActionIntentV1("human-1", "human", "episode-1", 0, ActionPriorityV0.PLAYER,
            self.clock[0], self.clock[0]+2_000_000_000, movement=MovementV1(strafe=1)))
        result = self.tick()
        self.assertEqual(result.report.state, "preempted")
        self.assertFalse(result.movement_selected)
        stopped = self.driver.stop(self.task, self.profile, self.clock[0]+1_000_000_000)
        self.assertEqual(stopped.runtime_result.decision.action.movement, MovementV1(strafe=1))
        self.assertIn(("movement", "human-1"), stopped.runtime_result.decision.selected_intents)

    def test_look_only_preemption_releases_follow_before_backend_dispatch(self):
        self.runtime.submit_intent(ActionIntentV1("turn", "human", "episode-1", 0, ActionPriorityV0.PLAYER,
            self.clock[0], self.clock[0]+2_000_000_000, look=LookV1(90, 0)))
        result = self.tick()
        self.assertEqual(result.decision.movement, MovementV1(forward=1))
        self.assertEqual(self.backend.actions[-1].movement, MovementV1())
        self.assertEqual(self.backend.actions[-1].look, LookV1(90, 0))
        self.assertEqual(result.report.state, "preempted")

    def test_fast_poll_does_not_send_catchup_actions_or_reuse_ids(self):
        self.tick()
        self.assertIsNone(self.tick())
        self.assertEqual(len(self.backend.actions), 1)
        self.clock[0] += 100_000_000
        self.tick()
        intents = [p["intent"] for k,p in self.trace.records if k == "intent"]
        self.assertEqual(len(intents), 2)
        self.assertNotEqual(intents[0].intent_id, intents[1].intent_id)
        self.assertEqual(len(self.backend.actions), 2)

    def test_expired_task_uses_separate_neutral_cleanup_not_expired_step(self):
        self.tick()
        expired = replace(self.task, deadline_monotonic_ns=self.clock[0])
        result = self.driver.tick(expired, self.profile, self.clock[0])
        self.assertEqual(result.report.state, "timed_out")
        self.assertEqual(result.runtime_result.decision.action.movement, MovementV1())
        self.assertEqual(self.runtime.state.value, "ready")
        self.assertEqual(expired.deadline_monotonic_ns, 101_000_000)

    def test_new_skill_after_stop_uses_same_ready_runtime_with_new_intents(self):
        self.tick()
        self.driver.stop(self.task, self.profile, self.clock[0]+1_000_000_000)
        fresh = request(skill_id="follow-2", started_at_ns=self.clock[0], deadline_ns=self.clock[0]+120_000_000_000)
        follower = RuleFollower(fresh, project_follow_view(self.runtime.observation, self.clock[0], "controller-test"))
        replacement = FollowDriver(self.runtime, follower, lambda: self.clock[0])
        result = replacement.tick(self.task, self.profile, self.clock[0]+1_000_000_000)
        self.assertEqual(result.runtime_result.decision.action.movement, MovementV1(forward=1))
        self.assertTrue(result.movement_selected)

    def test_mismatched_receipt_or_transport_uncertainty_never_resends(self):
        for changes, error in (({"generation_id": 9}, None), ({}, TimeoutError("partial send")),
                               ({"status": "pending_confirmation"}, None)):
            with self.subTest(changes=changes, error=error):
                self.backend.receipt_changes, self.backend.error = changes, error
                result = self.tick()
                self.assertEqual(result.report.state, "failed")
                self.assertTrue(result.report.terminal)
                sent = len(self.backend.actions)
                if self.runtime.state.value != "ready":
                    self.assertTrue(self.backend.closed)
                    with self.assertRaises(ContractViolation): self.tick()
                    self.assertEqual(len(self.backend.actions), sent)
                self.runtime.close()
                self.setUp()


if __name__ == "__main__": unittest.main()
