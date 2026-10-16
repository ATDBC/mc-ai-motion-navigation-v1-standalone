"""D112 deterministic worker-delay and verified-command degradation matrix.

The production navigation chain is unchanged.  These checks replace only the
motion-worker transport and the game backend, using the same formal simulation
fixtures as the existing continuity checks.  ``release_safe_command_indices``
is used only to choose fault-injection ticks; it never decides the outcome.
"""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.behavior import BehaviorProfileV0
from tests.motion_nav.test_action_continuity_formal import (
    _DeliveryWorker,
    _gap_case,
)
from tests.sim.backend import Perturbations
from tests.sim.continuous_height_matrix import height_action_scenarios
from tests.sim.runner import Result, Scenario, run


_STRICT_ACTIONS = frozenset({
    "JumpGapSegment",
    "JumpUpSegment",
    "ControlledDropSegment",
})
_DELAYS = (0, 2, 5, 10)
_GAP_EXTRA_TURNS = (1, 2, 3)
_BOUNDARIES = ("first", "middle", "last")

# 12 action/delay + 9 rotated-gap/delay + 3 alive/no-result +
# 9 action/release-safe-boundary command-loss cases.
_MATRIX_CASES = (
    3 * len(_DELAYS)
    + len(_GAP_EXTRA_TURNS) * 3
    + 3
    + 3 * len(_BOUNDARIES)
)


class _AliveNoResultWorker(_DeliveryWorker):
    """Accept work forever while truthfully producing no result."""

    def poll_available(self):
        self.poll_count += 1
        return ()


def _action_cases() -> dict[str, Scenario]:
    height = {case.name: case for case in height_action_scenarios()}
    return {
        "gap": _gap_case(),
        "jump_up": height["jump_up_1"],
        "drop": height["direct_drop_2"],
    }


