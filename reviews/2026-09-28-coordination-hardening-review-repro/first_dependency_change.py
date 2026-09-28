# Run from the repository root of an eb4653c checkout: PYTHONPATH=. python -B <script>
"""Which cells change on the first frames of the clean stair descent (route invalidated at tick 2)."""
from mc2p.motion_nav import runtime_adapter
from tests.sim.runner import run
from tests.sim.scenarios import SCENARIOS

original = runtime_adapter.apply_observed_blocks
log = []


def wrapped(world, stamp, blocks, geometries, air_results):
    changed = original(world, stamp, blocks, geometries, air_results)
    if changed and len(log) < 6:
        view = world.view()
        log.append((stamp.sequence_id, [(cell, view.cell(cell).knowledge.value) for cell in sorted(changed)[:8]],
                    len(changed)))
    return changed


runtime_adapter.apply_observed_blocks = wrapped
r = run(next(s for s in SCENARIOS if s.name == "stair_descent_4"))
print(r.outcome, r.trace[-1]["retry_total_failures"])
for item in log:
    print(item)
