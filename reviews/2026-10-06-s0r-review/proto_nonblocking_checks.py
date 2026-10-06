"""Prototype: deterministic replacements for the planner-worker timers removed in S0-R.  (e108a32)

    PYTHONPATH=. python -B proto_nonblocking_checks.py

Two structural checks, no wall-clock pass condition (the poll loop below is only a liveness bound
on waiting for the result to arrive):

  planning_never_runs_in_control_process
      PlannerWorker uses a spawned process, so patching planner_worker._execute_job in this
      process cannot affect the worker.  If submit or poll ever planned in the control process,
      the patched function raises.
  control_side_queue_operations_never_block
      Wrap the worker's two queues so that only put_nowait/get_nowait are allowed.

Each check is run on the unmodified worker and under two mutations:
  parent_plans  : _enqueue runs _execute_job synchronously before enqueueing (planning on the
                  control side)
  blocking_poll : poll_latest first does a blocking results.get(timeout=0.01)
A useful check passes unmodified and fails under the mutation it targets.
"""
import queue
import time
import unittest
from unittest.mock import patch

from mc2p.motion_nav import planner_worker as pw
from mc2p.motion_nav.known_map_planner import PlanningRequest
from mc2p.motion_nav.planner_worker import PlannerWorker
from tests.motion_nav.test_known_map_planning import grid_graph


class _NonBlockingOnly:
    def __init__(self, inner):
        self._inner = inner

    def put_nowait(self, item):
        return self._inner.put_nowait(item)

    def get_nowait(self):
        return self._inner.get_nowait()

    def put(self, *args, **kwargs):
        raise AssertionError("control side used a blocking put")

    def get(self, *args, **kwargs):
        raise AssertionError("control side used a blocking get")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _wait_result(worker):
    for _ in range(500):          # liveness bound only; nothing is asserted about elapsed time
        result = worker.poll_latest()
        if result is not None:
            return result
        time.sleep(.01)
    return None


def _job():
    graph, start, goal = grid_graph(2)
    return graph, PlanningRequest(1, "nonblocking", "goal", 1, "random", start, goal)


class NonBlockingChecks(unittest.TestCase):
    def test_planning_never_runs_in_control_process(self):
        graph, request = _job()
        with PlannerWorker(debug_delay_seconds=.1) as worker:
            with patch.object(pw, "_execute_job",
                              side_effect=AssertionError("planning ran in the control process")):
                worker.submit(graph, request)
                self.assertIsNotNone(_wait_result(worker))

    def test_control_side_queue_operations_never_block(self):
        graph, request = _job()
        worker = PlannerWorker(debug_delay_seconds=.1)
        originals = worker._requests, worker._results
        try:
            worker._requests, worker._results = (_NonBlockingOnly(q) for q in originals)
            worker.submit(graph, request)
            self.assertIsNone(worker.poll_latest())      # the worker is still delayed
            self.assertIsNotNone(_wait_result(worker))
        finally:
            worker._requests, worker._results = originals
            worker.close()


def mutation(name):
    if name == "parent_plans":
        original = PlannerWorker._enqueue
        def enqueue(self, job):
            pw._execute_job(job)
            return original(self, job)
        return patch.object(PlannerWorker, "_enqueue", enqueue)
    if name == "blocking_poll":
        original = PlannerWorker.poll_latest
        def poll(self):
            try:
                self._results.get(True, .01)
            except queue.Empty:
                pass
            return original(self)
        return patch.object(PlannerWorker, "poll_latest", poll)
    return None


if __name__ == "__main__":
    for test in ("test_planning_never_runs_in_control_process", "test_control_side_queue_operations_never_block"):
        row = []
        for name in (None, "parent_plans", "blocking_poll"):
            context = mutation(name)
            if context is not None:
                context.start()
            try:
                result = unittest.TestResult()
                NonBlockingChecks(test).run(result)
                row.append(f"{name or 'none'}={'pass' if result.wasSuccessful() else 'FAIL'}")
            finally:
                if context is not None:
                    context.stop()
        print(f"{test:<52} " + " ".join(row))
