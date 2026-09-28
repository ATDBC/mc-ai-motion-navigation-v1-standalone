"""Log every planner submission/result for the stuck revision case."""
import sys
import tests.sim.runner as runner
from mc2p.motion_nav import planner_worker

original_execute = planner_worker._execute_job
submitted = []


def logged(job):
    result = original_execute(job)
    request = job.request
    budget = getattr(request, "damage_budget", None)
    print("planner job gen", getattr(request, "sequence", None), "goal_rev", getattr(request, "goal_revision", None),
          "budget", None if budget is None else budget.maximum_expected_damage_points,
          "->", type(result).__name__, getattr(result, "status", None), getattr(result, "reason", None),
          getattr(getattr(result, "candidate", None), "route_id", None))
    return result


planner_worker._execute_job = logged
sys.argv = [sys.argv[0], "37"]
exec(open(__file__.replace("trace_planner_results.py", "trace_revise_in_drop.py")).read())
