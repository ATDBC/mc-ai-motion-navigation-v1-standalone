"""Round-16 review: hold-forward reference ticks for the continuous-height probe.

Same calculator (physics_1_21.step), same one-wide floating supports as
scripts/continuous_height_runtime.py, but the controller simply holds forward
(and sprint where the helper does) until the goal column is reached and the
body has settled.  The result is a lower bound to compare with the Fabric
elapsed ticks in the acceptance record (B12-C continuous height).

Run from the repository root of a 59c45bb checkout, with height_transition_cost.py
next to this script:
    PYTHONPATH=. python -B <this script>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import SLAB, run_hold_forward  # noqa: E402


def columns(tops):
    return tuple(list(range(63, top)) if top > 63 else [62] for top in tops)


print("probe low-height stairs (4 half-steps up over 4 blocks):",
      run_hold_forward(([63], [63, (64, SLAB)], [63, 64], [63, 64, (65, SLAB)], [63, 64, 65])))
print("probe stair descent (4 full blocks down over 4 blocks):",
      run_hold_forward(columns([68, 67, 66, 65, 64])))
for drop in (1, 2, 3, 5):
    print(f"probe direct drop {drop}:", run_hold_forward(columns([64 + drop, 64])))
