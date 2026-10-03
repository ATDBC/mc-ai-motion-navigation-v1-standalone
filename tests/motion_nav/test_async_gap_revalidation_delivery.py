"""Interleaving old results must not stop a test worker servicing new jobs."""
import unittest

from tests.sim.async_work_sequences import run_gap_sequence


class AsyncGapRevalidationDeliveryTests(unittest.TestCase):
    def test_delayed_interleaved_result_gets_real_background_revalidation(self):
        for seed in (270001, 270002, 270016, 270064):
            with self.subTest(seed=seed):
                result = run_gap_sequence(seed)
                self.assertTrue(result["passed"], result["trace"][-1])
                self.assertEqual(result["task_outcome"], "success")
                self.assertIn("revalidate", result["followup_operations"])
                self.assertEqual(result["verification"]["status"].value, "verified")
                self.assertFalse(result["verification"]["gaps"])

    def test_unserviced_followup_cannot_be_mislabeled_as_success(self):
        result = run_gap_sequence(270001, service_followup=False)
        self.assertFalse(result["passed"])
        self.assertEqual(result["task_outcome"], "failed")
        self.assertFalse(result["trace"][-1]["source_owned"])
        self.assertTrue(result["trace"][-1]["on_ground"])
        self.assertEqual(result["followup_operations"], [])
