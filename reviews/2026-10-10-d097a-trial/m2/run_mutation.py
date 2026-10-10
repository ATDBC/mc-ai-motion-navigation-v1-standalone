"""G9: run test_m2 once without a fault (must pass) and once per injected fault (must fail).

Run from the project checkout with PYTHONPATH=<checkout>:<reviews dir>:<reviews dir>/m2:
    python3 run_mutation.py
Writes mutation_results.json and mutation_log.txt next to this file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import incremental as I

HERE = Path(__file__).resolve().parent
ORDER = ("no_landing_proof", "unknown_as_free", "one_branch", "ignore_inflight",
         "accept_stale", "ignore_damage", "swap_locked_command")
assert set(ORDER) == I.FAULTS


def run(fault: str | None):
    env = dict(os.environ)
    env.pop("D097A_FAULT", None)
    if fault:
        env["D097A_FAULT"] = fault
    started = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "unittest", "test_m2", "-v"], cwd=os.getcwd(), env=env,
                          capture_output=True, text=True)
    output = done.stdout + done.stderr
    failing = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", output, flags=re.M)))
    ran = re.search(r"Ran (\d+) tests? in", output)
    skipped = re.search(r"skipped=(\d+)", output)
    return {
        "fault": fault, "returncode": done.returncode, "passed": done.returncode == 0,
        "tests_run": int(ran.group(1)) if ran else None,
        "skipped": int(skipped.group(1)) if skipped else 0,
        "failing_tests": [cls for _, cls in failing],
        "seconds": round(time.perf_counter() - started, 1),
    }, output


def main():
    results = {"faults": {}}
    log = []
    baseline, output = run(None)
    results["baseline"] = baseline
    (HERE / "logs").mkdir(exist_ok=True)
    (HERE / "logs" / "mutation_baseline.txt").write_text(output, encoding="utf-8")
    log.append(f"=== baseline (no fault): passed={baseline['passed']} ran={baseline['tests_run']} "
               f"skipped={baseline['skipped']} {baseline['seconds']}s\n{output[-2500:]}")
    print("baseline", baseline["passed"], baseline["tests_run"], flush=True)
    for fault in ORDER:
        record, output = run(fault)
        record["detected"] = not record["passed"]
        (HERE / "logs" / f"mutation_{fault}.txt").write_text(output, encoding="utf-8")
        results["faults"][fault] = record
        log.append(f"=== fault {fault}: detected={record['detected']} failing={record['failing_tests']} "
                   f"{record['seconds']}s\n{output[-1800:]}")
        print(fault, "DETECTED" if record["detected"] else "NOT DETECTED", record["failing_tests"], flush=True)
    results["all_detected"] = all(v["detected"] for v in results["faults"].values())
    results["baseline_passes"] = baseline["passed"]
    (HERE / "mutation_results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    (HERE / "mutation_log.txt").write_text("\n\n".join(log), encoding="utf-8")
    return 0 if results["all_detected"] and baseline["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
