from dataclasses import replace
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.external_motion import ExternalMotionEventV1, ExternalMotionSource
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.external_motion_recovery_driver import ExternalMotionRecoveryDriver
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.point_goal_driver import PointGoalDriver
from mc2p.skills.point_goal_policy import PointGoalPolicy
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.test_follow_driver import FollowBackend
from tests.test_navigation_joint_policy import TestCapabilities
from tests.test_player_runtime import _RecordingTrace


def damage_event(*, generation=1, tick=30):
    return ExternalMotionEventV1(
        event_id=f"damage/episode-1/{tick}/{generation}",
        generation=generation,
        episode_id="episode-1",
        observation_sequence_id=0,
        movement_tick_id=tick,
        source=ExternalMotionSource.DAMAGE_KNOCKBACK,
        health_delta_points=-2.0,
        previous_position=Vec3V0(.5, 64.0, .5),
        position=Vec3V0(.6, 64.1, .5),
        previous_velocity=Vec3V0(0.0, 0.0, 0.0),
        velocity=Vec3V0(.1, .2, 0.0),
        was_on_ground=True,
        is_on_ground=False,
    )


class RecoveryBackend(FollowBackend):
    def __init__(self, clock):
        super().__init__(clock)
        self.frames = (
            (31, False, .10),
            (32, True, .08),
            (33, True, .02),
            (34, True, .02),
            (35, True, 0.0),
        )

    def observation(self):
        tick, grounded, speed = self.frames[min(self.sequence, len(self.frames) - 1)]
        blocks = tuple(observed_block((x, 63, z))
                       for x in range(-2, 3) for z in range(-2, 3))
        return follow_snapshot(
            sequence=self.sequence,
            received=self.clock[0],
            position=(.5, 64, .5),
            entities=[],
            blocks=blocks,
            episode="episode-1",
            self_changes={
                "movement_tick_id": tick,
                "hurt_animation_ticks": 0,
                "is_on_ground": grounded,
                "velocity": {"x": speed, "y": 0.0, "z": 0.0},
            },
        )


class ExternalMotionRecoveryDriverTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100_000_000]
        self.backend = RecoveryBackend(self.clock)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        reset = ResetRequestV0("reset", "episode-1", "test", 1, 1_000_000_000)
        self.assertTrue(self.runtime.reset(reset).succeeded)
        self.driver = ExternalMotionRecoveryDriver(
            self.runtime,
            NavigationState("scope"),
            PointGoalPolicy("D", control_capabilities=TestCapabilities()),
            task_deadline_ns=10_000_000_000,
            clock_ns=lambda: self.clock[0],
        )
        self.profile = BehaviorProfileV0()

    def tearDown(self):
        self.runtime.close()

    def tick(self):
        return self.driver.tick(self.profile, self.clock[0] + 3_000_000_000)

    def test_air_source_hands_off_to_point_goal_without_overlap(self):
        self.driver.start(damage_event())

        result = self.tick()

        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertIs(type(self.driver.hold_driver), PointGoalDriver)
        records = [kind for kind, _ in self.trace.records]
        first_unregister = records.index("ordered_source_unregistered")
        second_register = records.index("ordered_source_registered", first_unregister + 1)
        self.assertLess(first_unregister, second_register)
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 1)

    def test_two_stable_ticks_release_hold_on_the_next_driver_tick(self):
        self.driver.start(damage_event())
        self.tick()
        self.clock[0] += 50_000_000
        self.tick()
        self.clock[0] += 50_000_000

        settled = self.tick()

        self.assertEqual(settled.decision.action.movement, MovementV1())
        self.assertEqual(self.driver.report.state, "releasing")
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 1)
        settled_sequence = settled.observation.sequence_id
        self.clock[0] += 50_000_000
        result = self.tick()

        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(result.observation.sequence_id, settled_sequence + 1)
        self.assertEqual(self.driver.report.state, "complete")
        self.assertEqual(self.driver.report.reason, "stable_reanchored")
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 0)

    def test_repeat_event_updates_same_driver(self):
        self.driver.start(damage_event())
        self.assertFalse(self.driver.observe_event(damage_event()))
        self.assertTrue(self.driver.observe_event(damage_event(generation=2, tick=31)))
        self.assertEqual(self.driver.report.active_generation, 2)
        self.assertEqual(self.driver.report.events, 2)

    def test_ground_hold_hands_back_to_air_after_a_second_knockback(self):
        self.backend.frames = (
            (31, False, .10),
            (32, True, .08),
            (33, False, .10),
            (34, False, .10),
            (35, True, .08),
            (36, True, .02),
            (37, True, .02),
            (38, True, 0.0),
        )
        self.driver.start(damage_event())

        self.tick()  # airborne source -> grounded hold
        self.clock[0] += 50_000_000
        self.tick()  # hold observes a second airborne interval
        self.assertEqual(self.driver.report.state, "returning_air")
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 1)

        self.clock[0] += 50_000_000
        handoff = self.tick()
        self.assertEqual(handoff.decision.action.movement, MovementV1())
        self.assertEqual(self.driver.report.state, "running")
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 1)

        for _ in range(5):
            self.clock[0] += 50_000_000
            self.tick()
            if self.driver.report.state == "complete":
                break

        self.assertEqual(self.driver.report.state, "complete")
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 0)

    def test_owner_loss_cancels_without_resuming(self):
        self.driver.start(damage_event())
        result = self.driver.tick(self.profile, self.clock[0])
        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(self.driver.report.state, "cancelled")
        with self.assertRaisesRegex(ContractViolation, "closed"):
            self.tick()

    def test_uncertain_receipt_seals_runtime(self):
        self.driver.start(damage_event())
        self.backend.receipt_changes = {"generation_id": 99}
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertIs(self.runtime.state, RuntimeStateV1.FAILED)


if __name__ == "__main__":
    unittest.main()
