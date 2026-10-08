"""F2-S keeps completion proof ownership tied to the real route endpoint."""
from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import (
    ActionRoute,
    ControlledDropSegment,
    JumpGapSegment,
    JumpUpSegment,
    StepSegment,
    WalkSegment,
)
from mc2p.motion_nav.controlled_drop import ControlledDropEdge
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.ground_traversal import (
    GroundTraversalStatus,
    verify_ground_traversal,
)
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.jump_up import JumpUpEdge
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationPlan,
    DependencyOwner,
    DependencyOwnerKind,
    DependencyProvenance,
    GroundCapabilityIdentity,
    StandableRegionQueryArgs,
    WalkValidationQueryKind,
    WalkValidationRecipe,
)
from mc2p.motion_nav.step_transition import StepEdge
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion,
    SupportSurface,
    SurfaceNodeId,
)
from mc2p.motion_nav.world_model import Aabb
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_ground_traversal import traversal_fixture


class CompletionOwnerContractTests(unittest.TestCase):
    dependencies = ((0, 63, 0), (1, 64, 0))

    @staticmethod
    def _surface(node: SurfaceNodeId) -> SupportSurface:
        return SupportSurface(
            node,
            (node.column_x + .5, float(node.vertical_band), node.column_z + .5),
            HorizontalRegion(
                float(node.column_x), float(node.column_z),
                float(node.column_x + 1), float(node.column_z + 1),
            ),
            1.,
            ("minecraft:stone",),
            CompletionOwnerContractTests.dependencies,
        )

    def _action(self, kind: str):
        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_y = 64 if kind == "jump_gap" else 62 if kind == "controlled_drop" else 65
        end_x = 2 if kind == "jump_gap" else 1
        end_id = SurfaceNodeId(end_x, 0, end_y, 0)
        start = self._surface(start_id)
        end = self._surface(end_id)
        if kind == "step":
            action = StepSegment(
                StepEdge(start_id, end_id, "step", "up", .5, self.dependencies),
                start, end, self.dependencies,
            )
        elif kind == "jump_up":
            action = JumpUpSegment(
                JumpUpEdge(
                    (0, 64, 0), (1, 65, 0), "jump-up", (1, 0),
                    .8, self.dependencies,
                ),
                self.dependencies,
            )
        elif kind == "jump_gap":
            action = JumpGapSegment(
                JumpGapEdge(start_id, end_id, "jump-gap", .9, self.dependencies),
                start, end, self.dependencies,
            )
        else:
            action = ControlledDropSegment(
                ControlledDropEdge(
                    start_id, end_id, "controlled-drop", 1., self.dependencies,
                ),
                start, end, self.dependencies,
            )
        return action, start, end

    def _route(self, kind: str) -> ActiveRoute:
        action, _, end = self._action(kind)
        prefix = WalkSegment(
            FixedRoute(
                "completion-prefix",
                (RoutePoint(-.5, 64., .5), RoutePoint(.5, 64., .5)),
            ),
            (SurfaceNodeId(-1, 0, 64, 0), SurfaceNodeId(0, 0, 64, 0)),
            self.dependencies,
        )
        goal_region = Aabb(-1., 60., -1., 4., 66.5, 2.)
        bounds = Aabb(
            end.position[0] - .2, end.position[1] - .05, end.position[2] - .2,
            end.position[0] + .2, end.position[1] + .05, end.position[2] + .2,
        )
        completion = GroundCompletionRegion(
            bounds,
            end.position,
            end.position[1],
            (
                end.node_id.column_x,
                end.node_id.column_z,
                end.node_id.vertical_band,
                end.node_id.surface_index,
            ),
            self.dependencies,
        )
        args = StandableRegionQueryArgs(
            end, goal_region, (0.5, 64., .5), completion,
        )
        profile = ordinary_profile()
        recipe = WalkValidationRecipe(
            "completion-recipe",
            WalkValidationQueryKind.STANDABLE_REGION,
            None,
            None,
            profile,
            GroundCapabilityIdentity.from_profile(profile),
            self.dependencies,
            args,
        )
        owners = (
            DependencyOwner(
                "completion-owner", DependencyOwnerKind.COMPLETION_REGION,
                1, None, recipe.recipe_id,
            ),
            DependencyOwner(
                "prefix-owner", DependencyOwnerKind.NON_RECIPE,
                0, None, None,
            ),
            DependencyOwner(
                "strict-owner", DependencyOwnerKind.STRICT_ACTION,
                1, None, None,
            ),
        )
        provenance = tuple(
            DependencyProvenance(
                position,
                tuple(owner.owner_id for owner in owners),
            )
            for position in self.dependencies
        )
        plan = ActiveRouteValidationPlan(
            (), (recipe,), owners, None, provenance,
        )
        goal = GoalState(
            goal_region,
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        action_route = ActionRoute(
            f"completion-{kind}", (prefix, action), goal_state=goal,
        )
        return ActiveRoute(
            action_route.route_id,
            1,
            "completion-request",
            "completion-goal",
            1,
            "completion-world",
            None,
            2.,
            0.,
            (),
            ExecutableCorridor(
                (SurfaceNodeId(0, 0, 64, 0), end.node_id),
                self.dependencies,
                2.,
                end.node_id,
            ),
            action_route,
            goal_state=goal,
            validation_plan=plan,
        )

    @staticmethod
    def _replace_completion_recipe(route: ActiveRoute, mutate):
        plan = route.validation_plan
        recipe = plan.recipe("completion-recipe")
        changed = mutate(recipe)
        return replace(
            route,
            validation_plan=replace(plan, recipes=(changed,)),
        )

    def test_completion_owner_rejects_wrong_action_index_for_every_strict_action(self):
        for kind in ("step", "jump_up", "jump_gap", "controlled_drop"):
            route = self._route(kind)
            plan = route.validation_plan
            owners = tuple(
                replace(owner, action_index=0)
                if owner.kind is DependencyOwnerKind.COMPLETION_REGION
                else owner
                for owner in plan.owners
            )
            with self.subTest(kind=kind), self.assertRaises(ContractViolation):
                replace(route, validation_plan=replace(plan, owners=owners))

    def test_completion_owner_rejects_start_surface_for_every_strict_action(self):
        for kind in ("step", "jump_up", "jump_gap", "controlled_drop"):
            route = self._route(kind)
            _, start, _ = self._action(kind)
            start_region = GroundCompletionRegion(
                Aabb(.3, 63.95, .3, .7, 64.05, .7),
                start.position,
                start.position[1],
                (start.node_id.column_x, start.node_id.column_z,
                 start.node_id.vertical_band, start.node_id.surface_index),
                self.dependencies,
            )
            with self.subTest(kind=kind), self.assertRaises(ContractViolation):
                self._replace_completion_recipe(
                    route,
                    lambda recipe: replace(
                        recipe,
                        standable_region=replace(
                            recipe.standable_region,
                            surface=start,
                            expected_region=start_region,
                        ),
                    ),
                )

    def test_completion_owner_rejects_wrong_surface_identity_and_height(self):
        for kind in ("step", "jump_up", "jump_gap", "controlled_drop"):
            route = self._route(kind)
            recipe = route.validation_plan.recipe("completion-recipe")
            expected = recipe.standable_region.expected_region
            wrong_identity = replace(
                expected,
                surface_identity=(99, 99, 99, 0),
            )
            wrong_height = GroundCompletionRegion(
                Aabb(
                    expected.bounds.min_x, expected.bounds.min_y + .25,
                    expected.bounds.min_z, expected.bounds.max_x,
                    expected.bounds.max_y + .25, expected.bounds.max_z,
                ),
                (expected.reference_point[0], expected.reference_point[1] + .25,
                 expected.reference_point[2]),
                expected.support_height + .25,
                expected.surface_identity,
                expected.dependencies,
            )
            for case, changed in (
                ("identity", wrong_identity),
                ("height", wrong_height),
            ):
                with self.subTest(kind=kind, case=case), self.assertRaises(
                    ContractViolation
                ):
                    self._replace_completion_recipe(
                        route,
                        lambda value, changed=changed: replace(
                            value,
                            standable_region=replace(
                                value.standable_region,
                                expected_region=changed,
                            ),
                        ),
                    )

    def test_completion_owner_rejects_same_position_and_height_with_wrong_node_identity(self):
        route = self._route("controlled_drop")
        recipe = route.validation_plan.recipe("completion-recipe")
        query = recipe.standable_region
        expected = query.expected_region
        wrong_node = replace(
            query.surface.node_id,
            surface_index=query.surface.node_id.surface_index + 17,
        )
        wrong_surface = replace(query.surface, node_id=wrong_node)
        wrong_region = replace(
            expected,
            surface_identity=(
                wrong_node.column_x,
                wrong_node.column_z,
                wrong_node.vertical_band,
                wrong_node.surface_index,
            ),
        )
        self.assertEqual(wrong_surface.position, query.surface.position)
        self.assertEqual(wrong_region.support_height, expected.support_height)
        with self.assertRaises(ContractViolation):
            self._replace_completion_recipe(
                route,
                lambda value: replace(
                    value,
                    standable_region=replace(
                        value.standable_region,
                        surface=wrong_surface,
                        expected_region=wrong_region,
                    ),
                ),
            )

    def test_ground_traversal_completion_rejects_same_xyz_and_height_with_wrong_node(self):
        state, fixed_route, world = traversal_fixture()
        nodes = (
            SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(0, 1, 15, 0),
            SurfaceNodeId(0, 2, 2, 0),
        )
        verified = verify_ground_traversal(
            state,
            fixed_route,
            world,
            ordinary_profile(),
            maximum_ticks=80,
            surface_node_path=nodes,
        )
        self.assertIs(verified.status, GroundTraversalStatus.VERIFIED)
        proof = verified.plan
        assert proof is not None
        terminal = proof.route.points[-1]
        terminal_surface = SupportSurface(
            nodes[-1],
            (terminal.x, terminal.y, terminal.z),
            HorizontalRegion(0., 2., 1., 3.),
            1.,
            ("minecraft:stone",),
            proof.dependencies,
        )
        goal_region = Aabb(0., 1.9, 2., 1., 2.1, 3.)
        completion = GroundCompletionRegion(
            Aabb(.3, 1.95, 2.3, .7, 2.05, 2.7),
            terminal_surface.position,
            terminal_surface.position[1],
            (
                nodes[-1].column_x,
                nodes[-1].column_z,
                nodes[-1].vertical_band,
                nodes[-1].surface_index,
            ),
            proof.dependencies,
        )
        query = StandableRegionQueryArgs(
            terminal_surface,
            goal_region,
            (
                proof.route.points[0].x,
                proof.route.points[0].y,
                proof.route.points[0].z,
            ),
            completion,
        )
        profile = ordinary_profile()
        recipe = WalkValidationRecipe(
            "traversal-completion-recipe",
            WalkValidationQueryKind.STANDABLE_REGION,
            None,
            None,
            profile,
            GroundCapabilityIdentity.from_profile(profile),
            proof.dependencies,
            query,
        )
        owner = DependencyOwner(
            "traversal-completion-owner",
            DependencyOwnerKind.COMPLETION_REGION,
            0,
            None,
            recipe.recipe_id,
        )
        validation = ActiveRouteValidationPlan(
            (),
            (recipe,),
            (owner,),
            None,
            tuple(
                DependencyProvenance(position, (owner.owner_id,))
                for position in proof.dependencies
            ),
        )
        goal = GoalState(
            goal_region,
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        action = WalkSegment(
            proof.route,
            nodes,
            proof.dependencies,
            traversal_plan=proof,
        )
        action_route = ActionRoute(
            "ground-traversal-completion",
            (action,),
            goal_state=goal,
        )
        wrong_node = replace(
            nodes[-1],
            surface_index=nodes[-1].surface_index + 1,
        )
        wrong_surface = replace(terminal_surface, node_id=wrong_node)
        wrong_completion = replace(
            completion,
            surface_identity=(
                wrong_node.column_x,
                wrong_node.column_z,
                wrong_node.vertical_band,
                wrong_node.surface_index,
            ),
        )
        wrong_recipe = replace(
            recipe,
            standable_region=replace(
                query,
                surface=wrong_surface,
                expected_region=wrong_completion,
            ),
        )
        self.assertEqual(wrong_surface.position, terminal_surface.position)
        self.assertEqual(
            wrong_completion.support_height,
            completion.support_height,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "strict traversal endpoint",
        ):
            ActiveRoute(
                action_route.route_id,
                1,
                "traversal-request",
                "traversal-goal",
                1,
                "traversal-world",
                None,
                2.,
                0.,
                (),
                ExecutableCorridor(
                    nodes,
                    proof.dependencies,
                    2.,
                    nodes[-1],
                ),
                action_route,
                goal_state=goal,
                validation_plan=replace(
                    validation,
                    recipes=(wrong_recipe,),
                ),
            )


if __name__ == "__main__":
    unittest.main()
