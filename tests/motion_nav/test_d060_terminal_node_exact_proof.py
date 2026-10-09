"""D060 proves an equality terminal without keeping selection-only facts."""
from __future__ import annotations

from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    SurfacePlanningRequest,
    SurfacePlanningStatus,
    astar_surface_plan,
    build_surface_graph,
)
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.route_admission import (
    ActiveRouteTracker,
    AdmissionStatus,
    RouteAdmitter,
)
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationDisposition,
    ActiveRouteValidationReason,
    DependencyOwnerKind,
    WalkValidationQueryKind,
)
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.runtime_adapter import (
    NavigationObservationAdapter,
    TEST_ORACLE,
)
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.support_surfaces import StandablePointResult
from mc2p.motion_nav.support_surfaces import StandableRegionResult
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    CellKnowledge,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)
from tests.motion_nav.test_b07_step_transition import (
    frame,
    profile as step_profile,
)
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import (
    _InlinePlanner,
    _ground_anchor,
    _source,
)
from tests.observation_v3_fixtures import valid_snapshot_v3


_CASES = (
    (9, 9.926195412950081, 5, 6.284452425686156, 10),
    (11, 10.82132191238611, 6, 7.167072772663142, 11),
    (13, 11.803689974112187, 7, 8.025439477531426, 12),
    (15, 12.797093391526154, 8, 8.88380618239971, 13),
)


def _equal_region(position, dependencies):
    """Freeze equality of the reference and graph node for D060's proof case."""
    x, y, z = position
    completion = GroundCompletionRegion(
        Aabb(x-.1, y-.01, z-.1, x+.1, y+.01, z+.1), position, y,
        (0, math.floor(z), math.floor(y), 0), ())
    return StandableRegionResult(QueryStatus.FEASIBLE, completion, dependencies)


