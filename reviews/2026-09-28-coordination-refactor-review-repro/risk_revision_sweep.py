"""Goal revision around the first drop input, with a budget that fits exactly one drop.

The revised goal is another cell on the same lower floor, so the physical drop
the body is about to take is still the only damaging action a correct stack needs.
"""
import sys
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS

base = next(s for s in SCENARIOS if s.name == "direct_drop_5_budget_2")


def revise_to(position):
    def action(context):
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal("goal", 2, goal, context.clock[0],
                                    damage_budget=TaskDamageBudget(context.risk_policy_id,
                                                                   context.damage_points))
        context.goal_state = goal
        context.goal_position = position
    return action


def summary(result):
    final = result.trace[-1]["risk_actions"] if result.trace else ()
    return [(a["action_id"].split("/")[-1], a["state"], a["expected_damage"], len(a["submitted_sequences"]))
            for a in final]


late = len(sys.argv) > 1 and sys.argv[1] == "late"
for at in range(30, 42):
    scenario = replace(
        base, name=f"drop5_revise_at_{at}",
        events=[Event("revise", lambda c, at=at: c.tick >= at, revise_to((1.5, 59.0, 4.5)))],
        perturbations=Perturbations(late_ticks=frozenset({at + 1}) if late else frozenset()),
        max_ticks=300,
    )
    result = run(scenario)
    print(f"revise@{at:<3} late={late!s:<5} outcome={result.outcome:<10} reason={result.reason:<40} "
          f"damage={result.damage:g} pos={result.final_position} risk={summary(result)} "
          f"violations={[v[1] for v in result.violations]}")
