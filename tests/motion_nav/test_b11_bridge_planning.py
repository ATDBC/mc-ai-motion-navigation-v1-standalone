from __future__ import annotations

import unittest

from mc2p.motion_nav.bridge_planner import (
    BridgePlacementPolicy,
    plan_next_bridge_interaction,
)
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    KnownMapSnapshotBuilder,
    SnapshotBuildStatus,
    SurfacePlanningRequest,
)
from mc2p.motion_nav.movement_transition import (
    GoalState, GoalSupport, MovementMode,
)
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.world_interaction import InteractionKind
from mc2p.motion_nav.world_model import (
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _InlinePlanner


SESSION = WorldSessionId("b11-bridge-world")
STAMP = ObservationStamp(SESSION, 1, 1, "clock", 1)


def bridge_world(*, gap=(1, 2, 3), unknown=(), detour=False):
    world = WorldKnowledge(SESSION)
    blocks = {}
    air = set()
    for x in range(-1, 6):
        for z in range(-1, 2):
            air.add((x, 62, z))
            air.add((x, 66, z))
            ground = (x, 63, z)
            if z == 0 and x in gap:
                air.add(ground)
            else:
                blocks[ground] = BlockGeometry.full_cube("minecraft:stone")
            if z == 0 or detour:
                air.add((x, 64, z))
                air.add((x, 65, z))
            else:
                blocks[(x, 64, z)] = BlockGeometry.full_cube("minecraft:stone")
                blocks[(x, 65, z)] = BlockGeometry.full_cube("minecraft:stone")
    for position in unknown:
        air.discard(position)
        blocks.pop(position, None)
    world.observe_blocks(STAMP, blocks)
    world.confirm_air(STAMP, tuple(sorted(air)))
    return world


def bridge_snapshot(*, gap=(1, 2, 3), unknown=(), detour=False):
    world = bridge_world(gap=gap, unknown=unknown, detour=detour)
    bounds = KnownMapBounds(-1, 5, 64, 64, -1, 1, True)
    builder = KnownMapSnapshotBuilder(world.view(), bounds)
    progress = builder.advance(world.view(), 10_000)
    assert progress.status is SnapshotBuildStatus.COMPLETE
    return progress.snapshot


def request():
    return SurfacePlanningRequest(
        1,
        "route-1",
        "goal-1",
        1,
        SESSION.value,
        SurfaceNodeId(0, 0, 64, 0),
        SurfaceNodeId(4, 0, 64, 0),
    )


class BridgePlanningTests(unittest.TestCase):
    @staticmethod
    def _body(x: float, *, sequence: int = 1) -> BodyState:
        stamp = ObservationStamp(SESSION, sequence, sequence, "clock", sequence)
        return BodyState(
            SESSION,
            sequence,
            stamp,
            (x, 64.0, 0.5),
            (0.0, 0.0, 0.0),
            0.0,
            0.0,
            "standing",
            Aabb(x - 0.3, 64.0, 0.2, x + 0.3, 65.8, 0.8),
            True,
            False,
            False,
        )

    def test_three_cell_gap_yields_only_the_first_confirmable_interaction(self):
        planned = plan_next_bridge_interaction(
            bridge_snapshot(), request(), BridgePlacementPolicy(maximum_blocks=3),
        )
        self.assertIsNotNone(planned)
        assert planned is not None
        self.assertEqual(planned.required_placements, 3)
        self.assertEqual(planned.work_node, SurfaceNodeId(0, 0, 64, 0))
        self.assertEqual(planned.path, tuple((x, 64, 0) for x in range(5)))
        requirement = planned.requirement
        self.assertIs(requirement.kind, InteractionKind.PLACE_BLOCK)
        self.assertEqual(requirement.support, (0, 63, 0))
        self.assertEqual(requirement.face, "east")
        self.assertEqual(requirement.destination, (1, 63, 0))
        # Even at the endpoint tolerance's near edge, the player's centre is
        # already beyond the clicked east face.  Otherwise the top face can
        # hide the side face and aiming never converges.
        self.assertEqual(requirement.work_position, (1.12, 64.0, 0.5))
        self.assertTrue(requirement.requires_sneak)
        self.assertEqual(requirement.work_position_tolerance, 0.08)

    def test_confirmed_first_block_moves_the_next_requirement_forward(self):
        planned = plan_next_bridge_interaction(
            bridge_snapshot(gap=(2, 3)), request(),
            BridgePlacementPolicy(maximum_blocks=3),
        )
        self.assertIsNotNone(planned)
        assert planned is not None
        self.assertEqual(planned.required_placements, 2)
        self.assertEqual(planned.work_node, SurfaceNodeId(1, 0, 64, 0))
        self.assertEqual(planned.requirement.support, (1, 63, 0))
        self.assertEqual(planned.requirement.destination, (2, 63, 0))

    def test_limit_unknown_or_existing_route_never_invents_an_interaction(self):
        self.assertIsNone(plan_next_bridge_interaction(
            bridge_snapshot(), request(), BridgePlacementPolicy(maximum_blocks=2),
        ))
        self.assertIsNone(plan_next_bridge_interaction(
            bridge_snapshot(unknown=((2, 63, 0),)), request(),
            BridgePlacementPolicy(maximum_blocks=3),
        ))
        self.assertIsNone(plan_next_bridge_interaction(
            bridge_snapshot(gap=()), request(),
            BridgePlacementPolicy(maximum_blocks=3),
        ))

    def test_known_detour_is_preferred_over_modifying_the_world(self):
        planned = plan_next_bridge_interaction(
            bridge_snapshot(gap=(1,), detour=True), request(),
            BridgePlacementPolicy(maximum_blocks=3),
        )
        self.assertIsNone(planned)

    def test_navigation_exposes_required_interaction_only_when_authorized(self):
        world = bridge_world()
        body = self._body(0.5)
        frame = NavigationFrame(SESSION, body, world.view(), "fixture")
        profiles = NavigationSessionProfiles(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        session = NavigationSession(
            "bridge-enabled-session",
            profiles,
            planner_worker=_InlinePlanner(),
            bridge_policy=BridgePlacementPolicy(maximum_blocks=3),
            clock_ns=lambda: 1,
        )
        self.addCleanup(session.close)
        session.start(request(), frame)
        for _ in range(4):
            session.propose(frame, None, 1_000_000)
            if session.report.state is NavigationSessionState.REQUIRES_INTERACTION:
                break
        self.assertEqual(
            session.report.state,
            NavigationSessionState.REQUIRES_INTERACTION,
        )
        self.assertIsNotNone(session.required_interaction)
        self.assertEqual(
            session.report.required_interaction_id,
            session.required_interaction.requirement.interaction_id,
        )

        denied = NavigationSession(
            "bridge-disabled-session",
            profiles,
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1,
        )
        self.addCleanup(denied.close)
        denied.start(request(), frame)
        for _ in range(4):
            denied.propose(frame, None, 1_000_000)
            if denied.report.terminal:
                break
        self.assertEqual(denied.report.state, NavigationSessionState.FAILED)
        self.assertIsNone(denied.required_interaction)

    def test_navigation_approaches_the_work_surface_before_releasing_interaction(self):
        world = bridge_world(gap=(2, 3))
        frame = NavigationFrame(SESSION, self._body(0.5), world.view(), "fixture")
        session = NavigationSession(
            "bridge-approach-session",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(),
            bridge_policy=BridgePlacementPolicy(maximum_blocks=3),
            clock_ns=lambda: 1,
        )
        self.addCleanup(session.close)
        session.start(request(), frame)
        for _ in range(8):
            session.propose(frame, None, 1_000_000)
            if session.report.state is NavigationSessionState.EXECUTING:
                break
        self.assertEqual(session.report.state, NavigationSessionState.EXECUTING)
        self.assertIsNotNone(session.required_interaction)
        self.assertEqual(
            session.required_interaction.work_node,
            SurfaceNodeId(1, 0, 64, 0),
        )

        arrived = NavigationFrame(
            SESSION, self._body(1.5, sequence=2), world.view(), "fixture",
        )
        for _ in range(4):
            session.propose(arrived, None, 1_000_000)
            if session.report.state is NavigationSessionState.REQUIRES_INTERACTION:
                break
        self.assertEqual(
            session.report.state,
            NavigationSessionState.REQUIRES_INTERACTION,
        )
        self.assertEqual(session.report.reason, "interaction_work_position_reached")

    def test_goal_revision_does_not_refill_the_task_bridge_budget(self):
        world = bridge_world(gap=(1,))
        frame = NavigationFrame(SESSION, self._body(0.5), world.view(), "fixture")
        session = NavigationSession(
            "bridge-budget-session",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(),
            bridge_policy=BridgePlacementPolicy(maximum_blocks=3),
            clock_ns=lambda: 1,
        )
        self.addCleanup(session.close)
        session.start(request(), frame)
        for _ in range(4):
            session.propose(frame, None, 1_000_000)
            if session.report.state is NavigationSessionState.REQUIRES_INTERACTION:
                break
        interaction = session.required_interaction
        self.assertIsNotNone(interaction)
        assert interaction is not None
        session.confirm_required_interaction(
            interaction.requirement.interaction_id,
        )
        self.assertEqual(session.bridge_remaining, 2)

        revised = GoalState(
            Aabb(3.4, 63.95, 0.4, 3.6, 64.05, 0.6),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            0.6,
        )
        session.update_goal("goal-1", 2, revised)

        self.assertEqual(session.bridge_remaining, 2)


if __name__ == "__main__":
    unittest.main()
