"""The new pursuit deadline check can still lose a race to the old raise.

Run from the repository root of commit cba2899:

    PYTHONPATH=. python -B <this-review-branch>/reviews/2026-09-26-d034-repro/pursuit_deadline_check_race.py

``MovingMeleeDriver.tick`` now checks ``clock >= task deadline`` before the
approach work.  ``_tick_visible_approach`` reads the clock again after the
moving-goal decision and still raises ``ContractViolation`` when the deadline
has passed by then.  The script uses the upstream unit fixtures, places the
task deadline 0.5 ms ahead, and lets the moving-goal decision take 1 ms of
(fake) time -- the only change to the fixture's timing.
"""
from __future__ import annotations

from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
import mc2p.skills.moving_melee_driver as moving
from tests.test_moving_melee_driver import (
    FakeNavigationSession, MeleeBackend, MovingMeleeDriverTests,
)


def main() -> None:
    case = MovingMeleeDriverTests("test_death_before_attack_completes_without_attack")
    case.setUp()
    try:
        case.runtime.close()
        case.clock[0] = 10_000_000_000
        case.backend = MeleeBackend(case.clock, distance=6.0)
        case.runtime = PlayerRuntimeV1(
            case.backend, case.trace, lambda: case.clock[0],
        )
        assert case.runtime.reset(ResetRequestV0(
            "reset-race", "episode-1", "test", 1, 120_000_000_000,
        )).succeeded
        driver = moving.MovingMeleeDriver(
            case.runtime, FakeNavigationSession(),
            clock_ns=lambda: case.clock[0],
        )
        driver.start(case.target, case.clock[0])
        for _ in range(3):
            case.clock[0] += 50_000_000
            case.tick(driver)
        assert driver.approach_driver is not None

        original = moving.decide_moving_goal

        def slow_goal_decision(*args, **kwargs):
            case.clock[0] += 1_000_000  # 1 ms spent deciding the goal
            return original(*args, **kwargs)

        case.clock[0] += 50_000_000
        driver._deadline_ns = case.clock[0] + 500_000  # 0.5 ms left
        print("at tick start: deadline passed?",
              case.clock[0] >= driver._deadline_ns)
        with patch.object(moving, "decide_moving_goal", slow_goal_decision):
            try:
                case.tick(driver)
            except ContractViolation as error:
                print("tick raised ContractViolation:", error)
            else:
                print("tick ended as", driver.report.state, driver.report.reason)
    finally:
        case.tearDown()


if __name__ == "__main__":
    main()
