from __future__ import annotations

from dataclasses import replace
import math
import unittest

from mc2p.contracts.action_v1 import ActionSnapshotV1, AttackEntityV1
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import TargetingStateV3
from mc2p.contracts.observation_v2 import ObservationGroupV2
from mc2p.contracts.observation_v3 import TrackedEntityStateV3
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from tests.follow_fixtures import player_value
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.test_action_receipt import receipt_value
from tests.navigation_session_fixtures import FakeNavigationSession
from tests.test_player_runtime import _RecordingTrace


TRACK = "entity-zombie-1"


class MeleeBackend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(self, clock, *, distance=2.5):
        self.clock = clock
        self.sequence = 0
        self.distance = distance
        self.track = TRACK
        self.profile = "navigation_v1"
        self.hurt = 0
        self.targeted = True
        self.reject_reason = None
        self.operation_reject_reason = None
        self.attack_error = None
        self.query_track = None
        self.query_air = ()
        self.dead = False
        self.health = 20.0
        self.confirm_hit = True
        self.visible = True
        self.own_hurt = 0
        self.own_health = 20.0
        self.own_ground = True
        self.own_velocity = (0.0, 0.0, 0.0)
        self.own_pose = "standing"
        self.own_eye_height = 1.62
        self.actions = []
        self.closed = False

    def observation(self):
        entity = player_value(self.track, (0, 0, self.distance), entity_type="minecraft:zombie")
        entity["hurt_animation_ticks"] = self.hurt
        floors = tuple(observed_block((x, 63, z)) for x in range(-2, 3) for z in range(-2, 8))
        observation = follow_snapshot(
            sequence=self.sequence,
            received=self.clock[0],
            position=(.5, 64, .5),
            entities=[entity] if self.visible else [],
            blocks=floors,
            profile=self.profile,
            self_changes={
                "attack_cooldown": 1.0,
                "hurt_animation_ticks": self.own_hurt,
                "movement_tick_id": self.sequence + 1,
                "health_points": self.own_health,
                "is_on_ground": self.own_ground,
                "velocity": dict(zip(("x", "y", "z"), self.own_velocity)),
                "pose": self.own_pose,
                "eye_height_blocks": self.own_eye_height,
            },
        )
        if self.profile == "interaction_v1":
            target = (TargetingStateV3(
                "entity", None, self.track, None,
                Vec3V0(.5, 65, .5 + self.distance), self.distance,
            ) if self.targeted else TargetingStateV3("miss", None, None, None, None, None))
            observation = replace(observation, targeting=replace(observation.targeting, value=target))
        if self.query_track == self.track:
            tick = observation.world_time_ticks.value
            tracked = TrackedEntityStateV3(
                self.track, "minecraft:zombie", Vec3V0(0, 0, self.distance),
                Vec3V0(0, 0, 0), 0, 0, Vec3V0(.6, 1.95, .6),
                "standing", True, True, self.dead, self.health, 20.0,
            )
            observation = replace(observation, tracked_entity=ObservationGroupV2.valid(
                tick, "client_registered_entity", tracked,
            ))
        return observation

    def reset(self, request):
        return ResetResultV0(request.request_id, request.episode_id, True, self.observation())

    def step(self, action, deadline, *, observation_request=None):
        if type(action) is not ActionSnapshotV1:
            raise AssertionError("wrong action")
        self.actions.append(action)
        self.profile = observation_request.field_profile
        self.query_track = observation_request.entity_track_id
        self.query_air = observation_request.air_positions
        status, reason = "executed", "neutral"
        if type(action.operation) is AttackEntityV1:
            if self.attack_error is not None:
                raise self.attack_error
            if self.operation_reject_reason is not None:
                status, reason = "operation_rejected", self.operation_reject_reason
            elif self.reject_reason is not None:
                status, reason = "rejected", self.reject_reason
            else:
                status, reason = "pending_confirmation", "entity_attack_dispatched"
                if self.confirm_hit:
                    self.hurt = 10
        self.sequence += 1
        self.clock[0] += 50_000_000
        observation = replace(self.observation(), request_sequence_id=action.request_sequence_id)
        receipt = receipt_value(
            episode_id=action.episode_id,
            request_sequence_id=action.request_sequence_id,
            generation_id=self.sequence,
            world_tick=observation.world_time_ticks.value,
            status=status,
            reason=reason,
        )
        return BackendStepResultV1(
            observation, 0, False, False,
            ClientBehaviorReceiptV2.from_mapping(receipt),
        )

    def close(self):
        self.closed = True


class FixedMeleeDriverTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100_000_000]
        self.backend = MeleeBackend(self.clock)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        reset = ResetRequestV0("reset", "episode-1", "test", 1, 2_000_000_000)
        self.assertTrue(self.runtime.reset(reset).succeeded)
        self.profile = BehaviorProfileV0()

    def tearDown(self):
        self.runtime.close()

    def driver(self):
        from mc2p.skills.fixed_melee_driver import FixedMeleeDriver
        return FixedMeleeDriver(
            self.runtime,
            FakeNavigationSession(),
            clock_ns=lambda: self.clock[0],
        )

    def target(self, revision=1, track=TRACK):
        return CombatTargetV1("combat-task", "combat-goal", revision, "episode-1", track)

    def tick_until_terminal(self, driver, limit=12):
        for _ in range(limit):
            if driver.report.terminal:
                return
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
            if driver.report.terminal:
                return
        self.fail("fixed melee driver did not terminate")

    def test_success_submits_one_identity_bound_attack_and_confirms_later_hurt(self):
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        self.tick_until_terminal(driver)
        attacks = [action.operation for action in self.backend.actions
                   if type(action.operation) is AttackEntityV1]
        self.assertEqual(attacks, [AttackEntityV1(TRACK)])
        self.assertEqual(driver.report.state, "complete")
        self.assertEqual(driver.report.reason, "hit_confirmed")
        self.assertTrue(driver.report.hit_observed)
        self.assertEqual(driver.report.target_revision, 1)
        kinds = [kind for kind, _ in self.trace.records]
        for expected in ("combat_assessment", "combat_candidates", "combat_selection", "combat_skill"):
            self.assertIn(expected, kinds)

    def test_far_target_delegates_to_navigation_session(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-far", "episode-1", "test", 1, 2_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        self.assertIs(type(driver.approach_driver), RuntimeNavigationDriver)
        goal = driver.approach_driver._goal
        self.assertAlmostEqual((goal.region.min_z + goal.region.max_z) / 2, 3.3)
        self.assertEqual(driver.report.state, "approaching")

    def test_cancel_keeps_delayed_navigation_owner_until_safe_stop_finishes(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-delayed-cancel", "episode-1", "test", 1,
                           2_000_000_000)
        ).succeeded)
        from mc2p.skills.fixed_melee_driver import FixedMeleeDriver
        driver = FixedMeleeDriver(
            self.runtime, FakeNavigationSession(cancel_steps=2),
            clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target(), self.clock[0])

        driver.cancel(self.profile, "user_cancelled")

        self.assertEqual(driver.report.state, "cancelling")
        self.assertIsNotNone(driver.approach_driver)
        driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertEqual(driver.report.state, "cancelled")
        self.assertIsNone(driver.approach_driver)

    def test_failed_approach_releases_ordered_source_before_next_trial(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-far-failure", "episode-1", "test", 1, 2_000_000_000)
        ).succeeded)
        first = self.driver()
        first.start(self.target(), self.clock[0])
        approach = first.approach_driver
        self.assertIsNotNone(approach)
        original_tick = approach.tick

        def blocked_tick(profile, deadline):
            result = original_tick(profile, deadline)
            approach.state = "blocked"
            approach.reason = "test_blocked"
            return result

        approach.tick = blocked_tick
        first.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertTrue(first.report.terminal)
        second = self.driver()
        second.start(self.target(), self.clock[0])
        self.assertEqual(second.report.state, "approaching")

    def test_target_revision_before_submit_invalidates_old_attack(self):
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        driver.replace_target(self.target(2, "entity-zombie-2"), self.clock[0])
        driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertFalse(any(type(action.operation) is AttackEntityV1 for action in self.backend.actions))
        self.assertEqual(driver.report.target_revision, 2)

    def test_client_rejection_is_not_retried(self):
        self.backend.reject_reason = "wrong_entity_target"
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        self.tick_until_terminal(driver)
        self.assertEqual(driver.report.state, "failed")
        self.assertEqual(driver.report.reason, "client_rejected/wrong_entity_target")
        self.assertEqual(sum(type(action.operation) is AttackEntityV1
                             for action in self.backend.actions), 1)

    def test_runtime_attack_failure_keeps_first_failure_instead_of_writing_after_seal(self):
        self.backend.attack_error = RuntimeError("attack transport failed")
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        result = None
        for _ in range(12):
            result = driver.tick(self.profile, self.clock[0] + 2_000_000_000)
            if result is not None and result.report.failure is not None:
                break
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.report.failure)
        self.assertIn("attack transport failed", result.report.failure.message)
        self.assertEqual(driver.report.state, "failed")
        self.assertEqual(driver.report.reason, "runtime_failure")
        kinds = [kind for kind, _ in self.trace.records]
        self.assertEqual(kinds.count("step_failure"), 1)

    def test_cancel_after_submit_never_resends_and_retains_hit_observation(self):
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        driver.cancel(self.profile, "player_cancelled")
        self.tick_until_terminal(driver)
        self.assertEqual(driver.report.state, "cancelled")
        self.assertEqual(driver.report.reason, "cancelled_after_submit")
        self.assertTrue(driver.report.hit_observed)
        self.assertEqual(sum(type(action.operation) is AttackEntityV1
                             for action in self.backend.actions), 1)

    def test_external_motion_before_submit_releases_source_and_requires_approach(self):
        driver = MeleeStrikeDriver(
            self.runtime, clock_ns=lambda: self.clock[0],
            task_deadline_ns=self.clock[0] + 2_000_000_000,
        )
        driver.start(self.target(), self.clock[0])

        result = driver.interrupt_for_external_motion(
            self.profile, "damage_knockback"
        )

        self.assertEqual(result, "retry_after_recovery")
        self.assertEqual(driver.report.state, "needs_approach")
        self.assertEqual(driver.report.attack_submissions, 0)
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 0)

    def test_external_motion_after_submit_observes_hit_without_resend(self):
        driver = MeleeStrikeDriver(
            self.runtime, clock_ns=lambda: self.clock[0],
            task_deadline_ns=self.clock[0] + 2_000_000_000,
        )
        driver.start(self.target(), self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        attacks = sum(type(action.operation) is AttackEntityV1
                      for action in self.backend.actions)

        result = driver.interrupt_for_external_motion(
            self.profile, "damage_knockback"
        )
        driver.observe_after_external_motion(self.runtime.observation)

        self.assertEqual(result, "observe_submitted_attack")
        self.assertEqual(driver.report.reason, "hit_confirmed")
        self.assertEqual(driver.report.attack_submissions, 1)
        self.assertEqual(
            sum(type(action.operation) is AttackEntityV1 for action in self.backend.actions),
            attacks,
        )

    def test_existing_hurt_animation_waits_then_attacks_after_it_clears(self):
        self.backend.hurt = 4
        driver = self.driver()
        driver.start(self.target(), self.clock[0])

        for _ in range(3):
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertFalse(any(
            type(action.operation) is AttackEntityV1
            for action in self.backend.actions
        ))

        self.backend.hurt = 0
        self.tick_until_terminal(driver)
        self.assertEqual(
            sum(type(action.operation) is AttackEntityV1 for action in self.backend.actions),
            1,
        )
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "hit_confirmed"))

    def test_health_loss_confirms_attack_when_hurt_timer_does_not_restart(self):
        self.backend.confirm_hit = False
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.backend.health = 18.0
        for _ in range(2):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "hit_confirmed"))

    def test_unconfirmed_aim_is_bounded_instead_of_spinning_until_runtime_deadline(self):
        self.backend.targeted = False
        driver = self.driver()
        driver.start(self.target(), self.clock[0])
        for _ in range(30):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertEqual(driver.report.state, "failed")
        self.assertEqual(driver.report.reason, "aim_not_confirmed")
        self.assertFalse(any(type(action.operation) is AttackEntityV1 for action in self.backend.actions))

    def test_crouching_aim_uses_observed_eye_height(self):
        self.backend.targeted = False
        self.backend.own_pose = "crouching"
        self.backend.own_eye_height = 1.27
        driver = self.driver()
        driver.start(self.target(), self.clock[0])

        driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        expected_pitch = math.degrees(math.atan2(1.27 - .9, 2.5))
        self.assertAlmostEqual(
            self.backend.actions[-1].look.pitch_delta_degrees,
            expected_pitch,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
