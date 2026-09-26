"""C1-B composes moving pursuit and the shared one-strike driver."""
from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import AttackEntityV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.player_runtime_v1 import RuntimeStateV1
from mc2p.motion_nav.motion_residual import MotionResidualResult, MotionResidualStatus
from mc2p.motion_nav.navigation_session import NavigationSessionState
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.engagement_memory import EngagementEventKind
from mc2p.skills.attack_evidence import AttackTaskOutcome
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver, MeleeStrikeOutcome
from mc2p.skills.moving_melee_driver import MAX_REAPPROACHES, MovingMeleeDriver
from tests.navigation_session_fixtures import FakeNavigationSession
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
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
            self.runtime, FakeNavigationSession(),
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

    def test_observation_gap_keeps_the_active_approach_until_reanchored(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-gap", "episode-1", "test", 1, 5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        approach = driver.approach_driver

        approach.tick(self.profile, self.clock[0] + 2_000_000_000)
        approach.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.tick(driver)

        self.assertFalse(driver.report.terminal)
        self.assertIs(driver.approach_driver, approach)
        self.assertFalse(driver._engagement.awaiting_continuity_reanchor)
        self.assertIsNotNone(driver._fact)

    def test_approach_that_fails_during_start_is_retired_before_retry(self):
        class StartFailedNavigationSession(FakeNavigationSession):
            def start_goal(self, goal_id, revision, goal_state, frame, **kwargs):
                super().start_goal(goal_id, revision, goal_state, frame, **kwargs)
                self.state = NavigationSessionState.FAILED
                self.reason = "goal_surface_unavailable"

        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-start-failed", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        session = StartFailedNavigationSession()
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])
        failed_owner = driver.approach_driver

        result = self.tick(driver)

        self.assertIsNone(result)
        self.assertIsNone(failed_owner.source)
        self.assertIsNot(driver.approach_driver, failed_owner)
        self.assertEqual(driver.report.state, "pursuing")

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

    def test_hidden_engaged_target_that_moves_out_of_range_resumes_navigation(self):
        class FailFirstApproachSession(FakeNavigationSession):
            def start_goal(self, goal_id, revision, goal_state, frame, **kwargs):
                super().start_goal(goal_id, revision, goal_state, frame, **kwargs)
                if len(self.starts) == 1:
                    self.state = NavigationSessionState.FAILED
                    self.reason = "goal_surface_unavailable"

        session = FailFirstApproachSession()
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])
        while driver.report.confirmed_hits < 1:
            self.tick(driver)
        self.backend.hurt = 0
        self.backend.visible = False

        self.tick(driver)
        self.assertIsNotNone(driver._reacquire_source)

        self.backend.distance = 6.0
        self.tick(driver)
        self.tick(driver)

        self.assertIsNone(driver._reacquire_source)
        self.assertIsNotNone(driver.approach_driver)
        self.assertEqual(len(session.starts) + len(session.updates), 2)
        self.assertEqual(session.state, NavigationSessionState.EXECUTING)
        self.assertEqual(driver.report.state, "pursuing")

    def test_transient_target_gap_inside_strike_returns_to_reacquisition(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.assertIsNotNone(driver.strike_driver)
        driver._observe(EngagementEventKind.CONFIRMED_HIT)
        self.backend.visible = False

        driver.strike_driver._state = "failed"
        driver.strike_driver._reason = "target_or_observation_unavailable"
        driver._adopt_strike_report()

        self.assertFalse(driver.report.terminal)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("pursuing", "target_observation_gap"))
        self.assertTrue(driver._engagement.active)
        self.assertIsNone(driver.strike_driver)

        for _ in range(3):
            self.tick(driver)
            if driver._reacquire_source is not None:
                break
        self.assertIsNotNone(driver._reacquire_source)

    def test_reacquire_uses_observed_eye_and_tracked_target_center(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while driver.report.confirmed_hits < 1:
            self.tick(driver)
        self.backend.hurt = 0
        self.backend.visible = False
        self.backend.own_pose = "crouching"
        self.backend.own_eye_height = 1.27

        self.tick(driver)

        expected_pitch = math.degrees(math.atan2(1.27 - 1.95 / 2, 2.5))
        self.assertGreater(expected_pitch, 0)
        self.assertGreater(self.backend.actions[-1].look.pitch_delta_degrees, 0)

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

    def test_cancel_while_airborne_keeps_navigation_until_safe_stop_finishes(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-delayed-cancel", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        session = FakeNavigationSession(cancel_steps=2)
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])

        driver.cancel(self.profile, "user_cancelled")

        self.assertEqual(driver.report.state, "cancelling")
        self.assertIsNotNone(driver.approach_driver)
        self.assertIsNotNone(driver.approach_driver.source)

        self.tick(driver)

        self.assertEqual(driver.report.state, "cancelled")
        self.assertIsNone(driver.approach_driver)

    def test_pursuit_deadline_hands_control_to_task_layer_after_safe_stop(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-pursuit-timeout", "episode-1", "test", 1,
                           60_000_000_000)
        ).succeeded)
        session = FakeNavigationSession(cancel_steps=2)
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])
        self.clock[0] = driver._deadline_ns

        result = driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.assertIsNotNone(result)
        self.assertEqual(driver.report.state, "cancelling")
        self.assertEqual(driver.report.reason, "pursuit_deadline_exhausted")
        self.assertIsNotNone(driver.approach_driver)

        driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.assertEqual(driver.report.state, "needs_task_decision")
        self.assertEqual(driver.report.reason, "pursuit_deadline_exhausted")
        self.assertIsNone(driver.approach_driver)

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

    def test_damage_on_unsupported_surface_still_invalidates_navigation_and_recovers(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-unverified-motion", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        driver.navigation_session.motion_residual = lambda *_: MotionResidualResult(
            MotionResidualStatus.UNSUPPORTED, 0, 1,
            reasons=("surface_motion_rule_not_supported",),
        )

        self.backend.own_hurt = 10
        self.backend.own_health = 18.0
        self.backend.own_ground = False
        self.backend.own_velocity = (.10, .20, 0.0)
        self.tick(driver)

        self.assertIsNone(driver.approach_driver)
        self.assertIsNotNone(driver.recovery_driver)
        self.assertEqual(driver.report.state, "recovering_external_motion")
        self.assertEqual(driver.report.external_motion_events, 1)
        self.assertEqual(
            driver.recovery_driver._event.source.value,
            "damage_with_unverified_motion",
        )

    def test_grounded_external_motion_keeps_safe_navigation_owner(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-ground-reentry", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        approach = driver.approach_driver

        self.backend.own_hurt = 10
        self.backend.own_health = 18.0
        self.backend.own_ground = True
        self.backend.own_velocity = (.10, 0.0, 0.0)
        self.tick(driver)

        self.assertIs(driver.approach_driver, approach)
        self.assertIsNone(driver.recovery_driver)
        self.assertEqual(driver.report.state, "pursuing")
        self.assertEqual(driver.report.external_motion_events, 1)

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

        def damaged_tick(profile, deadline, **kwargs):
            self.backend.own_hurt = 10
            self.backend.own_health = 18.0
            self.backend.own_ground = False
            self.backend.own_velocity = (.10, .20, 0.0)
            return original_tick(profile, deadline, **kwargs)

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

        def damaged_tick(profile, deadline, **kwargs):
            self.backend.own_hurt = 10
            self.backend.own_health = 18.0
            self.backend.own_ground = False
            self.backend.own_velocity = (.10, .20, 0.0)
            return original_tick(profile, deadline, **kwargs)

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

    def test_recovery_event_budget_returns_control_to_task_strategy(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-strategy-handoff", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.own_hurt = 10
        self.backend.own_health = 18.0
        self.backend.own_ground = False
        self.tick(driver)
        self.tick(driver)
        recovery = driver.recovery_driver
        self.assertIsNotNone(recovery)
        event = recovery._event
        own = self.runtime.observation.self_state.value
        for offset in range(1, 5):
            recovery.observe_event(replace(
                event,
                event_id=f"{event.event_id}/repeat/{offset}",
                generation=event.generation + offset,
                observation_sequence_id=self.runtime.observation.sequence_id,
                movement_tick_id=own.movement_tick_id,
            ))

        self.backend.own_ground = True
        self.backend.own_velocity = (.01, 0.0, 0.0)
        for _ in range(4):
            if driver.report.terminal:
                break
            self.tick(driver)

        self.assertEqual(driver.report.state, "needs_task_decision")
        self.assertEqual(
            driver.report.reason, "external_motion/event_budget_exhausted",
        )
        self.assertTrue(driver.report.terminal)

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

    def test_combat_step_can_share_selected_navigation_movement(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        source = self.runtime.register_ordered_source("navigation")
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
        results = []
        for _ in range(4):
            results.append(self.tick(driver))
            if (driver.report.terminal
                    or isinstance(results[-1].decision.action.operation, AttackEntityV1)):
                break
        self.assertTrue(all(
            result.decision.action.movement == MovementV1(forward=1)
            for result in results
        ))
        self.assertTrue(any(
            isinstance(result.decision.action.operation, AttackEntityV1)
            for result in results
        ))
        self.assertNotEqual(driver.report.state, "failed")
        self.assertEqual(self.runtime.state, RuntimeStateV1.READY)

    def test_real_approach_proposal_keeps_moving_when_strike_look_is_compatible(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-composed", "episode-1", "test", 1, 5_000_000_000)
        ).succeeded)
        session = FakeNavigationSession()
        session.look = LookV1()
        session.movement_look_tolerance_degrees = 5.0
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])
        self.assertIsNotNone(driver.approach_driver)

    def test_nonwalk_navigation_action_keeps_its_route_look(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-route-look", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        session = FakeNavigationSession()
        session.route_look_required = True
        session.look = LookV1(yaw_delta_degrees=7.0)
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])

        result = self.tick(driver)

        self.assertEqual(result.decision.action.look.yaw_delta_degrees, 7.0)
        self.assertEqual(session.conditioned_look_requests, [])
        self.assertIsNone(driver._pursuit_look_source)
        self.backend.distance = 2.5

        results = []
        for _ in range(8):
            if driver.report.terminal:
                break
            result = self.tick(driver)
            if result is not None:
                results.append(result)
            if result is not None and isinstance(
                result.decision.action.operation, AttackEntityV1,
            ):
                break

        attack = next(
            result for result in results
            if isinstance(result.decision.action.operation, AttackEntityV1)
        )
        self.assertEqual(attack.decision.action.movement, MovementV1(forward=1))
        self.assertIn("movement", dict(attack.decision.selected_intents))
        self.assertIn("look", dict(attack.decision.selected_intents))
        self.assertIsNotNone(driver.approach_driver)

    def test_visible_pursuit_turns_toward_target_while_navigation_keeps_moving(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=6.0)
        self.runtime = PlayerRuntimeV1(
            self.backend, self.trace, lambda: self.clock[0],
        )
        self.assertTrue(self.runtime.reset(
            ResetRequestV0(
                "reset-pursuit-look", "episode-1", "test", 1,
                5_000_000_000,
            )
        ).succeeded)
        session = FakeNavigationSession()
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])

        composed = None
        with patch(
            "mc2p.skills.moving_melee_driver.combat_aim_angles",
            return_value=(15.0, 0.0),
        ):
            for _ in range(4):
                result = self.tick(driver)
                if (result is not None
                        and abs(result.decision.action.look.yaw_delta_degrees)
                            > 0.0):
                    composed = result
                    break

        self.assertIsNotNone(composed)
        self.assertEqual(
            composed.decision.action.movement,
            MovementV1(forward=1),
        )
        selected = dict(composed.decision.selected_intents)
        self.assertIn("movement", selected)
        self.assertIn("look", selected)
        self.assertTrue(session.conditioned_look_requests)
        self.assertEqual(
            session.conditioned_look_requests[-1][0],
            composed.decision.action.look.yaw_delta_degrees,
        )

    def test_large_strike_turn_recomputes_and_keeps_same_frame_movement(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(
            self.backend, self.trace, lambda: self.clock[0],
        )
        self.assertTrue(self.runtime.reset(
            ResetRequestV0(
                "reset-conditioned-combat-look", "episode-1", "test", 1,
                5_000_000_000,
            )
        ).succeeded)
        session = FakeNavigationSession()
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])
        self.backend.distance = 2.5
        self.backend.targeted = False

        composed = None
        with patch(
            "mc2p.skills.melee_strike_driver.combat_aim_angles",
            return_value=(15.0, 0.0),
        ):
            for _ in range(8):
                if driver.report.terminal:
                    break
                result = self.tick(driver)
                if (result is not None
                        and result.decision.action.look
                           == LookV1(15.0, 0.0)):
                    composed = result
                    break

        self.assertIsNotNone(composed)
        self.assertEqual(
            composed.decision.action.movement,
            MovementV1(forward=1),
        )
        self.assertEqual(composed.decision.action.look, LookV1(15.0, 0.0))
        self.assertTrue(session.conditioned_look_requests)
        conditioned = session.conditioned_look_requests[-1]
        self.assertEqual(conditioned[0], 15.0)
        self.assertEqual(
            conditioned[1],
            dict(composed.decision.selected_intents)["look"],
        )

    def test_stable_combat_look_keeps_each_ground_travel_axis(self):
        for movement in (
            MovementV1(forward=1),
            MovementV1(strafe=1),
            MovementV1(strafe=-1),
            MovementV1(forward=-1),
        ):
            with self.subTest(movement=movement):
                self.runtime.close()
                self.backend = MeleeBackend(self.clock, distance=5.0)
                self.runtime = PlayerRuntimeV1(
                    self.backend, self.trace, lambda: self.clock[0],
                )
                self.assertTrue(self.runtime.reset(
                    ResetRequestV0(
                        "reset-combat-axis", "episode-1", "test", 1,
                        5_000_000_000,
                    )
                ).succeeded)
                session = FakeNavigationSession()
                session.movement = movement
                session.look = None
                session.movement_observed_yaw_limit_degrees = 5.0
                driver = MovingMeleeDriver(
                    self.runtime, session, clock_ns=lambda: self.clock[0],
                )
                driver.start(self.target, self.clock[0])
                self.backend.distance = 2.5

                attack = None
                for _ in range(8):
                    if driver.report.terminal:
                        break
                    result = self.tick(driver)
                    if (result is not None and isinstance(
                            result.decision.action.operation, AttackEntityV1)):
                        attack = result
                        break

                self.assertIsNotNone(attack)
                self.assertEqual(attack.decision.action.movement, movement)
                self.assertIn("movement", dict(attack.decision.selected_intents))
                self.assertIsInstance(
                    attack.decision.action.operation, AttackEntityV1,
                )
                self.assertEqual(attack.decision.action.look, LookV1())
                self.assertEqual(self.runtime.state, RuntimeStateV1.READY)

    def test_target_leaving_range_during_composed_strike_reuses_active_approach(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-composed-reapproach", "episode-1", "test", 1,
                           5_000_000_000)
        ).succeeded)
        session = FakeNavigationSession()
        driver = MovingMeleeDriver(
            self.runtime, session, clock_ns=lambda: self.clock[0],
        )
        driver.start(self.target, self.clock[0])
        original_approach = driver.approach_driver
        self.assertIsNotNone(original_approach)
        strike = MeleeStrikeDriver(
            self.runtime, clock_ns=lambda: self.clock[0],
        )
        strike.start(self.target, self.clock[0])
        self.assertEqual(strike.report.outcome, MeleeStrikeOutcome.NEEDS_APPROACH)
        driver.strike_driver = strike

        driver._adopt_strike_report()

        self.assertIsNone(driver.strike_driver)
        self.assertIs(driver.approach_driver, original_approach)
        self.assertEqual(driver.report.state, "pursuing")
        self.assertEqual(self.runtime.ordered_source_stats["active_sources"], 1)

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

    def test_hit_confirmation_timeout_is_bounded_and_returned_to_task_policy(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.confirm_hit = False
        while driver.report.attack_submissions == 0:
            self.tick(driver)
        for _ in range(80):
            if driver.report.terminal:
                break
            self.tick(driver)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("needs_task_decision", "hit_confirmation_uncertain"))
        self.assertEqual(driver.report.attack_submissions, 2)
        self.assertIs(
            driver.report.task_outcome,
            AttackTaskOutcome.CONFIRMATION_RETRY_EXHAUSTED,
        )

    def test_operation_rejection_retries_once_then_returns_to_task_policy(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.operation_reject_reason = "entity_target_mismatch"

        for _ in range(80):
            if driver.report.terminal:
                break
            self.tick(driver)

        self.assertEqual(
            (driver.report.state, driver.report.reason),
            ("needs_task_decision", "attack_operation_rejected"),
        )
        self.assertEqual(driver.report.attack_submissions, 2)
        self.assertEqual(self.runtime.state.value, "ready")
        self.assertIs(
            driver.report.task_outcome,
            AttackTaskOutcome.GATE_RETRY_EXHAUSTED,
        )

    def test_gate_rejection_and_confirmation_timeout_do_not_share_budget(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.operation_reject_reason = "entity_target_mismatch"
        for _ in range(30):
            self.tick(driver)
            if driver.report.attack_retry.gate_rejections_total == 1:
                break

        self.backend.operation_reject_reason = None
        self.backend.confirm_hit = False
        self.backend.hurt = 0
        for _ in range(60):
            self.tick(driver)
            if driver.report.attack_retry.confirmation_timeouts_total == 1:
                break

        self.assertFalse(driver.report.terminal)
        self.assertIsNone(driver.report.task_outcome)
        self.assertEqual(
            (driver.report.attack_retry.gate_rejections_total,
             driver.report.attack_retry.confirmation_timeouts_total),
            (1, 1),
        )
        self.assertEqual(
            (driver.report.attack_retry.consecutive_gate_rejections,
             driver.report.attack_retry.consecutive_confirmation_timeouts),
            (0, 1),
        )

    def test_alternating_attack_failures_return_to_task_policy(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.confirm_hit = False
        with patch(
            "mc2p.skills.melee_strike_driver.CONFIRMATION_NS",
            100_000_000,
        ):
            for failure_index in range(6):
                before = (
                    driver.report.attack_retry.gate_rejections_total
                    + driver.report.attack_retry.confirmation_timeouts_total
                )
                self.backend.operation_reject_reason = (
                    "entity_target_mismatch" if failure_index % 2 == 0 else None
                )
                self.backend.hurt = 0
                for _ in range(30):
                    if driver.report.terminal:
                        break
                    self.tick(driver)
                    after = (
                        driver.report.attack_retry.gate_rejections_total
                        + driver.report.attack_retry.confirmation_timeouts_total
                    )
                    if after > before:
                        break

        self.assertEqual(
            (driver.report.state, driver.report.reason),
            ("needs_task_decision", "attack_progress_exhausted"),
        )
        self.assertIs(
            driver.report.task_outcome,
            AttackTaskOutcome.NO_PROGRESS_RETRY_EXHAUSTED,
        )
        self.assertEqual(
            driver.report.attack_retry.failures_since_confirmed_hit,
            6,
        )

    def test_confirmed_hit_clears_streak_but_keeps_failure_history(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.operation_reject_reason = "entity_target_mismatch"
        for _ in range(30):
            self.tick(driver)
            if driver.report.attack_retry.gate_rejections_total == 1:
                break

        self.backend.operation_reject_reason = None
        self.backend.confirm_hit = True
        self.backend.hurt = 0
        for _ in range(30):
            self.tick(driver)
            if driver.report.confirmed_hits == 1:
                break

        retry = driver.report.attack_retry
        self.assertEqual(retry.gate_rejections_total, 1)
        self.assertEqual(retry.confirmed_hits_total, 1)
        self.assertEqual(retry.consecutive_gate_rejections, 0)
        self.assertIsNone(driver.report.task_outcome)

    def test_input_failures_have_a_separate_typed_budget(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.backend.reject_reason = "backend_rejected"
        for _ in range(80):
            if driver.report.terminal:
                break
            self.tick(driver)

        self.assertEqual(driver.report.state, "needs_task_decision")
        self.assertIs(
            driver.report.task_outcome,
            AttackTaskOutcome.INPUT_RETRY_EXHAUSTED,
        )
        self.assertEqual(driver.report.attack_retry.input_failures_total, 2)
        self.assertEqual(self.runtime.state, RuntimeStateV1.READY)


if __name__ == "__main__":
    unittest.main()
