"""D062 direct Walks must enter execution with one exact proof owner."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import math
import unittest
from unittest.mock import Mock, patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route_executor import ActionRouteState
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
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.retry_ledger import RetryCause
from mc2p.motion_nav.route_admission import (
    ActiveRouteTracker,
    AdmissionReason,
    AdmissionResult,
    AdmissionStatus,
    LocalDirectAdmissionEvidence,
    LocalDirectAdmissionPhase,
    RouteAdmitter,
)
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationDisposition,
    ActiveRouteValidationReason,
    DependencyOwnerKind,
    GroundCapabilityIdentity,
    RouteProgressEvidence,
    WalkValidationQueryKind,
    replay_walk_validation_recipe,
)
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion,
    StandablePointResult,
    SupportSurface,
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
from tests.motion_nav.test_b07_step_transition import frame, profile as step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_d060_terminal_node_exact_proof import _world
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import (
    _InlinePlanner,
    _ground_anchor,
    _source,
)


def _goal(target_z: float) -> GoalState:
    return GoalState(
        Aabb(.2, -60.01, target_z - .2, .8, -59.99, target_z + .2),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


class D062DirectWalkValidationTests(unittest.TestCase):
    @staticmethod
    def _local_request(world):
        start = SurfaceNodeId(0, 9, -60, 0)
        return SurfacePlanningRequest(
            62,
            "d062-local-request",
            "d062-local-goal",
            13,
            world.session.value,
            start,
            start,
            goal_state=_goal(9.8),
        )

    @staticmethod
    def _admit_local(world):
        profile = ordinary_profile()
        return RouteAdmitter().admit_local_direct(
            D062DirectWalkValidationTests._local_request(world),
            frame(world, 64, (.5, -60.0, 9.1)),
            ground_profile=profile,
            capability_identity=GroundCapabilityIdentity.from_profile(profile),
        )

    @staticmethod
    def _edge_overlap_request(world):
        center_z = 11.106
        half_extent = 2.5 / math.sqrt(2.0)
        goal = GoalState(
            Aabb(
                .5 - half_extent,
                -60.1,
                center_z - half_extent,
                .5 + half_extent,
                -59.9,
                center_z + half_extent,
            ),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        node = SurfaceNodeId(0, 9, -60, 0)
        return SurfacePlanningRequest(
            63,
            "d062-edge-overlap-request",
            "d062-edge-overlap-goal",
            14,
            world.session.value,
            node,
            node,
            goal_state=goal,
        )

    def test_local_direct_is_admitted_with_one_walk_leg_recipe(self):
        world = _world(62, 16)
        result = self._admit_local(world)

        self.assertIs(result.status, AdmissionStatus.ACCEPTED)
        route = result.route
        self.assertIsNotNone(route.validation_plan)
        self.assertEqual(route.connection_length_blocks, 0.0)
        self.assertIsNone(route.validation_plan.initial_connection)
        owners = route.validation_plan.owners
        self.assertEqual(len(owners), 1)
        self.assertIs(owners[0].kind, DependencyOwnerKind.WALK_LEG)
        recipe = route.validation_plan.recipe(owners[0].recipe_ref)
        self.assertIs(
            recipe.query_kind,
            WalkValidationQueryKind.STANDABLE_CONNECTION,
        )
        endpoint = route.action_route.actions[0].fixed_route.points[-1]
        self.assertEqual((endpoint.x, endpoint.y, endpoint.z), (.5, -60.0, 9.8))
        self.assertEqual(
            recipe.standable_connection.position,
            (endpoint.x, endpoint.y, endpoint.z),
        )
        self.assertEqual(
            set(route.action_route.actions[0].dependencies),
            set(recipe.dependencies),
        )
        leg = route.validation_plan.action_plans[0].legs[0]
        self.assertEqual((leg.start_point_index, leg.end_point_index), (0, 1))
        evidence = result.local_direct_evidence
        self.assertIsInstance(evidence, LocalDirectAdmissionEvidence)
        self.assertIs(evidence.phase, LocalDirectAdmissionPhase.ROUTE_BUILD)
        self.assertIs(evidence.final_status, AdmissionStatus.ACCEPTED)
        self.assertIs(evidence.final_reason, AdmissionReason.CANDIDATE_ADMITTED)
        self.assertEqual(evidence.body_surface_node, result.route.corridor.stop_node)
        self.assertEqual(evidence.selected_position, (.5, -60.0, 9.8))
        self.assertIs(evidence.selector_status, QueryStatus.FEASIBLE)
        self.assertIs(evidence.exact_status, QueryStatus.FEASIBLE)
        self.assertEqual(
            evidence.exact_dependency_count,
            len(result.route.action_route.actions[0].dependencies),
        )
        persisted = json.loads(json.dumps(asdict(evidence)))
        self.assertEqual(persisted["request_id"], "d062-local-request")
        self.assertEqual(persisted["goal_revision"], 13)
        self.assertEqual(
            persisted["goal_region"],
            asdict(self._local_request(world).goal_state.region),
        )

    def test_local_direct_evidence_covers_each_admission_phase(self):
        world = _world(626, 16)
        request = self._local_request(world)
        current = frame(world, 64, (.5, -60.0, 9.1))
        profile = ordinary_profile()
        capability = GroundCapabilityIdentity.from_profile(profile)
        start_surface = SupportSurface(
            request.start,
            (.5, -60.0, 9.5),
            HorizontalRegion(0.0, 9.0, 1.0, 10.0),
            1.0,
            ("minecraft:stone",),
            ((0, -61, 9),),
        )
        other_surface = replace(
            start_surface,
            node_id=SurfaceNodeId(0, 10, -60, 0),
        )
        endpoint = (.5, -60.0, 9.8)
        exact_dependencies = ((0, -61, 9),)

        cases = (
            (
                "request",
                dict(capability_identity=replace(
                    capability, environment_id="other-environment",
                )),
                (),
                LocalDirectAdmissionPhase.REQUEST,
                None,
            ),
            (
                "body_surface",
                dict(capability_identity=capability),
                (("RouteAdmitter._surface_for_local_body", (
                    QueryStatus.NEEDS_INFORMATION,
                    None,
                    (),
                    ((0, -59, 10),),
                )),),
                LocalDirectAdmissionPhase.BODY_SURFACE,
                QueryStatus.NEEDS_INFORMATION,
            ),
            (
                "surface_identity",
                dict(capability_identity=capability),
                (("RouteAdmitter._surface_for_local_body", (
                    QueryStatus.FEASIBLE, other_surface, (), (),
                )),),
                LocalDirectAdmissionPhase.SURFACE_IDENTITY,
                None,
            ),
            (
                "goal_selection",
                dict(capability_identity=capability),
                (("RouteAdmitter._surface_for_local_body", (
                    QueryStatus.FEASIBLE, start_surface, (), (),
                )), ("standable_point_in_region", StandablePointResult(
                    QueryStatus.BLOCKED,
                ))),
                LocalDirectAdmissionPhase.GOAL_SELECTION,
                QueryStatus.BLOCKED,
            ),
            (
                "exact_connection",
                dict(capability_identity=capability),
                (("RouteAdmitter._surface_for_local_body", (
                    QueryStatus.FEASIBLE, start_surface, (), (),
                )), ("standable_point_in_region", StandablePointResult(
                    QueryStatus.FEASIBLE, endpoint,
                )), ("query_standable_connection", StandablePointResult(
                    QueryStatus.BLOCKED,
                ))),
                LocalDirectAdmissionPhase.EXACT_CONNECTION,
                QueryStatus.BLOCKED,
            ),
            (
                "material_capability",
                dict(capability_identity=capability),
                (("RouteAdmitter._surface_for_local_body", (
                    QueryStatus.FEASIBLE, start_surface, (), (),
                )), ("standable_point_in_region", StandablePointResult(
                    QueryStatus.FEASIBLE, endpoint,
                )), ("query_standable_connection", StandablePointResult(
                    QueryStatus.FEASIBLE,
                    endpoint,
                    dependencies=exact_dependencies,
                )), ("ground_profile_allows_dependency_blocks", False)),
                LocalDirectAdmissionPhase.MATERIAL_CAPABILITY,
                QueryStatus.UNSUPPORTED,
            ),
        )
        for (name, kwargs, patches, expected_phase,
             expected_query_status) in cases:
            with self.subTest(name=name):
                contexts = [patch(
                    "mc2p.motion_nav.route_admission." + target,
                    return_value=value,
                ) for target, value in patches]
                for context in contexts:
                    context.start()
                try:
                    result = RouteAdmitter().admit_local_direct(
                        request,
                        current,
                        ground_profile=profile,
                        **kwargs,
                    )
                finally:
                    for context in reversed(contexts):
                        context.stop()

                evidence = result.local_direct_evidence
                self.assertIs(evidence.phase, expected_phase)
                self.assertIs(
                    evidence.phase_query_status,
                    expected_query_status,
                )
                self.assertIs(evidence.final_status, result.status)
                self.assertIs(evidence.final_reason, result.reason)
                self.assertEqual(
                    evidence.missing_count,
                    len(result.missing_cells),
                )
                self.assertEqual(evidence.request_id, request.request_id)
                self.assertEqual(evidence.goal_revision, request.goal_revision)
                self.assertEqual(evidence.start, request.start)
                self.assertEqual(evidence.goal, request.goal)
                self.assertEqual(evidence.goal_region, request.goal_state.region)
                self.assertEqual(evidence.body_position, current.body.position)

    def test_session_replaces_local_evidence_and_clears_it_for_graph_request(self):
        world = _world(627, 20)
        first_request = self._local_request(world)
        current = frame(world, 64, (.5, -60.0, 9.1))
        session = NavigationSession(
            "d062-local-evidence-lifecycle",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(hold_first=True),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(first_request, current)
        ledger = InputApplicationLedger()
        try:
            session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            first = session.diagnostics.local_direct_admission
            self.assertIsNotNone(first)
            self.assertEqual(first.goal_revision, 13)

            self.assertTrue(session.update_goal(
                "d062-local-goal", 14, _goal(15.8),
            ))
            next_frame = frame(world, 65, (.5, -60.0, 9.1))
            session.propose(
                next_frame,
                _ground_anchor(next_frame),
                2_050_000_000,
                input_ledger=ledger,
            )
            self.assertIsNone(session.diagnostics.local_direct_admission)
        finally:
            session.close()

    def test_local_direct_selects_standable_point_from_goal_surface_overlap(self):
        world = _world(621, 16)
        request = self._edge_overlap_request(world)
        current = frame(world, 64, (.5, -60.0, 9.338))
        profile = ordinary_profile()

        result = RouteAdmitter().admit_local_direct(
            request,
            current,
            ground_profile=profile,
            capability_identity=GroundCapabilityIdentity.from_profile(profile),
        )

        self.assertIs(
            result.status,
            AdmissionStatus.ACCEPTED,
            f"{result.status.value}/{result.reason.value}",
        )
        route = result.route
        endpoint = route.action_route.actions[0].fixed_route.points[-1]
        goal = request.goal_state.region
        self.assertLessEqual(goal.min_x, endpoint.x)
        self.assertLessEqual(endpoint.x, goal.max_x)
        self.assertLessEqual(goal.min_z, endpoint.z)
        self.assertLessEqual(endpoint.z, goal.max_z)
        surface = route.validation_plan.recipes[0].standable_connection.surface
        self.assertLessEqual(surface.region.min_x, endpoint.x)
        self.assertLessEqual(endpoint.x, surface.region.max_x)
        self.assertLessEqual(surface.region.min_z, endpoint.z)
        self.assertLessEqual(endpoint.z, surface.region.max_z)
        self.assertIs(
            route.validation_plan.recipes[0].query_kind,
            WalkValidationQueryKind.STANDABLE_CONNECTION,
        )
        recipe = route.validation_plan.recipes[0]
        self.assertEqual(
            recipe.standable_connection.position,
            (endpoint.x, endpoint.y, endpoint.z),
        )
        self.assertEqual(
            set(route.action_route.actions[0].dependencies),
            set(recipe.dependencies),
        )
        replay_status, replay_dependencies = replay_walk_validation_recipe(
            recipe,
            world.view(),
        )
        self.assertIs(replay_status, QueryStatus.FEASIBLE)
        self.assertEqual(replay_dependencies, recipe.dependencies)

    def test_local_direct_discards_selector_only_dependencies(self):
        world = _world(624, 16)
        extra = (4, -61, 4)
        exact = ((0, -61, 9),)
        world.observe_blocks(
            ObservationStamp(
                world.session, 2, 2, "test-clock", 100_000_000,
            ),
            {extra: BlockGeometry.full_cube("minecraft:dirt")},
        )
        request = self._edge_overlap_request(world)
        current = frame(world, 64, (.5, -60.0, 9.338))
        profile = ordinary_profile()
        endpoint = (.5, -60.0, 10.0)
        self.assertNotIn("minecraft:dirt", profile.support_materials)

        with patch(
            "mc2p.motion_nav.route_admission.standable_point_in_region",
            return_value=StandablePointResult(
                QueryStatus.FEASIBLE,
                endpoint,
                dependencies=tuple(sorted((extra, *exact))),
            ),
        ), patch(
            "mc2p.motion_nav.route_admission.query_standable_connection",
            return_value=StandablePointResult(
                QueryStatus.FEASIBLE,
                endpoint,
                dependencies=exact,
            ),
        ):
            result = RouteAdmitter().admit_local_direct(
                request,
                current,
                ground_profile=profile,
                capability_identity=GroundCapabilityIdentity.from_profile(
                    profile,
                ),
            )

        self.assertIs(result.status, AdmissionStatus.ACCEPTED)
        route = result.route
        action = route.action_route.actions[0]
        recipe = route.validation_plan.recipes[0]
        self.assertEqual(action.dependencies, exact)
        self.assertEqual(recipe.dependencies, exact)
        self.assertEqual(
            tuple(item.position
                  for item in route.validation_plan.dependency_provenance),
            exact,
        )
        self.assertNotIn(extra, action.dependencies)

    def test_standable_selector_skips_blocked_primary_connection(self):
        world = WorldKnowledge(WorldSessionId("d062-selector-alternative"))
        stamp = ObservationStamp(
            world.session, 1, 1, "test-clock", 50_000_000,
        )
        obstacle = (1, 1, 0)
        world.confirm_air(stamp, tuple(
            (x, y, z)
            for x in range(4)
            for y in range(1, 4)
            for z in range(4)
            if (x, y, z) != obstacle
        ))
        floor = {
            (x, 0, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(4)
            for z in range(4)
        }
        world.observe_blocks(stamp, {
            **floor,
            obstacle: BlockGeometry.full_cube("minecraft:stone"),
        })
        surface = SupportSurface(
            SurfaceNodeId(0, 0, 1, 0),
            (2.0, 1.0, 2.0),
            HorizontalRegion(0.0, 0.0, 4.0, 4.0),
            1.0,
            ("minecraft:stone",),
            tuple(sorted(floor)),
        )
        region = Aabb(.3, .9, .3, 3.7, 1.1, 3.7)

        endpoint_only = standable_point_in_region(
            world.view(), surface, region,
        )
        connected = standable_point_in_region(
            world.view(), surface, region,
            connection_from=(.5, 1.0, .5),
        )

        self.assertIs(endpoint_only.status, QueryStatus.FEASIBLE)
        self.assertEqual(endpoint_only.position, (2.0, 1.0, 2.0))
        self.assertIs(connected.status, QueryStatus.FEASIBLE)
        self.assertNotEqual(connected.position, endpoint_only.position)

    def test_session_accepts_edge_overlap_local_route_without_terminal_failure(self):
        world = _world(622, 16)
        request = self._edge_overlap_request(world)
        current = frame(world, 64, (.5, -60.0, 9.338))
        session = NavigationSession(
            "d062-edge-overlap-session",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(request, current)
        try:
            proposal = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=InputApplicationLedger(),
            )

            self.assertIs(
                proposal.report.state,
                NavigationSessionState.EXECUTING,
            )
            self.assertFalse(proposal.report.terminal)
            self.assertNotEqual(
                proposal.report.reason,
                "same_support_local_path_unavailable",
            )
            self.assertIsNotNone(session.active_route)
            self.assertTrue(session.active_route.route_id.endswith("-local"))
            self.assertIsNotNone(proposal.route_decision)
        finally:
            session.close()

    def test_pending_revision_blocks_stale_local_and_direct_activation(self):
        half_extent = 2.5 / math.sqrt(2.0)
        cases = (
            (9, 10, 11.923964938366375, 11),
            (11, 13, 14.923964938366375, 14),
        )
        for initial_goal_z, unknown_z, revised_center_z, expected_goal_z in cases:
            with self.subTest(initial_goal_z=initial_goal_z):
                world = _world(628 + initial_goal_z, unknown_z)
                current = frame(world, 64, (.5, -60.0, 9.33832512003593))
                revised_goal = GoalState(
                    Aabb(
                        .5 - half_extent,
                        -60.1,
                        revised_center_z - half_extent,
                        .5 + half_extent,
                        -59.9,
                        revised_center_z + half_extent,
                    ),
                    GoalSupport.SOLID,
                    frozenset({MovementMode.WALK}),
                    frozenset({"standing"}),
                    .6,
                )
                admitter = RouteAdmitter()
                admitter.admit_local_direct = Mock(
                    wraps=admitter.admit_local_direct,
                )
                admitter.admit_ground_direct = Mock(
                    wraps=admitter.admit_ground_direct,
                )
                session = NavigationSession(
                    f"d062-pending-{initial_goal_z}-session",
                    NavigationSessionProfiles(
                        ordinary_profile(), jump_profile(), step_profile(),
                    ),
                    planner_worker=_InlinePlanner(),
                    route_admitter=admitter,
                    clock_ns=lambda: 1_000_000_000,
                )
                session.bind_source(_source())
                goal_id = f"d062-pending-{initial_goal_z}-goal"
                session.start_goal(
                    goal_id,
                    10,
                    _goal(9.4 if initial_goal_z == 9 else 11.4),
                    current,
                    planning_policy=(
                        GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
                    ),
                )
                try:
                    initial = session._request
                    self.assertIsNotNone(initial)
                    if initial_goal_z == 9:
                        self.assertEqual(
                            session._local_goal_request_id,
                            initial.request_id,
                        )
                    else:
                        self.assertEqual(
                            session._direct_goal_request_id,
                            initial.request_id,
                        )
                    self.assertTrue(session.update_goal(
                        goal_id, 11, revised_goal,
                    ))
                    proposal = session.propose(
                        current,
                        _ground_anchor(current),
                        2_000_000_000,
                        input_ledger=InputApplicationLedger(),
                    )

                    self.assertFalse(proposal.report.terminal)
                    self.assertIs(
                        proposal.report.state,
                        NavigationSessionState.NEEDS_INFORMATION,
                    )
                    admitter.admit_local_direct.assert_not_called()
                    admitter.admit_ground_direct.assert_not_called()
                    newly_known = session.observation_request().air_positions
                    self.assertTrue(newly_known)

                    observed = ObservationStamp(
                        world.session, 2, 2, "test-clock", 100_000_000,
                    )
                    world.confirm_air(observed, newly_known)
                    updated = replace(
                        frame(
                            world, 65, (.5, -60.0, 9.33832512003593),
                        ),
                        changed_cells=newly_known,
                    )
                    resumed = session.propose(
                        updated,
                        _ground_anchor(updated),
                        2_050_000_000,
                        input_ledger=InputApplicationLedger(),
                    )

                    self.assertFalse(resumed.report.terminal, resumed.report.reason)
                    self.assertIsNone(session._pending_goal)
                    self.assertEqual(
                        session._request.goal,
                        SurfaceNodeId(0, expected_goal_z, -60, 0),
                    )
                finally:
                    session.close()

    def test_local_direct_query_failures_keep_typed_admission_reasons(self):
        expected = {
            QueryStatus.NEEDS_INFORMATION:
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
            QueryStatus.BLOCKED:
                AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
            QueryStatus.UNSUPPORTED:
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED,
        }
        for status, reason in expected.items():
            with self.subTest(status=status), patch(
                "mc2p.motion_nav.route_admission.standable_point_in_region",
                return_value=StandablePointResult(
                    status,
                    missing_cells=((0, -59, 10),)
                    if status is QueryStatus.NEEDS_INFORMATION else (),
                ),
            ) as selector:
                result = self._admit_local(_world(63, 16))
                self.assertIs(result.status, AdmissionStatus.REJECTED)
                self.assertIs(result.reason, reason)
                self.assertEqual(
                    selector.call_args.kwargs["connection_from"],
                    (.5, -60.0, 9.1),
                )

    def test_local_direct_exact_replay_failures_keep_typed_reasons(self):
        expected = {
            QueryStatus.NEEDS_INFORMATION:
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
            QueryStatus.BLOCKED:
                AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
            QueryStatus.UNSUPPORTED:
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED,
        }
        endpoint = (.5, -60.0, 9.8)
        for status, reason in expected.items():
            with self.subTest(status=status), patch(
                "mc2p.motion_nav.route_admission.standable_point_in_region",
                return_value=StandablePointResult(
                    QueryStatus.FEASIBLE,
                    endpoint,
                ),
            ), patch(
                "mc2p.motion_nav.route_admission.query_standable_connection",
                return_value=StandablePointResult(
                    status,
                    missing_cells=((0, -59, 10),)
                    if status is QueryStatus.NEEDS_INFORMATION else (),
                ),
            ) as exact_query:
                result = self._admit_local(_world(625, 16))

                self.assertIs(result.status, AdmissionStatus.REJECTED)
                self.assertIs(result.reason, reason)
                self.assertEqual(
                    exact_query.call_args.args[2],
                    endpoint,
                )
                self.assertEqual(
                    exact_query.call_args.args[3],
                    (.5, -60.0, 9.1),
                )

    def test_session_consumes_local_typed_rejections_without_geometry(self):
        expected = (
            (
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
                NavigationSessionState.NEEDS_INFORMATION,
                "same_support_local_requires_information",
            ),
            (
                AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
                NavigationSessionState.FAILED,
                "same_support_local_path_unavailable",
            ),
            (
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED,
                NavigationSessionState.FAILED,
                "same_support_local_mode_unsupported",
            ),
        )
        for admission_reason, state, session_reason in expected:
            with self.subTest(admission_reason=admission_reason):
                world = _world(65, 16)
                request = self._local_request(world)

                class RejectingAdmitter(RouteAdmitter):
                    def admit_local_direct(self, *args, **kwargs):
                        return AdmissionResult(
                            AdmissionStatus.REJECTED,
                            admission_reason,
                            missing_cells=((0, -59, 10),)
                            if admission_reason is (
                                AdmissionReason
                                .GOAL_STANDING_POINT_NEEDS_INFORMATION
                            ) else (),
                        )

                current = frame(world, 64, (.5, -60.0, 9.1))
                session = NavigationSession(
                    f"d062-session-{admission_reason.value}",
                    NavigationSessionProfiles(
                        ordinary_profile(), jump_profile(), step_profile(),
                    ),
                    planner_worker=_InlinePlanner(),
                    route_admitter=RejectingAdmitter(),
                    clock_ns=lambda: 1_000_000_000,
                )
                session.bind_source(_source())
                session.start(request, current)
                try:
                    proposal = session.propose(
                        current,
                        _ground_anchor(current),
                        2_000_000_000,
                        input_ledger=InputApplicationLedger(),
                    )
                    self.assertIs(proposal.report.state, state)
                    self.assertEqual(proposal.report.reason, session_reason)
                finally:
                    session.close()

    def test_zero_length_local_direct_settles_until_goal_speed_is_satisfied(self):
        world = _world(66, 16)
        ledger = InputApplicationLedger()
        moving = frame(
            world,
            66,
            (.5, -60.0, 9.8),
            velocity=(0.0, 0.0, 1.0),
        )
        session = NavigationSession(
            "d062-zero-length-local",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal("d062-zero-length-goal", 1, _goal(9.8), moving)
        try:
            admission = RouteAdmitter().admit_local_direct(
                self._local_request(world),
                moving,
                ground_profile=ordinary_profile(),
                capability_identity=GroundCapabilityIdentity.from_profile(
                    ordinary_profile(),
                ),
            )
            self.assertIs(admission.status, AdmissionStatus.REJECTED)
            self.assertIs(
                admission.reason,
                AdmissionReason.CANDIDATE_HAS_NO_ACTIONS,
            )
            self.assertIsNone(admission.route)
            self.assertIs(
                admission.local_direct_evidence.phase,
                LocalDirectAdmissionPhase.ROUTE_BUILD,
            )
            self.assertIs(
                admission.local_direct_evidence.exact_status,
                QueryStatus.FEASIBLE,
            )

            settling = session.propose(
                moving,
                _ground_anchor(moving),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertIs(
                settling.report.state,
                NavigationSessionState.EXECUTING,
            )
            self.assertEqual(
                settling.report.reason,
                "same_support_local_settling",
            )
            self.assertIsNone(session.active_route)
            self.assertIsNone(settling.route_decision)
            self.assertTrue(session.diagnostics.source_bound)
            self.assertEqual(session.diagnostics.controller_ids, ())
            self.assertTrue(all(
                ordered.intent.movement in (None, MovementV1())
                for ordered in settling.control_frame.intents
            ))

            stopped = frame(
                world,
                67,
                (.5, -60.0, 9.8),
                velocity=(0.0, 0.0, 0.0),
            )
            completed = session.propose(
                stopped,
                _ground_anchor(stopped),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertIs(
                completed.report.state,
                NavigationSessionState.COMPLETE,
            )
            self.assertEqual(completed.report.reason, "goal_state_satisfied")
            self.assertIsNone(session.active_route)
        finally:
            session.close()

    def test_seq64_equivalent_air_fact_admits_and_revalidates_local_route(self):
        world = _world(62, 10)
        ledger = InputApplicationLedger()
        position = (.5, -60.0, 9.1)
        initial = frame(world, 63, position)
        session = NavigationSession(
            "d062-seq64-local",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal("d062-seq64-goal", 13, _goal(9.8), initial)
        try:
            waiting = session.propose(
                initial,
                _ground_anchor(initial),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertIs(
                waiting.report.state,
                NavigationSessionState.NEEDS_INFORMATION,
            )
            self.assertIsNone(session.active_route)

            changed = ((0, -60, 10), (0, -59, 10))
            world.confirm_air(
                ObservationStamp(
                    world.session, 64, 64, "test-clock", 3_200_000_000,
                ),
                changed,
            )
            current = replace(
                frame(world, 64, position),
                changed_cells=changed,
            )
            proposal = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )

            route = session.active_route
            self.assertIsNotNone(route)
            self.assertTrue(route.route_id.endswith("-local"))
            self.assertEqual(route.goal_revision, 13)
            self.assertIs(
                proposal.report.state,
                NavigationSessionState.EXECUTING,
            )
            self.assertIsNotNone(proposal.route_decision)
            self.assertIs(
                proposal.route_decision.state,
                ActionRouteState.RUNNING,
            )
            self.assertNotEqual(proposal.route_decision.movement, MovementV1())
            validation = session.diagnostics.route_validation
            self.assertIsNotNone(validation)
            self.assertIsNotNone(validation.incumbent)
            self.assertIs(
                validation.incumbent.disposition,
                ActiveRouteValidationDisposition.CONTINUE,
            )
            self.assertIs(
                validation.incumbent.reason,
                ActiveRouteValidationReason.REVALIDATED,
            )
            self.assertEqual(validation.incumbent.affected_cells, changed)
            self.assertEqual(validation.incumbent.queries_used, 1)
            self.assertEqual(session.diagnostics.recovery_total_starts, 0)
            self.assertEqual(
                dict(session.diagnostics.retry_cause_counts).get(
                    RetryCause.DEPENDENCY,
                    0,
                ),
                0,
            )
        finally:
            session.close()

    def test_local_direct_selected_geometry_revalidates_and_stops(self):
        for kind in ("support", "clearance"):
            with self.subTest(kind=kind):
                world = _world(64, 16)
                route = self._admit_local(world).route
                recipe = route.validation_plan.recipes[0]
                if kind == "support":
                    changed = next(
                        cell for cell in
                        recipe.standable_connection.surface.dependencies
                        if world.view().cell(cell).knowledge
                            is CellKnowledge.BLOCK
                    )
                    world.confirm_air(
                        ObservationStamp(
                            world.session, 65, 65, "test-clock", 65,
                        ),
                        (changed,),
                    )
                else:
                    changed = next(
                        cell for cell in recipe.dependencies
                        if world.view().cell(cell).knowledge
                            is CellKnowledge.AIR
                    )
                    world.observe_blocks(
                        ObservationStamp(
                            world.session, 65, 65, "test-clock", 65,
                        ),
                        {changed: BlockGeometry.full_cube(
                            "minecraft:stone"
                        )},
                    )
                result = ActiveRouteTracker(route).validate(
                    world.view(),
                    (changed,),
                    ground_profile=ordinary_profile(),
                )
                self.assertIs(
                    result.disposition,
                    ActiveRouteValidationDisposition.STOP,
                )

    def test_rev16_forward_entry_reuses_only_initial_connection_owner(self):
        world = _world(16, 13)
        goal = GoalState(
            Aabb(-1.2678, -60.1, 11.2039, 2.2678, -59.9, 14.7396),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        goal_node = SurfaceNodeId(0, 12, -60, 0)
        self.assertEqual(goal_node, SurfaceNodeId(0, 12, -60, 0))
        graph = build_surface_graph(
            world.view(),
            KnownMapBounds(0, 0, -60, -60, 11, 12, False),
            ordinary_profile(),
            step_profile(),
        )
        request = SurfacePlanningRequest(
            21,
            "f1-d/straight_2_0-request-21",
            "f1-d-goal/straight_2_0",
            16,
            world.session.value,
            SurfaceNodeId(0, 11, -60, 0),
            goal_node,
            goal_state=goal,
        )
        candidate = astar_surface_plan(graph, request)

        with patch(
            "mc2p.motion_nav.route_admission.standable_point_in_region",
            return_value=StandablePointResult(
                QueryStatus.FEASIBLE,
                (.5, -60.0, 12.5),
                dependencies=((0, -59, 13),),
            ),
        ):
            result = RouteAdmitter().admit_surface(
                candidate,
                frame(world, 78, (.6516238829076266, -60.0,
                                  12.081778198429513)),
                expected_request_id=request.request_id,
                goal_id=request.goal_id,
                goal_revision=request.goal_revision,
                changed_cells=(),
            )

        self.assertIs(result.status, AdmissionStatus.ACCEPTED)
        route = result.route
        action = route.action_route.actions[0]
        self.assertEqual(len(action.node_ids), 1)
        self.assertEqual(len(action.fixed_route.points), 2)
        plan = route.validation_plan
        self.assertIsNotNone(plan.initial_connection)
        self.assertEqual(plan.action_plans, ())
        self.assertEqual(len(plan.owners), 1)
        self.assertIs(plan.owners[0].kind,
                      DependencyOwnerKind.INITIAL_CONNECTION)
        self.assertEqual(
            set(action.dependencies),
            set(plan.recipe(plan.owners[0].recipe_ref).dependencies),
        )
        self.assertNotIn((0, -59, 13), action.dependencies)
        self.assertEqual(
            plan.initial_connection.retire_after_progress_blocks,
            route.connection_length_blocks,
        )

    def test_initial_owner_retires_at_exact_connection_length(self):
        world = _world(16, 13)
        goal = GoalState(
            Aabb(-1.2678, -60.1, 11.2039, 2.2678, -59.9, 14.7396),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        goal_node = SurfaceNodeId(0, 12, -60, 0)
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 0, -60, -60, 11, 12, False),
            ordinary_profile(), step_profile(),
        )
        request = SurfacePlanningRequest(
            21, "d062-retire-request", "d062-retire-goal", 16,
            world.session.value, SurfaceNodeId(0, 11, -60, 0), goal_node,
            goal_state=goal,
        )
        candidate = astar_surface_plan(graph, request)
        with patch(
            "mc2p.motion_nav.route_admission.standable_point_in_region",
            return_value=StandablePointResult(
                QueryStatus.FEASIBLE,
                (.5, -60.0, 12.5),
                dependencies=((0, -59, 13),),
            ),
        ):
            route = RouteAdmitter().admit_surface(
                candidate,
                frame(world, 78, (.6516238829076266, -60.0,
                                  12.081778198429513)),
                expected_request_id=request.request_id,
                goal_id=request.goal_id,
                goal_revision=request.goal_revision,
                changed_cells=(),
            ).route
        owner = route.validation_plan.owner(
            route.validation_plan.initial_connection.owner_ref
        )
        fixed_route_id = route.action_route.actions[0].fixed_route.route_id
        length = route.connection_length_blocks
        dependency = route.validation_plan.dependencies_for_owner(
            owner.owner_id
        )[0]

        before = ActiveRouteTracker(route)
        before.record_progress(
            0,
            RouteProgressEvidence(0, fixed_route_id, length - 1.0e-6, 79),
            observation_sequence_id=79,
        )
        self.assertIn(dependency, before.effective_dependencies)
        support = next(
            cell for cell in route.validation_plan.recipe(
                owner.recipe_ref
            ).standable_connection.surface.dependencies
            if world.view().cell(cell).knowledge is CellKnowledge.BLOCK
        )
        world.confirm_air(
            ObservationStamp(world.session, 80, 80, "test-clock", 80),
            (support,),
        )
        before_change = before.validate(
            world.view(), (support,), ground_profile=ordinary_profile(),
        )
        self.assertIs(
            before_change.disposition,
            ActiveRouteValidationDisposition.STOP,
        )

        exact = ActiveRouteTracker(route)
        exact.record_progress(
            0,
            RouteProgressEvidence(0, fixed_route_id, length, 79),
            observation_sequence_id=79,
        )
        self.assertNotIn(dependency, exact.effective_dependencies)
        after = exact.validate(
            world.view(), (support,), ground_profile=ordinary_profile(),
        )
        self.assertIs(
            after.disposition,
            ActiveRouteValidationDisposition.UNAFFECTED,
        )


if __name__ == "__main__":
    unittest.main()
