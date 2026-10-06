"""Backend loss on the formal point/follow path does not reuse an old frame."""
import unittest
from unittest.mock import patch
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.report import FailureCodeV0, FailureV0
from mc2p.motion_nav.navigation_session import NavigationSessionState

TERMINAL = {"success", "failed", "cancelled", "stopped", "interaction_required"}

from tests.sim.runtime_faults import build_runtime_case, run_io_case


class RuntimeBackendIOTests(unittest.TestCase):
    def fixture(self, *, follow=False, gap=False):
        case = build_runtime_case(follow=follow, gap=gap)
        self.addCleanup(case.close)
        return case.components

    def test_io_loss_is_terminal_for_point_and_follow_in_each_body_phase(self):
        for follow in (False, True):
            for phase in ("walking", "braking", "airborne"):
                for retryable in (False, True):
                    with self.subTest(follow=follow, phase=phase, retryable=retryable):
                        result = run_io_case(follow=follow, phase=phase, retryable=retryable)
                        self.assertEqual(result["terminal_propose_calls"], 0)
                        self.assertEqual(result["writes_after_failure"], 0)

    def test_plain_oserror_keeps_existing_retryable_mapping(self):
        clock, backend, runtime, session, driver, _ = self.fixture()
        backend.failure = OSError("plain transport loss")
        result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertTrue(result.report.failure.retryable)
        self.assertEqual(runtime.last_failure_disposition.disposition.value, "recreate_runtime")
        self.assertEqual(driver.reason, "control_unavailable")

    def test_unchanged_frame_is_not_proposed_twice_after_contract_failure(self):
        clock, backend, runtime, session, driver, _ = self.fixture()
        for _ in range(10):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        with (
            patch.object(session, "propose", side_effect=ContractViolation(
                "fixed route frame did not advance",
            )) as proposal,
            patch.object(session, "contract_stop_proposal",
                         wraps=session.contract_stop_proposal) as fallback,
        ):
            result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertEqual(proposal.call_count, 1)
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertIsNone(result.report.failure)
        for _ in range(40):
            if driver.state in TERMINAL:
                break
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertEqual(session.report.state, NavigationSessionState.FAILED)
        self.assertEqual(session.report.reason, "navigation_internal_contract_failure")

    def test_backend_io_failure_rejects_other_failure_codes(self):
        from mc2p.runtime.backend_v1 import BackendIOFailure
        with self.assertRaises(ContractViolation):
            BackendIOFailure(FailureV0(FailureCodeV0.CONTRACT, "invalid", True, "test"))

if __name__ == "__main__":
    unittest.main()
