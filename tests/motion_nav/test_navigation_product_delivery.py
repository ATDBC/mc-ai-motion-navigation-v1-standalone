"""The v7 transport uses physical ticks, preserves FIFO, and reports its cost."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from mc2p.motion_nav.motion_solver import (
    AirTransitionSolveRequest, DEFAULT_AIR_TRANSITION_POLICIES, GapSolveRequest,
    LandingRegion, MotionSolveKind,
)
from mc2p.motion_nav.motion_worker import GapMotionSolveJob, MotionJobOperation
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav.test_b10_gap_solver import fixture


def job(action="jump_gap", revision=1):
    anchor, world, target, _ = fixture()
    landing = LandingRegion(*target)
    window = CandidateExecutionWindow(11, 12)
    if action == "jump_gap":
        request = GapSolveRequest((0, 1), landing, window)
    else:
        kind = MotionSolveKind.JUMP_UP if action == "jump_up" else MotionSolveKind.CONTROLLED_DROP
        height = 1 if action == "jump_up" else -int(action[-1])
        request = AirTransitionSolveRequest(kind, (0, 1),
            replace(landing, surface_y=anchor.physics_state.position[1] + height), window,
            policy=DEFAULT_AIR_TRANSITION_POLICIES[kind])
    return GapMotionSolveJob("test-delivery", revision, anchor, world, request)


class ProductDeliveryTests(unittest.TestCase):
    def test_cold_job_blocks_later_warm_job_and_repeated_polls_do_not_advance_time(self):
        from tests.sim.motion_delivery import DeterministicMotionWorker
        worker = DeterministicMotionWorker(max_pending=2)
        worker.advance_tick(10)
        self.assertTrue(worker.submit(job(revision=1)))
        self.assertTrue(worker.submit(job(revision=2)))
        self.assertFalse(worker.submit(job(revision=3)))
        for tick in range(10, 17):
            worker.advance_tick(tick)
            self.assertEqual(worker.poll_available(), ())
            self.assertEqual(worker.poll_available(), ())
        worker.advance_tick(17)
        self.assertEqual([row.candidate_revision for row in worker.poll_available()], [1, 2])
        self.assertEqual([row["ready_tick"] for row in worker.records], [17, 11])
        self.assertEqual([row["delivery_tick"] for row in worker.records], [17, 17])
        self.assertEqual([row.operation for row in worker.activity], ["submit", "submit", "poll", "poll"])
        worker.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.poll_available(), ())
        with self.assertRaises(ValueError):
            worker.submit(job())

    def test_warm_delays_follow_action_and_revalidation_does_not_restart_cold_time(self):
        from tests.sim.motion_delivery import DeterministicMotionWorker
        for action, delay in (("jump_gap", 1), ("jump_up", 1), ("drop_2", 2), ("drop_5", 3)):
            with self.subTest(action=action):
                worker = DeterministicMotionWorker()
                worker.advance_tick(10)
                worker.submit(job())
                worker.advance_tick(17)
                proof = worker.poll_available()[0].solve_result.proof
                worker.submit(job(action, revision=2))
                worker.advance_tick(17 + delay - 1)
                self.assertEqual(worker.poll_available(), ())
                worker.advance_tick(17 + delay)
                self.assertEqual(len(worker.poll_available()), 1)
                worker.submit(replace(job(action, revision=3),
                    operation=MotionJobOperation.REVALIDATE, proof=proof))
                worker.advance_tick(17 + 2 * delay)
                self.assertEqual(len(worker.poll_available()), 1)
                self.assertEqual([row["delay_ticks"] for row in worker.records], [7, delay, delay])
                self.assertEqual([row["action"] for row in worker.records], ["jump_gap", action, action])
                self.assertEqual(worker.records[-1]["operation"], "revalidate")

    def test_v7_changes_only_the_delivery_model_from_v6_task_inputs(self):
        from scripts.navigation_coordination_metrics import load_manifest
        old, old_hash = load_manifest(Path("tests/sim/manifests/navigation-product-r28-v4.json"))
        new, _ = load_manifest(Path("tests/sim/manifests/navigation-product-r28-v7.json"))
        profile = new.pop("motion_delivery_profile")
        self.assertEqual(new, old)
        self.assertEqual(profile["base_manifest_sha256"], old_hash)
        self.assertEqual(profile["cold_first_job_ticks"], 7)
        self.assertEqual(profile["warm_ticks"], {"jump_gap": 1, "jump_up": 1, "drop_2": 2, "drop_5": 3})
        malformed = deepcopy(profile)
        malformed["warm_ticks"]["drop_2"] = 1
        from tests.sim.motion_delivery import validate_profile
        with self.assertRaises(ValueError):
            validate_profile(malformed)

    def test_v7_record_and_metadata_include_frozen_delivery_identity(self):
        from scripts.navigation_coordination_metrics import baseline, compare_delivery, verify_repeat
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            summary = baseline(Path("tests/sim/manifests/navigation-product-r28-v7.json"), root,
                               seed_count=1, groups=["drop-normal"])
            metadata = json.loads((root / "metadata.json").read_text("utf-8"))
            record = json.loads((root / "runs.jsonl").read_text("utf-8"))
            self.assertEqual(metadata["motion_delivery_profile"], record["motion_delivery_profile"])
            self.assertEqual(record["motion_job_count"], len(record["motion_jobs"]))
            self.assertGreaterEqual(record["motion_job_count"], 2)
            self.assertEqual(record["motion_jobs"][0]["delay_ticks"], 7)
            self.assertEqual(record["motion_jobs"][1]["delay_ticks"], 2)
            for row in record["motion_jobs"]:
                self.assertGreaterEqual(row["delivery_tick"], row["ready_tick"])
                self.assertGreaterEqual(row["delivery_poll"], row["submission_poll"])
            self.assertEqual(summary["groups"][0]["exceptions"], 0)
            self.assertEqual(summary["groups"][0]["safety_violation_tasks"], 0)
            self.assertEqual(summary["groups"][0]["insufficient_evidence"], 0)
            repeated = Path(temporary) / "repeat"
            baseline(Path("tests/sim/manifests/navigation-product-r28-v7.json"), repeated,
                     seed_count=1, groups=["drop-normal"])
            self.assertEqual(verify_repeat(root, repeated, Path(temporary) / "repeat-check")["differences"], [])
            old = Path(temporary) / "synchronous"
            baseline(Path("tests/sim/manifests/navigation-product-r28-v4.json"), old,
                     seed_count=1, groups=["drop-normal"])
            report = compare_delivery(old, root, Path(temporary) / "delivery-comparison")
            self.assertEqual(report["pairs"], 1)
            self.assertTrue(report["input_parameters_verified"])
            self.assertIn("motion_jobs", report["differences"][0])
            changed = json.loads((old / "metadata.json").read_text("utf-8"))
            changed["manifest"]["budgets"]["finite_max_ticks"] += 1
            (old / "metadata.json").write_text(json.dumps(changed), "utf-8")
            with self.assertRaisesRegex(ValueError, "task parameters"):
                compare_delivery(old, root, Path(temporary) / "wrong-input-comparison")


if __name__ == "__main__":
    unittest.main()
