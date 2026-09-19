from __future__ import annotations

import json
import math
from pathlib import Path
import time
import unittest

from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.jump_up import (
    JumpUpController, JumpUpEdge, JumpUpProfile, JumpUpState, load_jump_up_profile,
    query_jump_up,
)
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, PlanningRequest, PlanningStatus,
    SnapshotBuildStatus, astar_plan, build_walk_graph, dijkstra_reference,
    plan_known_snapshot,
)
from mc2p.motion_nav.action_route import JumpUpSegment, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp
from tests.motion_nav.test_fixed_route_walk import (
    FlatFixture, apply, profile as ground_profile,
)


ROOT = Path(__file__).resolve().parents[2]


def jump_profile() -> JumpUpProfile:
    return load_jump_up_profile(ROOT / "config/motion-navigation/jump-up-v1.json")


class JumpUpTests(unittest.TestCase):
    def raised_fixture(self) -> FlatFixture:
        fixture = FlatFixture()
        stamp = ObservationStamp(fixture.session, 1, 1, "test-clock", 50_000_000)
        fixture.world.observe_blocks(stamp, {
            (0, 1, 1): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        fixture.world.confirm_air(stamp, tuple(
            (0, y, z) for y in (1, 2, 3, 4) for z in (0, 1)
            if (0, y, z) != (0, 1, 1)
        ))
        return fixture

    def test_profile_has_explicit_scope_and_observed_reference(self) -> None:
        profile = jump_profile()
        self.assertEqual(profile.minecraft_version, "1.21")
        self.assertEqual(profile.support_materials, frozenset({"minecraft:grass_block"}))
        self.assertEqual(profile.reference_positions[0], (0.0, 0.0, 0.0))
        self.assertAlmostEqual(profile.reference_positions[-1][1], 1.0)
        self.assertGreater(profile.cost_seconds, 0)
        document = json.loads((ROOT / "config/motion-navigation/jump-up-v1.json").read_text("utf-8"))
        self.assertIn("source_run", document["validation"])

    def test_geometry_distinguishes_feasible_unknown_blocked_and_unsupported(self) -> None:
        profile = jump_profile()
        fixture = self.raised_fixture()
        feasible = query_jump_up(fixture.world.view(), (0, 1, 0), (0, 2, 1), profile)
        self.assertIs(feasible.status, QueryStatus.FEASIBLE)
        self.assertTrue(feasible.dependencies)

        stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
        fixture.world.invalidate(stamp, ((0, 3, 1),))
        missing = query_jump_up(fixture.world.view(), (0, 1, 0), (0, 2, 1), profile)
        self.assertIs(missing.status, QueryStatus.NEEDS_INFORMATION)
        self.assertIn((0, 3, 1), missing.missing_cells)

        fixture = self.raised_fixture()
        stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
        fixture.world.observe_blocks(stamp, {
            (0, 3, 1): BlockGeometry.full_cube("minecraft:stone"),
        })
        self.assertIs(query_jump_up(
            fixture.world.view(), (0, 1, 0), (0, 2, 1), profile,
        ).status, QueryStatus.BLOCKED)

        fixture = self.raised_fixture()
        fixture.world.observe_blocks(stamp, {
            (0, 1, 1): BlockGeometry.full_cube("minecraft:ice"),
        })
        self.assertIs(query_jump_up(
            fixture.world.view(), (0, 1, 0), (0, 2, 1), profile,
        ).status, QueryStatus.UNSUPPORTED)

    def test_query_rejects_any_relation_outside_adjacent_one_block_up(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()
        for end in ((0, 1, 1), (0, 3, 1), (1, 2, 1), (0, 2, 2)):
            with self.subTest(end=end):
                self.assertIs(query_jump_up(
                    fixture.world.view(), (0, 1, 0), end, profile,
                ).status, QueryStatus.UNSUPPORTED)

    def test_controller_uses_observed_takeoff_and_landing(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()
        controller = JumpUpController(profile)
        start = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller.start((0, 1, 0), (0, 2, 1), start)

        request = controller.decide(start)
        self.assertIs(request.state, JumpUpState.REQUEST_TAKEOFF)
        self.assertTrue(request.movement.jump)

        still_grounded = controller.decide(
            fixture.frame(1, PlanarBodyState(.5, .55, 0, .8, 0)),
            input_confirmed=True,
        )
        self.assertIs(still_grounded.state, JumpUpState.REQUEST_TAKEOFF)
        self.assertFalse(still_grounded.movement.jump)

        airborne_frame = fixture.frame(
            2, PlanarBodyState(.5, .70, 0, 1.2, 0), body_y=1.42,
        )
        airborne_body = airborne_frame.body
        object.__setattr__(airborne_body, "is_on_ground", False)
        airborne = controller.decide(airborne_frame)
        self.assertIs(airborne.state, JumpUpState.AIRBORNE)

        landing = fixture.frame(
            3, PlanarBodyState(.5, 1.48, 0, 0, 0), body_y=2.0,
        )
        landed = controller.decide(landing)
        self.assertIs(landed.state, JumpUpState.COMPLETE)

    def test_confirmed_input_without_takeoff_times_out_instead_of_succeeding(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()
        controller = JumpUpController(profile)
        frame = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller.start((0, 1, 0), (0, 2, 1), frame)
        controller.decide(frame)
        decision = None
        for sequence in range(1, profile.maximum_takeoff_wait_ticks + 2):
            decision = controller.decide(
                fixture.frame(sequence, PlanarBodyState(.5, .5, 0, 0, 0)),
                input_confirmed=True,
            )
        self.assertIs(decision.state, JumpUpState.FAILED)
        self.assertEqual(decision.reason_code, "takeoff_not_observed")

    def test_rejected_takeoff_input_fails_without_claiming_airborne(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()
        frame = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller = JumpUpController(profile)
        controller.start((0, 1, 0), (0, 2, 1), frame)
        self.assertIs(controller.decide(frame).state, JumpUpState.REQUEST_TAKEOFF)

        rejected = controller.decide(
            fixture.frame(1, PlanarBodyState(.5, .5, 0, 0, 0)),
            input_confirmed=False,
        )
        self.assertIs(rejected.state, JumpUpState.FAILED)
        self.assertEqual(rejected.reason_code, "takeoff_input_rejected")

    def test_entry_domain_is_directional_and_speed_bounded(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()

        forward = fixture.frame(0, PlanarBodyState(.5, .52, 0, 0, 0))
        controller = JumpUpController(profile)
        controller.start((0, 1, 0), (0, 2, 1), forward)
        decision = controller.decide(forward)
        self.assertIs(decision.state, JumpUpState.UNSUPPORTED)
        self.assertEqual(decision.reason_code, "entry_directional_offset_out_of_range")

        too_fast = fixture.frame(1, PlanarBodyState(.5, .5, 0, .11, 0))
        controller = JumpUpController(profile)
        controller.start((0, 1, 0), (0, 2, 1), too_fast)
        decision = controller.decide(too_fast)
        self.assertIs(decision.state, JumpUpState.UNSUPPORTED)
        self.assertEqual(decision.reason_code, "entry_speed_out_of_range")

        wrong_pose = fixture.frame(2, PlanarBodyState(.5, .5, 0, 0, 0))
        object.__setattr__(wrong_pose.body, "pose", "crouching")
        controller = JumpUpController(profile)
        controller.start((0, 1, 0), (0, 2, 1), wrong_pose)
        decision = controller.decide(wrong_pose)
        self.assertIs(decision.state, JumpUpState.UNSUPPORTED)
        self.assertEqual(decision.reason_code, "invalid_entry_body")

    def test_takeoff_aligns_view_before_sending_jump(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()
        controller = JumpUpController(profile)
        turned = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, math.pi / 4))
        controller.start((0, 1, 0), (0, 2, 1), turned)
        alignment = controller.decide(turned)
        self.assertIs(alignment.state, JumpUpState.PREPARE)
        self.assertIsNotNone(alignment.look)
        self.assertFalse(alignment.movement.jump)
        aligned = fixture.frame(1, PlanarBodyState(.5, .5, 0, 0, 0))
        takeoff = controller.decide(aligned)
        self.assertIs(takeoff.state, JumpUpState.REQUEST_TAKEOFF)
        self.assertTrue(takeoff.movement.jump)

    def test_airborne_cancel_finishes_only_after_real_landing(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()
        controller = JumpUpController(profile)
        start = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller.start((0, 1, 0), (0, 2, 1), start)
        controller.decide(start)
        airborne = fixture.frame(1, PlanarBodyState(.5, .7, 0, 1, 0), body_y=1.42)
        object.__setattr__(airborne.body, "is_on_ground", False)
        controller.decide(airborne)
        controller.cancel()
        cancelling = controller.decide(airborne)
        self.assertIs(cancelling.state, JumpUpState.CANCELLING)
        landed = fixture.frame(2, PlanarBodyState(.5, 1.48, 0, 0, 0), body_y=2.0)
        self.assertIs(controller.decide(landed).state, JumpUpState.CANCELLED)

    def test_airborne_input_loss_is_reported_only_after_safe_landing(self) -> None:
        fixture, profile = self.raised_fixture(), jump_profile()
        controller = JumpUpController(profile)
        start = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller.start((0, 1, 0), (0, 2, 1), start)
        self.assertIs(controller.decide(start).state, JumpUpState.REQUEST_TAKEOFF)

        airborne = fixture.frame(1, PlanarBodyState(.5, .70, 0, 1.0, 0), body_y=1.42)
        object.__setattr__(airborne.body, "is_on_ground", False)
        lost = controller.decide(airborne, input_confirmed=False)
        self.assertIs(lost.state, JumpUpState.CANCELLING)

        landing = fixture.frame(2, PlanarBodyState(.5, 1.5, 0, 0, 0), body_y=2.0)
        terminal = controller.decide(landing, input_confirmed=True)
        self.assertIs(terminal.state, JumpUpState.INPUT_LOST)

    def test_multilevel_graph_and_snapshot_plan_walk_jump_walk(self) -> None:
        fixture, jump = self.raised_fixture(), jump_profile()
        stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
        fixture.world.observe_blocks(stamp, {
            (0, 1, z): BlockGeometry.full_cube("minecraft:grass_block")
            for z in (1, 2, 3)
        })
        fixture.world.confirm_air(stamp, tuple(
            (0, y, z) for y in (2, 3, 4) for z in (1, 2, 3)
        ))
        bounds = KnownMapBounds(0, 0, 1, 2, -2, 3, True, 1)
        graph = build_walk_graph(
            fixture.world.view(), bounds, profile=ground_profile(), jump_profile=jump,
        )
        request = PlanningRequest(
            1, "b05-multilevel", "upper-goal", 1, fixture.session.value,
            (0, 1, -2), (0, 2, 3),
        )
        candidate = astar_plan(graph, request)
        self.assertIs(candidate.status, PlanningStatus.COMPLETE)
        self.assertEqual(sum(type(edge) is JumpUpEdge for edge in candidate.segments), 1)
        reference = dijkstra_reference(graph, request.start, request.goal)
        self.assertIsNotNone(reference)
        self.assertAlmostEqual(candidate.total_cost_seconds, reference[0])

        builder = KnownMapSnapshotBuilder(fixture.world.view(), bounds)
        built = builder.advance(fixture.world.view(), 10_000)
        self.assertIs(built.status, SnapshotBuildStatus.COMPLETE)
        lazy = plan_known_snapshot(
            built.snapshot,
            ground_profile(),
            request, jump,
        )
        self.assertIs(lazy.status, PlanningStatus.COMPLETE)
        self.assertEqual(tuple(type(edge) for edge in lazy.segments),
                         tuple(type(edge) for edge in candidate.segments))
        self.assertAlmostEqual(lazy.total_cost_seconds, candidate.total_cost_seconds)

        admitted = RouteAdmitter(maximum_corridor_blocks=10).admit(
            candidate,
            fixture.frame(0, PlanarBodyState(.5, -1.5, 0, 0, 0)),
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertEqual(
            tuple(type(action) for action in admitted.route.action_route.actions),
            (WalkSegment, JumpUpSegment, WalkSegment),
        )
        self.assertIsNone(admitted.route.fixed_route,
                          "跨高度路线不能伪装成 B03 的同高 FixedRoute")

        executor = ActionRouteExecutor(ground_profile(), jump)
        body = PlanarBodyState(.5, -1.5, 0, 0, 0)
        frame = fixture.frame(0, body)
        executor.start(admitted.route.action_route, frame)
        decision = None
        for sequence in range(1, 100):
            frame = fixture.frame(sequence, body)
            decision = executor.decide(frame)
            if decision.action_index == 1:
                break
            body = apply(body, decision.movement, ground_profile())
        self.assertEqual(decision.action_index, 1, decision)
        self.assertTrue(decision.movement.jump, decision)

        airborne = fixture.frame(101, PlanarBodyState(.5, .62, 0, 1.4, 0), body_y=1.42)
        object.__setattr__(airborne.body, "is_on_ground", False)
        self.assertIs(executor.decide(airborne).state, ActionRouteState.RUNNING)
        landing_body = PlanarBodyState(.5, 1.48, 0, 0, 0)
        landing = fixture.frame(102, landing_body, body_y=2.0)
        handed_off = executor.decide(landing)
        self.assertEqual(handed_off.action_index, 2)

        body = landing_body
        for sequence in range(103, 220):
            frame = fixture.frame(sequence, body, body_y=2.0)
            decision = executor.decide(frame)
            if decision.state is ActionRouteState.COMPLETE:
                break
            body = apply(body, decision.movement, ground_profile())
        self.assertIs(decision.state, ActionRouteState.COMPLETE)

    def test_disabling_jump_profile_preserves_walk_only_graph(self) -> None:
        fixture = self.raised_fixture()
        bounds = KnownMapBounds(0, 0, 1, 2, 0, 1, True)
        graph = build_walk_graph(fixture.world.view(), bounds, ground_profile())
        self.assertFalse(any(type(edge) is JumpUpEdge for edge in graph.edges))
        request = PlanningRequest(
            1, "without-jump", "upper-goal", 1, fixture.session.value,
            (0, 1, 0), (0, 2, 1),
        )
        self.assertIsNot(astar_plan(graph, request).status, PlanningStatus.COMPLETE)

    def test_background_worker_accepts_calibrated_jump_profile(self) -> None:
        fixture, jump = self.raised_fixture(), jump_profile()
        stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
        fixture.world.observe_blocks(stamp, {
            (0, 1, z): BlockGeometry.full_cube("minecraft:grass_block")
            for z in (1, 2)
        })
        fixture.world.confirm_air(stamp, tuple(
            (0, y, z) for y in (2, 3, 4) for z in (1, 2)
        ))
        bounds = KnownMapBounds(0, 0, 1, 2, 0, 2, True, 1)
        builder = KnownMapSnapshotBuilder(fixture.world.view(), bounds)
        snapshot = builder.advance(fixture.world.view(), 10_000).snapshot
        request = PlanningRequest(
            1, "worker-jump", "upper-goal", 1, fixture.session.value,
            (0, 1, 0), (0, 2, 2),
        )
        worker = PlannerWorker()
        try:
            worker.submit_snapshot(snapshot, ground_profile(), request, jump)
            deadline = time.perf_counter() + 3
            result = None
            while result is None and time.perf_counter() < deadline:
                result = worker.poll_latest()
                time.sleep(.01)
            self.assertIsNotNone(result)
            self.assertIs(result.status, PlanningStatus.COMPLETE)
            self.assertTrue(any(type(edge) is JumpUpEdge for edge in result.segments))
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()
