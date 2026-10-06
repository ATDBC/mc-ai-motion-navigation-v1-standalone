"""D064 ordinary ground goals use one proved multi-block direct Walk."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import WalkSegment
from mc2p.motion_nav.action_route_executor import _ordinary_walk_start_window
from mc2p.motion_nav.fixed_route import (
    FixedRoute,
    FixedRouteDecision,
    FixedRouteState,
    RoutePoint,
)
from mc2p.motion_nav.navigation_owners import GoalRequestLedger
from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
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
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.route_admission import (
    AdmissionReason,
    AdmissionStatus,
    RouteAdmitter,
)
from mc2p.motion_nav.route_validation import (
    DependencyOwnerKind,
    GroundCapabilityIdentity,
    WalkValidationQueryKind,
    replay_walk_validation_recipe,
)
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp
from tests.motion_nav.test_b07_step_transition import frame, profile as step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_d060_terminal_node_exact_proof import _world
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import (
    _InlinePlanner,
    _ground_anchor,
    _source,
)


def _goal(z: float, *, feet_y: float = -60.0) -> GoalState:
    return GoalState(
        Aabb(.2, feet_y - .01, z - .2, .8, feet_y + .01, z + .2),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _request(world, *, start_z: int = 1, goal_z: int = 7):
    return SurfacePlanningRequest(
        64,
        "d064-ground-direct-request",
        "d064-ground-direct-goal",
        3,
        world.session.value,
        SurfaceNodeId(0, start_z, -60, 0),
        SurfaceNodeId(0, goal_z, -60, 0),
        goal_state=_goal(goal_z + .5),
    )


class D064GroundDirectAdmissionTests(unittest.TestCase):
    def _admit(self, world, *, start_z=1, goal_z=7, maximum=8.0):
        profile = ordinary_profile()
        return RouteAdmitter(
            maximum_corridor_blocks=maximum,
        ).admit_ground_direct(
            _request(world, start_z=start_z, goal_z=goal_z),
            frame(world, 64, (.5, -60.0, start_z + .5)),
            ground_profile=profile,
            capability_identity=GroundCapabilityIdentity.from_profile(profile),
        )

    def test_multi_block_direct_builds_exact_walk_validation(self):
        result = self._admit(_world(64, 16))

        self.assertIs(result.status, AdmissionStatus.ACCEPTED)
        route = result.route
        self.assertIsNotNone(route)
        self.assertEqual(len(route.action_route.actions), 1)
        self.assertEqual(
            route.action_route.actions[0].node_ids,
            (
                SurfaceNodeId(0, 1, -60, 0),
                SurfaceNodeId(0, 7, -60, 0),
            ),
        )
        self.assertLessEqual(route.fixed_route_length_blocks, 8.0)
        self.assertIsNotNone(route.validation_plan)
        self.assertEqual(len(route.validation_plan.owners), 1)
        owner = route.validation_plan.owners[0]
        self.assertIs(owner.kind, DependencyOwnerKind.WALK_LEG)
        recipe = route.validation_plan.recipe(owner.recipe_ref)
        self.assertIs(
            recipe.query_kind,
            WalkValidationQueryKind.STANDABLE_CONNECTION,
        )
        self.assertEqual(
            set(route.action_route.actions[0].dependencies),
            set(recipe.dependencies),
        )


    def test_multi_block_direct_rejects_disallowed_intermediate_support(self):
        world = _world(641, 16)
        profile = ordinary_profile()
        self.assertEqual(
            profile.support_materials,
            frozenset({"minecraft:stone", "minecraft:smooth_stone_slab"}),
        )
        self.assertIsNone(profile.motion_catalog)
        world.observe_blocks(
            ObservationStamp(
                world.session, 2, 2, "test-clock", 100_000_000,
            ),
            {(0, -61, 4): BlockGeometry.full_cube("minecraft:dirt")},
        )

        result = RouteAdmitter().admit_ground_direct(
            _request(world),
            frame(world, 64, (.5, -60.0, 1.5)),
            ground_profile=profile,
            capability_identity=GroundCapabilityIdentity.from_profile(profile),
        )

        self.assertIs(result.status, AdmissionStatus.NOT_APPLICABLE)
        self.assertIs(result.reason, AdmissionReason.ROUTE_CAPABILITIES_CHANGED)
        self.assertIsNone(result.route)

    def test_direct_recipe_revalidation_rejects_changed_support_material(self):
        world = _world(642, 16)
        admitted = self._admit(world)
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        route = admitted.route
        owner = route.validation_plan.owners[0]
        recipe = route.validation_plan.recipe(owner.recipe_ref)
        self.assertIn((0, -61, 4), recipe.dependencies)
        self.assertIsNone(recipe.ground_profile.motion_catalog)

        world.observe_blocks(
            ObservationStamp(
                world.session, 2, 2, "test-clock", 100_000_000,
            ),
            {(0, -61, 4): BlockGeometry.full_cube("minecraft:dirt")},
        )
        status, dependencies = replay_walk_validation_recipe(
            recipe, world.view(),
        )

        self.assertIs(status, QueryStatus.UNSUPPORTED)
        self.assertIn((0, -61, 4), dependencies)

    def test_unknown_direct_corridor_returns_exact_missing(self):
        result = self._admit(_world(65, 4))

        self.assertIs(result.status, AdmissionStatus.NEEDS_INFORMATION)
        self.assertIsNone(result.route)
        self.assertTrue(result.missing_cells)
        self.assertEqual(
            result.missing_cells,
            tuple(sorted(set(result.missing_cells))),
        )

    def test_distance_and_height_outside_direct_scope_are_not_applicable(self):
        world = _world(66, 24)
        distant = self._admit(world, goal_z=12, maximum=8.0)
        high_request = _request(world)
        high_request = SurfacePlanningRequest(
            high_request.sequence,
            high_request.request_id,
            high_request.goal_id,
            high_request.goal_revision,
            high_request.world_session,
            high_request.start,
            SurfaceNodeId(0, 7, -59, 0),
            goal_state=_goal(7.5, feet_y=-59.0),
        )
        profile = ordinary_profile()
        high = RouteAdmitter().admit_ground_direct(
            high_request,
            frame(world, 66, (.5, -60.0, 1.5)),
            ground_profile=profile,
            capability_identity=GroundCapabilityIdentity.from_profile(profile),
        )

        self.assertIs(distant.status, AdmissionStatus.NOT_APPLICABLE)
        self.assertIs(high.status, AdmissionStatus.NOT_APPLICABLE)

    def test_airborne_strict_and_known_blocked_paths_do_not_use_direct(self):
        world = _world(70, 16)
        profile = ordinary_profile()
        identity = GroundCapabilityIdentity.from_profile(profile)
        request = _request(world)
        current = frame(world, 70, (.5, -60.0, 1.5))
        airborne = replace(
            current,
            body=replace(current.body, is_on_ground=False),
        )
        strict_goal = replace(
            request,
            goal_state=replace(
                request.goal_state,
                allowed_modes=frozenset({MovementMode.JUMP_UP}),
            ),
        )
        world.observe_blocks(
            ObservationStamp(
                world.session, 2, 2, "test-clock", 100_000_000,
            ),
            {(0, -60, 4): BlockGeometry.full_cube("minecraft:stone")},
        )

        airborne_result = RouteAdmitter().admit_ground_direct(
            request,
            airborne,
            ground_profile=profile,
            capability_identity=identity,
        )
        strict_result = RouteAdmitter().admit_ground_direct(
            strict_goal,
            current,
            ground_profile=profile,
            capability_identity=identity,
        )
        blocked_result = RouteAdmitter().admit_ground_direct(
            request,
            frame(world, 71, (.5, -60.0, 1.5)),
            ground_profile=profile,
            capability_identity=identity,
        )

        self.assertIs(airborne_result.status, AdmissionStatus.NOT_APPLICABLE)
        self.assertIs(strict_result.status, AdmissionStatus.NOT_APPLICABLE)
        self.assertIs(blocked_result.status, AdmissionStatus.NOT_APPLICABLE)

    def test_world_and_capability_identity_mismatch_fail_closed(self):
        world = _world(71, 16)
        profile = ordinary_profile()
        identity = GroundCapabilityIdentity.from_profile(profile)
        current = frame(world, 71, (.5, -60.0, 1.5))

        wrong_world = RouteAdmitter().admit_ground_direct(
            replace(_request(world), world_session="foreign-world-session"),
            current,
            ground_profile=profile,
            capability_identity=identity,
        )
        wrong_capability = RouteAdmitter().admit_ground_direct(
            _request(world),
            current,
            ground_profile=profile,
            capability_identity=replace(
                identity,
                profile_id="different-ground-profile",
            ),
        )

        self.assertIs(wrong_world.status, AdmissionStatus.REJECTED)
        self.assertIs(wrong_capability.status, AdmissionStatus.REJECTED)


class OrdinaryWalkStartWindowPolicyTests(unittest.TestCase):
    def setUp(self):
        world = _world(640, 16)
        current = frame(world, 64, (.5, -60.0, 1.5))
        self.frame = replace(
            current,
            body=replace(current.body, movement_tick_id=8),
        )
        self.action = WalkSegment(
            FixedRoute("d068-straight", (
                RoutePoint(.5, -60.0, 1.5),
                RoutePoint(.5, -60.0, 7.5),
            )),
            (SurfaceNodeId(0, 1, -60, 0), SurfaceNodeId(0, 7, -60, 0)),
            (),
        )
        self.decision = FixedRouteDecision(
            FixedRouteState.RUNNING,
            MovementV1(forward=1),
            0.0,
            0.0,
            (),
            1,
            2,
            "tracking",
        )

    def test_only_stopped_straight_ordinary_walk_gets_one_tick_slack(self):
        self.assertEqual(
            _ordinary_walk_start_window(
                self.action, self.decision, self.frame,
            ),
            (9, 10),
        )

        traversal = SimpleNamespace(
            fixed_route=self.action.fixed_route,
            traversal_plan=object(),
            transition=None,
        )
        non_walk = SimpleNamespace(
            fixed_route=self.action.fixed_route,
            traversal_plan=None,
            transition=SimpleNamespace(mode=MovementMode.SPRINT),
        )
        corner = WalkSegment(
            FixedRoute("d068-corner", (
                RoutePoint(.5, -60.0, 1.5),
                RoutePoint(.5, -60.0, 4.5),
                RoutePoint(3.5, -60.0, 4.5),
            )),
            (SurfaceNodeId(0, 1, -60, 0), SurfaceNodeId(0, 4, -60, 3)),
            (),
        )
        cases = {
            "traversal": (traversal, self.decision, self.frame),
            "non_walk": (non_walk, self.decision, self.frame),
            "corner": (corner, self.decision, self.frame),
            "braking": (
                self.action,
                replace(self.decision, state=FixedRouteState.BRAKING),
                self.frame,
            ),
            "neutral": (
                self.action,
                replace(self.decision, movement=MovementV1()),
                self.frame,
            ),
            "one_tick_lease": (
                self.action,
                replace(self.decision, input_lease_ticks=1),
                self.frame,
            ),
            "airborne": (
                self.action,
                self.decision,
                replace(
                    self.frame,
                    body=replace(self.frame.body, is_on_ground=False),
                ),
            ),
            "crouching": (
                self.action,
                self.decision,
                replace(
                    self.frame,
                    body=replace(self.frame.body, pose="crouching"),
                ),
            ),
        }
        for name, arguments in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(_ordinary_walk_start_window(*arguments))


class D064GroundDirectSessionTests(unittest.TestCase):
    def _owned_planning_session(self, suffix: str, world_revision: int):
        world = _world(world_revision, 16)
        planner = _InlinePlanner(hold_first=True)
        ledger = InputApplicationLedger()
        current = frame(world, world_revision, (.5, -60.0, 1.5))
        session = NavigationSession(
            f"d064-owned-planning-{suffix}",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            f"d064-owned-planning-{suffix}-goal",
            1,
            _goal(12.5),
            current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        session.propose(
            current,
            _ground_anchor(current),
            2_000_000_000,
            input_ledger=ledger,
        )
        self.addCleanup(session.close)
        diagnostics = session.async_work_diagnostics[0]
        self.assertEqual(len(diagnostics.planning_work), 1)
        self.assertEqual(len(planner.jobs), 1)
        return world, planner, ledger, current, session, diagnostics.planning_work

    def test_goal_planning_policy_is_typed_and_fixed_for_the_task(self):
        ledger = GoalRequestLedger()
        ledger.select_planning_policy(
            GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND,
        )

        with self.assertRaises(ContractViolation):
            ledger.select_planning_policy(
                GoalPlanningPolicy.BACKGROUND_PLANNER,
            )

        self.assertIs(
            ledger.planning_policy,
            GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND,
        )

    def test_default_goal_policy_keeps_background_planning_contract(self):
        world = _world(72, 16)
        planner = _InlinePlanner(hold_first=True)
        current = frame(world, 72, (.5, -60.0, 1.5))
        session = NavigationSession(
            "d064-default-background",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-default-background-goal", 1, _goal(7.5), current,
        )
        try:
            proposal = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=InputApplicationLedger(),
            )

            self.assertEqual(len(planner.jobs), 1)
            self.assertIs(
                proposal.report.state,
                NavigationSessionState.PLANNING,
            )
            self.assertIsNone(session.body_route_snapshot)
        finally:
            session.close()

    def test_direct_revision_retires_owned_planning_before_direct_route(self):
        world = _world(721, 16)
        planner = _InlinePlanner(hold_first=True)
        ledger = InputApplicationLedger()
        current = frame(world, 72, (.5, -60.0, 1.5))
        session = NavigationSession(
            "d064-owned-planning-to-direct",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-owned-planning-to-direct-goal",
            1,
            _goal(12.5),
            current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            planning = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertIs(
                planning.report.state,
                NavigationSessionState.PLANNING,
            )
            self.assertTrue(session.diagnostics.planning_work_owned)
            self.assertEqual(len(planner.jobs), 1)
            self.assertEqual(planner.jobs[0][3].goal_revision, 1)
            before = session.async_work_diagnostics[0]
            old_work = tuple(identity for identity, _ in before.planning_work)
            self.assertEqual(len(old_work), 1)

            self.assertTrue(session.update_goal(
                "d064-owned-planning-to-direct-goal", 2, _goal(7.5),
            ))
            retired = session.async_work_diagnostics[0]
            self.assertEqual(retired.planning_work, ())
            self.assertEqual(retired.pending_planning_receipts, old_work)
            self.assertTrue(any(
                event.operation == "finish" and event.identity == old_work[0]
                for event in retired.events
            ))
            self.assertEqual(session.report.goal_revision, 2)

            next_frame = frame(world, 73, (.5, -60.0, 1.5))
            direct = session.propose(
                next_frame,
                _ground_anchor(next_frame),
                2_050_000_000,
                input_ledger=ledger,
            )

            self.assertEqual(len(planner.jobs), 1)
            self.assertIs(
                direct.report.state,
                NavigationSessionState.EXECUTING,
            )
            self.assertIsNotNone(session.body_route_snapshot)
            self.assertTrue(
                session.body_route_snapshot.route_id.endswith("-direct")
            )
            self.assertIsNone(session.body_route_snapshot.work_identity)
            self.assertIsNotNone(direct.route_decision)
            self.assertTrue(direct.route_decision.submit_input)
            self.assertEqual(
                session.body_route_snapshot.source_request_id,
                direct.report.request_id,
            )
        finally:
            session.close()

    def test_background_revision_keeps_owned_planning_and_revises_request(self):
        world = _world(722, 16)
        planner = _InlinePlanner(hold_first=True)
        ledger = InputApplicationLedger()
        current = frame(world, 72, (.5, -60.0, 1.5))
        session = NavigationSession(
            "d064-background-owned-planning",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-background-owned-planning-goal",
            1,
            _goal(12.5),
            current,
        )
        try:
            session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertTrue(session.diagnostics.planning_work_owned)
            self.assertEqual(len(planner.jobs), 1)

            self.assertTrue(session.update_goal(
                "d064-background-owned-planning-goal", 2, _goal(7.5),
            ))
            self.assertTrue(session.diagnostics.planning_work_owned)
            self.assertEqual(session.report.goal_revision, 2)

            next_frame = frame(world, 73, (.5, -60.0, 1.5))
            revised = session.propose(
                next_frame,
                _ground_anchor(next_frame),
                2_050_000_000,
                input_ledger=ledger,
            )

            self.assertEqual(len(planner.jobs), 1)
            self.assertIs(
                revised.report.state,
                NavigationSessionState.PLANNING,
            )
            self.assertIsNone(session.body_route_snapshot)
            self.assertTrue(session.diagnostics.planning_work_owned)
        finally:
            session.close()

    def test_ineligible_strict_and_airborne_revisions_keep_owned_planning(self):
        for suffix, world_revision, goal, airborne in (
            (
                "strict",
                723,
                replace(
                    _goal(7.5),
                    allowed_modes=frozenset({MovementMode.JUMP_UP}),
                ),
                False,
            ),
            ("airborne", 724, _goal(7.5), True),
            (
                "same-support-strict",
                725,
                replace(
                    _goal(1.8),
                    allowed_modes=frozenset({MovementMode.JUMP_UP}),
                ),
                False,
            ),
        ):
            with self.subTest(case=suffix):
                (world, planner, ledger, current,
                 session, original_work) = self._owned_planning_session(
                    suffix, world_revision,
                )
                if airborne:
                    air_frame = frame(
                        world, world_revision + 1,
                        (.5, -60.0, 1.5),
                    )
                    current = replace(
                        air_frame,
                        body=replace(air_frame.body, is_on_ground=False),
                    )
                    session.propose(
                        current,
                        _ground_anchor(current),
                        2_050_000_000,
                        input_ledger=ledger,
                    )

                self.assertTrue(session.update_goal(
                    f"d064-owned-planning-{suffix}-goal", 2, goal,
                ))

                diagnostics = session.async_work_diagnostics[0]
                self.assertEqual(diagnostics.planning_work, original_work)
                self.assertEqual(len(planner.jobs), 1)
                self.assertIs(
                    session.report.state,
                    NavigationSessionState.PLANNING,
                )
                self.assertIsNone(session.body_route_snapshot)

    def test_elevated_revision_keeps_owned_planning(self):
        world = _world(727, 16)
        stamp = ObservationStamp(
            world.session, 2, 2, "test-clock", 100_000_000,
        )
        world.observe_blocks(stamp, {
            (0, -60, 7): BlockGeometry.full_cube("minecraft:stone"),
        })
        world.confirm_air(stamp, ((0, -59, 7), (0, -58, 7)))
        planner = _InlinePlanner(hold_first=True)
        ledger = InputApplicationLedger()
        current = frame(world, 727, (.5, -60.0, 1.5))
        session = NavigationSession(
            "d064-owned-planning-elevated",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-owned-planning-elevated-goal",
            1,
            _goal(12.5),
            current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            original_work = session.async_work_diagnostics[0].planning_work
            self.assertEqual(len(original_work), 1)

            self.assertTrue(session.update_goal(
                "d064-owned-planning-elevated-goal",
                2,
                _goal(7.5, feet_y=-59.0),
            ))

            self.assertEqual(
                session.async_work_diagnostics[0].planning_work,
                original_work,
            )
            self.assertEqual(len(planner.jobs), 1)
            self.assertIs(
                session.report.state,
                NavigationSessionState.PLANNING,
            )
            self.assertIsNone(session.body_route_snapshot)
        finally:
            session.close()

    def test_same_support_revision_uses_existing_local_direct_contract(self):
        (world, planner, ledger, current,
         session, old_work) = self._owned_planning_session(
            "same-support", 726,
        )

        self.assertTrue(session.update_goal(
            "d064-owned-planning-same-support-goal", 2, _goal(1.8),
        ))
        retired = session.async_work_diagnostics[0]
        self.assertEqual(retired.planning_work, ())
        self.assertEqual(retired.pending_planning_receipts, tuple(
            identity for identity, _ in old_work
        ))

        next_frame = frame(world, 727, (.5, -60.0, 1.5))
        local = session.propose(
            next_frame,
            _ground_anchor(next_frame),
            2_050_000_000,
            input_ledger=ledger,
        )

        self.assertEqual(len(planner.jobs), 1)
        self.assertIs(
            local.report.state,
            NavigationSessionState.EXECUTING,
        )
        self.assertIsNotNone(session.body_route_snapshot)
        self.assertTrue(session.body_route_snapshot.route_id.endswith("-local"))
        self.assertIsNone(session.body_route_snapshot.work_identity)
        self.assertIsNotNone(local.route_decision)
        self.assertTrue(local.route_decision.submit_input)

    def test_initial_ground_goal_uses_direct_route_before_delayed_planner(self):
        world = _world(67, 16)
        planner = _InlinePlanner(hold_first=True)
        current = frame(world, 67, (.5, -60.0, 1.5))
        current = replace(
            current,
            body=replace(current.body, movement_tick_id=8),
        )
        session = NavigationSession(
            "d064-initial-direct",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-ground-direct-goal",
            1,
            _goal(7.5),
            current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            proposal = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=InputApplicationLedger(),
            )

            self.assertEqual(planner.jobs, [])
            self.assertIs(
                proposal.report.state,
                NavigationSessionState.EXECUTING,
            )
            self.assertIsNotNone(session.body_route_snapshot)
            self.assertTrue(
                session.body_route_snapshot.route_id.endswith("-direct")
            )
            self.assertIsNotNone(proposal.route_decision)
            self.assertTrue(proposal.route_decision.submit_input)
            movement_intents = tuple(
                envelope.intent
                for envelope in proposal.control_frame.intents
                if envelope.intent.movement is not None
                and envelope.intent.movement != MovementV1()
            )
            self.assertEqual(len(movement_intents), 1)
            window = movement_intents[0].movement_tick_window
            self.assertIsNotNone(window)
            self.assertEqual(
                (window.earliest_tick, window.latest_tick),
                (9, 10),
            )
        finally:
            session.close()

    def test_moving_ground_direct_does_not_claim_a_late_start_window(self):
        world = _world(671, 16)
        planner = _InlinePlanner(hold_first=True)
        current = frame(
            world,
            67,
            (.5, -60.0, 1.5),
            velocity=(0.0, 0.0, 0.2),
        )
        current = replace(
            current,
            body=replace(current.body, movement_tick_id=8),
        )
        session = NavigationSession(
            "d064-moving-direct",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-moving-direct-goal",
            1,
            _goal(7.5),
            current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            proposal = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=InputApplicationLedger(),
            )
            movement_intents = tuple(
                envelope.intent
                for envelope in proposal.control_frame.intents
                if envelope.intent.movement is not None
                and envelope.intent.movement != MovementV1()
            )
            self.assertEqual(len(movement_intents), 1)
            self.assertIsNone(movement_intents[0].movement_tick_window)
        finally:
            session.close()

    def test_missing_revised_goal_keeps_ordinary_walk_incumbent(self):
        world = _world(68, 8)
        current = frame(world, 68, (.5, -60.0, 1.5))
        ledger = InputApplicationLedger()
        session = NavigationSession(
            "d064-revision-information",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(hold_first=True),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-revision-goal", 1, _goal(6.5), current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            first = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertIsNotNone(first.route_decision)
            incumbent = session.body_route_snapshot
            self.assertIsNotNone(incumbent)

            self.assertTrue(session.update_goal(
                "d064-revision-goal", 2, _goal(8.5),
            ))
            next_frame = frame(world, 69, (.5, -60.0, 1.55))
            waiting = session.propose(
                next_frame,
                _ground_anchor(next_frame),
                2_000_000_000,
                input_ledger=ledger,
            )

            self.assertIs(
                waiting.report.state,
                NavigationSessionState.EXECUTING,
            )
            self.assertIs(session.body_route_snapshot, incumbent)
            self.assertTrue(session.observation_request().air_positions)
            self.assertIsNotNone(waiting.route_decision)
            self.assertTrue(waiting.route_decision.submit_input)
        finally:
            session.close()

    def test_completed_incumbent_keeps_revised_missing_goal_until_facts_arrive(self):
        world = _world(681, 8)
        planner = _InlinePlanner(hold_first=True)
        ledger = InputApplicationLedger()
        current = frame(world, 68, (.5, -60.0, 1.5))
        session = NavigationSession(
            "d064-completed-incumbent-missing-revision",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-completed-incumbent-goal", 1, _goal(6.5), current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            self.assertTrue(session.update_goal(
                "d064-completed-incumbent-goal", 2, _goal(10.5),
            ))
            missing = session.report.missing_cells
            self.assertEqual(len(missing), 30)

            old_terminal = frame(
                world, 69, (.5, -60.0, 6.5),
                velocity=(0.0, 0.0, 0.0),
            )
            waiting = session.propose(
                old_terminal,
                _ground_anchor(old_terminal),
                2_050_000_000,
                input_ledger=ledger,
            )

            self.assertIs(
                waiting.report.state,
                NavigationSessionState.NEEDS_INFORMATION,
            )
            self.assertEqual(session.diagnostics.pending_goal_revision, 2)
            self.assertEqual(waiting.report.missing_cells, missing)
            self.assertEqual(
                tuple(sorted(session.observation_request().air_positions)),
                missing,
            )
            self.assertIsNone(session.body_route_snapshot)
            self.assertEqual(planner.jobs, [])
            self.assertEqual(session.diagnostics.retry_approved_total, 0)

            world.confirm_air(
                ObservationStamp(
                    world.session, 2, 2, "test-clock", 100_000_000,
                ),
                missing,
            )
            ready_frame = replace(
                frame(
                    world, 70, (.5, -60.0, 6.5),
                    velocity=(0.0, 0.0, 0.0),
                ),
                changed_cells=missing,
            )
            resumed = session.propose(
                ready_frame,
                _ground_anchor(ready_frame),
                2_100_000_000,
                input_ledger=ledger,
            )

            if resumed.report.state is NavigationSessionState.NEEDS_INFORMATION:
                connection_missing = resumed.report.missing_cells
                self.assertTrue(connection_missing)
                self.assertEqual(
                    tuple(sorted(session.observation_request().air_positions)),
                    connection_missing,
                )
                world.confirm_air(
                    ObservationStamp(
                        world.session, 3, 3, "test-clock", 150_000_000,
                    ),
                    connection_missing,
                )
                connection_frame = replace(
                    frame(
                        world, 71, (.5, -60.0, 6.5),
                        velocity=(0.0, 0.0, 0.0),
                    ),
                    changed_cells=connection_missing,
                )
                resumed = session.propose(
                    connection_frame,
                    _ground_anchor(connection_frame),
                    2_150_000_000,
                    input_ledger=ledger,
                )

            self.assertIn(
                resumed.report.state,
                {
                    NavigationSessionState.EXECUTING,
                    NavigationSessionState.PLANNING,
                },
            )
            if resumed.report.state is NavigationSessionState.EXECUTING:
                self.assertIsNotNone(resumed.route_decision)
                self.assertTrue(resumed.route_decision.submit_input)
                self.assertEqual(planner.jobs, [])
            else:
                self.assertIsNone(resumed.route_decision)
                self.assertEqual(len(planner.jobs), 1)
            self.assertEqual(session.diagnostics.retry_approved_total, 0)
        finally:
            session.close()

    def test_satisfied_idle_goal_waits_for_revised_target_facts(self):
        world = _world(682, 8)
        planner = _InlinePlanner(hold_first=True)
        ledger = InputApplicationLedger()
        current = frame(world, 68, (.5, -60.0, 1.5))
        session = NavigationSession(
            "d064-satisfied-idle-missing-revision",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-satisfied-idle-goal", 1, _goal(6.5), current,
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            held = frame(
                world, 69, (.5, -60.0, 6.5),
                velocity=(0.0, 0.0, 0.0),
            )
            for sequence in range(69, 73):
                held = frame(
                    world, sequence, (.5, -60.0, 6.5),
                    velocity=(0.0, 0.0, 0.0),
                )
                session.propose(
                    held,
                    _ground_anchor(held),
                    2_000_000_000 + sequence * 50_000_000,
                    input_ledger=ledger,
                )
                if (session.report.observed_goal_status
                        is ObservedGoalStatus.SATISFIED
                        and session.body_route_snapshot is None):
                    break

            self.assertIs(
                session.report.observed_goal_status,
                ObservedGoalStatus.SATISFIED,
            )
            self.assertIsNone(session.body_route_snapshot)
            self.assertTrue(session.diagnostics.source_bound)

            self.assertTrue(session.update_goal(
                "d064-satisfied-idle-goal", 2, _goal(10.5),
            ))
            missing = session.report.missing_cells

            self.assertTrue(missing)
            self.assertIs(
                session.report.state,
                NavigationSessionState.NEEDS_INFORMATION,
            )
            self.assertEqual(session.diagnostics.pending_goal_revision, 2)
            self.assertEqual(
                tuple(sorted(session.observation_request().air_positions)),
                missing,
            )
            self.assertTrue(session.diagnostics.source_bound)
            self.assertIsNone(session.body_route_snapshot)
            self.assertEqual(session.diagnostics.retry_approved_total, 0)
            self.assertFalse(session.report.terminal)

            still_waiting = session.propose(
                held,
                _ground_anchor(held),
                2_300_000_000,
                input_ledger=ledger,
            )
            self.assertIs(
                still_waiting.report.state,
                NavigationSessionState.NEEDS_INFORMATION,
            )
            self.assertFalse(still_waiting.report.terminal)
            self.assertEqual(session.diagnostics.retry_approved_total, 0)

            for observation_sequence in range(2, 6):
                requested = session.report.missing_cells
                if not requested:
                    break
                stamp = ObservationStamp(
                    world.session,
                    observation_sequence,
                    observation_sequence,
                    "test-clock",
                    observation_sequence * 50_000_000,
                )
                world.confirm_air(stamp, requested)
                ready = replace(
                    frame(
                        world, 72 + observation_sequence,
                        (.5, -60.0, 6.5),
                        velocity=(0.0, 0.0, 0.0),
                    ),
                    changed_cells=requested,
                )
                resumed = session.propose(
                    ready,
                    _ground_anchor(ready),
                    2_300_000_000 + observation_sequence * 50_000_000,
                    input_ledger=ledger,
                )
                if resumed.report.state is not NavigationSessionState.NEEDS_INFORMATION:
                    break

            self.assertIn(
                session.report.state,
                {
                    NavigationSessionState.EXECUTING,
                    NavigationSessionState.PLANNING,
                },
            )
            self.assertFalse(session.report.terminal)
            self.assertTrue(session.diagnostics.source_bound)
            self.assertEqual(session.diagnostics.retry_approved_total, 0)
        finally:
            session.close()

    def test_revised_direct_route_retires_incumbent_only_after_selected_input(self):
        world = _world(69, 8)
        current = frame(world, 69, (.5, -60.0, 1.5))
        ledger = InputApplicationLedger()
        session = NavigationSession(
            "d064-selected-successor",
            NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(hold_first=True),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "d064-selected-successor-goal", 1, _goal(6.5), current,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        try:
            first = session.propose(
                current,
                _ground_anchor(current),
                2_000_000_000,
                input_ledger=ledger,
            )
            old_route_id = first.route_owner_id
            self.assertIsNotNone(old_route_id)
            self.assertTrue(session.update_goal(
                "d064-selected-successor-goal", 2, _goal(8.5),
            ))
            waiting_frame = frame(world, 70, (.5, -60.0, 1.55))
            session.propose(
                waiting_frame,
                _ground_anchor(waiting_frame),
                2_000_000_000,
                input_ledger=ledger,
            )
            missing = session.report.missing_cells
            self.assertTrue(missing)
            stamp = ObservationStamp(
                world.session, 2, 2, "test-clock", 100_000_000,
            )
            world.confirm_air(stamp, missing)
            ready_frame = replace(
                frame(world, 71, (.5, -60.0, 1.6)),
                changed_cells=missing,
            )

            successor = session.propose(
                ready_frame,
                _ground_anchor(ready_frame),
                2_000_000_000,
                input_ledger=ledger,
            )

            before = session.diagnostics
            self.assertEqual(before.incumbent_route_id, old_route_id)
            self.assertIsNotNone(before.pending_route_id)
            self.assertEqual(successor.route_owner_id, before.pending_route_id)
            self.assertTrue(successor.route_decision.submit_input)
            session.register_verified_submission(
                successor,
                control_sequence=99,
                actual_movement=successor.route_decision.movement,
            )
            after = session.diagnostics
            self.assertEqual(
                after.incumbent_route_id,
                before.pending_route_id,
            )
            self.assertIsNone(after.pending_route_id)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
