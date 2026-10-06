"""Run motion-navigation checks in either order and audit shared scene isolation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.sim.scenario_isolation import shared_scenario_hash


def _cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite check evidence: {args.output}")
    before = shared_scenario_hash()
    cases = list(_cases(unittest.defaultTestLoader.discover("tests/motion_nav", pattern="test_*.py")))
    if args.reverse:
        cases.reverse()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(cases))
    after = shared_scenario_hash()
    sources = sorted(Path("mc2p/motion_nav").glob("*.py")) + [Path(name) for name in (
        "mc2p/skills/navigation_session_driver.py",
        "mc2p/runtime/player_runtime_v1.py", "mc2p/runtime/backend_v1.py",
    )]
    hashes = {path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    passed = result.wasSuccessful() and not result.expectedFailures and before == after
    payload = {
        "schema_version": "mc2p.motion-navigation-checks.v1",
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_worktree_clean": not subprocess.check_output(["git", "status", "--porcelain"], text=True).strip(),
        "production_files_sha256": hashes,
        "python": sys.version, "platform": platform.platform(),
        "order": "reverse" if args.reverse else "forward",
        "shared_scenario_hash_before": before, "shared_scenario_hash_after": after,
        "tests": result.testsRun,
        "failures": [{"test": test.id(), "traceback": detail} for test, detail in result.failures],
        "errors": [{"test": test.id(), "traceback": detail} for test, detail in result.errors],
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "passed": passed,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tests": result.testsRun, "passed": passed, "scene_hash_unchanged": before == after}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
