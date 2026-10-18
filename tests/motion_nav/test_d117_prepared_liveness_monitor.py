"""D117 monitor checks use typed prepared state and control ticks."""
from __future__ import annotations

import unittest

from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutorState
from tests.sim.async_monitor import (
    AsyncInvariantMonitor,
    PreparedHandoffActivity,
)


class D117PreparedLivenessMonitorTests(unittest.TestCase):
    def test_boundary_wait_exceeding_tick_limit_is_reported(self) -> None:
        monitor = AsyncInvariantMonitor()
        for tick in range(20, 24):
            monitor.check_prepared_liveness(PreparedHandoffActivity(
                movement_tick=tick,
                prepared_successor_required=True,
                pending_action_boundary_index=2,
                controller_state=None,
            ), boundary_limit_ticks=2, recovery_limit_ticks=3)

        self.assertIn(
            ("I23", "prepared action boundary wait exceeded 2 control ticks"),
            monitor.violations,
        )

    def test_recovery_exceeding_tick_limit_is_reported(self) -> None:
        monitor = AsyncInvariantMonitor()
        for tick in range(30, 35):
            monitor.check_prepared_liveness(PreparedHandoffActivity(
                movement_tick=tick,
                prepared_successor_required=False,
                pending_action_boundary_index=None,
                controller_state=VerifiedMotionExecutorState.RECOVERING,
            ), boundary_limit_ticks=2, recovery_limit_ticks=3)

        self.assertIn(
            ("I23", "prepared recovery exceeded 3 control ticks"),
            monitor.violations,
        )

    def test_clearing_typed_state_resets_each_wait(self) -> None:
        monitor = AsyncInvariantMonitor()
        monitor.check_prepared_liveness(PreparedHandoffActivity(
            movement_tick=20,
            prepared_successor_required=True,
            pending_action_boundary_index=2,
            controller_state=None,
        ), boundary_limit_ticks=1, recovery_limit_ticks=1)
        monitor.check_prepared_liveness(PreparedHandoffActivity(
            movement_tick=21,
            prepared_successor_required=False,
            pending_action_boundary_index=None,
            controller_state=VerifiedMotionExecutorState.RUNNING,
        ), boundary_limit_ticks=1, recovery_limit_ticks=1)
        monitor.check_prepared_liveness(PreparedHandoffActivity(
            movement_tick=22,
            prepared_successor_required=False,
            pending_action_boundary_index=None,
            controller_state=VerifiedMotionExecutorState.RECOVERING,
        ), boundary_limit_ticks=1, recovery_limit_ticks=1)
        monitor.check_prepared_liveness(PreparedHandoffActivity(
            movement_tick=23,
            prepared_successor_required=False,
            pending_action_boundary_index=None,
            controller_state=VerifiedMotionExecutorState.RUNNING,
        ), boundary_limit_ticks=1, recovery_limit_ticks=1)

        self.assertEqual(monitor.violations, [])

    def test_activity_rejects_reason_strings_and_untyped_controller_state(self) -> None:
        with self.assertRaises(ValueError):
            PreparedHandoffActivity(
                movement_tick=1,
                prepared_successor_required=True,
                pending_action_boundary_index=2,
                controller_state="recovering",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
