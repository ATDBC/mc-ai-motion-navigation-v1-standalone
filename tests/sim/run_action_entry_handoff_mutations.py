"""Restore three D092 handoff defects in isolated exports.

Each mutant must import normally and fail the behavior assertion that protects
the affected path.  The source checkout is never edited.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_motion_navigation_standalone import export_tree


MODULE = "tests.motion_nav.test_action_entry_handoff_repair.ActionEntryHandoffRepairTests."
MUTATIONS = (
    (
        "ignore_next_entry_yaw_tolerance",
        "mc2p/motion_nav/action_route_executor.py",
        (("tolerance = window.maximum_yaw_error_radians or 0.0",
          "tolerance = 3.141592653589793  # mutant ignores the declared yaw entry"),),
        "test_walk_owns_required_yaw_while_still_moving_before_jump",
    ),
    (
        "charge_stale_anticipated_negative_as_real_failure",
        "mc2p/motion_nav/motion_coordination.py",
        (("if negative_stale:\n                    self.last_failure_reason = \"\"\n                elif prepared.retryable:",
          "if False and negative_stale:\n                    self.last_failure_reason = \"\"\n                elif prepared.retryable:"),),
        "test_stale_anticipated_negative_retires_without_buying_real_failure",
    ),
    (
        "retain_retired_motion_mailbox",
        "mc2p/motion_nav/motion_coordination.py",
        (("self.result_inbox.retire(identity)",
          "pass  # mutant leaves the retired job active in the mailbox"),),
        "test_repeated_stale_anticipated_results_keep_one_job_and_end_bounded",
    ),
)


def run(output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite mutation evidence: {output}")
    output.mkdir(parents=True)
    rows = []
    temporary_parent = Path(__file__).resolve().parents[2] / ".tmp"
    temporary_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="action-entry-handoff-mutation-", dir=temporary_parent,
    ) as temporary:
        root = Path(temporary) / "export"
        export_tree(root, clean=False)
        for name, relative, replacements, method in MUTATIONS:
            test = MODULE + method
            path = root / relative
            original = path.read_text(encoding="utf-8")
            baseline = subprocess.run(
                [sys.executable, "-m", "unittest", test, "-q"],
                cwd=root, capture_output=True, text=True, timeout=90,
            )
            changed = original
            for before, after in replacements:
                if changed.count(before) != 1:
                    raise ValueError(
                        f"mutation {name} no longer matches one implementation site"
                    )
                changed = changed.replace(before, after)
            path.write_text(changed, encoding="utf-8")
            mutated = subprocess.run(
                [sys.executable, "-m", "unittest", test, "-q"],
                cwd=root, capture_output=True, text=True, timeout=90,
            )
            path.write_text(original, encoding="utf-8")
            output.joinpath(name + ".txt").write_text(
                "BASELINE\n" + baseline.stdout + baseline.stderr
                + "\nMUTATION\n" + mutated.stdout + mutated.stderr,
                encoding="utf-8",
            )
            detected = (
                baseline.returncode == 0
                and mutated.returncode != 0
                and "FAIL:" in mutated.stderr
                and "ImportError" not in mutated.stderr
                and "SyntaxError" not in mutated.stderr
            )
            rows.append({
                "mutation": name,
                "test": test,
                "baseline_exit": baseline.returncode,
                "mutation_exit": mutated.returncode,
                "detected": detected,
            })
    result = {
        "schema_version": "mc2p.action-entry-handoff-mutations.v1",
        "cases": rows,
        "all_detected": all(row["detected"] for row in rows),
    }
    output.joinpath("summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    result = run(parser.parse_args().output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["all_detected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
