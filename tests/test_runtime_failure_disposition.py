import unittest

from mc2p.contracts.report import FailureCodeV0, FailureV0
from mc2p.runtime.failure_disposition import (
    FailureDisposition,
    FailureDispositionPolicy,
    classify_failure,
)


def failure(code, *, retryable=True, source="runtime"):
    return FailureV0(code, f"failure:{code.value}", retryable, source)


class RuntimeFailureDispositionTests(unittest.TestCase):
    def test_async_trace_failure_keeps_control_but_degrades_evidence(self):
        decision = classify_failure(failure(
            FailureCodeV0.TRACE_IO, source="async_trace",
        ))
        self.assertIs(
            decision.disposition,
            FailureDisposition.CONTINUE_WITH_INCOMPLETE_EVIDENCE,
        )
        self.assertFalse(decision.evidence_complete)
        self.assertFalse(decision.retry_requested)

    def test_control_uncertainty_requires_a_new_runtime(self):
        for code in (
            FailureCodeV0.BACKEND_IO,
            FailureCodeV0.BACKEND_DISCONNECTED,
            FailureCodeV0.OBSERVATION_INVARIANT,
            FailureCodeV0.CONTRACT,
        ):
            with self.subTest(code=code):
                decision = classify_failure(failure(code))
                self.assertIs(
                    decision.disposition, FailureDisposition.RECREATE_RUNTIME,
                )

    def test_explicit_cancellation_is_not_retried(self):
        decision = classify_failure(failure(FailureCodeV0.CANCELLED))
        self.assertIs(decision.disposition, FailureDisposition.CANCEL_TASK)
        self.assertFalse(decision.retry_requested)

    def test_same_retryable_cause_has_a_frozen_retry_limit(self):
        policy = FailureDispositionPolicy(max_same_cause_retries=2)
        timeout = failure(FailureCodeV0.DEADLINE_EXCEEDED, source="task")

        first = policy.decide(timeout)
        second = policy.decide(timeout)
        exhausted = policy.decide(timeout)

        self.assertTrue(first.retry_requested)
        self.assertTrue(second.retry_requested)
        self.assertIs(exhausted.disposition, FailureDisposition.CANCEL_TASK)
        self.assertEqual(exhausted.reason_code, "same_cause_retry_exhausted")

    def test_control_deadline_recreates_runtime_instead_of_reusing_uncertain_input(self):
        decision = classify_failure(failure(FailureCodeV0.DEADLINE_EXCEEDED))
        self.assertIs(decision.disposition, FailureDisposition.RECREATE_RUNTIME)
        self.assertFalse(decision.retry_requested)

    def test_nonretryable_transport_failure_ends_the_episode(self):
        decision = classify_failure(failure(
            FailureCodeV0.BACKEND_DISCONNECTED, retryable=False,
        ))
        self.assertIs(decision.disposition, FailureDisposition.END_EPISODE)


if __name__ == "__main__":
    unittest.main()
