"""Public worker failures and arbitration loss on the formal Runtime path."""
from dataclasses import replace

from mc2p.contracts.common import ContractViolation
from tests.sim.event_sequences import _ArbitratedControl
from tests.sim.backend import Perturbations, SLAB_ID
from tests.sim.runner import InlinePlannerWorker, run
from tests.sim.scenarios import SCENARIOS


class DeadPlanner(InlinePlannerWorker):
    def is_alive(self):
        return False

    def poll_latest(self):
        return None

    def poll_available(self):
        from mc2p.motion_nav.planner_worker import _failure_candidate
        from tests.sim.async_monitor import ObservedAsyncActivity
        jobs = (() if self._job is None else (self._job,)) + tuple(self._queued_jobs)
        self._job = None
        self._queued_jobs.clear()
        self.activity.extend(ObservedAsyncActivity(job.request.work_identity, "poll") for job in jobs)
        return tuple(_failure_candidate(job, "planner_worker_died") for job in jobs)


class FaultingPlanner(InlinePlannerWorker):
    def poll_latest(self):
        if self._job is not None:
            raise ContractViolation("test_worker_contract_fault")
        return None


def assert_route_replacement_entered(result):
    stops = [index for index, row in enumerate(result.trace)
             if row["route_validation"] is not None
             and row["route_validation"]["incumbent"] is not None
             and row["route_validation"]["incumbent"]["disposition"] == "stop"]
    assert stops, "world-change premise did not enter route validation stop"
    submitted = any(row["planning_submissions"] for row in result.trace[stops[0] + 1:])
    assert submitted, "world-change premise did not submit a replacement plan"


def assert_dependency_recovery_entered(result):
    assert_route_replacement_entered(result)
    assert result.recovery_failures > 0, "world-change premise did not enter begin_recovery"


def run_fault_case(name, *, dependency_material=SLAB_ID):
    base = next(s for s in SCENARIOS if s.name == (
        "half_steps_up_down" if name == "ground_stall_retry" else "flat_walk"))
    if name == "worker_death":
        result = run(replace(base, expect="failed"), planner_factory=DeadPlanner)
        expected = "failed", "planner_worker_died_retry_exhausted"
    elif name == "driver_contract_containment":
        result = run(replace(base, expect="failed"), planner_factory=FaultingPlanner)
        expected = "failed", "navigation_internal_contract_failure"
    elif name == "ground_stall_retry":
        result = run(base, control_step=_ArbitratedControl(frozenset(range(10, 50))))
        expected = "success", "goal_state_satisfied"
    elif name == "active_route_dependency_retry":
        edits = {tick: {(0, 63, 5): "minecraft:stone" if tick % 2
                       else dependency_material}
                 for tick in range(3, 65, 3)}
        result = run(replace(base, perturbations=Perturbations(world_edits=edits)))
        expected = "success", "goal_state_satisfied"
        assert_dependency_recovery_entered(result)
    else:
        raise ValueError(name)
    assert (result.outcome, result.reason) == expected
    assert result.verification_complete and not result.violations
    assert result.trace[-1]["on_ground"] and not result.trace[-1]["source_bound"]
    return result


FAULT_CASES = ("worker_death", "driver_contract_containment", "ground_stall_retry",
               "active_route_dependency_retry")
