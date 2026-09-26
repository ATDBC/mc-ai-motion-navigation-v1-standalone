"""B12-B 的六个确定性运行时边界场景。"""
from dataclasses import replace
import unittest

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.moving_melee_driver import MovingMeleeDriver
from tests.navigation_session_fixtures import FakeNavigationSession
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
from tests.test_player_runtime import _RecordingTrace


class B12BRuntimeInjectionAcceptanceTests(unittest.TestCase):
    def fresh(self, *, distance=5.0):
        clock = [100_000_000]
        backend = MeleeBackend(clock, distance=distance)
        trace = _RecordingTrace()
        runtime = PlayerRuntimeV1(backend, trace, lambda: clock[0])
        self.assertTrue(runtime.reset(ResetRequestV0(
            "b12b-reset", "episode-1", "test", 1, 2_000_000_000,
        )).succeeded)
        target = CombatTargetV1(
            "b12b-task", "b12b-goal", 1, "episode-1", TRACK,
        )
        driver = MovingMeleeDriver(
            runtime, FakeNavigationSession(), clock_ns=lambda: clock[0],
        )
        driver.start(target, clock[0])
        return clock, backend, runtime, target, driver

    @staticmethod
    def tick(driver, clock):
        return driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)

    def test_observation_gap_twice_keeps_the_active_navigation_owner(self):
        for repeat in (1, 2):
            with self.subTest(repeat=repeat):
                clock, backend, runtime, target, driver = self.fresh()
                try:
                    owner = driver.approach_driver
                    owner.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
                    owner.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)

                    self.tick(driver, clock)

                    self.assertIs(driver.approach_driver, owner)
                    self.assertFalse(driver.report.terminal)
                    self.assertEqual(driver.report.attack_submissions, 0)
                    self.assertFalse(driver._engagement.awaiting_continuity_reanchor)
                    self.assertIs(runtime.state, RuntimeStateV1.READY)
                finally:
                    runtime.close()

    def test_hidden_without_engagement_twice_never_grants_an_attack(self):
        for repeat in (1, 2):
            with self.subTest(repeat=repeat):
                clock, backend, runtime, target, driver = self.fresh(distance=2.5)
                try:
                    backend.visible = False
                    backend.targeted = False
                    for _ in range(6):
                        if driver.report.terminal:
                            break
                        self.tick(driver, clock)

                    self.assertFalse(driver._engagement.engagement_granted)
                    self.assertEqual(driver.report.attack_submissions, 0)
                    self.assertIs(runtime.state, RuntimeStateV1.READY)
                finally:
                    runtime.close()

    def test_target_or_world_revision_invalidates_old_work(self):
        # 第一次只改目标修订；旧修订不能继续提交攻击。
        clock, backend, runtime, target, driver = self.fresh(distance=2.5)
        try:
            revised = replace(target, revision=2)
            driver.replace_target(revised, clock[0])
            for _ in range(10):
                if driver.report.confirmed_hits:
                    break
                self.tick(driver, clock)
            self.assertEqual(driver.report.target_revision, 2)
            self.assertEqual(driver.report.confirmed_hits, 1)
            self.assertIs(runtime.state, RuntimeStateV1.READY)
        finally:
            runtime.close()

        # 第二次只改世界会话；旧会话的动作必须失效且不能攻击。
        clock, backend, runtime, target, driver = self.fresh(distance=2.5)
        try:
            runtime._observation = replace(
                runtime.observation, episode_id="episode-2",
            )
            self.tick(driver, clock)
            self.assertEqual(
                (driver.report.state, driver.report.reason),
                ("failed", "world_session_changed"),
            )
            self.assertEqual(driver.report.attack_submissions, 0)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
