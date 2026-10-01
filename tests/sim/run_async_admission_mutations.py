"""Restore four known defects in isolated exports; each targeted test must fail."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from scripts.export_motion_navigation_standalone import export_tree


MODULE = "tests.motion_nav.test_r27_async_admission.R27IdentityTests."
MUTATIONS = (
    ("reused_execution_identity", "mc2p/motion_nav/navigation_session.py",
     (("owner_instance_id=self._execution_instances.allocate(),", 'owner_instance_id="reused-route-owner",'),),
     "test_formal_runtime_successor_rejects_old_same_goal_gap_failure"),
    ("receipt_time_as_processing_time", "mc2p/motion_nav/world_interaction.py",
     (("self._work.check(self._work_identity, self._clock())", "self._work.check(self._work_identity, frame.body.stamp.received_monotonic_ns)"),),
     "test_placement_uses_dispatch_and_processing_clock_even_without_new_observation"),
    ("ignored_information_failure", "mc2p/motion_nav/navigation_session.py",
     (("if outcome.kind is PlanningUpdateKind.FAILED:", "if False and outcome.kind is PlanningUpdateKind.FAILED:"),),
     "test_session_consumes_information_failure_instead_of_reopening_without_permit"),
    ("sticky_placement_confirmation", "mc2p/motion_nav/world_interaction.py",
     (("elif destination.knowledge is CellKnowledge.AIR:\n            self._world_confirmed = False", "elif destination.knowledge is CellKnowledge.AIR:\n            pass"),
      ("self._inventory_confirmed = actual_count == self._submitted_item_count - 1", "self._inventory_confirmed = self._inventory_confirmed or actual_count == self._submitted_item_count - 1")),
     "test_revoked_partial_placement_evidence_cannot_be_combined"),
    ("terminal_business_cancelled_again", "mc2p/skills/block_placement_driver.py",
     (("if not self.transaction.report.terminal:\n            self.transaction.cancel(reason)",
       "self.transaction.cancel(reason)"),),
     "tests.motion_nav.test_async_work_verification.AsyncWorkVerificationTests.test_repeated_cancel_preserves_terminal_placement_and_body_owner"),
    ("stale_notification_borrows_active_attempt", "mc2p/motion_nav/planning_coordinator.py",
     (("return replace(selection, kind=PlanningUpdateKind.DISCARDED,\n                           reason=\"stale_information_batch\")",
       "return self._update(PlanningUpdateKind.DISCARDED, \"stale_information_batch\")"),),
     "tests.motion_nav.test_async_work_verification.AsyncWorkVerificationTests.test_old_information_notification_after_cancel_is_discarded"),
    ("empty_records_pass_gate", "tests/sim/runner.py",
     ((" and self.verification_complete", ""),),
     "tests.motion_nav.test_async_work_verification.AsyncWorkVerificationTests.test_missing_formal_async_records_cannot_pass"),
    ("receivers_deduplicated_before_check", "tests/sim/async_monitor.py",
     (("Counter(owner.active_identity for owner in owners if owner.active_identity is not None)",
       "Counter({owner.active_identity: 1 for owner in owners if owner.active_identity is not None})"),),
     "tests.motion_nav.test_async_work_verification.AsyncWorkVerificationTests.test_duplicate_receivers_are_detected_before_event_deduplication"),
)


def run(output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite mutation evidence: {output}")
    output.mkdir(parents=True)
    rows = []
    # Only this newly created temporary tree is removed; source and evidence stay intact.
    temporary_parent = Path(__file__).resolve().parents[2] / ".tmp"
    temporary_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r27-mutation-", dir=temporary_parent) as temporary:
        root = Path(temporary) / "export"
        export_tree(root, clean=False)
        for name, relative, replacements, test in MUTATIONS:
            full_test = test if test.startswith("tests.") else MODULE + test
            path = root / relative
            original = path.read_text(encoding="utf-8")
            baseline = subprocess.run([sys.executable, "-m", "unittest", full_test, "-q"],
                                      cwd=root, capture_output=True, text=True, timeout=60)
            changed = original
            for before, after in replacements:
                if changed.count(before) != 1:
                    raise ValueError(f"mutation {name} no longer matches one implementation site")
                changed = changed.replace(before, after)
            path.write_text(changed, encoding="utf-8")
            mutated = subprocess.run([sys.executable, "-m", "unittest", full_test, "-q"],
                                     cwd=root, capture_output=True, text=True, timeout=60)
            path.write_text(original, encoding="utf-8")
            output.joinpath(name + ".txt").write_text(
                "BASELINE\n" + baseline.stdout + baseline.stderr + "\nMUTATION\n" + mutated.stdout + mutated.stderr,
                encoding="utf-8")
            # An import/crash is not the intended behavioral detection.
            detected = baseline.returncode == 0 and mutated.returncode != 0 and "FAIL:" in mutated.stderr
            rows.append({"mutation": name, "test": full_test,
                         "baseline_exit": baseline.returncode, "mutation_exit": mutated.returncode,
                         "detected": detected})
    result = {"schema_version": "mc2p.async-admission-mutations.v1", "cases": rows,
              "all_detected": all(row["detected"] for row in rows)}
    output.joinpath("summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    result = run(parser.parse_args().output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["all_detected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
