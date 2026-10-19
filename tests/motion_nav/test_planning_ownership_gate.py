"""Static ownership gate for the R25 planning boundary."""
from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "mc2p" / "motion_nav"
COORDINATOR = PACKAGE / "planning_coordinator.py"
OWNERS = PACKAGE / "navigation_owners.py"


class PlanningOwnershipGateTests(unittest.TestCase):
    def production_sources(self) -> dict[Path, str]:
        return {
            path: path.read_text(encoding="utf-8")
            for path in PACKAGE.glob("*.py")
        }

    def test_only_coordinator_calls_planner_submit_and_poll(self):
        offenders = []
        pattern = re.compile(
            r"\.(?:submit_surface_snapshot|submit_snapshot|poll_latest)\("
            r"|\b(?:self\.)?_planner\.poll_available\("
        )
        for path, source in self.production_sources().items():
            if path != COORDINATOR and pattern.search(source):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_pipeline_state_is_written_only_by_owner_and_coordinator(self):
        offenders = []
        pattern = re.compile(
            r"(?:PlanningPipelineState\(|_pipeline\."
            r"(?:builder|snapshot|snapshot_request_id|submitted_request_id|"
            r"submitted_movement_tick|submitted_monotonic_ns|"
            r"result_deadline_monotonic_ns)\s*=)"
        )
        for path, source in self.production_sources().items():
            if path not in {COORDINATOR, OWNERS} and pattern.search(source):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_goal_request_generation_has_one_writer(self):
        offenders = []
        pattern = re.compile(r"\bsequence\s*=\s*[^\n]*\.sequence\s*\+\s*1")
        for path, source in self.production_sources().items():
            if path != OWNERS and pattern.search(source):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_computation_scope_is_created_only_by_goal_ledger(self):
        offenders = [path.name for path, source in self.production_sources().items()
                     if path != OWNERS and re.search(r"\bAsyncComputationScope\(", source)]
        self.assertEqual(offenders, [])

    def test_navigation_session_has_no_legacy_planning_writes(self):
        source = (PACKAGE / "navigation_session.py").read_text(encoding="utf-8")
        forbidden = (
            "PlanningPipelineState",
            "submit_surface_snapshot(",
            "submit_snapshot(",
            "poll_latest(",
            "poll_available(",
            "_snapshot_builder",
            "_planning_submission_expired",
            "_record_planning_submission",
        )
        self.assertEqual(
            tuple(item for item in forbidden if item in source),
            (),
        )


if __name__ == "__main__":
    unittest.main()
