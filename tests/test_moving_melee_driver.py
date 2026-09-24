"""C1-B composes moving pursuit and the shared one-strike driver."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import AttackEntityV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.player_runtime_v1 import RuntimeStateV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.moving_melee_driver import MAX_REAPPROACHES, MovingMeleeDriver
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.point_goal_policy import PointGoalPolicy
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
from tests.test_navigation_joint_policy import TestCapabilities
from tests.test_player_runtime import _RecordingTrace


class MovingMeleeDriverTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100_000_000]
        self.backend = MeleeBackend(self.clock)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset", "episode-1", "test", 1, 5_000_000_000)
        ).succeeded)
        self.profile = BehaviorProfileV0()
        self.target = CombatTargetV1(
            "combat-task", "combat-goal", 1, "episode-1", TRACK,
        )

    def tearDown(self):
        self.runtime.close()

    def driver(self):
        return MovingMeleeDriver(
            self.runtime, NavigationState("scope"),
            PointGoalPolicy("D", control_capabilities=TestCapabilities()),
            clock_ns=lambda: self.clock[0],
        )

    def tick(self, driver):
        return driver.tick(self.profile, self.clock[0] + 2_000_000_000)

    def test_death_before_attack_completes_without_attack(self):
        self.backend.dead = True
        self.backend.health = 0.0
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        for _ in range(6):
            if driver.report.terminal:
                break
            self.tick(driver)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "target_dead"))
        self.assertEqual(driver.report.attack_submissions, 0)

    def test_two_hits_with_reapproach_then_explicit_death(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while driver.report.confirmed_hits < 1:
            self.tick(driver)

        self.backend.hurt = 0
        self.backend.distance = 5.0
        for _ in range(5):
            self.tick(driver)
            if driver.approach_driver is not None:
                break
        self.assertIsNotNone(driver.approach_driver)
        self.assertIsNone(driver.strike_driver)

        self.backend.distance = 2.5
        driver.approach_driver.state = "success"
        self.tick(driver)
        while driver.report.confirmed_hits < 2:
            self.tick(driver)
        self.assertEqual(driver.report.attack_submissions, 2)

        self.backend.dead = True
        self.backend.health = 0.0
        for _ in range(5):
            if driver.report.terminal:
                break
            self.tick(driver)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "target_dead"))

    def test_health_decline_and_unload_are_not_death(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.health = 1.0
        self.tick(driver)
        self.assertFalse(driver.report.terminal)
        self.backend.query_track = "missing"
        self.backend.track = "other"
        for _ in range(3):
            if driver.report.terminal:
                break
            self.tick(driver)
        self.assertNotEqual(driver.report.reason, "target_dead")

    def test_hidden_engaged_target_refreshes_before_starting_another_strike(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while driver.report.confirmed_hits < 1:
            self.tick(driver)
        self.backend.hurt = 0
        self.backend.visible = False
        self.tick(driver)
        self.assertGreaterEqual(driver.report.engagement_position_uses, 1)
        self.assertIsNotNone(driver._reacquire_source)
        self.assertIsNone(driver.strike_driver)
        self.backend.visible = True
        self.tick(driver)
        self.assertIsNone(driver._reacquire_source)

    def test_navigation_source_is_released_before_strike_source(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-far", "episode-1", "test", 1, 5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        nav_source = driver.approach_driver.source.source_id
        self.backend.distance = 2.5
        driver.approach_driver.state = "success"
        release = self.tick(driver)
        self.assertFalse(any(value.startswith(nav_source + "/")
                             for _, value in release.decision.selected_intents))
        self.assertIsNone(driver.approach_driver)
        self.tick(driver)
        self.assertIsNotNone(driver.strike_driver)

    def test_completed_approach_is_released_before_moving_goal_refresh(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-completed-approach", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        driver.approach_driver.state = "success"
        self.backend.distance = 6.0

        changed = replace(driver._moving_goal, changed=True, reason="target_moved")
        with patch("mc2p.skills.moving_melee_driver.decide_moving_goal",
                   return_value=changed):
            result = self.tick(driver)

        self.assertIsNotNone(result)
        self.assertIsNone(driver.approach_driver)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("recovering_cadence", "standoff_reached_refresh_required"))

    def test_cancel_while_pursuing_releases_navigation(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-cancel", "episode-1", "test", 1, 5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        result = driver.cancel(self.profile, "user_cancelled")
        self.assertIsNotNone(result)
        self.assertEqual(driver.report.state, "cancelled")

    def test_damage_knockback_releases_approach_then_recovers_from_latest_body(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-knockback", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        old_approach = driver.approach_driver

        self.backend.own_hurt = 10
        self.backend.own_health = 18.0
        self.backend.own_ground = False
        self.backend.own_velocity = (.10, .20, 0.0)
        self.tick(driver)
        self.backend.own_ground = True
        self.backend.own_velocity = (.08, 0.0, 0.0)
        self.tick(driver)

        self.assertIsNone(driver.approach_driver)
        self.assertIsNotNone(driver.recovery_driver)
        self.assertEqual(driver.report.state, "recovering_external_motion")
        self.assertEqual(driver.report.external_motion_events, 1)
        self.assertEqual(old_approach.state, "stopped")

        self.backend.own_velocity = (.02, 0.0, 0.0)
        self.tick(driver)
        self.tick(driver)
        self.tick(driver)

        self.assertIsNone(driver.recovery_driver)
        self.assertEqual(driver.report.external_recoveries_completed, 1)
        self.assertIsNotNone(driver.approach_driver)
        self.assertEqual(driver.report.state, "pursuing")

    def test_damage_observed_inside_approach_tick_starts_recovery_immediately(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-child-damage", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        approach = driver.approach_driver
        self.assertIsNotNone(approach)
        original_tick = approach.tick

        def damaged_tick(profile, deadline):
            self.backend.own_hurt = 10
            self.backend.own_health = 18.0
            self.backend.own_ground = False
            self.backend.own_velocity = (.10, .20, 0.0)
            return original_tick(profile, deadline)

        approach.tick = damaged_tick
        self.tick(driver)

        self.assertEqual(driver.report.external_motion_events, 1)
        self.assertEqual(driver.report.state, "recovering_external_motion")
        self.assertIsNone(driver.approach_driver)
        self.assertIsNotNone(driver.recovery_driver)
        self.assertEqual(
            driver.last_external_motion_observation.sequence_id,
            driver.recovery_driver._event.observation_sequence_id,
        )

    def test_damage_observed_inside_strike_tick_starts_recovery_before_adoption(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        strike = driver.strike_driver
        self.assertIsNotNone(strike)
        original_tick = strike.tick

        def damaged_tick(profile, deadline):
            self.backend.own_hurt = 10
            self.backend.own_health = 18.0
            self.backend.own_ground = False
            self.backend.own_velocity = (.10, .20, 0.0)
            return original_tick(profile, deadline)

        strike.tick = damaged_tick
        self.tick(driver)

        self.assertEqual(driver.report.external_motion_events, 1)
        self.assertEqual(driver.report.state, "recovering_external_motion")
        self.assertIsNotNone(driver.recovery_driver)

        self.tick(driver)

        self.assertTrue(driver._engagement.active)
        self.assertNotEqual(driver._engagement.revocation_reason, "observation_gap")

    def test_target_revision_during_recovery_is_kept_without_abandoning_body(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-revision-recovery", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.own_hurt = 10
        self.backend.own_health = 18.0
        self.backend.own_ground = False
        self.tick(driver)
        self.backend.own_ground = True
        self.tick(driver)
        recovery = driver.recovery_driver

        revised = replace(self.target, revision=2)
        driver.replace_target(revised, self.clock[0])

        self.assertIs(driver.recovery_driver, recovery)
        self.assertEqual(driver.report.state, "recovering_external_motion")
        self.assertEqual(driver.report.target_revision, 2)

    def test_world_change_during_recovery_fails_closed(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.own_hurt = 10
        self.backend.own_health = 18.0
        self.backend.own_ground = False
        self.tick(driver)
        self.tick(driver)
        self.runtime._observation = replace(
            self.runtime.observation, episode_id="episode-2",
        )

        result = self.tick(driver)

        self.assertIsNone(result)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("failed", "world_session_changed"))
        self.assertIs(self.runtime.state, RuntimeStateV1.FAILED)

    def test_separate_recoveries_share_one_task_event_budget(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-event-budget", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.own_hurt = 10
        self.backend.own_health = 18.0
        self.backend.own_ground = False
        self.tick(driver)
        self.tick(driver)
        first_controller = driver.recovery_driver.controller
        self.backend.own_ground = True
        self.backend.own_velocity = (.02, 0.0, 0.0)
        for _ in range(4):
            if driver.recovery_driver is None:
                break
            self.tick(driver)
        self.assertIsNone(driver.recovery_driver)

        self.backend.own_hurt = 0
        self.tick(driver)
        self.backend.own_hurt = 10
        self.backend.own_health = 16.0
        self.backend.own_ground = False
        self.tick(driver)
        self.tick(driver)

        self.assertIs(driver.recovery_driver.controller, first_controller)
        self.assertEqual(driver.recovery_driver.report.events, 2)

    def test_transient_approach_failure_retries_with_latest_target_fact(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-retry", "episode-1", "test", 1, 5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        first = driver.approach_driver
        original_tick = first.tick
        def fail_once(profile, deadline):
            result = original_tick(profile, deadline)
            first.state, first.reason = "blocked", "no_admissible_candidate"
            return result
        first.tick = fail_once
        self.tick(driver)
        self.assertFalse(driver.report.terminal)
        self.assertIsNot(driver.approach_driver, first)
        self.assertEqual(driver.report.reapproaches, 2)

    def test_combat_step_with_selected_navigation_movement_fails_closed(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        original = driver.strike_driver.tick

        def contaminated(profile, deadline):
            source = self.runtime.register_ordered_source("rogue-navigation")
            observation = self.runtime.observation
            from mc2p.contracts.action import ActionPriorityV0
            from mc2p.contracts.action_v1 import ActionIntentV1
            from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
            intent = ActionIntentV1(
                ordered_intent_id(source, 1), source.source_id, source.episode_id,
                observation.sequence_id, ActionPriorityV0.TASK,
                self.clock[0], self.clock[0] + 250_000_000,
                movement=MovementV1(forward=1),
            )
            self.runtime.submit_ordered_intent(OrderedIntentV1(source, 1, intent))
            return original(profile, deadline)

        driver.strike_driver.tick = contaminated
        self.tick(driver)
        self.assertEqual(driver.report.state, "failed")
        self.assertEqual(driver.report.reason, "simultaneous_navigation_and_combat_control")

    def test_target_revision_before_attack_rebinds_without_using_old_request(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        revised = CombatTargetV1(
            self.target.task_id, self.target.goal_id, 2,
            self.target.episode_id, self.target.track_id,
        )
        driver.replace_target(revised, self.clock[0])
        self.assertEqual(driver.report.target_revision, 2)
        for _ in range(8):
            if driver.report.confirmed_hits:
                break
            self.tick(driver)
        self.assertEqual(driver.report.confirmed_hits, 1)

    def test_world_session_change_fails_without_attack(self):
        from dataclasses import replace
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.runtime._observation = replace(
            self.runtime.observation, episode_id="episode-2",
        )
        self.tick(driver)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("failed", "world_session_changed"))
        self.assertEqual(driver.report.attack_submissions, 0)

    def test_reapproach_attempts_have_a_frozen_limit(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        driver._reapproaches = MAX_REAPPROACHES
        self.backend.distance = 5.0
        for _ in range(5):
            if driver.report.terminal:
                break
            self.tick(driver)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("failed", "reapproach_attempts_exhausted"))

    def test_hit_confirmation_timeout_is_not_retried(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.confirm_hit = False
        while driver.report.attack_submissions == 0:
            self.tick(driver)
        for _ in range(30):
            if driver.report.terminal:
                break
            self.tick(driver)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("failed", "confirmation_deadline_reached"))
        self.assertEqual(driver.report.attack_submissions, 1)


if __name__ == "__main__":
    unittest.main()
