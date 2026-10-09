"""D059 freezes and fixes terminal-selection dependencies on formal routes."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route_executor import ActionRouteState
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    KnownMapSnapshotBuilder,
    SurfacePlanningRequest,
    astar_surface_plan,
    build_surface_graph,
    plan_known_surface_snapshot,
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
from mc2p.motion_nav.retry_ledger import RetryCause
from mc2p.motion_nav.route_admission import (
    ActiveRouteTracker,
    AdmissionStatus,
    CorridorStatus,
    RouteAdmitter,
)
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationDisposition,
    ActiveRouteValidationReason,
    DependencyOwnerKind,
    WalkValidationQueryKind,
)
from mc2p.motion_nav.support_surfaces import (
    StandablePointResult,
    SurfaceNodeId,
    standable_point_in_region,
)
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
from tests.motion_nav.test_b07_support_surfaces import surface_world
from tests.motion_nav.test_navigation_session import (
    _InlinePlanner,
    _ground_anchor,
    _source,
)
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_b10_gap_solver import fixture as gap_fixture
from tests.motion_nav.test_d058_validation_plan import _admit, _candidate


_ROWS = (1, 2, 3, 4)
_OBSERVATION_SEQUENCES = (45, 56, 62, 73)


def _world_with_unselected_unknowns() -> WorldKnowledge:
    session = WorldSessionId("d059-terminal-selection")
    world = WorldKnowledge(session)
    unknown = {(3, 1, z) for z in _ROWS}
    observed = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
    world.confirm_air(observed, tuple(
        (x, y, z)
        for x in range(-1, 6)
        for y in range(-1, 4)
        for z in range(-1, 7)
        if (x, y, z) not in unknown
    ))
    world.observe_blocks(observed, {
        (x, 0, z): BlockGeometry.full_cube("minecraft:stone")
        for x in range(3)
        for z in range(6)
    })
    return world


def _goal_for_row(row: int) -> GoalState:
    return GoalState(
        Aabb(2.2, .99, row + .2, 5.8, 1.01, row + .8),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _admit_large_goal_route(
    world: WorldKnowledge,
    *,
    row: int,
    goal_revision: int,
):
    graph = build_surface_graph(
        world.view(),
        KnownMapBounds(0, 4, 0, 2, 0, 5, True),
        ordinary_profile(),
        step_profile(),
    )
    nodes = {
        (node.node_id.column_x, node.node_id.column_z): node
        for node in graph.nodes
    }
    goal = _goal_for_row(row)
    request = SurfacePlanningRequest(
        goal_revision,
        f"d059-route-{goal_revision}",
        "d059-goal",
        goal_revision,
        world.session.value,
        nodes[(0, row)].node_id,
        nodes[(2, row)].node_id,
        goal_state=goal,
    )
    candidate = astar_surface_plan(graph, request)
    admitted = RouteAdmitter().admit_surface(
        candidate,
        frame(world, goal_revision + 1, candidate.path[0].position),
        expected_request_id=request.request_id,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        changed_cells=(),
    )
    if admitted.status is not AdmissionStatus.ACCEPTED:
        raise AssertionError(admitted)
    return candidate, admitted.route


def _assert_non_recipe_stop(
    case: unittest.TestCase,
    world: WorldKnowledge,
    route,
    changed,
    *,
    reason: ActiveRouteValidationReason = (
        ActiveRouteValidationReason.NON_RECIPE_OWNER_CHANGED
    ),
) -> None:
    provenance = next(
        item for item in route.validation_plan.dependency_provenance
        if item.position == changed
    )
    case.assertIn(
        DependencyOwnerKind.NON_RECIPE,
        tuple(
            route.validation_plan.owner(ref).kind
            for ref in provenance.owner_refs
        ),
    )
    stopped = ActiveRouteTracker(route).validate(
        world.view(),
        (changed,),
        ground_profile=ordinary_profile(),
    )
    case.assertIs(
        stopped.disposition,
        ActiveRouteValidationDisposition.STOP,
    )
    case.assertIs(
        stopped.reason,
        reason,
    )


class D059TerminalSelectionDependencyTests(unittest.TestCase):
    def test_exact_terminal_proof_removes_only_unselected_goal_dependencies(self):
        world = _world_with_unselected_unknowns()
        candidate, route = _admit_large_goal_route(
            world,
            row=1,
            goal_revision=1,
        )
        plan = route.validation_plan
        unknown = (3, 1, 1)

        self.assertEqual(
            world.view().cell(unknown).knowledge,
            CellKnowledge.UNKNOWN,
        )
        self.assertEqual(candidate.path[-1].position, (2.5, 1, 1.5))
        selected = route.action_route.actions[-1].fixed_route.points[-1]
        self.assertEqual((selected.x, selected.y, selected.z), (2.45, 1, 1.5))
        exact = tuple(
            recipe for recipe in plan.recipes
            if recipe.query_kind
                is WalkValidationQueryKind.STANDABLE_CONNECTION
        )
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0].standable_connection.position, (2.45, 1, 1.5))
        self.assertNotIn(unknown, exact[0].dependencies)
        self.assertNotIn(unknown, candidate.dependencies)
        self.assertIsNone(plan.initial_connection)
        self.assertTrue(any(
            recipe.query_kind is WalkValidationQueryKind.SURFACE_EDGE
            for recipe in plan.recipes
        ))
        self.assertTrue(all(
            unknown not in recipe.dependencies
            for recipe in plan.recipes
            if recipe.query_kind is WalkValidationQueryKind.SURFACE_EDGE
        ))
        terminal_selection = standable_point_in_region(
            world.view(),
            candidate.path[-1].surface,
            candidate.goal_state.region,
            connection_from=candidate.path[-1].position,
        )
        self.assertIs(terminal_selection.status, QueryStatus.FEASIBLE)
        self.assertIn(unknown, terminal_selection.dependencies)

        self.assertNotIn(
            unknown,
            route.action_route.actions[-1].dependencies,
        )
        self.assertNotIn(unknown, route.corridor.dependencies)
        self.assertNotIn(
            unknown,
            tuple(item.position for item in plan.dependency_provenance),
        )
        self.assertTrue(
            set(exact[0].dependencies).issubset(
                route.action_route.actions[-1].dependencies
            )
        )
        self.assertTrue(
            set(exact[0].dependencies).issubset(route.corridor.dependencies)
        )
        preterminal_execution = (
            set(route.connection_dependencies)
            | {
                position
                for node in candidate.path
                for position in node.dependencies
            }
            | {
                position
                for edge in candidate.segments
                for position in edge.dependencies
            }
        )
        expected_execution = (
            preterminal_execution | set(exact[0].dependencies)
            | set(route.action_route.actions[-1].fixed_route.execution_contract.completion_region.dependencies)
        )
        self.assertEqual(
            set(route.action_route.actions[-1].dependencies),
            expected_execution,
        )
        self.assertEqual(set(route.corridor.dependencies), expected_execution)

        world.confirm_air(
            ObservationStamp(
                world.session,
                45,
                45,
                "test-clock",
                2_250_000_000,
            ),
            (unknown,),
        )
        validation = ActiveRouteTracker(route).validate(
            world.view(),
            (unknown,),
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

    def test_four_new_routes_classify_each_late_air_as_selection_only(self):
        world = _world_with_unselected_unknowns()
        identities = []
        classifications = []

        for revision, row, sequence in zip(
            range(1, 5),
            _ROWS,
            _OBSERVATION_SEQUENCES,
        ):
            with self.subTest(goal_revision=revision, row=row):
                candidate, route = _admit_large_goal_route(
                    world,
                    row=row,
                    goal_revision=revision,
                )
                plan = route.validation_plan
                changed = (3, 1, row)
                exact_dependencies = {
                    position
                    for recipe in plan.recipes
                    if recipe.query_kind
                        is WalkValidationQueryKind.STANDABLE_CONNECTION
                    for position in recipe.dependencies
                }
                preterminal_dependencies = {
                    position
                    for recipe in plan.recipes
                    if recipe.query_kind is WalkValidationQueryKind.SURFACE_EDGE
                    for position in recipe.dependencies
                }
                self.assertIsNone(plan.initial_connection)
                self.assertNotIn(changed, candidate.dependencies)
                self.assertTrue(any(
                    recipe.query_kind
                        is WalkValidationQueryKind.STANDABLE_CONNECTION
                    for recipe in plan.recipes
                ))
                self.assertTrue(all(
                    changed not in recipe.dependencies
                    for recipe in plan.recipes
                    if recipe.query_kind is WalkValidationQueryKind.SURFACE_EDGE
                ))
                terminal_selection = standable_point_in_region(
                    world.view(),
                    candidate.path[-1].surface,
                    candidate.goal_state.region,
                    connection_from=candidate.path[-1].position,
                )
                self.assertIn(changed, terminal_selection.dependencies)
                classification = (
                    changed in terminal_selection.dependencies,
                    changed in exact_dependencies,
                    changed in preterminal_dependencies,
                    any(
                        item.position == changed and len(item.owner_refs) > 1
                        for item in plan.dependency_provenance
                    ),
                )
                classifications.append(classification)
                identities.append((route.route_id, route.route_revision))
                self.assertNotIn(
                    changed,
                    route.action_route.actions[-1].dependencies,
                )
                self.assertNotIn(changed, route.corridor.dependencies)
                self.assertNotIn(
                    changed,
                    tuple(item.position for item in plan.dependency_provenance),
                )

                world.confirm_air(
                    ObservationStamp(
                        world.session,
                        sequence,
                        sequence,
                        "test-clock",
                        sequence * 50_000_000,
                    ),
                    (changed,),
                )
                validation = ActiveRouteTracker(route).validate(
                    world.view(),
                    (changed,),
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
                self.assertEqual(validation.identity.goal_revision, revision)

        self.assertEqual(len(set(identities)), 4)
        self.assertEqual(
            classifications,
            [(True, False, False, False)] * 4,
        )

    def test_exact_terminal_keeps_initial_and_preterminal_provenance(self):
        world = _world_with_unselected_unknowns()
        candidate, _ = _admit_large_goal_route(
            world,
            row=1,
            goal_revision=1,
        )
        start = candidate.path[0].position
        admitted = RouteAdmitter().admit_surface(
            candidate,
            frame(
                world,
                3,
                (start[0] + .451, start[1], start[2]),
            ),
            expected_request_id="d059-route-1",
            goal_id="d059-goal",
            goal_revision=1,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        route = admitted.route
        plan = route.validation_plan
        self.assertIsNotNone(plan.initial_connection)
        initial_owner = plan.owner(plan.initial_connection.owner_ref)
        self.assertIs(
            initial_owner.kind,
            DependencyOwnerKind.INITIAL_CONNECTION,
        )
        self.assertEqual(
            set(plan.dependencies_for_owner(initial_owner.owner_id)),
            set(route.connection_dependencies),
        )
        self.assertGreaterEqual(sum(
            recipe.query_kind
                is WalkValidationQueryKind.STANDABLE_CONNECTION
            for recipe in plan.recipes
        ), 2)
        self.assertNotIn((3, 1, 1), route.corridor.dependencies)

    def test_prefix_corridor_does_not_import_terminal_dependencies(self):
        world = _world_with_unselected_unknowns()
        candidate, _ = _admit_large_goal_route(
            world,
            row=4,
            goal_revision=4,
        )
        admitted = RouteAdmitter(
            maximum_corridor_blocks=1.1,
        ).admit_surface(
            candidate,
            frame(world, 5, candidate.path[0].position),
            expected_request_id="d059-route-4",
            goal_id="d059-goal",
            goal_revision=4,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        route = admitted.route
        self.assertNotEqual(
            route.corridor.stop_node,
            candidate.path[-1].node_id,
        )
        self.assertNotIn((3, 1, 4), route.corridor.dependencies)
        exact = next(
            recipe for recipe in route.validation_plan.recipes
            if recipe.query_kind
                is WalkValidationQueryKind.STANDABLE_CONNECTION
        )
        exact_only = next(
            position for position in exact.dependencies
            if position[0] == 2
        )
        self.assertNotIn(exact_only, route.corridor.dependencies)

        terminal = ActiveRouteTracker(
            route,
            candidate,
            maximum_corridor_blocks=1.1,
        ).update(route.fixed_route_length_blocks)
        self.assertIs(terminal.status, CorridorStatus.READY)
        self.assertEqual(
            terminal.route.corridor.stop_node,
            candidate.path[-1].node_id,
        )
        self.assertNotIn(
            (3, 1, 4), terminal.route.corridor.dependencies,
        )
        self.assertIn(exact_only, terminal.route.corridor.dependencies)

    def test_preterminal_non_recipe_shared_with_exact_proof_still_stops(self):
        world = _world_with_unselected_unknowns()
        _, route = _admit_large_goal_route(
            world,
            row=1,
            goal_revision=1,
        )
        plan = route.validation_plan
        shared = next(
            item.position
            for item in plan.dependency_provenance
            if {
                DependencyOwnerKind.WALK_LEG,
                DependencyOwnerKind.NON_RECIPE,
            }.issubset({
                plan.owner(ref).kind for ref in item.owner_refs
            })
            and any(
                plan.owner(ref).recipe_ref is not None
                and plan.recipe(plan.owner(ref).recipe_ref).query_kind
                    is WalkValidationQueryKind.STANDABLE_CONNECTION
                for ref in item.owner_refs
            )
        )
        world.observe_blocks(
            ObservationStamp(
                world.session, 45, 45, "test-clock", 2_250_000_000,
            ),
            {shared: BlockGeometry.full_cube("minecraft:stone")},
        )

        _assert_non_recipe_stop(self, world, route, shared)

    def test_strict_and_exact_shared_dependency_still_stops(self):
        session = WorldSessionId("d059-strict-exact")
        world = WorldKnowledge(session)
        observed = ObservationStamp(
            session, 1, 1, "test-clock", 50_000_000,
        )
        world.confirm_air(observed, tuple(
            (x, y, z)
            for x in range(-1, 5)
            for y in range(-2, 5)
            for z in range(-1, 2)
        ))
        slab = BlockGeometry(
            "minecraft:smooth_stone_slab",
            "boxes",
            (Aabb(0, 0, 0, 1, .5, 1),),
        )
        world.observe_blocks(observed, {
            (0, 0, 0): slab,
            (1, 0, 0): slab,
            (2, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (3, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        goal = GoalState(
            Aabb(3.54, .99, .49, 3.56, 1.01, .51),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        request, candidate = _candidate(
            world,
            (0, 0),
            (3, 0),
            request_id="d059-strict-exact",
            goal_state=goal,
        )
        route = _admit(
            world,
            request,
            candidate,
            candidate.path[0].position,
        )
        plan = route.validation_plan
        shared = next(
            item.position
            for item in plan.dependency_provenance
            if {
                DependencyOwnerKind.WALK_LEG,
                DependencyOwnerKind.STRICT_ACTION,
            }.issubset({
                plan.owner(ref).kind for ref in item.owner_refs
            })
            and any(
                plan.owner(ref).recipe_ref is not None
                and plan.recipe(plan.owner(ref).recipe_ref).query_kind
                    is WalkValidationQueryKind.STANDABLE_CONNECTION
                for ref in item.owner_refs
            )
        )
        world.observe_blocks(
            ObservationStamp(
                session, 3, 3, "test-clock", 150_000_000,
            ),
            {shared: BlockGeometry.full_cube("minecraft:stone")},
        )
        stopped = ActiveRouteTracker(route).validate(
            world.view(),
            (shared,),
            ground_profile=ordinary_profile(),
        )

        self.assertIs(
            stopped.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertIs(
            stopped.reason,
            ActiveRouteValidationReason.STRICT_OWNER_CHANGED,
        )

    def test_no_exact_proof_keeps_selection_and_feasible_tail_gets_recipe(self):
        with self.subTest(branch="second-direct-query-blocked"):
            world = _world_with_unselected_unknowns()
            request, candidate = _candidate(
                world,
                (0, 1),
                (2, 1),
                request_id="d059-no-proof-blocked",
                goal_state=_goal_for_row(1),
            )
            with patch(
                "mc2p.motion_nav.route_admission.query_standable_connection",
                return_value=StandablePointResult(QueryStatus.BLOCKED),
            ):
                route = _admit(
                    world,
                    request,
                    candidate,
                    candidate.path[0].position,
                )
            changed = (3, 1, 1)
            world.confirm_air(
                ObservationStamp(
                    world.session, 45, 45, "test-clock", 2_250_000_000,
                ),
                (changed,),
            )
            _assert_non_recipe_stop(self, world, route, changed)

        with self.subTest(branch="ground-traversal-tail"):
            selection_only = (3, 5, 0)
            world = surface_world({
                (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
                (1, 0, 0): BlockGeometry(
                    "minecraft:smooth_stone_slab",
                    "boxes",
                    (Aabb(0, 0, 0, 1, .5, 1),),
                ),
                (2, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            })
            world.confirm_air(
                ObservationStamp(
                    world.session, 2, 2, "test-clock", 100_000_000,
                ),
                tuple(
                    (x, y, z)
                    for x in (2, 3)
                    for y in range(-2, 6)
                    for z in range(-1, 2)
                    if (x, y, z) not in {(2, 0, 0), selection_only}
                ),
            )
            snapshot = KnownMapSnapshotBuilder(
                world.view(),
                KnownMapBounds(0, 2, 0, 1, 0, 0, True),
            ).advance(world.view(), 10_000).snapshot
            anchor, _, _, _ = gap_fixture()
            entry = replace(
                anchor.physics_state,
                session=world.session,
                movement_tick_id=0,
                position=(.5, 1.0, .5),
                velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
                yaw_radians=-1.5707963267948966,
            )
            request = SurfacePlanningRequest(
                1,
                "d059-no-proof-ground",
                "d059-goal",
                1,
                world.session.value,
                SurfaceNodeId(0, 0, 1, 0),
                SurfaceNodeId(2, 0, 1, 0),
                entry_physics_state=entry,
            )
            candidate = plan_known_surface_snapshot(
                snapshot,
                ordinary_profile(),
                replace(step_profile(), cost_seconds=.8),
                request,
            )
            self.assertTrue(candidate.ground_traversal_plans)
            goal = GoalState(
                Aabb(2.74, .99, .49, 2.76, 1.01, .51),
                GoalSupport.SOLID,
                frozenset({MovementMode.WALK}),
                frozenset({"standing"}),
                .6,
            )
            candidate = replace(candidate, goal_state=goal)
            admitted = RouteAdmitter().admit_surface(
                candidate,
                frame(world, 3, (.5, 1.0, .5)),
                expected_request_id=request.request_id,
                goal_id=request.goal_id,
                goal_revision=request.goal_revision,
                changed_cells=(),
            )
            self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
            route = admitted.route
            tail_recipes = tuple(
                recipe for recipe in route.validation_plan.recipes
                if recipe.query_kind
                    is WalkValidationQueryKind.STANDABLE_CONNECTION
            )
            self.assertTrue(tail_recipes)
            self.assertNotIn(selection_only, route.action_route.dependencies)
            self.assertNotIn(selection_only, route.corridor.dependencies)
            self.assertFalse(any(
                item.position == selection_only
                for item in route.validation_plan.dependency_provenance
            ))
            world.confirm_air(
                ObservationStamp(
                    world.session, 4, 4, "test-clock", 200_000_000,
                ),
                (selection_only,),
            )
            unaffected = ActiveRouteTracker(route).validate(
                world.view(),
                (selection_only,),
                ground_profile=ordinary_profile(),
            )
            self.assertIs(
                unaffected.disposition,
                ActiveRouteValidationDisposition.UNAFFECTED,
            )
            self.assertIs(
                unaffected.reason,
                ActiveRouteValidationReason.NO_INTERSECTION,
            )
            self.assertEqual(unaffected.queries_used, 0)

            tail_support = (2, 0, 0)
            self.assertTrue(any(
                tail_support in recipe.dependencies for recipe in tail_recipes
            ))
            world.confirm_air(
                ObservationStamp(
                    world.session, 5, 5, "test-clock", 250_000_000,
                ),
                (tail_support,),
            )
            stopped = ActiveRouteTracker(route).validate(
                world.view(),
                (tail_support,),
                ground_profile=ordinary_profile(),
            )
            self.assertIs(
                stopped.disposition,
                ActiveRouteValidationDisposition.STOP,
            )

    def test_formal_session_ignores_each_selection_only_change(self):
        world = _world_with_unselected_unknowns()
        planner = _InlinePlanner()
        session = NavigationSession(
            "d059-selection-timeline",
            NavigationSessionProfiles(
                ordinary_profile(),
                jump_profile(),
                step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        # D063 now waits for the complete bounded goal-surface fact set.
        # This D059 lifecycle test intentionally isolates the older route
        # validation boundary, so supply the already-selected graph node and
        # leave the terminal selection dependency to RouteAdmitter.
        session._surface_for_goal = lambda frame, goal: (
            SurfaceNodeId(
                2,
                round((goal.region.min_z + goal.region.max_z) / 2.0 - .5),
                1,
                0,
            ),
            (),
        )
        session.bind_source(_source())
        ledger = InputApplicationLedger()
        body_position = (.5, 1, 1.5)
        sequence_trace = []
        route_identities = []
        work_identities = []
        written_changes = []
        consumed_changes = []

        def validation_for(route, body_validation):
            matches = tuple(
                item for item in (
                    body_validation.incumbent,
                    body_validation.pending,
                )
                if item is not None
                and item.identity.route_id == route.route_id
                and item.identity.route_revision == route.route_revision
            )
            self.assertEqual(len(matches), 1)
            return matches[0]

        try:
            initial = frame(world, 1, body_position)
            session.start_goal("d059-goal", 1, _goal_for_row(1), initial)
            self.assertEqual(session.report.goal_revision, 1)

            admitted = frame(world, 2, body_position)
            proposal = session.propose(
                admitted,
                _ground_anchor(admitted),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertIs(
                proposal.report.state,
                NavigationSessionState.EXECUTING,
            )

            for revision, row, observation_sequence in zip(
                range(1, 5),
                _ROWS,
                _OBSERVATION_SEQUENCES,
            ):
                with self.subTest(goal_revision=revision, row=row):
                    route = session.active_route
                    self.assertIsNotNone(route)
                    self.assertEqual(route.goal_revision, revision)
                    validation = session.diagnostics.route_validation
                    self.assertIsNotNone(validation)
                    route_validation = validation_for(route, validation)
                    identity = route_validation.identity
                    self.assertEqual(identity.route_id, route.route_id)
                    self.assertEqual(identity.route_revision, route.route_revision)
                    self.assertEqual(identity.goal_revision, revision)
                    self.assertEqual(
                        validation.observation_sequence_id,
                        admitted.body.sequence_id,
                    )
                    sequence_trace.append(validation.observation_sequence_id)
                    route_identities.append((route.route_id, route.route_revision))
                    work_identities.append(identity.work_identity)

                    changed = (3, 1, row)
                    self.assertIs(
                        world.view().cell(changed).knowledge,
                        CellKnowledge.UNKNOWN,
                    )
                    stamp = ObservationStamp(
                        world.session,
                        observation_sequence,
                        observation_sequence,
                        "test-clock",
                        observation_sequence * 50_000_000,
                    )
                    world.confirm_air(stamp, (changed,))
                    written_changes.append((stamp.sequence_id, changed))
                    self.assertIs(
                        world.view().cell(changed).knowledge,
                        CellKnowledge.AIR,
                    )

                    changed_frame = replace(
                        frame(world, observation_sequence, body_position),
                        changed_cells=(changed,),
                    )
                    stopped = session.propose(
                        changed_frame,
                        _ground_anchor(changed_frame),
                        2_000_000_000,
                        input_ledger=ledger,
                    )
                    diagnostics = session.diagnostics
                    validation = diagnostics.route_validation
                    self.assertIsNotNone(validation)
                    route_validation = validation_for(route, validation)
                    self.assertEqual(
                        validation.observation_sequence_id,
                        observation_sequence,
                    )
                    sequence_trace.append(validation.observation_sequence_id)
                    consumed_changes.append((
                        validation.observation_sequence_id,
                        changed,
                    ))
                    self.assertIs(
                        route_validation.disposition,
                        ActiveRouteValidationDisposition.UNAFFECTED,
                    )
                    self.assertIs(
                        route_validation.reason,
                        ActiveRouteValidationReason.NO_INTERSECTION,
                    )
                    self.assertEqual(
                        route_validation.affected_cells,
                        (),
                    )
                    self.assertEqual(route_validation.queries_used, 0)
                    self.assertIs(
                        stopped.report.state,
                        NavigationSessionState.EXECUTING,
                    )
                    self.assertEqual(
                        stopped.report.reason,
                        "tracking_fixed_route",
                    )
                    self.assertIsNotNone(stopped.route_decision)
                    self.assertIs(
                        stopped.route_decision.state,
                        ActionRouteState.RUNNING,
                    )
                    self.assertNotEqual(stopped.route_decision.movement, MovementV1())
                    self.assertEqual(diagnostics.recovery_total_starts, 0)
                    cause_counts = dict(diagnostics.retry_cause_counts)
                    self.assertTrue(all(count == 0 for count in cause_counts.values()))
                    self.assertEqual(diagnostics.illegal_transition_count, 0)
                    self.assertEqual(diagnostics.damage_spent, 0.0)

                    if revision == 4:
                        continue
                    self.assertTrue(session.update_goal(
                        "d059-goal",
                        revision + 1,
                        _goal_for_row(revision + 1),
                    ))
                    self.assertEqual(session.report.goal_revision, revision + 1)
                    admitted = frame(
                        world,
                        observation_sequence + 1,
                        body_position,
                    )
                    proposal = session.propose(
                        admitted,
                        _ground_anchor(admitted),
                        2_000_000_000,
                        input_ledger=ledger,
                    )
                    self.assertIs(
                        proposal.report.state,
                        NavigationSessionState.EXECUTING,
                    )

            self.assertEqual(written_changes, consumed_changes)
            self.assertEqual(sequence_trace, sorted(set(sequence_trace)))
            self.assertEqual(len(set(route_identities)), 4)
            self.assertEqual(len(set(work_identities)), 4)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