def _goal(target_z: float, *, feet_y: float = -60.0) -> GoalState:
    half = 2.5 / math.sqrt(2.0)
    return GoalState(
        Aabb(
            .5 - half,
            feet_y - .1,
            target_z - half,
            .5 + half,
            feet_y + .1,
            target_z + half,
        ),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _world(revision: int, unknown_z: int) -> WorldKnowledge:
    session = WorldSessionId(f"d060-terminal-equality-{revision}")
    world = WorldKnowledge(session)
    observed = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
    world.observe_blocks(observed, {
        (0, -61, z): BlockGeometry.full_cube("minecraft:stone")
        for z in range(16)
    })
    world.confirm_air(observed, tuple(
        (0, y, z)
        for z in range(unknown_z)
        for y in (-60, -59)
    ))
    return world


def _admit_equal_terminal(
    revision: int,
    target_z: float,
    start_z: int,
    admission_z: float,
    unknown_z: int,
):
    world = _world(revision, unknown_z)
    goal = _goal(target_z)
    goal_node = SurfaceNodeId(0, unknown_z - 1, -60, 0)
    graph = build_surface_graph(
        world.view(),
        KnownMapBounds(
            0, 0, -60, -60, start_z, goal_node.column_z, False,
        ),
        ordinary_profile(),
        step_profile(),
    )
    request = SurfacePlanningRequest(
        revision,
        f"d060-request-{revision}",
        "d060-goal",
        revision,
        world.session.value,
        SurfaceNodeId(0, start_z, -60, 0),
        goal_node,
        goal_state=goal,
    )
    candidate = astar_surface_plan(graph, request)
    with patch(
        "mc2p.motion_nav.route_admission.standable_region_in_goal",
        return_value=_equal_region(
            candidate.path[-1].position,
            dependencies=(
                (0, -60, unknown_z),
                (0, -59, unknown_z),
            ),
        ),
    ):
        admitted = RouteAdmitter().admit_surface(
            candidate,
            frame(world, revision + 20, (.5, -60, admission_z)),
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )
    if admitted.status is not AdmissionStatus.ACCEPTED:
        raise AssertionError(admitted)
    return world, candidate, admitted.route, (0, -60, unknown_z)


def _snapshot(sequence: int, z: float):
    snapshot = valid_snapshot_v3(sequence=sequence)
    own = replace(
        snapshot.self_state.value,
        position=Vec3V0(.5, 64.0, z),
        velocity=Vec3V0(0.0, 0.0, 0.0),
        is_on_ground=True,
    )
    return replace(
        snapshot,
        self_state=replace(snapshot.self_state, value=own),
        position=replace(snapshot.position, value=own.position),
        is_on_ground=replace(snapshot.is_on_ground, value=True),
    )


class D060TerminalNodeExactProofTests(unittest.TestCase):
    @staticmethod
    def _terminal_recipe(route):
        action = route.action_route.actions[-1]
        endpoint = action.fixed_route.points[-1]
        position = endpoint.x, endpoint.y, endpoint.z
        return next(
            recipe for recipe in route.validation_plan.recipes
            if recipe.query_kind
                is WalkValidationQueryKind.STANDABLE_CONNECTION
            and recipe.standable_connection.position == position
        )

    def test_four_equal_terminals_bind_exact_last_leg_and_drop_selection_only(self):
        identities = []
        for revision, target_z, start_z, admission_z, unknown_z in _CASES:
            with self.subTest(revision=revision):
                world, candidate, route, selection_only = (
                    _admit_equal_terminal(
                        revision,
                        target_z,
                        start_z,
                        admission_z,
                        unknown_z,
                    )
                )
                identities.append((route.route_id, route.route_revision))
                action_index = len(route.action_route.actions) - 1
                action = route.action_route.actions[action_index]
                points = action.fixed_route.points
                self.assertEqual(
                    (points[-1].x, points[-1].y, points[-1].z),
                    candidate.path[-1].position,
                )

                terminals = tuple(
                    recipe for recipe in route.validation_plan.recipes
                    if recipe.query_kind
                        is WalkValidationQueryKind.STANDABLE_CONNECTION
                    and recipe.standable_connection.position
                        == candidate.path[-1].position
                )
                self.assertEqual(len(terminals), 1)
                terminal = terminals[0]
                self.assertEqual(
                    terminal.standable_connection.connection_from,
                    (points[-2].x, points[-2].y, points[-2].z),
                )
                action_plan = next(
                    plan for plan in route.validation_plan.action_plans
                    if plan.action_index == action_index
                )
                last_leg = next(
                    leg for leg in action_plan.legs
                    if leg.recipe_ref == terminal.recipe_id
                )
                owner = route.validation_plan.owner(last_leg.owner_ref)
                self.assertEqual(owner.action_index, action_index)
                self.assertEqual(owner.fixed_route_id, action.fixed_route.route_id)
                self.assertEqual(last_leg.start_point_index, len(points) - 2)
                self.assertEqual(last_leg.end_point_index, len(points) - 1)

                self.assertNotIn(selection_only, candidate.dependencies)
                self.assertNotIn(
                    selection_only,
                    candidate.path[-1].dependencies,
                )
                self.assertNotIn(selection_only, action.dependencies)
                self.assertNotIn(selection_only, route.corridor.dependencies)
                self.assertNotIn(
                    selection_only,
                    tuple(
                        item.position
                        for item in route.validation_plan.dependency_provenance
                    ),
                )

                world.confirm_air(
                    ObservationStamp(
                        world.session,
                        revision + 100,
                        revision + 100,
                        "test-clock",
                        (revision + 100) * 50_000_000,
                    ),
                    (selection_only, (0, -59, unknown_z)),
                )
                validation = ActiveRouteTracker(route).validate(
                    world.view(),
                    (selection_only, (0, -59, unknown_z)),
                    ground_profile=ordinary_profile(),
                )
                self.assertIs(
                    validation.disposition,
                    ActiveRouteValidationDisposition.UNAFFECTED,
                )
                self.assertIs(
                    validation.reason,
                    ActiveRouteValidationReason.NO_INTERSECTION,
                )
                self.assertEqual(validation.affected_cells, ())
                self.assertEqual(validation.queries_used, 0)

        self.assertEqual(len(set(identities)), 4)

    def test_body_connection_with_one_graph_node_uses_exact_terminal_proof(self):
        world = _world(21, 10)
        goal = _goal(9.926195412950081)
        goal_node = SurfaceNodeId(0, 9, -60, 0)
        graph = build_surface_graph(
            world.view(),
            KnownMapBounds(0, 0, -60, -60, 9, 9, False),
            ordinary_profile(),
            step_profile(),
        )
        request = SurfacePlanningRequest(
            21,
            "d060-one-node-request",
            "d060-goal",
            21,
            world.session.value,
            goal_node,
            goal_node,
            goal_state=goal,
        )
        candidate = astar_surface_plan(graph, request)
        candidate = replace(
            candidate,
            status=SurfacePlanningStatus.COMPLETE,
            total_cost_seconds=.05,
            total_cost_ticks=1,
        )
        with patch(
            "mc2p.motion_nav.route_admission.standable_region_in_goal",
            return_value=_equal_region(
                candidate.path[-1].position,
                dependencies=((0, -60, 10), (0, -59, 10)),
            ),
        ):
            admitted = RouteAdmitter().admit_surface(
                candidate,
                frame(world, 22, (.5, -60, 9.1)),
                expected_request_id=request.request_id,
                goal_id=request.goal_id,
                goal_revision=request.goal_revision,
                changed_cells=(),
            )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        route = admitted.route
        selection_only = (0, -60, 10)
        self.assertEqual(len(route.action_route.actions[-1].node_ids), 1)
        self.assertNotIn(selection_only, route.action_route.actions[-1].dependencies)
        self.assertFalse(any(
            item.position == selection_only
            for item in route.validation_plan.dependency_provenance
        ))
        self.assertTrue(any(
            recipe.query_kind is WalkValidationQueryKind.STANDABLE_CONNECTION
            and recipe.standable_connection.position
                == candidate.path[-1].position
            for recipe in route.validation_plan.recipes
        ))

    def test_equal_terminal_direct_query_blocked_stays_non_recipe(self):
        with patch(
            "mc2p.motion_nav.route_admission.query_standable_connection",
            return_value=StandablePointResult(QueryStatus.BLOCKED),
        ):
            _, _, route, selection_only = _admit_equal_terminal(*_CASES[0])

        self.assertIn(
            selection_only,
            route.action_route.actions[-1].dependencies,
        )
        provenance = next(
            item for item in route.validation_plan.dependency_provenance
            if item.position == selection_only
        )
        self.assertIn(
            DependencyOwnerKind.NON_RECIPE,
            tuple(
                route.validation_plan.owner(ref).kind
                for ref in provenance.owner_refs
            ),
        )
        self.assertFalse(any(
            recipe.query_kind is WalkValidationQueryKind.STANDABLE_CONNECTION
            and recipe.standable_connection.position
                == tuple((
                    route.action_route.actions[-1].fixed_route.points[-1].x,
                    route.action_route.actions[-1].fixed_route.points[-1].y,
                    route.action_route.actions[-1].fixed_route.points[-1].z,
                ))
            for recipe in route.validation_plan.recipes
        ))

    def test_equal_terminal_consumers_exclude_selection_and_keep_exact_proof(self):
        adapter = NavigationObservationAdapter()
        session = NavigationSession(
            "d060-consumer-session",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.attach_observation_adapter(adapter)
        initial = session.ingest(_snapshot(1, 6.284452425686156))
        blocks = {
            (0, 63, z): BlockGeometry.full_cube("minecraft:stone")
            for z in range(16)
        }
        air = tuple(
            (0, y, z)
            for z in range(10)
            for y in (64, 65)
        )
        initial = adapter.seed_test_oracle_memory(TEST_ORACLE, blocks, air)
        session.bind_source(_source())
        try:
            with patch.object(
                NavigationSession,
                "_surface_for_goal",
                return_value=(SurfaceNodeId(0, 9, 64, 0), ()),
            ), patch(
                "mc2p.motion_nav.route_admission.standable_region_in_goal",
                return_value=_equal_region(
                    (.5, 64.0, 9.5),
                    dependencies=((0, 64, 10), (0, 65, 10)),
                ),
            ):
                session.start_goal(
                    "d060-goal", 1,
                    _goal(9.926195412950081, feet_y=64.0),
                    initial,
                )
                proposal = session.propose(
                    initial,
                    _ground_anchor(initial),
                    2_000_000_000,
                    input_ledger=InputApplicationLedger(),
                )
            self.assertIs(
                proposal.report.state,
                NavigationSessionState.EXECUTING,
            )
            route = session.active_route
            selection_only = (0, 64, 10)
            terminal = self._terminal_recipe(route)
            effective = ActiveRouteTracker(route).effective_dependencies
            self.assertNotIn(selection_only, effective)
            self.assertTrue(set(terminal.dependencies).issubset(effective))
            self.assertNotIn(
                selection_only,
                proposal.control_frame.observation_request.air_positions,
            )

            protected = []
            original = WorldKnowledge.set_protection

            def record(owner, center, dependencies):
                protected.append(dependencies)
                return original(owner, center, dependencies)

            with patch.object(WorldKnowledge, "set_protection", new=record):
                session.ingest(_snapshot(2, 6.284452425686156))

            self.assertTrue(protected)
            self.assertNotIn(selection_only, protected[-1])
            self.assertTrue(
                set(terminal.dependencies).issubset(protected[-1])
            )
        finally:
            session.close()

    def _assert_selected_terminal_change_stops_without_forward(
        self,
        *,
        kind: str,
    ) -> None:
        world, candidate, route, _ = _admit_equal_terminal(*_CASES[0])
        terminal = self._terminal_recipe(route)
        if kind == "support":
            changed = next(
                position for position in terminal.dependencies
                if world.view().cell(position).knowledge
                    is CellKnowledge.BLOCK
            )
            world.confirm_air(
                ObservationStamp(
                    world.session, 80, 80, "test-clock", 4_000_000_000,
                ),
                (changed,),
            )
        else:
            changed = next(
                position for position in terminal.dependencies
                if world.view().cell(position).knowledge
                    is CellKnowledge.AIR
            )
            world.observe_blocks(
                ObservationStamp(
                    world.session, 80, 80, "test-clock", 4_000_000_000,
                ),
                {changed: BlockGeometry.full_cube("minecraft:stone")},
            )
        point = route.action_route.actions[0].fixed_route.points[0]
        initial = frame(world, 79, (point.x, point.y, point.z))
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, initial)
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        self.assertTrue(supervisor.offer_route(
            RouteControl(route, executor),
            initial,
            ledger,
            _ground_anchor(initial),
        ))
        current = replace(
            frame(world, 80, (point.x, point.y, point.z)),
            changed_cells=(changed,),
        )

        advance = supervisor.advance_body(
            current,
            ledger,
            _ground_anchor(current),
        )

        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertFalse(advance.route_advance.decision.movement.forward)

    def test_equal_terminal_support_removal_stops_without_forward(self):
        self._assert_selected_terminal_change_stops_without_forward(
            kind="support",
        )

    def test_equal_terminal_clearance_block_stops_without_forward(self):
        self._assert_selected_terminal_change_stops_without_forward(
            kind="clearance",
        )


if __name__ == "__main__":
    unittest.main()
