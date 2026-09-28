"""Round-18 review: a route with two 3-block ledges (a terraced slope).

Landing evidence is checked for every direct drop in the candidate when the
whole route is admitted, from the body's current position.  Scene (fully
known, formal profiles, inline planner, closed loop with the 1.21 calculator):
  top terrace   (0, 63, z) z = 0..2        feet 64
  mid terrace   (0, 60, z) z = 3..8        feet 61   (first 3-block ledge)
  low terrace   (0, 57, z) z = 9..10       feet 58   (second 3-block ledge)
Both landing body cells carry the most favourable evidence possible: lower part
visible, observed at frame 0 from the start, with the true nearest-point
distance from the start eye.  The goal is on the low terrace.

Run from the repository root of an 8898cf9 checkout, with height_transition_cost.py
next to this script:
    PYTHONPATH=. python -B <this script>
"""
import math
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import SESSION, frame_from, initial_state  # noqa: E402
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, VisualAirEvidence, WorldKnowledge,
)
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal, _source

STONE = BlockGeometry.full_cube("minecraft:stone")
blocks = {}
for z in range(0, 3):
    blocks[(0, 63, z)] = STONE
for z in range(3, 9):
    blocks[(0, 60, z)] = STONE
for z in range(9, 11):
    blocks[(0, 57, z)] = STONE
eye = (.5, 64.0 + 1.62, .5)


def nearest_distance(cell):
    d = [max(cell[i] - eye[i], eye[i] - cell[i] - 1, 0.0) for i in range(3)]
    return math.sqrt(sum(v * v for v in d))


world = WorldKnowledge(SESSION)
stamp = ObservationStamp(SESSION, 0, 0, "sim", 0)
world.observe_blocks(stamp, blocks)
landings = {(0, 61, 3): "first ledge", (0, 58, 9): "second ledge"}
air = tuple((x, y, z) for x in range(-3, 4) for y in range(54, 70) for z in range(-3, 14)
            if (x, y, z) not in blocks)
world.confirm_air(stamp, air, visual_evidence={
    cell: VisualAirEvidence(stamp, nearest_distance(cell), True) for cell in landings
})
for cell, label in landings.items():
    print(f"{label} landing body cell {cell}: distance from the start eye {nearest_distance(cell):.2f} "
          f"(limit 4.5), lower part visible")
physics_world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
profiles = NavigationSessionProfiles.load(Path("config/motion-navigation"))
session = NavigationSession("terrace", profiles, planner_worker=_InlinePlanner(),
                            clock_ns=lambda: 1_000_000_000)
session.bind_source(_source())
frame, state = initial_state(world, (.5, 64.0, .5))
session.start_goal("task", 1, _goal((.5, 58.0, 10.5)), frame)
failed_at = None
for sequence in range(1, 60):
    current = frame_from(state, world, sequence)
    proposal = session.propose(current, None, 2_000_000_000)
    intents = proposal.control_frame.intents if proposal.control_frame else ()
    movement = intents[0].intent.movement if intents else None
    look = intents[0].intent.look if intents else None
    probe = session._edge_probe
    if sequence in (1, 2, 5, 10, 20, 30, 40) or sequence >= 41:
        print(f"frame {sequence:>2}: {proposal.report.state.value:<17} {str(proposal.report.reason):<34} "
              f"missing {tuple(proposal.report.missing_cells)[:2]} "
              f"probe {str(None if probe is None else (probe.state.value, probe.landing_cell))} "
              f"body ({state.position[0]:.2f}, {state.position[1]:.2f}, {state.position[2]:.2f})")
    if proposal.report.state.value in {"failed", "complete"} and failed_at is None:
        failed_at = sequence
    if failed_at is not None and sequence >= failed_at + 6:
        break
    if movement is None:
        continue
    if look is not None:
        state = replace(state, yaw_radians=state.yaw_radians + math.radians(look.yaw_delta_degrees),
                        pitch_radians=state.pitch_radians + math.radians(look.pitch_delta_degrees))
    calculated = step(state, project_movement_command(state, movement).tick_input,
                      physics_world, JAVA_1_21_RULESET)
    if calculated.next_state is None:
        print("calculator stopped:", calculated.status)
        break
    state = calculated.next_state
print(f"final body ({state.position[0]:.2f}, {state.position[1]:.2f}, {state.position[2]:.2f}), "
      f"on_ground={state.on_ground}")
