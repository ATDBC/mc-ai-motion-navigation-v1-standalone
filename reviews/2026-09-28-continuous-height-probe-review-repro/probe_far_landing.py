"""Round-18 review: where does LandingEdgeProbe start, and which way does it walk?

Scene (fully known, formal profile set, inline planner, closed loop with the
1.21 calculator, which models vanilla's sneak ledge clipping):
  an L-shaped stone walkway: from the start (0, 63, 0) along +z to (0, 63, 6),
  then along +x to (6, 63, 6); at its end a 3-block direct drop onto (7, 60, 6).
  Everything else is known air down to y=55, so the straight line from the
  start to the landing crosses a deep pit that the planner walks around.
  The landing body cell's air is confirmed without near, lower-part visual
  evidence, as it would be when first seen from the start.

The session is started with the landing as goal and driven for up to 50 frames.
Variant "timeout": nothing else happens until the probe times out.
Variant "goal_update": at frame 30 the same task moves its goal back to the
start of the walkway (as pursuit does when the target moves).  No
air-query results are fed back (the sensor is not simulated), so the probe can
never obtain evidence; the question is only where it drives the body first.

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
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp, WorldKnowledge
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal, _source

STONE = BlockGeometry.full_cube("minecraft:stone")
world = WorldKnowledge(SESSION)
stamp = ObservationStamp(SESSION, 0, 0, "sim", 0)
walkway = {(0, 63, z) for z in range(0, 7)} | {(x, 63, 6) for x in range(0, 7)}
blocks = {position: STONE for position in walkway} | {(7, 60, 6): STONE}
world.observe_blocks(stamp, blocks)
world.confirm_air(stamp, tuple(
    (x, y, z) for x in range(-3, 11) for y in range(55, 70) for z in range(-3, 10)
    if (x, y, z) not in blocks
))
physics_world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)

profiles = NavigationSessionProfiles.load(Path("config/motion-navigation"))


def run(variant):
    print(f"\n=== variant {variant}")
    session = NavigationSession(f"far-probe-{variant}", profiles, planner_worker=_InlinePlanner(),
                                clock_ns=lambda: 1_000_000_000)
    session.bind_source(_source())
    frame, state = initial_state(world, (.5, 64.0, .5))
    session.start_goal("task", 1, _goal((7.5, 61.0, 6.5)), frame)
    failed_at = None
    for sequence in range(1, 51):
        if variant == "goal_update" and sequence == 30:
            session.update_goal("task", 2, _goal((.5, 64.0, .5)))
        current = frame_from(state, world, sequence)
        proposal = session.propose(current, None, 2_000_000_000)
        intents = proposal.control_frame.intents if proposal.control_frame else ()
        movement = intents[0].intent.movement if intents else None
        look = intents[0].intent.look if intents else None
        probe = session._edge_probe
        if sequence in (1, 10, 20) or sequence >= 28:
            print(f"frame {sequence:>2}: {proposal.report.state.value:<17} {str(proposal.report.reason):<34} "
                  f"body ({state.position[0]:.2f}, {state.position[1]:.2f}, {state.position[2]:.2f}) "
                  f"probe {str(None if probe is None else probe.state.value):<11} "
                  f"keys f={None if movement is None else movement.forward} "
                  f"s={None if movement is None else movement.strafe} "
                  f"sneak={None if movement is None else movement.sneak} "
                  f"vx={state.velocity_blocks_per_tick[0]:+.3f} ground={state.on_ground}")
        if proposal.report.state.value in {"failed", "complete"} and failed_at is None:
            failed_at = sequence
        if failed_at is not None and sequence >= failed_at + 5:
            break
        if state.position[1] < 62.0 and sequence > 35:
            break
        if movement is None:
            continue
        if look is not None:
            state = replace(state,
                            yaw_radians=state.yaw_radians + math.radians(look.yaw_delta_degrees),
                            pitch_radians=state.pitch_radians + math.radians(look.pitch_delta_degrees))
        projected = project_movement_command(state, movement)
        calculated = step(state, projected.tick_input, physics_world, JAVA_1_21_RULESET)
        if calculated.next_state is None:
            print("calculator stopped:", calculated.status)
            break
        state = calculated.next_state
    print(f"final body ({state.position[0]:.2f}, {state.position[1]:.2f}, {state.position[2]:.2f}), "
          f"on_ground={state.on_ground}; walkway top is y=64, the pit floor is below y=55")


landing = (7.5, 6.5)
print("walkway route length from start to the ledge: 12 blocks (6 along +z, 6 along +x)")
print(f"straight-line distance from start to landing column centre: {math.dist((.5, .5), landing):.1f} blocks")
for variant in ("timeout", "goal_update"):
    run(variant)
