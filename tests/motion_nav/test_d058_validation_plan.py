"""D058-A freezes route validation proof metadata without changing execution."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    SurfacePlanningRequest,
    astar_surface_plan,
    build_surface_graph,
)
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationPlan,
    DependencyOwner,
    DependencyOwnerKind,
    DependencyProvenance,
    WalkValidationQueryKind,
    replay_walk_validation_recipe,
)
from mc2p.motion_nav.support_surfaces import (
    StandablePointResult,
    query_standable_connection,
)
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldQueryCache,
    WorldSessionId,
)
from tests.motion_nav.test_b07_step_transition import (
    frame,
    profile as step_profile,
)
from tests.motion_nav.test_b07_surface_planning import (
    flat_surface_world,
    ordinary_profile,
)


def _candidate(world, start, goal, *, request_id="d058-route", goal_state=None):
    graph = build_surface_graph(
        world.view(), KnownMapBounds(0, 4, 0, 2, 0, 4, True),
        ordinary_profile(), step_profile(),
    )
    nodes = {
        (node.node_id.column_x, node.node_id.column_z): node
        for node in graph.nodes
    }
    request = SurfacePlanningRequest(
        1, request_id, "goal", 1, world.session.value,
        nodes[start].node_id, nodes[goal].node_id,
        goal_state=goal_state,
    )
    return request, astar_surface_plan(graph, request)


def _admit(world, request, candidate, position):
    result = RouteAdmitter().admit_surface(
        candidate, frame(world, 2, position),
        expected_request_id=request.request_id,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        changed_cells=(),
    )
    if result.status is not AdmissionStatus.ACCEPTED:
        raise AssertionError(result)
    return result.route


class D058ValidationPlanTests(unittest.TestCase):
    def test_plain_surface_route_records_each_leg_and_shared_provenance(self):
        world = flat_surface_world(5)
        request, candidate = _candidate(world, (1, 1), (3, 1))

        route = _admit(world, request, candidate, candidate.path[0].position)
        plan = route.validation_plan

        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.action_plans), 1)
        action_plan = plan.action_plans[0]
        self.assertEqual(action_plan.action_index, 0)
        self.assertEqual(len(action_plan.legs), 2)
        self.assertEqual(
            tuple(leg.start_point_index for leg in action_plan.legs),
            (0, 1),
        )
        self.assertEqual(
            tuple(leg.end_point_index for leg in action_plan.legs),
            (1, 2),
        )
        self.assertAlmostEqual(action_plan.legs[0].start_progress_blocks, 0.0)
        self.assertAlmostEqual(action_plan.legs[-1].end_progress_blocks, 2.0)
        self.assertEqual(
            {recipe.query_kind for recipe in plan.recipes},
            {WalkValidationQueryKind.SURFACE_EDGE},
        )
        self.assertTrue(all(
            recipe.ground_profile == ordinary_profile()
            and recipe.capability.profile_id == ordinary_profile().profile_id
            for recipe in plan.recipes
        ))
        shared = [item for item in plan.dependency_provenance
                  if len(item.owner_refs) > 1]
        self.assertTrue(shared)
        owners = {owner.owner_id: owner for owner in plan.owners}
        self.assertTrue(any(
            all(owners[ref].kind is DependencyOwnerKind.WALK_LEG
                for ref in item.owner_refs)
            for item in shared
        ))

    def test_d057_skip_records_standable_initial_connection_not_old_edge(self):
        world = flat_surface_world(5)
        request, candidate = _candidate(world, (1, 1), (3, 1), request_id="d058-skip")
        start = candidate.path[0].position
        position = (start[0] + .451, start[1], start[2])

        route = _admit(world, request, candidate, position)
        plan = route.validation_plan

        self.assertIsNotNone(plan.initial_connection)
        initial_owner = plan.owner(plan.initial_connection.owner_ref)
        self.assertIs(initial_owner.kind, DependencyOwnerKind.INITIAL_CONNECTION)
        initial_recipe = plan.recipe(initial_owner.recipe_ref)
        self.assertIs(
            initial_recipe.query_kind,
            WalkValidationQueryKind.STANDABLE_CONNECTION,
        )
        self.assertEqual(initial_recipe.standable_connection.connection_from, position)
        self.assertEqual(
            initial_recipe.standable_connection.position,
            candidate.path[1].position,
        )
        action_plan = plan.action_plans[0]
        self.assertEqual(len(action_plan.legs), 1)
        self.assertEqual(action_plan.legs[0].start_point_index, 1)
        self.assertAlmostEqual(
            action_plan.legs[0].start_progress_blocks,
            route.connection_length_blocks,
        )
        old_start = candidate.path[0].surface
        self.assertFalse(any(
            recipe.surface_edge is not None
            and recipe.surface_edge.start_surface == old_start
            for recipe in plan.recipes
        ))

    def test_standable_tail_and_unmappable_tail_have_distinct_metadata(self):
        world = flat_surface_world(4)
        goal = GoalState(
            Aabb(2.72, .99, 1.52, 2.92, 1.01, 1.72),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        request, candidate = _candidate(
            world, (1, 1), (2, 1), request_id="d058-tail", goal_state=goal,
        )
        route = _admit(world, request, candidate, candidate.path[0].position)
        plan = route.validation_plan
        tail = [recipe for recipe in plan.recipes
                if recipe.query_kind is WalkValidationQueryKind.STANDABLE_CONNECTION]
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0].standable_connection.position,
                         tuple((
                             route.action_route.actions[-1].fixed_route.points[-1].x,
                             route.action_route.actions[-1].fixed_route.points[-1].y,
                             route.action_route.actions[-1].fixed_route.points[-1].z,
                         )))

        blocked = StandablePointResult(QueryStatus.BLOCKED)
        with patch(
            "mc2p.motion_nav.route_admission.query_standable_connection",
            return_value=blocked,
        ):
            unmappable = _admit(
                world, request, candidate, candidate.path[0].position,
            )
        unmappable_plan = unmappable.validation_plan
        self.assertFalse(any(
            recipe.query_kind is WalkValidationQueryKind.STANDABLE_CONNECTION
            for recipe in unmappable_plan.recipes
        ))
        self.assertTrue(any(
            owner.kind is DependencyOwnerKind.NON_RECIPE
            for owner in unmappable_plan.owners
        ))

    def test_walk_step_walk_keeps_two_action_plans_and_strict_owner(self):
        session = WorldSessionId("d058-walk-step-walk")
        world = WorldKnowledge(session)
        observed = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
        world.confirm_air(observed, tuple(
            (x, y, z)
            for x in range(-1, 5)
            for y in range(-2, 5)
            for z in range(-1, 2)
        ))
        slab = BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            (Aabb(0, 0, 0, 1, .5, 1),),
        )
        world.observe_blocks(observed, {
            (0, 0, 0): slab,
            (1, 0, 0): slab,
            (2, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (3, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        request, candidate = _candidate(
            world, (0, 0), (3, 0), request_id="d058-mixed",
        )

        route = _admit(world, request, candidate, candidate.path[0].position)
        plan = route.validation_plan

        self.assertEqual(
            tuple(action.action_index for action in plan.action_plans),
            (0, 2),
        )
        self.assertTrue(any(
            owner.kind is DependencyOwnerKind.STRICT_ACTION
            and owner.action_index == 1
            for owner in plan.owners
        ))
        self.assertEqual(
            tuple(recipe.query_kind for recipe in plan.recipes),
            (WalkValidationQueryKind.SURFACE_EDGE,
             WalkValidationQueryKind.SURFACE_EDGE),
        )

    def test_plan_rejects_dangling_owner_and_query_kind_rewrite(self):
        world = flat_surface_world(4)
        request, candidate = _candidate(world, (1, 1), (2, 1))
        route = _admit(world, request, candidate, candidate.path[0].position)
        plan = route.validation_plan

        with self.assertRaises(ContractViolation):
            replace(
                plan,
                dependency_provenance=(
                    DependencyProvenance((0, 0, 0), ("missing-owner",)),
                ),
            )
        recipe = plan.recipes[0]
        with self.assertRaises(ContractViolation):
            replace(
                recipe,
                query_kind=WalkValidationQueryKind.STANDABLE_CONNECTION,
            )

    def test_query_kind_dispatch_never_substitutes_the_other_geometry(self):
        world = flat_surface_world(4)
        request, candidate = _candidate(world, (1, 1), (2, 1))
        route = _admit(world, request, candidate, candidate.path[0].position)
        recipe = route.validation_plan.recipes[0]
        expected = (QueryStatus.FEASIBLE, recipe.dependencies)

        with patch(
            "mc2p.motion_nav.route_validation.query_surface_walk_edge",
            return_value=expected,
        ) as surface, patch(
            "mc2p.motion_nav.route_validation.query_standable_connection",
            side_effect=AssertionError("standable query must not replace surface edge"),
        ) as standable:
            self.assertEqual(
                replay_walk_validation_recipe(recipe, world.view()), expected,
            )
        surface.assert_called_once()
        standable.assert_not_called()

        request, candidate = _candidate(
            world, (1, 1), (3, 1), request_id="d058-dispatch",
        )
        start = candidate.path[0].position
        position = (start[0] + .451, start[1], start[2])
        route = _admit(world, request, candidate, position)
        plan = route.validation_plan
        owner = plan.owner(plan.initial_connection.owner_ref)
        recipe = plan.recipe(owner.recipe_ref)
        original = query_standable_connection(
            world.view(),
            recipe.standable_connection.surface,
            recipe.standable_connection.position,
            recipe.standable_connection.connection_from,
        )
        with patch(
            "mc2p.motion_nav.route_validation.query_surface_walk_edge",
            side_effect=AssertionError("surface query must not replace standable"),
        ) as surface, patch(
            "mc2p.motion_nav.route_validation.query_standable_connection",
            return_value=original,
        ) as standable:
            self.assertEqual(
                replay_walk_validation_recipe(recipe, world.view()),
                (original.status, original.dependencies),
            )
        surface.assert_not_called()
        standable.assert_called_once()

    def test_surface_recipe_replays_the_planner_edge_dependencies_exactly(self):
        world = flat_surface_world(4)
        request, candidate = _candidate(world, (1, 1), (2, 1))
        route = _admit(world, request, candidate, candidate.path[0].position)
        recipe = route.validation_plan.recipes[0]

        replayed = replay_walk_validation_recipe(recipe, world.view())

        self.assertEqual(
            replayed,
            (QueryStatus.FEASIBLE, candidate.segments[0].dependencies),
        )

    def test_standable_recipe_matches_cached_and_uncached_original_query(self):
        world = flat_surface_world(5)
        request, candidate = _candidate(
            world, (1, 1), (3, 1), request_id="d058-cache",
        )
        start = candidate.path[0].position
        position = (start[0] + .451, start[1], start[2])
        route = _admit(world, request, candidate, position)
        plan = route.validation_plan
        owner = plan.owner(plan.initial_connection.owner_ref)
        recipe = plan.recipe(owner.recipe_ref)
        args = recipe.standable_connection
        view = world.view()

        uncached = query_standable_connection(
            view, args.surface, args.position, args.connection_from,
            body_width=args.body_width_blocks,
            body_height=args.body_height_blocks,
        )
        cached = query_standable_connection(
            view, args.surface, args.position, args.connection_from,
            body_width=args.body_width_blocks,
            body_height=args.body_height_blocks,
            query_cache=WorldQueryCache(view),
        )

        self.assertEqual(cached, uncached)
        self.assertEqual(
            replay_walk_validation_recipe(recipe, view),
            (uncached.status, uncached.dependencies),
        )

    def test_nonempty_plan_exactly_covers_final_route_dependencies(self):
        world = flat_surface_world(4)
        request, candidate = _candidate(
            world, (1, 1), (2, 1), request_id="d058-exact",
        )
        route = _admit(world, request, candidate, candidate.path[0].position)
        plan = route.validation_plan
        owner = next(
            item for item in plan.owners
            if item.kind is DependencyOwnerKind.WALK_LEG
        )
        recipe = plan.recipe(owner.recipe_ref)

        unavailable = replace(route, validation_plan=None)
        self.assertIsNone(unavailable.validation_plan)
        self.assertTrue(unavailable.action_route.dependencies)

        with self.subTest(case="non-none-empty-plan"):
            with self.assertRaises(ContractViolation):
                replace(
                    route,
                    validation_plan=ActiveRouteValidationPlan(
                        (), (), (), None, (),
                    ),
                )

        with self.subTest(case="extra-provenance"):
            extra = DependencyProvenance((99, 99, 99), (owner.owner_id,))
            with self.assertRaises(ContractViolation):
                bad = replace(
                    plan,
                    dependency_provenance=tuple(sorted(
                        (*plan.dependency_provenance, extra),
                        key=lambda item: item.position,
                    )),
                )
                replace(route, validation_plan=bad)

        with self.subTest(case="missing-provenance"):
            missing = recipe.dependencies[0]
            shorter_recipe = replace(
                recipe,
                dependencies=tuple(
                    position for position in recipe.dependencies
                    if position != missing
                ),
            )
            shorter_provenance = []
            for item in plan.dependency_provenance:
                if item.position != missing:
                    shorter_provenance.append(item)
                    continue
                refs = tuple(
                    ref for ref in item.owner_refs if ref != owner.owner_id
                )
                if refs:
                    shorter_provenance.append(replace(item, owner_refs=refs))
            with self.assertRaises(ContractViolation):
                bad = replace(
                    plan,
                    recipes=tuple(
                        shorter_recipe if item.recipe_id == recipe.recipe_id else item
                        for item in plan.recipes
                    ),
                    dependency_provenance=tuple(shorter_provenance),
                )
                replace(route, validation_plan=bad)

    def test_recipe_owner_cannot_claim_a_strict_action_cell(self):
        session = WorldSessionId("d058-strict-provenance")
        world = WorldKnowledge(session)
        observed = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
        world.confirm_air(observed, tuple(
            (x, y, z)
            for x in range(-1, 5)
            for y in range(-2, 5)
            for z in range(-1, 2)
        ))
        slab = BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            (Aabb(0, 0, 0, 1, .5, 1),),
        )
        world.observe_blocks(observed, {
            (0, 0, 0): slab,
            (1, 0, 0): slab,
            (2, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (3, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        request, candidate = _candidate(
            world, (0, 0), (3, 0), request_id="d058-strict-owner",
        )
        route = _admit(world, request, candidate, candidate.path[0].position)
        plan = route.validation_plan
        recipe_owner = next(
            owner for owner in plan.owners
            if owner.kind is DependencyOwnerKind.WALK_LEG
        )
        strict_owner = next(
            owner for owner in plan.owners
            if owner.kind is DependencyOwnerKind.STRICT_ACTION
        )
        strict_cell = next(
            item.position for item in plan.dependency_provenance
            if strict_owner.owner_id in item.owner_refs
            and recipe_owner.owner_id not in item.owner_refs
        )
        provenance = tuple(
            replace(
                item,
                owner_refs=tuple(sorted((*item.owner_refs, recipe_owner.owner_id))),
            ) if item.position == strict_cell else item
            for item in plan.dependency_provenance
        )

        with self.assertRaises(ContractViolation):
            bad = replace(plan, dependency_provenance=provenance)
            replace(route, validation_plan=bad)

    def test_leg_and_initial_owner_cardinality_is_exact(self):
        world = flat_surface_world(5)
        request, candidate = _candidate(
            world, (1, 1), (3, 1), request_id="d058-cardinality",
        )
        start = candidate.path[0].position
        position = (start[0] + .451, start[1], start[2])
        route = _admit(world, request, candidate, position)
        plan = route.validation_plan

        with self.subTest(case="duplicate-leg"):
            action = plan.action_plans[0]
            with self.assertRaises(ContractViolation):
                duplicate = replace(action, legs=(action.legs[0], *action.legs))
                bad = replace(plan, action_plans=(duplicate,))
                replace(route, validation_plan=bad)

        initial_owner = plan.owner(plan.initial_connection.owner_ref)
        with self.subTest(case="duplicate-initial-owner"):
            duplicate_owner = DependencyOwner(
                f"{initial_owner.owner_id}/duplicate",
                DependencyOwnerKind.INITIAL_CONNECTION,
                initial_owner.action_index,
                initial_owner.fixed_route_id,
                None,
            )
            provenance = tuple(
                replace(
                    item,
                    owner_refs=tuple(sorted((*item.owner_refs,
                                             duplicate_owner.owner_id))),
                ) if item.position in route.connection_dependencies else item
                for item in plan.dependency_provenance
            )
            with self.assertRaises(ContractViolation):
                bad = replace(
                    plan,
                    owners=tuple(sorted(
                        (*plan.owners, duplicate_owner),
                        key=lambda item: item.owner_id,
                    )),
                    dependency_provenance=provenance,
                )
                replace(route, validation_plan=bad)

        with self.subTest(case="missing-initial-owner"):
            recipe_ref = initial_owner.recipe_ref
            owners = tuple(
                owner for owner in plan.owners
                if owner.owner_id != initial_owner.owner_id
            )
            recipes = tuple(
                recipe for recipe in plan.recipes
                if recipe.recipe_id != recipe_ref
            )
            provenance = []
            for item in plan.dependency_provenance:
                refs = tuple(
                    ref for ref in item.owner_refs
                    if ref != initial_owner.owner_id
                )
                if refs:
                    provenance.append(replace(item, owner_refs=refs))
            with self.assertRaises(ContractViolation):
                bad = replace(
                    plan,
                    recipes=recipes,
                    owners=owners,
                    initial_connection=None,
                    dependency_provenance=tuple(provenance),
                )
                replace(route, validation_plan=bad)


if __name__ == "__main__":
    unittest.main()
