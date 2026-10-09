"""Run the frozen continuous-height jump_up_1 family on the current checkout.

    python <this file> late 0 100       (from a checkout root)

Arguments: condition (normal|late) and a [lo, hi) slice of the 100 frozen
cases.  Prints one line per case and an outcome tally.
"""
import sys, collections
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import tests.sim.runner as runner
from tests.sim.continuous_height_matrix import height_action_matrix_cases
cond = sys.argv[1]; lo, hi = int(sys.argv[2]), int(sys.argv[3])
cases = [c for c in height_action_matrix_cases(cond) if c.family == "jump_up_1"][lo:hi]
cnt = collections.Counter()
for c in cases:
    r = runner.run(c.scenario)
    cnt[(r.outcome, r.reason)] += 1
    print(c.direction, c.speed_band, c.seed, r.outcome, r.reason, len(r.violations), flush=True)
print(dict(cnt))
