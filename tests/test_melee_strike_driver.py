"""Shared one-strike core has no navigation and preserves causal attack evidence."""
import unittest

from mc2p.contracts.action_v1 import AttackEntityV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
from tests.test_player_runtime import _RecordingTrace


class MeleeStrikeDriverTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100_000_000]
        self.backend = MeleeBackend(self.clock)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset", "episode-1", "test", 1, 2_000_000_000)
        ).succeeded)
        self.profile = BehaviorProfileV0()
        self.target = CombatTargetV1(
            "combat-task", "combat-goal", 1, "episode-1", TRACK,
        )

    def tearDown(self):
        self.runtime.close()

    def driver(self):
        return MeleeStrikeDriver(self.runtime, clock_ns=lambda: self.clock[0])

    def test_far_target_requests_approach_without_creating_navigation(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-far", "episode-1", "test", 1, 2_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.assertEqual(driver.report.state, "needs_approach")
        self.assertFalse(driver.report.terminal)
        self.assertFalse(hasattr(driver, "approach_driver"))
        self.assertEqual(self.backend.actions, [])

    def test_explicit_death_completes_without_submitting_attack(self):
        self.backend.dead = True
        self.backend.health = 0.0
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        for _ in range(3):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "target_dead"))
        self.assertFalse(any(type(action.operation) is AttackEntityV1
                             for action in self.backend.actions))

    def test_submitted_attack_cancel_keeps_actual_hit_result_without_resend(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        driver.cancel(self.profile, "test_cancel")
        attacks = [action for action in self.backend.actions
                   if type(action.operation) is AttackEntityV1]
        self.assertEqual(len(attacks), 1)
        self.assertEqual(driver.report.state, "cancelled")
        self.assertTrue(driver.report.hit_observed)

    def test_external_recovery_cannot_erase_a_confirmed_hit(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        driver.interrupt_for_external_motion(self.profile, "damage_knockback")
        driver.observe_after_external_motion(self.runtime.observation)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "hit_confirmed"))

        self.backend.hurt = 0
        self.backend.sequence += 1
        self.clock[0] += 50_000_000
        driver.observe_after_external_motion(self.backend.observation())

        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "hit_confirmed"))


if __name__ == "__main__":
    unittest.main()
