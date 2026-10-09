"""D058-B validates admitted Walk proofs before advancing body control."""
from dataclasses import replace
import math
import unittest
from unittest.mock import patch, PropertyMock

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route import (
    ActionRoute,
    ControlledDropSegment,
    WalkSegment,
)
from mc2p.motion_nav.action_route_executor import (
    ActionRouteExecutor,
    ActionRouteState,
)
from mc2p.motion_nav.body_control import StopCause
from mc2p.motion_nav.controlled_drop import ControlledDropEdge
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.fixed_route import (
    FixedRoute,
    FixedRouteDecision,
    FixedRouteState,
    RoutePoint,
)
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.route_admission import (
    ActiveRoute,
    ActiveRouteTracker,
    ExecutableCorridor,
    RouteAdmitter,
)
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationDisposition,
    ActiveRouteValidationReason,
    RouteProgressEvidence,
    RouteValidationBudget,
    DependencyOwnerKind,
)
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.segment_entry import SegmentEntryWindow
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion,
    SupportSurface,
    SurfaceNodeId,
)
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldQueryCache,
    WorldSessionId,
    CellKnowledge,
)
from tests.motion_nav.test_b07_step_transition import (
    frame as make_frame,
    profile as step_profile,
)
from tests.motion_nav.test_b07_step_route import B07StepRouteTests
from tests.motion_nav.test_b07_surface_planning import (
    flat_surface_world,
    ordinary_profile,
)
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_d058_validation_plan import _admit, _candidate
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import (
    NavigationSessionTests,
    _InlinePlanner,
    _ground_anchor,
)
from tests.motion_nav.test_route_body_advance import _FrameExecutor
from tests.motion_nav.test_execution_supervisor import ExecutionSupervisorTests


