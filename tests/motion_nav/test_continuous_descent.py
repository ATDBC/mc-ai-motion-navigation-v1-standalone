from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, MotionTickPhase, StateAnchor,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, VisualAirEvidence, WorldKnowledge,
    WorldSessionId,
)


SESSION = WorldSessionId("continuous-descent-test")


def world_and_anchor(*, direct_height=1, stair_count=0, speed=1.5,
                     material="minecraft:grass_block"):
    stamp = ObservationStamp(SESSION, 0, 0, "test", 0)
    knowledge = WorldKnowledge(SESSION)
    knowledge.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-4, 5)
        for y in range(20, 71)
        for z in range(-4, max(10, stair_count + 4))
    ))
    blocks = {(0, 63, 0): BlockGeometry.full_cube(material)}
    if stair_count:
        for index in range(1, stair_count + 1):
            blocks[(0, 63 - index, index)] = BlockGeometry.full_cube(
                material
            )
    else:
        blocks[(0, 63 - direct_height, 1)] = BlockGeometry.full_cube(
            material
        )
    knowledge.observe_blocks(stamp, blocks)
    state = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        SESSION, 10, (.5, 64., .5),
        (0., -.0784000015258789, speed / 20.0),
        0., 0., "standing", .6, 1.8, True, False, True,
        False, False, 0, 0., .1, .6, .08, .42, 20, 5., "survival",
        (), False, False, False, False, False, False,
    )
    anchor = StateAnchor(
        SESSION, 3, 10, MotionTickPhase.AFTER_MOVEMENT,
        None, None, JAVA_1_21_RULESET.ruleset_id,
        JAVA_1_21_RULESET.state_schema, "mc2p.input-projection.v1", state,
        health_points=20., absorption_points=0.,
    )
    return anchor, PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)