class D112WorkerDegradationTests(unittest.TestCase):
    def _run(self, case: Scenario, worker: _DeliveryWorker, **kwargs) -> Result:
        return run(
            replace(case, max_ticks=max(case.max_ticks, 500)),
            motion_factory=lambda: worker,
            **kwargs,
        )

    def _assert_bounded_and_safe(self, result: Result) -> None:
        self.assertEqual(result.damage, 0.0, result.reason)
        self.assertEqual(result.violations, [], result.reason)
        self.assertTrue(result.trace, result.reason)
        self.assertFalse(result.trace[-1]["source_bound"], result.reason)
        self.assertTrue(result.trace[-1]["on_ground"], result.reason)
        self.assertTrue(all(
            row["runtime_failure"] is None for row in result.trace
        ), result.reason)
        self.assertTrue(all(
            row["controller_ids"] and row["source_bound"]
            for row in result.trace if not row["on_ground"]
        ), result.reason)

    def _assert_no_blind_strict_start(
        self,
        result: Result,
        worker: _DeliveryWorker,
    ) -> None:
        committed = [
            row for row in result.trace
            if row["action_kind"] in _STRICT_ACTIONS
            and (row["applied_movement"]["jump"] or not row["on_ground"])
        ]
        if not committed:
            return
        self.assertTrue(worker.observed_deliveries, committed[0])
        first_delivery_tick = worker.observed_deliveries[0][0]
        self.assertLessEqual(
            first_delivery_tick,
            committed[0]["movement_tick"],
            (first_delivery_tick, committed[0]),
        )

    def _assert_delivery_outcome(
        self,
        result: Result,
        worker: _DeliveryWorker,
        delay: int,
    ) -> None:
        self._assert_bounded_and_safe(result)
        self._assert_no_blind_strict_start(result, worker)
        if delay in (0, 2, 5):
            # Five movement ticks is the frozen product requirement.  It is RED
            # on the current code and must not be relabelled as an allowed
            # failure merely because the body stops safely.
            self.assertEqual(result.outcome, "success", result.reason)
        else:
            self.assertIn(result.outcome, {"success", "failed"}, result.reason)
            if result.outcome == "failed":
                self.assertTrue(
                    result.reason.startswith("motion_unsolvable:"),
                    result.reason,
                )

    def test_matrix_has_exactly_thirty_three_scenarios(self):
        self.assertEqual(_MATRIX_CASES, 33)

    def test_three_action_families_degrade_by_delivery_ticks(self):
        for action, case in _action_cases().items():
            for delay in _DELAYS:
                with self.subTest(action=action, delay=delay):
                    worker = _DeliveryWorker(delivery_polls=delay)
                    result = self._run(case, worker)
                    self._assert_delivery_outcome(result, worker, delay)

    def test_gap_delivery_delay_is_cardinally_symmetric(self):
        for turns in _GAP_EXTRA_TURNS:
            for delay in (0, 5, 10):
                with self.subTest(turns=turns, delay=delay):
                    worker = _DeliveryWorker(delivery_polls=delay)
                    result = self._run(_gap_case(turns), worker)
                    self._assert_delivery_outcome(result, worker, delay)

    def test_alive_worker_that_never_returns_is_bounded(self):
        for action, case in _action_cases().items():
            with self.subTest(action=action):
                worker = _AliveNoResultWorker(delivery_polls=0)
                result = self._run(case, worker)
                self._assert_bounded_and_safe(result)
                self._assert_no_blind_strict_start(result, worker)
                self.assertEqual(result.outcome, "failed", result.reason)
                self.assertEqual(
                    result.reason,
                    "motion_unsolvable:motion_solver_request_expired",
                )
                self.assertEqual(worker.observed_deliveries, [])
                self.assertEqual(len(worker.jobs), 2)

    def test_capacity_rejected_before_any_timeout_keeps_capacity_detail(self):
        from mc2p.motion_nav.motion_worker import MotionResultInbox

        worker = _AliveNoResultWorker(delivery_polls=0)
        # Reject the initial request, before any request can expire.
        with patch.object(MotionResultInbox, "register", return_value=False):
            result = self._run(_gap_case(), worker)
        self._assert_bounded_and_safe(result)
        self._assert_no_blind_strict_start(result, worker)
        self.assertEqual(result.outcome, "failed", result.reason)
        self.assertEqual(result.reason, "motion_unsolvable:motion_inbox_capacity_exhausted")
        self.assertEqual(worker.jobs, [])

    def test_losing_command_after_release_safe_boundaries_recovers(self):
        for action, case in _action_cases().items():
            for boundary_index, boundary in enumerate(_BOUNDARIES):
                with self.subTest(action=action, boundary=boundary):
                    injected = replace(case, perturbations=Perturbations())
                    worker = _DeliveryWorker(delivery_polls=0)
                    injection = {}

                    def arm_from_delivered_proof(context):
                        context.driver.tick(
                            BehaviorProfileV0(),
                            context.clock[0] + 500_000_000,
                        )
                        if injection:
                            return ()
                        proofs = tuple(
                            value.solve_result.proof for value in worker.results
                            if value.solve_result.proof is not None
                        )
                        if not proofs:
                            return ()
                        proof = proofs[0]
                        # A boundary is eligible only when a following command
                        # really exists.  These indices choose the experiment;
                        # runtime still detects and recovers on its own.
                        eligible = tuple(
                            index for index in proof.release_safe_command_indices
                            if index + 1 < len(proof.commands)
                        )
                        self.assertGreaterEqual(len(eligible), 3)
                        selected = (
                            eligible[0],
                            eligible[len(eligible) // 2],
                            eligible[-1],
                        )
                        self.assertEqual(len(set(selected)), 3)
                        command_index = selected[boundary_index]
                        lost_tick = (
                            proof.execution_window.earliest_start_tick
                            + command_index
                            + 1
                        )
                        self.assertGreater(
                            lost_tick,
                            context.backend.movement_tick,
                        )
                        context.backend.perturbations.late_ticks = frozenset({
                            lost_tick,
                        })
                        injection.update(
                            proof=proof,
                            command_index=command_index,
                            lost_tick=lost_tick,
                        )
                        return ()

                    result = self._run(
                        injected,
                        worker,
                        control_step=arm_from_delivered_proof,
                    )
                    self.assertTrue(injection, "no delivered proof armed the fault")
                    lost_tick = injection["lost_tick"]
                    self._assert_bounded_and_safe(result)
                    self._assert_no_blind_strict_start(result, worker)
                    self.assertEqual(result.outcome, "success", result.reason)
                    self.assertIn(
                        ("late_input", lost_tick),
                        result.applied_perturbations,
                    )
                    row = next(
                        value for value in result.trace
                        if value["movement_tick"] == lost_tick
                    )
                    self.assertIsNone(row["applied_request"], row)
                    self.assertEqual(row["sample_state"], "lease_exhausted")


if __name__ == "__main__":
    unittest.main()
