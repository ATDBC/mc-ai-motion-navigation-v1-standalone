"""Do the planner-worker tests still detect a blocking submit or poll after S0-R removed their timers?

    PYTHONPATH=. python -B blocking_mutation_check.py      (at 1f0fefe and at e108a32)

Two mutations of PlannerWorker, applied in the test process only:
  submit_blocks : every submit_* sleeps 0.15 s before returning (search no longer overlaps control)
  poll_blocks   : every poll_latest sleeps 0.06 s (each control tick would stall)
For each named test we report whether it still passes under the mutation.  A test that passes
under a mutation does not protect the property its name states.
"""
import time
import unittest
from unittest.mock import patch

from mc2p.motion_nav.planner_worker import PlannerWorker

TESTS = [
    "tests.motion_nav.test_b07_surface_planning.B07SurfacePlanningTests.test_surface_snapshot_graph_construction_also_runs_in_worker",
    "tests.motion_nav.test_planner_worker.PlannerWorkerTests.test_snapshot_graph_construction_and_search_both_run_in_worker",
    "tests.motion_nav.test_planner_worker.PlannerWorkerTests.test_background_delay_never_blocks_submit_or_poll",
    "tests.motion_nav.test_planner_worker.PlannerWorkerTests.test_fixed_route_control_continues_during_five_hundred_ms_search",
]


def slow(method, seconds):
    def wrapper(self, *args, **kwargs):
        time.sleep(seconds)
        return method(self, *args, **kwargs)
    return wrapper


def run(test_id, mutation):
    patches = []
    if mutation == "submit_blocks":
        for name in ("submit", "submit_snapshot", "submit_surface_snapshot"):
            patches.append(patch.object(PlannerWorker, name, slow(getattr(PlannerWorker, name), .15)))
    elif mutation == "poll_blocks":
        patches.append(patch.object(PlannerWorker, "poll_latest", slow(PlannerWorker.poll_latest, .06)))
    for item in patches:
        item.start()
    try:
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromName(test_id).run(result)
        return "pass" if result.wasSuccessful() else "FAIL"
    finally:
        for item in patches:
            item.stop()


if __name__ == "__main__":
    for test_id in TESTS:
        name = test_id.rsplit(".", 1)[1]
        print(f"{name:<72} none={run(test_id, None)} submit_blocks={run(test_id, 'submit_blocks')} "
              f"poll_blocks={run(test_id, 'poll_blocks')}")