class ContinuousDescentTests(unittest.TestCase):
    def request(self, anchor, height, *, budget=TaskDamageBudget(),
                keep_moving=False):
        from mc2p.motion_nav.motion_solver import (
            AirTransitionSolveRequest, DEFAULT_AIR_TRANSITION_POLICIES,
            LandingRegion, MotionSolveKind,
        )

        target_z = math.floor(anchor.physics_state.position[2]) + 1
        target_y = anchor.physics_state.position[1] - height
        return AirTransitionSolveRequest(
            MotionSolveKind.CONTROLLED_DROP,
            (0, 1),
            LandingRegion(.3, .7, target_z + .3, target_z + .7, target_y),
            CandidateExecutionWindow(
                anchor.movement_tick_id + 1,
                anchor.movement_tick_id + 2,
            ),
            budget,
            max_candidates=64,
            max_ticks=80,
            exit_direction=(0, 1) if keep_moving else None,
            exit_motion_ticks=1 if keep_moving else 0,
            policy=DEFAULT_AIR_TRANSITION_POLICIES[
                MotionSolveKind.CONTROLLED_DROP
            ],
        )

    def test_direct_drop_is_one_proof_for_one_two_and_three_blocks(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_air_transition

        for height in (1, 2, 3):
            with self.subTest(height=height):
                anchor, world = world_and_anchor(direct_height=height)
                result = solve_air_transition(
                    anchor, world, self.request(anchor, height),
                )
                self.assertIs(result.status, SolveStatus.SOLVED)
                self.assertEqual(result.proof.maximum_expected_damage_points, 0.0)
                self.assertTrue(result.proof.landing.contains(
                    result.proof.exit_state
                ))
                self.assertEqual(result.proof.kind.value, "controlled_drop")
                self.assertLess(len(result.proof.commands), 80)

    def test_direct_drop_can_start_from_a_confirmed_sneak_edge_probe(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_air_transition

        for height in (1, 2, 3):
            with self.subTest(height=height):
                anchor, world = world_and_anchor(
                    direct_height=height, speed=0.0,
                )
                edge_state = replace(
                    anchor.physics_state,
                    position=(.5, 64.0, 1.29),
                    velocity_blocks_per_tick=(0.0, -.0784000015258789, 0.0),
                    pose="crouching",
                    body_height=1.5,
                    sneaking=True,
                )
                edge_anchor = replace(anchor, physics_state=edge_state)

                result = solve_air_transition(
                    edge_anchor, world, self.request(anchor, height),
                )

                self.assertIs(result.status, SolveStatus.SOLVED)
                self.assertEqual(result.proof.entry_state, edge_state)
                self.assertIn(
                    "crouch_exited",
                    tuple(event for events in result.proof.step_events
                          for event in events),
                )
                self.assertTrue(result.proof.landing.contains(
                    result.proof.exit_state
                ))

    def test_damage_budget_gates_one_complete_direct_fall(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_air_transition

        height = 6
        anchor, world = world_and_anchor(direct_height=height)
        unrestricted = solve_air_transition(
            anchor, world,
            self.request(
                anchor, height,
                budget=TaskDamageBudget("measure-direct-fall", 20.0),
            ),
        )
        self.assertIs(unrestricted.status, SolveStatus.SOLVED)
        damage = unrestricted.proof.maximum_expected_damage_points
        self.assertGreater(damage, 0.0)

        allowed = solve_air_transition(
            anchor, world,
            self.request(
                anchor, height,
                budget=TaskDamageBudget("allow-direct-fall", damage),
            ),
        )
        rejected = solve_air_transition(
            anchor, world,
            self.request(
                anchor, height,
                budget=TaskDamageBudget(
                    "reject-direct-fall", max(0.0, damage - 1.0),
                ),
            ),
        )
        self.assertIs(allowed.status, SolveStatus.SOLVED)
        self.assertIs(rejected.status, SolveStatus.NO_SOLUTION_WITHIN_SEARCH)

    def test_two_four_and_eight_step_descents_keep_a_moving_exit(self):
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_air_transition

        for count in (2, 4, 8):
            with self.subTest(count=count):
                anchor, world = world_and_anchor(stair_count=count)
                for index in range(count):
                    result = solve_air_transition(
                        anchor, world,
                        self.request(
                            anchor, 1, keep_moving=index + 1 < count,
                        ),
                    )
                    self.assertIs(result.status, SolveStatus.SOLVED)
                    if index + 1 < count:
                        speed = math.hypot(
                            result.proof.exit_state.velocity_blocks_per_tick[0],
                            result.proof.exit_state.velocity_blocks_per_tick[2],
                        ) * 20.0
                        self.assertGreater(speed, .1)
                    anchor = replace(
                        anchor,
                        observation_sequence_id=anchor.observation_sequence_id + 1,
                        movement_tick_id=result.proof.exit_state.movement_tick_id,
                        physics_state=result.proof.exit_state,
                    )

    def test_surface_planner_exposes_direct_drop_only_with_sufficient_budget(self):
        from mc2p.motion_nav.known_map_planner import (
            KnownMapBounds, SurfaceControlledDropEdge, _SurfaceExpander,
        )
        from mc2p.motion_nav.movement_transition import MovementMode
        from tests.motion_nav.test_b07_surface_planning import (
            ordinary_profile, step_profile,
        )
        from tests.motion_nav.test_b09_air_transitions import air_profile

        _, physics_world = world_and_anchor(
            direct_height=6, material="minecraft:stone",
        )
        world = physics_world._world
        bounds = KnownMapBounds(0, 0, 50, 65, 0, 1, True)

        def edges(budget):
            expander = _SurfaceExpander(
                world, bounds, ordinary_profile(), step_profile(),
                air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
                damage_budget=budget,
            )
            start = max(expander.column(0, 0), key=lambda node: node.position[1])
            end = min(expander.column(0, 1), key=lambda node: node.position[1])
            return expander._build_edges(start, end)

        self.assertFalse(any(
            type(edge) is SurfaceControlledDropEdge
            for edge in edges(TaskDamageBudget())
        ))
        self.assertTrue(any(
            type(edge) is SurfaceControlledDropEdge
            for edge in edges(TaskDamageBudget("allow-direct-fall", 3.0))
        ))

    def test_formal_snapshot_planner_returns_one_direct_drop_segment(self):
        from mc2p.motion_nav.known_map_planner import (
            KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
            SurfaceControlledDropEdge, SurfacePlanningRequest,
            SurfacePlanningStatus, plan_known_surface_snapshot,
        )
        from mc2p.motion_nav.movement_transition import MovementMode
        from mc2p.motion_nav.support_surfaces import SurfaceNodeId
        from tests.motion_nav.test_b07_surface_planning import (
            ordinary_profile, step_profile,
        )
        from tests.motion_nav.test_b09_air_transitions import air_profile

        _, physics_world = world_and_anchor(
            direct_height=6, material="minecraft:stone",
        )
        world = physics_world._world
        bounds = KnownMapBounds(0, 0, 58, 64, 0, 1, True)
        progress = KnownMapSnapshotBuilder(world, bounds).advance(world, 10_000)
        self.assertIs(progress.status, SnapshotBuildStatus.COMPLETE)
        request = SurfacePlanningRequest(
            1, "direct-six-block-drop", "landing", 1,
            world.session.value,
            SurfaceNodeId(0, 0, 64, 0),
            SurfaceNodeId(0, 1, 58, 0),
            damage_budget=TaskDamageBudget("allow-direct-fall", 3.0),
        )

        candidate = plan_known_surface_snapshot(
            progress.snapshot, ordinary_profile(), step_profile(), request,
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )

        self.assertIs(
            candidate.status, SurfacePlanningStatus.COMPLETE,
            (candidate.reasons, candidate.expanded_nodes),
        )
        self.assertEqual(len(candidate.segments), 1)
        self.assertIs(type(candidate.segments[0]), SurfaceControlledDropEdge)

    def test_multi_block_drop_defers_visual_evidence_to_action_boundary(self):
        from mc2p.motion_nav.action_preconditions import (
            ActionPreconditionStatus, check_action_precondition,
        )
        from mc2p.motion_nav.known_map_planner import (
            KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest,
            plan_known_surface_snapshot,
        )
        from mc2p.motion_nav.movement_transition import MovementMode
        from mc2p.motion_nav.route_admission import (
            AdmissionStatus, RouteAdmitter,
        )
        from mc2p.motion_nav.support_surfaces import SurfaceNodeId
        from tests.motion_nav.test_b07_surface_planning import (
            ordinary_profile, step_profile,
        )
        from tests.motion_nav.test_b09_air_transitions import air_profile, frame

        _, physics_world = world_and_anchor(
            direct_height=6, material="minecraft:stone",
        )
        view = physics_world._world
        owner = view._owner
        assert owner is not None
        bounds = KnownMapBounds(0, 0, 58, 64, 0, 1, True)
        snapshot = KnownMapSnapshotBuilder(view, bounds).advance(
            view, 10_000,
        ).snapshot
        request = SurfacePlanningRequest(
            1, "drop-needs-bottom-evidence", "landing", 1,
            view.session.value,
            SurfaceNodeId(0, 0, 64, 0),
            SurfaceNodeId(0, 1, 58, 0),
            damage_budget=TaskDamageBudget("allow-direct-fall", 3.0),
        )
        candidate = plan_known_surface_snapshot(
            snapshot, ordinary_profile(), step_profile(), request,
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )
        initial = frame(owner, 2, (.5, 64., .5), (0., 0., 1.5), on_ground=True)

        admitted = RouteAdmitter().admit_surface(
            candidate, initial, expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        )

        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertIsNotNone(admitted.route)
        missing = check_action_precondition(
            admitted.route, 0, initial, task_id="drop-evidence-task",
        )
        self.assertIs(
            missing.status, ActionPreconditionStatus.NEEDS_ACQUISITION,
        )
        self.assertEqual(missing.missing_cells, ((0, 58, 1),))

        evidence_stamp = ObservationStamp(
            SESSION, 3, 3, "test-clock", 150_000_000,
        )
        owner.confirm_air(
            evidence_stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(
                evidence_stamp, 6.0, False,
            )},
        )
        still_missing = check_action_precondition(
            admitted.route, 0,
            frame(owner, 3, (.5, 64., .5), (0., 0., 1.5), on_ground=True),
            task_id="drop-evidence-task",
        )
        self.assertIs(
            still_missing.status, ActionPreconditionStatus.NEEDS_ACQUISITION,
        )

        edge_frame = frame(
            owner, 3, (.5, 64., 1.29), (0., 0., 0.), on_ground=True,
        )
        edge_frame = replace(
            edge_frame,
            body=replace(
                edge_frame.body, pose="crouching", is_sneaking=True,
            ),
        )
        upper_only_at_edge = check_action_precondition(
            admitted.route, 0, edge_frame, task_id="drop-evidence-task",
        )
        self.assertIs(
            upper_only_at_edge.status,
            ActionPreconditionStatus.NEEDS_ACQUISITION,
        )

        edge_bottom_stamp = ObservationStamp(
            SESSION, 4, 4, "test-clock", 200_000_000,
        )
        owner.confirm_air(
            edge_bottom_stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(
                edge_bottom_stamp, 6.0, True,
            )},
        )
        unowned_edge_frame = frame(
            owner, 4, (1.15, 64., .85), (0., 0., 0.), on_ground=True,
        )
        unowned_edge_frame = replace(
            unowned_edge_frame,
            body=replace(
                unowned_edge_frame.body, pose="crouching", is_sneaking=True,
            ),
        )
        unowned_edge_evidence = check_action_precondition(
            admitted.route, 0,
            unowned_edge_frame,
            task_id="drop-evidence-task",
        )
        self.assertIs(
            unowned_edge_evidence.status,
            ActionPreconditionStatus.NEEDS_ACQUISITION,
        )

        edge_probe = LandingEdgeProbe(
            request.goal_id, request.goal_revision, (0, 58, 1), 3,
        )
        edge_probe.movement(frame(
            owner, 3, (.5, 64., .5), (0., 0., 0.), on_ground=True,
        ))
        edge_probe.vantage_position = (1.15, .85)
        self.assertEqual(
            edge_probe.movement(unowned_edge_frame), MovementV1(sneak=True),
        )
        edge_not_ready = check_action_precondition(
            admitted.route, 0,
            unowned_edge_frame,
            edge_probe=edge_probe,
            task_id="drop-evidence-task",
        )
        self.assertIs(
            edge_not_ready.status,
            ActionPreconditionStatus.NEEDS_ACQUISITION,
        )

        edge_probe.begin_entry_alignment(unowned_edge_frame)
        entry_frame = frame(
            owner, 5, (.5, 64., .85), (0., 0., 0.), on_ground=True,
        )
        entry_frame = replace(
            entry_frame,
            body=replace(
                entry_frame.body, pose="crouching", is_sneaking=True,
            ),
        )
        self.assertEqual(edge_probe.movement(entry_frame), MovementV1())
        standing_entry = replace(
            entry_frame,
            body=replace(
                entry_frame.body, pose="standing", is_sneaking=False,
            ),
        )
        self.assertTrue(edge_probe.finish_release(standing_entry))
        owned_edge_evidence = check_action_precondition(
            admitted.route, 0,
            standing_entry,
            edge_probe=edge_probe,
            task_id="drop-evidence-task",
        )
        self.assertIs(
            owned_edge_evidence.status, ActionPreconditionStatus.READY,
        )

        bottom_stamp = ObservationStamp(
            SESSION, 5, 5, "test-clock", 250_000_000,
        )
        owner.confirm_air(
            bottom_stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(
                bottom_stamp, 4.0, True,
            )},
        )
        bottom_ready = check_action_precondition(
            admitted.route, 0,
            frame(owner, 5, (.5, 64., .5), (0., 0., 1.5), on_ground=True),
            task_id="drop-evidence-task",
        )
        self.assertIs(bottom_ready.status, ActionPreconditionStatus.READY)

    def test_drop_damage_is_committed_when_the_body_leaves_support(self):
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
        from mc2p.motion_nav.known_map_planner import (
            KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest,
            plan_known_surface_snapshot,
        )
        from mc2p.motion_nav.movement_transition import MovementMode
        from mc2p.motion_nav.route_admission import RouteAdmitter
        from mc2p.motion_nav.support_surfaces import SurfaceNodeId
        from tests.motion_nav.test_b07_surface_planning import ordinary_profile
        from tests.motion_nav.test_b07_step_transition import (
            profile as step_profile,
        )
        from tests.motion_nav.test_b09_air_transitions import air_profile, frame
        from tests.motion_nav.test_jump_up import jump_profile

        _, physics_world = world_and_anchor(
            direct_height=6, material="minecraft:stone",
        )
        view = physics_world._world
        owner = view._owner
        assert owner is not None
        stamp = ObservationStamp(
            SESSION, 2, 2, "test-clock", 100_000_000,
        )
        owner.confirm_air(
            stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(stamp, 4.0, True)},
        )
        fresh_view = owner.view()
        snapshot = KnownMapSnapshotBuilder(
            fresh_view, KnownMapBounds(0, 0, 58, 64, 0, 1, True),
        ).advance(fresh_view, 10_000).snapshot
        budget = TaskDamageBudget("allow-direct-fall", 3.0)
        request = SurfacePlanningRequest(
            1, "commit-drop-damage", "landing", 1,
            owner.session.value,
            SurfaceNodeId(0, 0, 64, 0),
            SurfaceNodeId(0, 1, 58, 0),
            damage_budget=budget,
        )
        profile = air_profile(MovementMode.CONTROLLED_DROP)
        candidate = plan_known_surface_snapshot(
            snapshot, ordinary_profile(), step_profile(), request,
            air_profiles=(profile,),
        )
        start_frame = frame(
            owner, 2, (.5, 64.0, .5), (0.0, 0.0, 0.0), on_ground=True,
        )
        admitted = RouteAdmitter().admit_surface(
            candidate, start_frame,
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIsNotNone(admitted.route)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
            air_profiles=(profile,),
        )
        executor.start(
            admitted.route.action_route, start_frame,
            damage_budget=budget,
            require_verified_gap_motion=False,
        )

        executor.decide(frame(
            owner, 3, (.5, 63.6, .8), (0.0, -2.0, 1.0), on_ground=False,
        ), input_confirmed=False)

        self.assertEqual(executor.completed_movement_damage_points, 3.0)

    def test_direct_fall_uses_existing_worker_admission_and_executor_chain(self):
        from mc2p.motion_nav.action_route import (
            ActionRoute, ControlledDropSegment,
        )
        from mc2p.motion_nav.controlled_drop import ControlledDropEdge
        from mc2p.motion_nav.motion_coordination import (
            GapPreparationStatus, prepare_planned_air_transition,
        )
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_air_transition
        from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
        from mc2p.motion_nav.support_surfaces import (
            HorizontalRegion, SupportSurface, SurfaceNodeId,
        )

        height = 6
        anchor, world = world_and_anchor(direct_height=height)
        measurement = solve_air_transition(
            anchor, world,
            self.request(
                anchor, height,
                budget=TaskDamageBudget("measure-direct-fall", 20.0),
            ),
        )
        self.assertIs(measurement.status, SolveStatus.SOLVED)
        damage = measurement.proof.maximum_expected_damage_points
        budget = TaskDamageBudget("direct-fall-task", damage)
        solved = solve_air_transition(
            anchor, world,
            self.request(anchor, height, budget=budget),
        )
        self.assertIs(solved.status, SolveStatus.SOLVED)

        start_id = SurfaceNodeId(0, 0, 64, 0)
        end_id = SurfaceNodeId(0, 1, 58, 0)
        start = SupportSurface(
            start_id, (.5, 64., .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end = SupportSurface(
            end_id, (.5, 58., 1.5), HorizontalRegion(0, 1, 1, 2),
            1.0, ("minecraft:grass_block",), (),
        )
        segment = ControlledDropSegment(
            ControlledDropEdge(start_id, end_id, "direct-fall", 1.0, ()),
            start, end, (),
        )
        action_route = ActionRoute("direct-fall-route", (segment,))
        route = ActiveRoute(
            "direct-fall-route", 1, "request", "goal", 1,
            SESSION.value, None, 1.0, 0.0, (),
            ExecutableCorridor((start_id, end_id), (), 1.0, end_id),
            action_route, planning_generation=1,
        )

        prepared = prepare_planned_air_transition(
            route, 0, anchor, world, candidate_revision=1,
            intended_start_tick=11, damage_budget=budget,
            precomputed=solved,
        )
        changed_budget = prepare_planned_air_transition(
            route, 0, anchor, world, candidate_revision=2,
            intended_start_tick=11,
            damage_budget=TaskDamageBudget(
                "revised-direct-fall-task", max(0.0, damage - 1.0),
            ),
            precomputed=solved,
        )

        self.assertIs(prepared.status, GapPreparationStatus.READY)
        self.assertEqual(prepared.candidate.proof.kind.value, "controlled_drop")
        self.assertEqual(
            prepared.candidate.proof.maximum_expected_damage_points, damage,
        )
        self.assertIs(
            changed_budget.status, GapPreparationStatus.SOLVE_FAILED,
        )
        self.assertEqual(changed_budget.reason, "precomputed_connection_mismatch")

    def test_next_stair_drop_is_solved_from_the_confirmed_moving_exit(self):
        from mc2p.motion_nav.action_route import (
            ActionRoute, ControlledDropSegment,
        )
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
        from mc2p.motion_nav.controlled_drop import ControlledDropEdge
        from mc2p.motion_nav.motion_coordination import (
            GapPreparationStatus, MotionRouteCoordinator,
            prepare_planned_air_transition,
        )
        from mc2p.motion_nav.motion_solver import SolveStatus, solve_air_transition
        from mc2p.motion_nav.motion_worker import MotionSolverWorker, _execute_job
        from mc2p.motion_nav.online_motion import InputApplicationLedger
        from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
        from mc2p.motion_nav.support_surfaces import (
            HorizontalRegion, SupportSurface, SurfaceNodeId,
        )
        from tests.motion_nav.test_b10_motion_candidate import (
            VerifiedMotionExecutorTests, VerifiedMotionRouteIntegrationTests,
            air_profile, ground_profile, jump_profile, step_profile,
        )
        from mc2p.motion_nav.movement_transition import MovementMode

        anchor, world = world_and_anchor(stair_count=2)
        ids = (
            SurfaceNodeId(0, 0, 64, 0),
            SurfaceNodeId(0, 1, 63, 0),
            SurfaceNodeId(0, 2, 62, 0),
        )
        surfaces = tuple(
            SupportSurface(
                node_id, (.5, float(64 - index), index + .5),
                HorizontalRegion(0, index, 1, index + 1),
                1.0, ("minecraft:grass_block",), (),
            )
            for index, node_id in enumerate(ids)
        )
        actions = tuple(
            ControlledDropSegment(
                ControlledDropEdge(
                    ids[index], ids[index + 1], "moving-drop", .7, (),
                ),
                surfaces[index], surfaces[index + 1], (),
            )
            for index in range(2)
        )
        action_route = ActionRoute("two-moving-drops", actions)
        route = ActiveRoute(
            "two-moving-drops", 1, "request", "goal", 1,
            SESSION.value, None, 2.0, 0.0, (),
            ExecutableCorridor(ids, (), 2.0, ids[-1]),
            action_route, planning_generation=1,
        )
        first_solve = solve_air_transition(
            anchor, world, self.request(anchor, 1, keep_moving=True),
        )
        self.assertIs(first_solve.status, SolveStatus.SOLVED)
        first = prepare_planned_air_transition(
            route, 0, anchor, world, candidate_revision=1,
            intended_start_tick=11, precomputed=first_solve,
        )
        self.assertIs(first.status, GapPreparationStatus.READY)

        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )
        initial_frame = VerifiedMotionRouteIntegrationTests.frame(
            world._world, anchor.physics_state, 1,
        )
        ledger = InputApplicationLedger(max_records=64)
        jobs = []
        with MotionSolverWorker(max_pending=1) as worker:
            coordinator = MotionRouteCoordinator(route, executor, worker)
            coordinator.start(initial_frame)
            executor.install_verified_motion(first.candidate)
            first_decision = coordinator.decide(
                initial_frame, anchor, ledger, world, changed_cells=(),
            )
            executor.register_verified_submission(
                0, control_sequence=20,
                requested_movement_tick=first_decision.expected_movement_tick,
                requested_latest_movement_tick=first_decision.latest_movement_tick,
            )
            VerifiedMotionExecutorTests.applied(
                ledger, anchor, 20, 11, first_decision.movement,
                requested_tick=11, requested_latest_tick=12,
            )
            applied_state = first.candidate.proof.trajectory[1]
            applied_anchor = replace(
                anchor,
                observation_sequence_id=anchor.observation_sequence_id + 1,
                movement_tick_id=11,
                physics_state=applied_state,
            )
            applied_frame = VerifiedMotionRouteIntegrationTests.frame(
                world._world, applied_state, 2,
            )
            with (
                patch.object(worker, "is_alive", return_value=True),
                patch.object(worker, "poll_available", return_value=()),
                patch.object(
                    worker, "submit",
                    side_effect=lambda job: jobs.append(job) or True,
                ),
            ):
                next_decision = coordinator.decide(
                    applied_frame, applied_anchor, ledger, world,
                    changed_cells=(),
                )

            self.assertEqual(len(jobs), 1)
            accepted_ahead = coordinator._accept_result(
                _execute_job(jobs[0]), applied_anchor, world, (),
            )

            current_anchor = applied_anchor
            for command_index in range(1, len(first.candidate.proof.commands)):
                self.assertEqual(
                    next_decision.verified_command_index, command_index,
                )
                sequence = 20 + command_index
                expected_tick = 11 + command_index
                executor.register_verified_submission(
                    command_index, control_sequence=sequence,
                    requested_movement_tick=expected_tick,
                    requested_latest_movement_tick=expected_tick,
                )
                VerifiedMotionExecutorTests.applied(
                    ledger, current_anchor, sequence, expected_tick,
                    next_decision.movement,
                )
                next_state = first.candidate.proof.start_variant(
                    11
                ).trajectory[command_index + 1]
                current_anchor = replace(
                    current_anchor,
                    observation_sequence_id=(
                        current_anchor.observation_sequence_id + 1
                    ),
                    movement_tick_id=expected_tick,
                    physics_state=next_state,
                )
                next_decision = executor.decide(
                    VerifiedMotionRouteIntegrationTests.frame(
                        world._world, next_state, 10 + command_index,
                    ),
                    state_anchor=current_anchor,
                    input_ledger=ledger,
                )

        self.assertTrue(accepted_ahead)
        self.assertTrue(executor.has_verified_motion(1))
        self.assertEqual(next_decision.action_index, 1)
        self.assertTrue(next_decision.submit_input)
        self.assertEqual(next_decision.verified_command_index, 0)
        self.assertNotEqual(next_decision.reason_code, "awaiting_verified_motion")
        self.assertEqual(jobs[0].connection_id, "two-moving-drops/action-1")
        self.assertEqual(
            jobs[0].anchor.physics_state,
            first.candidate.proof.start_variant(11).exit_state,
        )
        self.assertEqual(jobs[0].request.exit_direction, None)


if __name__ == "__main__":
    unittest.main()
