"""Validate Fabric startup options without creating a game process."""
from contextlib import redirect_stderr
import io
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import run_action_continuity_fabric as launcher
from scripts import action_continuity_fabric_runtime as runtime_probe


class FabricStartupOptionsTests(unittest.TestCase):
    def test_recording_planner_satisfies_formal_protocol_and_preserves_two_receipts(self):
        from mc2p.motion_nav.planner_worker import PlannerWorkerPort
        from tests.motion_nav.test_r28_dual_planning import DualPlanningTests
        fixture = DualPlanningTests()
        world, planner, owner, request = fixture.start()
        fixture.advance(owner, world, 0)
        fixture.revise(owner, 2)
        fixture.advance(owner, world, 1)
        results = tuple(planner.results)
        self.assertEqual(len(results), 2)
        planner.deliveries = list(results)
        planner.pid = 123
        planner.outstanding_identities = tuple(r.work_identity for r in results)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runtime_probe, 'PlannerWorker', return_value=planner):
            recorded = runtime_probe._ObservedPlannerWorker(Path(directory) / 'worker.jsonl', 'protocol', 0.)
            self.assertIsInstance(recorded, PlannerWorkerPort)
            self.assertEqual(recorded.poll_available(), results)
            self.assertIsNone(recorded.poll_latest())
            recorded.close()

    def test_flat_view_requires_explicit_start_delivery_drop(self):
        for options in (["--flat-drop-view"],
                        ["--start-delivery", "--flat-drop-view"],
                        ["--start-delivery", "--kind", "jump_up", "--flat-drop-view"],
                        ["--kind", "drop_2", "--flat-drop-view"]):
            with self.subTest(options=options), patch.dict(os.environ, {}, clear=True), \
                    patch.object(launcher, "launcher_main") as execute, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    launcher.main(options)
                self.assertEqual(raised.exception.code, 2)
                execute.assert_not_called()

    def test_flat_drop_option_reaches_runtime_and_preserves_launcher_options(self):
        for kind in ("drop_2", "drop_5"):
            with self.subTest(kind=kind), patch.dict(os.environ, {}, clear=True), \
                    patch.object(launcher, "launcher_main", return_value=0) as execute:
                self.assertEqual(launcher.main(["--start-delivery", "--kind", kind, "--direction", "0",
                                               "--flat-drop-view", "--timeout-seconds", "300"]), 0)
                execute.assert_called_once_with(["--r25-planning-information-probe", "--timeout-seconds", "300"])
                enabled, kinds, pitch = runtime_probe._start_delivery_configuration()
                self.assertTrue(enabled)
                self.assertEqual(kinds, (kind,))
                self.assertEqual(pitch[kind], 0.)

    def test_default_drop_view_is_45_degrees_and_bad_environment_fails_before_game_work(self):
        with patch.dict(os.environ, {"MC2P_MOTION_START_DELIVERY_PROBE": "1"}, clear=True):
            _, kinds, pitch = runtime_probe._start_delivery_configuration()
            self.assertIn("drop_2", kinds)
            self.assertEqual(pitch["drop_2"], 45.)
            self.assertEqual(pitch["drop_5"], 45.)
            self.assertEqual(pitch["jump_up"], 0.)
        for values in ({"MC2P_MOTION_FLAT_DROP_VIEW": "1"},
                       {"MC2P_MOTION_START_DELIVERY_PROBE": "1", "MC2P_MOTION_FLAT_DROP_VIEW": "1"},
                       {"MC2P_MOTION_START_DELIVERY_PROBE": "1", "MC2P_MOTION_FLAT_DROP_VIEW": "1",
                        "MC2P_MOTION_START_DELIVERY_KIND": "moving_gap"}):
            with self.subTest(environment=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError):
                    runtime_probe._start_delivery_configuration()


if __name__ == "__main__":
    unittest.main()
