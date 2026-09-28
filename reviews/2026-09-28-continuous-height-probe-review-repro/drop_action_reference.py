"""Round-18 review: hold-forward reference for the direct-drop action window.

Acceptance section 6 compares the ControlledDrop action "from the first verified
forward input to the confirmed landing" with the hold-forward reference defined
in section 1 (same 1.21 calculator, same start).  Section 12.2 instead compares
it with an older Fabric batch (16/18/21/24 ticks).

Here: the body stands still at the edge-probe entry stance (0.35 blocks from
the block centre toward the ledge, i.e. 0.15 blocks of overhang, as
LandingEdgeProbe leaves it), holds forward without sprint, and the tick of the
first ground contact after leaving the ledge is counted, for 1, 2, 3 and 5 block
drops.  The centre start (0.5) is shown for comparison.

Run from the repository root of an 8898cf9 checkout, with height_transition_cost.py
next to this script:
    PYTHONPATH=. python -B <this script>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import build_world, initial_state, top  # noqa: E402
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput


def first_landing(drop, start_z):
    columns = (list(range(63, 64 + drop)), [63], [63])
    world = build_world(columns)
    physics_world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
    _, state = initial_state(world, (.5, top(columns, 0), start_z))
    left_ground = False
    for tick in range(1, 80):
        state = step(state, TickInput(1.0, 0.0, False, False, False, 0.0),
                     physics_world, JAVA_1_21_RULESET).next_state
        if not state.on_ground:
            left_ground = True
        elif left_ground:
            return tick
    return None


for drop in (1, 2, 3, 5):
    print(f"drop {drop}: first landing after {first_landing(drop, .85)} ticks from the entry stance "
          f"(from the block centre: {first_landing(drop, .5)})")
print("Fabric action window reported in 12.2 for 1/2/3/5 blocks: 13/14/16/20 ticks")