class D058RuntimeValidationTests(unittest.TestCase):
    def _route(self, *, size=5, start=(1, 1), goal=(3, 1), request_id="d058-runtime"):
        world = flat_surface_world(size)
        request, candidate = _candidate(
            world, start, goal, request_id=request_id,
        )
        route = _admit(world, request, candidate, candidate.path[0].position)
        return world, candidate, route

    def _mixed_route(self):
        session = WorldSessionId("d058-runtime-mixed")
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
            world, (0, 0), (3, 0), request_id="d058-runtime-mixed",
        )
        return world, _admit(
            world, request, candidate, candidate.path[0].position,
        )

    def _walk_drop_executor(self):
        world = flat_surface_world(5)
        start_id = SurfaceNodeId(0, 1, 1, 0)
        end_id = SurfaceNodeId(0, 1, -1, 1)
        start = SupportSurface(
            start_id, (1.5, 1.0, 1.5), HorizontalRegion(1, 1, 2, 2),
            1.0, ("minecraft:stone",), (),
        )
        end = SupportSurface(
            end_id, (1.5, -1.0, 2.5), HorizontalRegion(1, 2, 2, 3),
            1.0, ("minecraft:stone",), (),
        )
        drop_profile = air_profile(MovementMode.CONTROLLED_DROP)
        entry_window = SegmentEntryWindow(
            start.position,
            (0.0, 1.0),
            -.25,
            .25,
            .25,
            .9,
            1.1,
            0.0,
            4.4,
            math.pi,
            frozenset({"standing", "crouching"}),
            frozenset({MovementMode.WALK, MovementMode.CROUCH}),
            None,
            None,
            drop_profile.profile_id,
        )
        route = ActionRoute("d058-pending-boundary", (
            WalkSegment(
                FixedRoute("d058-pending-walk", (
                    RoutePoint(.5, 1.0, 1.5),
                    RoutePoint(1.5, 1.0, 1.5),
                )),
                (start_id,), ((0, 0, 0),),
            ),
            ControlledDropSegment(
                ControlledDropEdge(
                    start_id, end_id, drop_profile.profile_id, 1.0, (),
                ),
                start, end, ((1, -2, 2),), entry_window=entry_window,
            ),
        ))
        initial = make_frame(world, 80, (.5, 1.0, 1.5))
        boundary = make_frame(world, 81, start.position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
            air_profiles=(drop_profile,),
        )
        executor.start(route, initial)
        active = ActiveRoute(
            route.route_id, 1, "d058-pending-request", "d058-pending-goal", 1,
            initial.session.value, None, 3.0, 0.0, (),
            ExecutableCorridor(
                (start_id, end_id), route.dependencies, 3.0, end_id,
            ),
            route, planning_generation=1,
        )
        return world, active, executor, initial, boundary

    def _assert_same_frame_stop_without_forward(
        self, world, route, changed, mutate,
    ):
        point = route.action_route.actions[0].fixed_route.points[0]
        position = point.x, point.y, point.z
        initial = make_frame(world, 60, position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, initial)
        control = RouteControl(route, executor)
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        self.assertTrue(supervisor.offer_route(
            control, initial, ledger, _ground_anchor(initial),
        ))
        mutate()
        current = replace(
            make_frame(world, 61, position), changed_cells=(changed,),
        )

        advance = supervisor.advance_body(
            current, ledger, _ground_anchor(current),
        )

        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertFalse(advance.route_advance.decision.movement.forward)

    def test_unrelated_change_is_zero_query_and_keeps_effective_dependencies(self):
        world, _, route = self._route()
        tracker = ActiveRouteTracker(route)
        before = tracker.effective_dependencies

        with patch(
            "mc2p.motion_nav.route_admission.replay_walk_validation_recipe",
            side_effect=AssertionError("unrelated change must not query"),
        ):
            result = tracker.validate(
                world.view(), ((99, 99, 99),),
                budget=RouteValidationBudget(4),
            )

        self.assertIs(result.disposition, ActiveRouteValidationDisposition.UNAFFECTED)
        self.assertIs(result.reason, ActiveRouteValidationReason.NO_INTERSECTION)
        self.assertEqual(result.queries_used, 0)
        self.assertEqual(tracker.effective_dependencies, before)

    def test_changed_walk_owner_replays_and_refreshes_its_dependencies(self):
        world, _, route = self._route()
        tracker = ActiveRouteTracker(route)
        plan = route.validation_plan
        owner = plan.owner(plan.action_plans[0].legs[0].owner_ref)
        changed = plan.dependencies_for_owner(owner.owner_id)[0]
        refreshed = tuple(sorted(set(plan.recipe(owner.recipe_ref).dependencies) | {(9, 0, 9)}))
        view = world.view()

        with patch(
            "mc2p.motion_nav.route_admission.replay_walk_validation_recipe",
            return_value=(QueryStatus.FEASIBLE, refreshed),
        ) as replay:
            result = tracker.validate(
                view, (changed,),
                ground_profile=ordinary_profile(),
                query_cache=WorldQueryCache(view),
                budget=RouteValidationBudget(4),
            )

        self.assertIs(result.disposition, ActiveRouteValidationDisposition.CONTINUE)
        self.assertIs(result.reason, ActiveRouteValidationReason.REVALIDATED)
        self.assertEqual(result.refreshed_dependencies, ((9, 0, 9),))
        self.assertIn((9, 0, 9), tracker.effective_dependencies)
        self.assertEqual(replay.call_count, 1)

    def test_standable_connection_recipe_revalidates_through_tracker(self):
        world, candidate, _ = self._route()
        start = candidate.path[0].position
        request, candidate = _candidate(
            world, (1, 1), (3, 1), request_id="d058-runtime-standable",
        )
        route = _admit(
            world, request, candidate,
            (start[0] + .451, start[1], start[2]),
        )
        initial = route.validation_plan.initial_connection
        owner = route.validation_plan.owner(initial.owner_ref)
        changed = route.validation_plan.dependencies_for_owner(owner.owner_id)[0]

        result = ActiveRouteTracker(route).validate(
            world.view(), (changed,),
            ground_profile=ordinary_profile(),
            budget=RouteValidationBudget(4),
        )

        self.assertIs(result.disposition, ActiveRouteValidationDisposition.CONTINUE)
        self.assertIs(result.reason, ActiveRouteValidationReason.REVALIDATED)
        self.assertEqual(result.queries_used, 1)

    def test_shared_strict_owner_fails_closed_without_replaying_walk(self):
        # Existing Walk-Step-Walk metadata contains cells jointly owned by the
        # preceding Walk proof and the strict Step action.
        world, route = self._mixed_route()
        tracker = ActiveRouteTracker(route)
        plan = route.validation_plan
        strict_owner = next(owner for owner in plan.owners
                            if owner.kind.value == "strict_action")
        shared = next(item.position for item in plan.dependency_provenance
                      if strict_owner.owner_id in item.owner_refs
                      and len(item.owner_refs) > 1)

        with patch(
            "mc2p.motion_nav.route_admission.replay_walk_validation_recipe",
            side_effect=AssertionError("strict provenance must stop before query"),
        ):
            result = tracker.validate(
                world.view(), (shared,), budget=RouteValidationBudget(4),
            )

        self.assertIs(result.disposition, ActiveRouteValidationDisposition.STOP)
        self.assertIs(result.reason, ActiveRouteValidationReason.STRICT_OWNER_CHANGED)

    def test_frame_query_limit_fails_closed_before_fifth_recipe(self):
        world, _, route = self._route()
        tracker = ActiveRouteTracker(route)
        changed = route.validation_plan.recipes[0].dependencies[0]
        budget = RouteValidationBudget(0)

        result = tracker.validate(
            world.view(), (changed,),
            ground_profile=ordinary_profile(), budget=budget,
        )

        self.assertIs(result.disposition, ActiveRouteValidationDisposition.STOP)
        self.assertIs(result.reason, ActiveRouteValidationReason.QUERY_LIMIT_EXCEEDED)

    def test_capability_identity_change_stops_without_query(self):
        world, _, route = self._route(goal=(2, 1))
        changed = route.validation_plan.recipes[0].dependencies[0]
        budget = RouteValidationBudget(4)

        with patch(
            "mc2p.motion_nav.route_admission.replay_walk_validation_recipe",
            side_effect=AssertionError("identity mismatch must stop before query"),
        ):
            result = ActiveRouteTracker(route).validate(
                world.view(), (changed,),
                ground_profile=replace(
                    ordinary_profile(), profile_id="d058-other-profile",
                ),
                budget=budget,
            )

        self.assertIs(result.disposition, ActiveRouteValidationDisposition.STOP)
        self.assertIs(
            result.reason,
            ActiveRouteValidationReason.CAPABILITY_IDENTITY_CHANGED,
        )
        self.assertEqual(budget.queries_used, 0)

    def test_revalidation_requires_the_exact_frozen_ground_profile(self):
        world, _, route = self._route(goal=(2, 1))
        changed = route.validation_plan.recipes[0].dependencies[0]
        frozen = ordinary_profile()
        mismatches = (
            None,
            replace(frozen, tick_seconds=frozen.tick_seconds * .5),
            replace(
                frozen,
                acceleration_blocks_per_second2=(
                    frozen.acceleration_blocks_per_second2 + .1
                ),
            ),
            replace(
                frozen,
                velocity_retention_per_tick=(
                    frozen.velocity_retention_per_tick * .9
                ),
            ),
            replace(
                frozen,
                maximum_speed_blocks_per_second=(
                    frozen.maximum_speed_blocks_per_second + .1
                ),
            ),
        )

        for profile in mismatches:
            with self.subTest(profile=profile):
                budget = RouteValidationBudget(4)
                with patch(
                    "mc2p.motion_nav.route_admission."
                    "replay_walk_validation_recipe",
                    side_effect=AssertionError(
                        "profile mismatch must stop before query"
                    ),
                ):
                    result = ActiveRouteTracker(route).validate(
                        world.view(), (changed,),
                        ground_profile=profile,
                        budget=budget,
                    )
                self.assertIs(
                    result.disposition,
                    ActiveRouteValidationDisposition.STOP,
                )
                self.assertIs(
                    result.reason,
                    ActiveRouteValidationReason.CAPABILITY_IDENTITY_CHANGED,
                )
                self.assertEqual(budget.queries_used, 0)

    def test_real_unknown_to_air_revalidates_but_removed_support_and_cube_stop(self):
        world, _, route = self._route(goal=(2, 1))
        plan = route.validation_plan
        recipe = plan.recipes[0]
        source = world.view()
        target_air = next(
            position for position in recipe.dependencies
            if source.cell(position).knowledge is CellKnowledge.AIR
        )
        runtime = WorldKnowledge(world.session)
        first = ObservationStamp(world.session, 20, 20, "test-clock", 1_000_000_000)
        air = tuple(
            position for position in recipe.dependencies
            if position != target_air
            and source.cell(position).knowledge is CellKnowledge.AIR
        )
        blocks = {
            position: source.cell(position).block
            for position in recipe.dependencies
            if source.cell(position).knowledge is CellKnowledge.BLOCK
        }
        runtime.confirm_air(first, air)
        runtime.observe_blocks(first, blocks)
        self.assertIs(
            runtime.view().cell(target_air).knowledge,
            CellKnowledge.UNKNOWN,
        )
        second = ObservationStamp(world.session, 21, 21, "test-clock", 1_050_000_000)
        runtime.confirm_air(second, (target_air,))

        continued = ActiveRouteTracker(route).validate(
            runtime.view(), (target_air,),
            ground_profile=ordinary_profile(),
            budget=RouteValidationBudget(4),
        )
        self.assertIs(
            continued.disposition, ActiveRouteValidationDisposition.CONTINUE,
        )

        support = next(
            position for position in recipe.dependencies
            if source.cell(position).knowledge is CellKnowledge.BLOCK
        )
        removed = flat_surface_world(5)
        request, candidate = _candidate(
            removed, (1, 1), (2, 1), request_id="d058-removed",
        )
        removed_route = _admit(
            removed, request, candidate, candidate.path[0].position,
        )
        removed_support = next(
            position for position in removed_route.validation_plan.recipes[0].dependencies
            if removed.view().cell(position).knowledge is CellKnowledge.BLOCK
        )
        removed.confirm_air(
            ObservationStamp(removed.session, 30, 30, "test-clock", 1_500_000_000),
            (removed_support,),
        )
        stopped = ActiveRouteTracker(removed_route).validate(
            removed.view(), (removed_support,),
            ground_profile=ordinary_profile(),
            budget=RouteValidationBudget(4),
        )
        self.assertIs(stopped.disposition, ActiveRouteValidationDisposition.STOP)

        blocked = flat_surface_world(5)
        request, candidate = _candidate(
            blocked, (1, 1), (2, 1), request_id="d058-cube",
        )
        blocked_route = _admit(
            blocked, request, candidate, candidate.path[0].position,
        )
        blocked_air = next(
            position for position in blocked_route.validation_plan.recipes[0].dependencies
            if (position[1] >= 1
                and blocked.view().cell(position).knowledge is CellKnowledge.AIR)
        )
        blocked.observe_blocks(
            ObservationStamp(blocked.session, 30, 30, "test-clock", 1_500_000_000),
            {blocked_air: BlockGeometry.full_cube("minecraft:stone")},
        )
        stopped = ActiveRouteTracker(blocked_route).validate(
            blocked.view(), (blocked_air,),
            ground_profile=ordinary_profile(),
            budget=RouteValidationBudget(4),
        )
        self.assertIs(stopped.disposition, ActiveRouteValidationDisposition.STOP)
        self.assertIs(stopped.reason, ActiveRouteValidationReason.BLOCKED)

    def test_unsupported_support_material_fails_closed(self):
        world, _, route = self._route(goal=(2, 1))
        support = next(
            position for position in route.validation_plan.recipes[0].dependencies
            if world.view().cell(position).knowledge is CellKnowledge.BLOCK
        )
        world.observe_blocks(
            ObservationStamp(world.session, 40, 40, "test-clock", 2_000_000_000),
            {support: BlockGeometry.full_cube("minecraft:dirt")},
        )

        result = ActiveRouteTracker(route).validate(
            world.view(), (support,),
            ground_profile=ordinary_profile(),
            budget=RouteValidationBudget(4),
        )

        self.assertIs(result.disposition, ActiveRouteValidationDisposition.STOP)
        self.assertIs(result.reason, ActiveRouteValidationReason.UNSUPPORTED)

    def test_walk_step_walk_retires_by_typed_action_and_progress(self):
        world, route = self._mixed_route()
        tracker = ActiveRouteTracker(route)
        first, second = route.validation_plan.action_plans
        first_end = first.legs[-1].end_progress_blocks
        final_walk_dependencies = set(
            route.action_route.actions[second.action_index].dependencies
        )
        early_dependencies = {
            position
            for owner in route.validation_plan.owners
            if owner.action_index == first.action_index
            for position in route.validation_plan.dependencies_for_owner(
                owner.owner_id
            )
        }
        later_dependencies = {
            position
            for owner in route.validation_plan.owners
            if owner.action_index > first.action_index
            for position in route.validation_plan.dependencies_for_owner(
                owner.owner_id
            )
        }
        early_exclusive = early_dependencies - later_dependencies
        self.assertTrue(early_exclusive)
        self.assertTrue(
            early_exclusive.isdisjoint(final_walk_dependencies)
        )

        left_walk = tracker.record_progress(
            1,
            RouteProgressEvidence(
                0, first.fixed_route_id, first_end, 50,
            ),
            observation_sequence_id=50,
        )
        self.assertIs(
            left_walk.disposition, ActiveRouteValidationDisposition.CONTINUE,
        )
        self.assertTrue(
            early_exclusive.isdisjoint(tracker.effective_dependencies)
        )
        entered_second_walk = tracker.record_progress(
            2, None, observation_sequence_id=51,
        )
        self.assertIs(
            entered_second_walk.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )
        strict = next(owner for owner in route.validation_plan.owners
                      if owner.kind.value == "strict_action")
        self.assertFalse(
            set(route.validation_plan.dependencies_for_owner(strict.owner_id))
            .difference(
                set(route.validation_plan.dependencies_for_owner(
                    second.legs[0].owner_ref
                ))
            ).intersection(tracker.effective_dependencies)
        )
        current = tracker.record_progress(
            2,
            RouteProgressEvidence(
                2, second.fixed_route_id, .1, 52,
            ),
            observation_sequence_id=52,
        )
        self.assertIs(current.reason, ActiveRouteValidationReason.PROGRESS_RECORDED)
        shared = next(iter(
            set(route.validation_plan.dependencies_for_owner(strict.owner_id))
            .intersection(route.validation_plan.dependencies_for_owner(
                second.legs[0].owner_ref
            ))
        ))
        result = tracker.validate(
            world.view(), (shared,),
            ground_profile=ordinary_profile(),
            budget=RouteValidationBudget(4),
        )
        self.assertIs(
            result.disposition, ActiveRouteValidationDisposition.CONTINUE,
        )

    def test_progress_rejects_stale_sequence_and_wrong_fixed_route_identity(self):
        _, _, route = self._route()
        plan = route.validation_plan.action_plans[0]
        tracker = ActiveRouteTracker(route)

        stale = tracker.record_progress(
            0,
            RouteProgressEvidence(0, plan.fixed_route_id, .5, 6),
            observation_sequence_id=7,
        )
        self.assertIs(stale.disposition, ActiveRouteValidationDisposition.STOP)
        self.assertIs(stale.reason, ActiveRouteValidationReason.PROGRESS_EVIDENCE_STALE)

        tracker = ActiveRouteTracker(route)
        wrong = tracker.record_progress(
            0,
            RouteProgressEvidence(0, "another-route", .5, 7),
            observation_sequence_id=7,
        )
        self.assertIs(wrong.disposition, ActiveRouteValidationDisposition.STOP)
        self.assertIs(wrong.reason, ActiveRouteValidationReason.PROGRESS_IDENTITY_MISMATCH)

        missing = ActiveRouteTracker(route).record_progress(
            0, None, observation_sequence_id=7,
        )
        self.assertIs(
            missing.reason, ActiveRouteValidationReason.PROGRESS_EVIDENCE_MISSING,
        )

    def test_progress_cannot_skip_or_leave_fixed_route_without_current_evidence(self):
        _, route = self._mixed_route()
        first = route.validation_plan.action_plans[0]

        for action_index, evidence in (
            (1, None),
            (2, RouteProgressEvidence(
                0, first.fixed_route_id, first.legs[-1].end_progress_blocks, 70,
            )),
            (999, RouteProgressEvidence(
                0, first.fixed_route_id, first.legs[-1].end_progress_blocks, 70,
            )),
        ):
            with self.subTest(action_index=action_index):
                tracker = ActiveRouteTracker(route)
                before = tracker.effective_dependencies

                result = tracker.record_progress(
                    action_index, evidence, observation_sequence_id=70,
                )

                self.assertIs(
                    result.disposition,
                    ActiveRouteValidationDisposition.STOP,
                )
                self.assertEqual(tracker.effective_dependencies, before)

    def test_pending_boundary_is_rechecked_and_cleared_after_body_moves_away(self):
        world, active, executor, _, boundary = self._walk_drop_executor()
        control = RouteControl(active, executor)
        before = control.effective_dependencies

        self.assertTrue(executor.enter_upcoming_action_boundary(1, boundary))
        self.assertEqual(executor.pending_action_boundary_index, 1)
        moved = make_frame(world, 82, (.5, 1.0, 1.5))
        self.assertFalse(executor.enter_upcoming_action_boundary(1, moved))
        self.assertIsNone(executor.pending_action_boundary_index)
        decision = FixedRouteDecision(
            FixedRouteState.RUNNING,
            MovementV1(forward=1),
            .25,
            0.0,
            (),
            0,
            1,
            "tracking",
        )
        with patch.object(
            type(executor._controller), "decide", return_value=decision,
        ):
            advance = control.advance(moved, None, None)

        self.assertEqual(advance.decision.action_index, 0)
        self.assertEqual(control.tracker.identity.action_index, 0)
        self.assertEqual(control.effective_dependencies, before)

    def test_pending_boundary_requires_a_typed_entry_window(self):
        _, active, _, initial, boundary = self._walk_drop_executor()
        actions = active.action_route.actions
        route_without_window = replace(
            active.action_route,
            actions=(actions[0], replace(actions[1], entry_window=None)),
        )
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )
        executor.start(route_without_window, initial)

        entered = executor.enter_upcoming_action_boundary(1, boundary)

        self.assertFalse(entered)
        self.assertIsNone(executor.pending_action_boundary_index)
        self.assertEqual(executor.action_index, 0)

    def test_pending_boundary_clears_on_stop_and_successful_advance(self):
        _, _, executor, _, boundary = self._walk_drop_executor()
        self.assertIsNone(executor.pending_action_boundary_index)
        self.assertTrue(executor.enter_upcoming_action_boundary(1, boundary))

        executor.request_stop(StopCause.GOAL_REVISED)

        self.assertIsNone(executor.pending_action_boundary_index)

        _, _, executor, _, boundary = self._walk_drop_executor()
        self.assertTrue(executor.enter_upcoming_action_boundary(1, boundary))
        decision = FixedRouteDecision(
            FixedRouteState.RUNNING,
            MovementV1(forward=1),
            .75,
            0.0,
            (),
            0,
            1,
            "tracking",
        )
        with patch.object(
            type(executor._controller), "decide", return_value=decision,
        ):
            advanced = executor.decide(boundary)

        self.assertEqual(advanced.action_index, 1)
        self.assertIsNone(executor.pending_action_boundary_index)

    def test_goal_revision_and_acquisition_stop_clear_pending_boundary(self):
        for cause in (StopCause.GOAL_REVISED, StopCause.ROUTE_REPLACED):
            with self.subTest(cause=cause):
                _, active, executor, initial, boundary = (
                    self._walk_drop_executor()
                )
                supervisor = ExecutionSupervisor()
                self.assertTrue(supervisor.offer_route(
                    RouteControl(active, executor), initial,
                    InputApplicationLedger(), _ground_anchor(initial),
                ))
                session = NavigationSession(
                    f"d058-pending-{cause.value}",
                    NavigationSessionTests().profiles(),
                    planner_worker=_InlinePlanner(),
                )
                session._supervisor = supervisor
                self.assertTrue(
                    executor.enter_upcoming_action_boundary(1, boundary),
                )

                self.assertFalse(session._request_probe_stop(cause))

                self.assertIsNone(executor.pending_action_boundary_index)

    def test_superseded_route_clears_pending_boundary_before_precondition(self):
        _, active, executor, initial, boundary = self._walk_drop_executor()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            RouteControl(active, executor), initial,
            InputApplicationLedger(), _ground_anchor(initial),
        ))
        session = NavigationSession(
            "d058-pending-superseded",
            NavigationSessionTests().profiles(),
            planner_worker=_InlinePlanner(),
        )
        session._supervisor = supervisor
        session._request = SurfacePlanningRequest(
            2,
            "d058-superseding-request",
            active.goal_id,
            2,
            active.world_session,
            active.corridor.node_ids[0],
            active.corridor.node_ids[-1],
        )
        self.assertTrue(executor.enter_upcoming_action_boundary(1, boundary))

        proposal = session._prepare_route_action(boundary, 2_000_000_000)

        self.assertIsNone(proposal)
        self.assertIsNone(executor.pending_action_boundary_index)

    def test_supervisor_empty_changes_skip_dependency_materialization_and_queries(self):
        control, current, anchor = ExecutionSupervisorTests()._control()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            control, current, InputApplicationLedger(), anchor,
        ))
        assert control.tracker is not None

        original_validate = ActiveRouteTracker.validate
        validation_calls = []

        def validate_identity_only(instance, *args, **kwargs):
            validation_calls.append((args, kwargs))
            return original_validate(instance, *args, **kwargs)

        with patch.object(
            ActiveRouteTracker,
            "effective_dependencies",
            new_callable=PropertyMock,
            side_effect=AssertionError("empty changes must not materialize dependencies"),
        ), patch.object(
            ActiveRouteTracker,
            "validate",
            new=validate_identity_only,
        ), patch(
            "mc2p.motion_nav.execution_supervisor.WorldQueryCache",
            side_effect=AssertionError("empty changes must not create a cache"),
        ):
            result = supervisor._validate_routes(current)

        self.assertIs(
            result.incumbent.disposition,
            ActiveRouteValidationDisposition.UNAFFECTED,
        )
        self.assertEqual(len(validation_calls), 1)
        self.assertEqual(validation_calls[0][0][1], ())
        self.assertNotIn("effective_dependencies", validation_calls[0][1])
        self.assertNotIn("query_cache", validation_calls[0][1])
    def test_supervisor_materializes_each_changed_controls_dependencies_once(self):
        control, current, anchor = ExecutionSupervisorTests()._control()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            control, current, InputApplicationLedger(), anchor,
        ))
        assert control.tracker is not None
        dependencies = control.tracker.effective_dependencies
        changed = replace(current, changed_cells=(dependencies[0],))
        original_validate = ActiveRouteTracker.validate
        calls = []

        def validate_once(instance, *args, **kwargs):
            calls.append(kwargs)
            return original_validate(instance, *args, **kwargs)

        with patch.object(
            ActiveRouteTracker,
            "effective_dependencies",
            new_callable=PropertyMock,
            return_value=dependencies,
        ) as materialized, patch.object(
            ActiveRouteTracker, "validate", new=validate_once,
        ):
            supervisor._validate_routes(changed)

        self.assertEqual(materialized.call_count, 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("effective_dependencies", calls[0])

    def test_tracker_validate_has_no_external_dependency_override(self):
        world, _, route = self._route()
        tracker = ActiveRouteTracker(route)

        with self.assertRaises(TypeError):
            tracker.validate(
                world.view(), ((99, 99, 99),),
                effective_dependencies=(),
            )

    def test_supervisor_empty_changes_still_rejects_foreign_world(self):
        control, current, anchor = ExecutionSupervisorTests()._control()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            control, current, InputApplicationLedger(), anchor,
        ))
        foreign = WorldKnowledge(WorldSessionId("d058-foreign-empty-change"))
        changed_world = replace(current, world=foreign.view(), changed_cells=())

        result = supervisor._validate_routes(changed_world)

        self.assertIs(
            result.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertIs(
            result.incumbent.reason,
            ActiveRouteValidationReason.ROUTE_IDENTITY_CHANGED,
        )

    def test_supervisor_empty_changes_preserves_sticky_progress_failure(self):
        control, current, anchor = ExecutionSupervisorTests()._control()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            control, current, InputApplicationLedger(), anchor,
        ))
        assert control.tracker is not None
        failed = control.tracker.record_progress(
            999, None, observation_sequence_id=current.body.sequence_id,
        )
        self.assertIs(failed.disposition, ActiveRouteValidationDisposition.STOP)

        result = supervisor._validate_routes(replace(current, changed_cells=()))

        self.assertIs(
            result.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertIs(
            result.incumbent.reason,
            ActiveRouteValidationReason.PROGRESS_ACTION_INDEX_INVALID,
        )

    def test_actual_walk_decision_exports_progress_but_no_controller_does_not(self):
        world, candidate, route = self._route(goal=(2, 1))
        initial = make_frame(world, 2, candidate.path[0].position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, initial)
        fixed_route_id = route.action_route.actions[0].fixed_route.route_id
        fixed = FixedRouteDecision(
            FixedRouteState.RUNNING, MovementV1(forward=1), .25, 0.0, (), 0, 1,
            "tracking",
        )
        with patch.object(type(executor._controller), "decide", return_value=fixed):
            decision = executor.decide(initial)

        self.assertEqual(
            decision.route_progress_evidence,
            RouteProgressEvidence(0, fixed_route_id, .25, initial.body.sequence_id),
        )

        idle = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        ).decide(initial)
        self.assertIsNone(idle.route_progress_evidence)

        step_world, step_candidate, step_request = B07StepRouteTests().candidate()
        step_initial = make_frame(
            step_world, 0, step_candidate.path[0].position,
        )
        step_route = RouteAdmitter().admit_surface(
            step_candidate, step_initial,
            expected_request_id=step_request.request_id,
            goal_id=step_request.goal_id,
            goal_revision=step_request.goal_revision,
            changed_cells=(),
        ).route
        strict = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        strict.start(step_route.action_route, step_initial)
        self.assertIsNone(strict.decide(step_initial).route_progress_evidence)

    def test_failed_ground_decision_does_not_retire_walk_owners(self):
        world, candidate, route = self._route()
        initial = make_frame(world, 2, candidate.path[0].position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, initial)
        control = RouteControl(route, executor)
        before = control.effective_dependencies
        failed = FixedRouteDecision(
            FixedRouteState.BLOCKED,
            MovementV1(),
            2.0,
            0.0,
            (),
            0,
            1,
            "blocked",
        )

        with patch.object(
            type(executor._controller), "decide", return_value=failed,
        ):
            advance = control.advance(initial, None, None)

        self.assertIs(advance.decision.state, ActionRouteState.BLOCKED)
        self.assertEqual(control.effective_dependencies, before)

    def test_terminal_replacement_does_not_mislabel_initial_connection_recipe(self):
        world = flat_surface_world(6)
        goal = GoalState(
            Aabb(1.45, .99, 2.85, 1.55, 1.01, 2.95),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        request, candidate = _candidate(
            world, (1, 1), (1, 2), request_id="d058-terminal-initial",
            goal_state=goal,
        )

        # This case exercises the fail-closed owner with no exact initial
        # proof. F2 can now rebind a real first-leg proof to its reference.
        with patch.object(RouteAdmitter, '_forward_ground_entry', return_value=None):
            route = _admit(world, request, candidate, (1.5, 1, 1.636))

        initial = route.validation_plan.initial_connection
        self.assertIsNotNone(initial)
        owner = route.validation_plan.owner(initial.owner_ref)
        self.assertIs(owner.kind, DependencyOwnerKind.INITIAL_CONNECTION)
        self.assertIsNone(owner.recipe_ref)
        progress = ActiveRouteTracker(route).record_progress(
            0,
            RouteProgressEvidence(
                0,
                route.action_route.actions[0].fixed_route.route_id,
                .1,
                3,
            ),
            observation_sequence_id=3,
        )
        self.assertIs(
            progress.disposition, ActiveRouteValidationDisposition.CONTINUE,
        )
        changed = route.validation_plan.dependencies_for_owner(owner.owner_id)[0]
        invalidated = ActiveRouteTracker(route).validate(
            world.view(), (changed,),
            ground_profile=ordinary_profile(),
            budget=RouteValidationBudget(4),
        )
        self.assertIs(
            invalidated.disposition, ActiveRouteValidationDisposition.STOP,
        )
        self.assertIs(
            invalidated.reason,
            ActiveRouteValidationReason.OWNER_REFERENCE_INVALID,
        )

    def test_supervisor_discards_invalid_pending_without_stopping_incumbent(self):
        world, candidate, route = self._route(goal=(2, 1))
        current = make_frame(world, 3, candidate.path[0].position)
        incumbent_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        incumbent_executor.start(route.action_route, current)
        incumbent = RouteControl(route, incumbent_executor)
        pending_route = replace(
            route,
            route_id="d058-invalid-pending",
            source_request_id="d058-pending-request",
            validation_plan=None,
        )
        pending = RouteControl(
            pending_route, _FrameExecutor(pending_route.action_route),
        )
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        anchor = _ground_anchor(current)
        self.assertTrue(supervisor.offer_route(incumbent, current, ledger, anchor))
        self.assertTrue(supervisor.offer_route(pending, current, ledger, anchor))
        changed = next(
            item.position for item in route.validation_plan.dependency_provenance
            if len(item.owner_refs) == 1
        )
        changed_frame = replace(current, changed_cells=(changed,))

        with patch.object(RouteControl, "request_stop") as stopped:
            advance = supervisor.advance_body(changed_frame, ledger, anchor)

        self.assertIs(advance.route_advance.control, incumbent)
        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )
        self.assertIs(
            advance.route_validation.pending.reason,
            ActiveRouteValidationReason.PLAN_UNAVAILABLE,
        )
        self.assertFalse(supervisor.has_pending_route)
        stopped.assert_called_once()
        self.assertEqual(
            stopped.call_args.args,
            (StopCause.DEPENDENCY_CHANGED,),
        )

    def test_incumbent_blocker_stops_before_any_forward_input(self):
        world, candidate, route = self._route(goal=(2, 1))
        initial = make_frame(world, 2, candidate.path[0].position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, initial)
        control = RouteControl(route, executor)
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        anchor = _ground_anchor(initial)
        self.assertTrue(supervisor.offer_route(control, initial, ledger, anchor))
        collision_air = next(
            position for position in route.validation_plan.recipes[0].dependencies
            if (position[1] >= 1
                and world.view().cell(position).knowledge is CellKnowledge.AIR)
        )
        world.observe_blocks(
            ObservationStamp(world.session, 3, 3, "test-clock", 150_000_000),
            {collision_air: BlockGeometry.full_cube("minecraft:stone")},
        )
        changed = make_frame(world, 3, candidate.path[0].position)
        changed = replace(changed, changed_cells=(collision_air,))

        advance = supervisor.advance_body(
            changed, ledger, _ground_anchor(changed),
        )

        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertFalse(advance.route_advance.decision.movement.forward)

    def test_all_fail_closed_owner_classes_stop_in_the_same_frame(self):
        for kind in ("removed_support", "unsupported_material"):
            with self.subTest(kind=kind):
                world, _, route = self._route(
                    goal=(2, 1), request_id=f"d058-{kind}",
                )
                support = next(
                    position
                    for position in route.validation_plan.recipes[0].dependencies
                    if world.view().cell(position).knowledge
                        is CellKnowledge.BLOCK
                )
                stamp = ObservationStamp(
                    world.session, 61, 61, "test-clock", 3_050_000_000,
                )
                if kind == "removed_support":
                    def mutate(w=world, s=stamp, p=support):
                        w.confirm_air(s, (p,))
                else:
                    def mutate(w=world, s=stamp, p=support):
                        w.observe_blocks(s, {
                            p: BlockGeometry.full_cube("minecraft:dirt"),
                        })
                self._assert_same_frame_stop_without_forward(
                    world, route, support, mutate,
                )

        world, route = self._mixed_route()
        strict_owner = next(
            owner for owner in route.validation_plan.owners
            if owner.kind is DependencyOwnerKind.STRICT_ACTION
        )
        strict_cell = next(
            item.position for item in route.validation_plan.dependency_provenance
            if strict_owner.owner_id in item.owner_refs and item.position[1] >= 1
        )
        self._assert_same_frame_stop_without_forward(
            world,
            route,
            strict_cell,
            lambda: world.observe_blocks(
                ObservationStamp(
                    world.session, 61, 61, "test-clock", 3_050_000_000,
                ),
                {strict_cell: BlockGeometry.full_cube("minecraft:stone")},
            ),
        )

        world = flat_surface_world(6)
        goal = GoalState(
            Aabb(1.45, .99, 2.85, 1.55, 1.01, 2.95),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        request, candidate = _candidate(
            world, (1, 1), (1, 2), request_id="d058-unmapped-runtime",
            goal_state=goal,
        )
        with patch.object(RouteAdmitter, '_forward_ground_entry', return_value=None):
            route = _admit(world, request, candidate, (1.5, 1, 1.636))
        initial = route.validation_plan.initial_connection
        owner = route.validation_plan.owner(initial.owner_ref)
        unmapped_cell = route.validation_plan.dependencies_for_owner(
            owner.owner_id
        )[0]
        self._assert_same_frame_stop_without_forward(
            world, route, unmapped_cell, lambda: None,
        )

    def test_four_forward_unknown_air_confirmations_keep_formal_route_running(self):
        session = WorldSessionId("d058-four-air-confirmations")
        world = WorldKnowledge(session)
        observed = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
        world.confirm_air(observed, tuple(
            (x, y, z)
            for x in range(-1, 6)
            for y in (1, 2, 3)
            for z in range(-1, 4)
        ))
        world.observe_blocks(observed, {
            (x, 0, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-1, 6)
            for z in range(-1, 4)
        })
        request, candidate = _candidate(
            world, (0, 1), (4, 1), request_id="d058-four-air-route",
        )
        route = _admit(
            world, request, candidate, candidate.path[0].position,
        )
        changed_positions = tuple((x, -1, 1) for x in range(1, 5))
        self.assertTrue(all(
            world.view().cell(position).knowledge is CellKnowledge.UNKNOWN
            for position in changed_positions
        ))
        initial = make_frame(world, 2, candidate.path[0].position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, initial)
        control = RouteControl(route, executor)
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        self.assertTrue(supervisor.offer_route(
            control, initial, ledger, _ground_anchor(initial),
        ))

        validations = []
        reasons = []
        for sequence, position in enumerate(changed_positions, 3):
            world.confirm_air(
                ObservationStamp(
                    session, sequence, sequence, "test-clock",
                    sequence * 50_000_000,
                ),
                (position,),
            )
            current = replace(
                make_frame(world, sequence, candidate.path[0].position),
                changed_cells=(position,),
            )
            advance = supervisor.advance_body(
                current, ledger, _ground_anchor(current),
            )
            validations.append(advance.route_validation.incumbent)
            reasons.append(advance.route_advance.decision.reason_code)

        self.assertTrue(all(
            result.disposition is ActiveRouteValidationDisposition.CONTINUE
            and result.reason is ActiveRouteValidationReason.REVALIDATED
            for result in validations
        ))
        self.assertEqual(
            tuple(result.affected_cells for result in validations),
            tuple((position,) for position in changed_positions),
        )
        self.assertNotIn("cancel_braking", reasons)
        self.assertEqual(supervisor.async_work_diagnostics, ())

    def test_incumbent_and_pending_share_exact_four_query_frame_budget(self):
        world, _, route = self._route(goal=(3, 1))
        shared = next(
            item.position for item in route.validation_plan.dependency_provenance
            if len(item.owner_refs) == 2
        )
        current = replace(
            make_frame(world, 3, (1.5, 1, 1.5)),
            changed_cells=(shared,),
        )
        incumbent_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        incumbent_executor.start(route.action_route, current)
        incumbent = RouteControl(route, incumbent_executor)
        successor_route = replace(
            route,
            route_id="d058-four-query-pending",
            source_request_id="d058-four-query-request",
        )
        pending_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        pending_executor.start(successor_route.action_route, current)
        pending = RouteControl(successor_route, pending_executor)
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        anchor = _ground_anchor(current)
        self.assertTrue(supervisor.offer_route(incumbent, current, ledger, anchor))
        self.assertTrue(supervisor.offer_route(pending, current, ledger, anchor))

        advance = supervisor.advance_body(current, ledger, anchor)

        self.assertEqual(advance.route_validation.incumbent.queries_used, 2)
        self.assertEqual(advance.route_validation.pending.queries_used, 2)
        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )
        self.assertIs(
            advance.route_validation.pending.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )

    def test_straight_following_has_no_dependency_braking_or_recovery_work(self):
        from tests.sim.known_world_following import SCENARIO_BY_NAME, run_scenario

        result = run_scenario(SCENARIO_BY_NAME["straight_2_0"])

        self.assertTrue(result["passed"])
        self.assertEqual(result["task_recoveries"], 0)
        self.assertNotIn(
            "cancel_braking",
            [item["reason"] for item in result["state_history"]],
        )


if __name__ == "__main__":
    unittest.main()
