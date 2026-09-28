"""Remove the evidenced landing floor k ticks before the first airborne tick of the clean run.

A correct stack may still fall when the edit lands inside the physical point of no return,
but with enough lead time it must stop at the edge (sneak) instead of taking an unauthorized fall."""
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import Scenario, run
from tests.sim.scenarios import drop_ledge

base = Scenario("drop2", drop_ledge(2), (.5, 64.0, .5), (.5, 62.0, 4.5), max_ticks=400, expect="failed")
clean = run(base)
depart = next(row["tick"] for row in clean.trace if not row["on_ground"])
first_drop_input = next(row["tick"] for row in clean.trace
                        if row["action_kind"] == "ControlledDropSegment" and row["controller_ids"] == ("route_executor",))
print(f"clean run: drop controller from tick {first_drop_input}, first airborne tick {depart}")
for lead in (1, 2, 3, 4, 6, 8, 12):
    edits = {depart - lead: {(x, 61, z): None for x in (-1, 0, 1) for z in (3, 4, 5)}}
    r = run(replace(base, name=f"lead{lead}", perturbations=Perturbations(world_edits=edits)))
    print(f"edit {lead:>2} ticks before departure -> {r.outcome:<8} {r.reason:<40} pos={r.final_position} damage={r.damage:g} "
          f"violations={[(t, c) for t, c, _ in r.violations]}", flush=True)
