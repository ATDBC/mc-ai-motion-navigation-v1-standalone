"""A visible pursuit that outlives the 30 s task limit.

Run from the repository root of the commit under test:

    PYTHONPATH=. python -B <this-review-branch>/reviews/2026-09-26-b12-pursuit-repro/pursuit_task_limit_raises_contract_violation.py

It reuses the fixtures of ``tests/test_moving_melee_driver.py``: the target
stays 6 blocks away, so the driver keeps pursuing; then the task deadline is
placed 120 ms ahead and the clock advances in 50 ms control frames.
"""
from __future__ import annotations

from mc2p.contracts.common import ContractViolation
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.contracts.reset import ResetRequestV0
from mc2p.skills.moving_melee_driver import MovingMeleeDriver
from tests.test_moving_melee_driver import (
    FakeNavigationSession, MeleeBackend, MovingMeleeDriverTests,
)


def main() -> None:
    case = MovingMeleeDriverTests("test_death_before_attack_completes_without_attack")
    case.setUp()
    try:
        case.runtime.close()
        # FakeNavigationSession stamps intents at deadline - 500 ms, so keep
        # the fake clock far from zero.
        case.clock[0] = 10_000_000_000
        case.backend = MeleeBackend(case.clock, distance=6.0)
        case.runtime = PlayerRuntimeV1(
            case.backend, case.trace, lambda: case.clock[0],
        )
        assert case.runtime.reset(ResetRequestV0(
            "reset-limit", "episode-1", "test", 1, 120_000_000_000,
        )).succeeded
        driver = MovingMeleeDriver(
            case.runtime, FakeNavigationSession(),
            clock_ns=lambda: case.clock[0],
        )
        driver.start(case.target, case.clock[0])
        for _ in range(4):
            case.tick(driver)
        print("before limit: phase", driver.report.state,
              "approach active", driver.approach_driver is not None)
        # The unit fixture cannot run 600 frames (its fake Runtime stops being
        # ready after about 60), so move the task deadline instead of the
        # clock: this is the state after ~29.9 s of continuous visible pursuit.
        driver._deadline_ns = case.clock[0] + 120_000_000
        for frame in range(1, 6):
            case.clock[0] += 50_000_000
            left_ms = (driver._deadline_ns - case.clock[0]) / 1e6
            try:
                result = case.tick(driver)
            except ContractViolation as error:
                print(f"frame {frame} ({left_ms:+.0f} ms to task limit): "
                      f"ContractViolation: {error}")
                return
            failure = None if result is None else result.report.failure
            print(f"frame {frame} ({left_ms:+.0f} ms to task limit): "
                  f"status={None if result is None else result.report.status.value} "
                  f"failure={None if failure is None else (failure.code.value, failure.reason)} "
                  f"driver={driver.report.state}/{driver.report.reason}")
            if driver.report.terminal:
                return
    finally:
        case.tearDown()


if __name__ == "__main__":
    main()
