"""Combat look still reaches the first frame of a Step action.

Run from the repository root of commit cba2899:

    PYTHONPATH=. python -B <this-review-branch>/reviews/2026-09-26-d034-repro/combat_look_on_step_handoff_frame.py

``MovingMeleeDriver._tick_visible_approach`` asks
``current_action_requires_route_look()`` *before* navigation decides the
frame.  ``ActionRouteExecutor`` hands a finished walk to the next action
inside the same ``decide`` call, so on the hand-off frame the pre-check still
sees the walk while the proposal already carries Step input.  The script drives
a real ``NavigationSession`` over three slabs and a stone block (Walk ->
Step), then resolves the hand-off frame with the formal arbiter together with
a combat look, as the driver would submit it.
"""
from __future__ import annotations

from dataclasses import replace

from mc2p.contracts.action_v1 import ActionIntentV1, ActionPriorityV0, LookV1
from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import Aabb, BlockGeometry
from mc2p.runtime.arbiter_v1 import ActionArbiterV1
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav.test_navigation_session import (
    NavigationSessionTests, _InlinePlanner, _goal, _known_world, _source,
)

COMBAT_LOOK_ID = "combat-pursuit-look"


def _slab() -> BlockGeometry:
    return BlockGeometry(
        "minecraft:smooth_stone_slab", "boxes", (Aabb(0, 0, 0, 1, .5, 1),),
    )


def main() -> None:
    world = _known_world({
        (-2, 0, 0): _slab(), (-1, 0, 0): _slab(), (0, 0, 0): _slab(),
        (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
    })
    start = query_support_surfaces(world.view(), -2, 0, .5, .5).surfaces[0]
    goal = query_support_surfaces(world.view(), 1, 0, 1, 1).surfaces[0]
    request = SurfacePlanningRequest(
        1, "walk-step", "walk-step-goal", 1, world.session.value,
        start.node_id, goal.node_id, goal_state=_goal(goal.position),
    )
    session = NavigationSession(
        "walk-step", NavigationSessionTests("run").profiles(),
        planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
    )
    session.bind_source(_source())
    first = frame(world, 0, start.position)
    session.start(request, first)
    session.propose(first, None, 2_000_000_000)
    print("route actions:", [
        type(action).__name__
        for action in session.active_route.action_route.actions
    ])

    # Walk at about 4 b/s, then brake onto the end of the walk segment.
    path = [(-1.5 + .2 * k, 4.0) for k in range(1, 9)]
    path += [(.2, 2.0), (.3, 1.0), (.4, .4), (.45, .1), (.5, 0.0)]
    for sequence, (x, speed) in enumerate(path, start=1):
        driver_sees_route_look = session.current_action_requires_route_look()
        current = frame(world, sequence, (x, .5, .5), velocity=(speed, 0, 0))
        proposal = session.propose(
            current, None, 2_000_000_000,
            conditioned_yaw_delta_degrees=6.0,
            conditioned_look_intent_id=COMBAT_LOOK_ID,
        )
        decision = proposal.route_decision
        action = session.active_route.action_route.actions[
            decision.action_index
        ]
        if type(action).__name__ == "WalkSegment" or driver_sees_route_look:
            continue
        nav = proposal.control_frame.intents[0].intent
        print(f"hand-off frame {sequence} (x={x}): driver pre-check "
              f"requires_route_look={driver_sees_route_look}; navigation "
              f"decided {type(action).__name__} reason={decision.reason_code}")
        print(f"  navigation intent: movement={nav.movement} look={nav.look} "
              f"requires_look={nav.movement_requires_look} "
              f"observed_yaw_limit={nav.movement_observed_yaw_limit_degrees} "
              f"conditioned_look={nav.movement_conditioned_look_intent_id}")

        # The driver builds its look before navigation proposes, so it is
        # stamped first; give navigation a later timestamp as in production.
        combat = ActionIntentV1(
            COMBAT_LOOK_ID, "combat-source", nav.episode_id,
            nav.observation_sequence_id, ActionPriorityV0.TASK,
            nav.submitted_at_monotonic_ns - 1, nav.expires_at_monotonic_ns,
            look=LookV1(6.0, 0.0), valid_for_ticks=1,
        )
        # Plain ids keep this outside the ordered-source registry; the
        # arbitration rules for movement and look are the same.
        plain_nav = replace(
            nav, intent_id="navigation-step", source_id="navigation-source",
        )
        arbiter = ActionArbiterV1()
        arbiter.submit(combat)
        arbiter.submit(plain_nav)
        result = arbiter.resolve(
            nav.submitted_at_monotonic_ns, nav.episode_id,
            nav.observation_sequence_id, 1, nav.expires_at_monotonic_ns,
        )
        print("  arbiter selected:", result.selected_intents)
        print("  dispatched:", result.action.movement, result.action.look)
        break
    session.close()


if __name__ == "__main__":
    main()
