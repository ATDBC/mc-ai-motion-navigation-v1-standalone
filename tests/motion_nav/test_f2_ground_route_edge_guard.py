"""Declared edge protection uses the real controller/calculator loop."""
from dataclasses import replace
import json
import math
import unittest
from unittest.mock import patch

from scripts.f2_ground_route_evidence import MANIFEST, ROOT, _frame, project_route, run_route
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.action_route import ActionRoute, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.ground_route_execution import (
    GroundRouteCapability as Capability, GroundRouteCapabilityInterval as Interval,
    GroundRouteExecutionContract as Contract, GroundRouteGuardPhase as Phase,
)
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.body_control import BodyControlPhase, HandoffDisposition
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor, RouteControl
from mc2p.motion_nav.movement_transition import (
    MovementMode, MovementStateClass, MovementTransition, ResourceChange, CancellationMode,
)
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.motion_nav.online_motion import InputApplicationLedger, MotionTickPhase, StateAnchor, project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.safe_ground_control import verified_ground_route_candidate
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp, WorldQueryCache
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.product_cases import f2_ground_route_scene
from tests.motion_nav.test_f2_non_center_ground_route import case


PROFILES = NavigationSessionProfiles.load(ROOT / "config/motion-navigation")
EDGE = frozenset({Capability.SNEAK_EDGE_GUARD})


def fixture(intervals=None, item=None):
    item = item or case("declared_edge")
    backend = CalculatorBackend([0], f2_ground_route_scene(item), tuple(item["start"]), item["yaw_degrees"])
    points = tuple(RoutePoint(*p) for p in item["route_points"])
    length = sum(math.hypot(b.x-a.x, b.z-a.z) for a, b in zip(points, points[1:]))
    contract = Contract(tuple(Interval(a, b, EDGE) for a, b in (intervals or [(1.7, length)])),
                        (), PROFILES.ground.profile_id)
    route = FixedRoute("edge-test", points, contract)
    controller = FixedRouteController(PROFILES.ground)
    controller.start(route, _frame(backend.state, backend.world._world, 0))
    return backend, route, controller


def advance(state, movement, backend):
    return step(state, project_movement_command(state, movement).tick_input,
                backend.world, JAVA_1_21_RULESET).next_state


def reach_guard(item=None):
    backend, route, controller = fixture(item=item)
    state = backend.state
    previous = MovementV1()
    for tick in range(1, 150):
        decision = controller.decide(_frame(state, backend.world._world, tick), physics_state=state)
        state = advance(state, previous if item is not None and tick in item["late_ticks"]
                        else decision.movement, backend)
        previous = decision.movement
        if state.sneaking and state.pose == "crouching":
            return backend, route, controller, state, tick
    raise AssertionError("actual calculator never entered edge protection")


def physics_anchor(state, tick):
    return StateAnchor(state.session, tick, state.movement_tick_id,
        MotionTickPhase.AFTER_MOVEMENT, None, None, state.ruleset_id,
        state.state_schema, "test-projection", state)


def reverse_displacement_fixture(*, raised_floor=False, use_executor=False):
    """Review reproduction uses public start/decide and actual physics only."""
    backend, route, controller = fixture()
    if raised_floor:
        item = case("declared_edge")
        scene = f2_ground_route_scene(item)
        slabs = {(x, 64, z): "minecraft:smooth_stone_slab"
                 for x, y, z in scene.solids if y == 63}
        backend = CalculatorBackend([0], Scene({**scene.solids, **slabs}, scene.volume),
                                    (item["start"][0], 64.5, item["start"][2]))
        route = replace(route, points=tuple(replace(p, y=64.5) for p in route.points))
        controller = FixedRouteController(PROFILES.ground)
        controller.start(route, _frame(backend.state, backend.world._world, 0))
    state = backend.state
    for tick in range(1, 150):
        decision = controller.decide(_frame(state, backend.world._world, tick), physics_state=state)
        if decision.movement.sneak:
            break
        state = advance(state, decision.movement, backend)
    else:
        raise AssertionError("entry did not request guard")
    narrow = FixedRoute("narrow", route.points[-2:],
                       Contract((Interval(3.88, 4., EDGE),), (), PROFILES.ground.profile_id))
    controller = FixedRouteController(PROFILES.ground)
    if use_executor:
        controller = ActionRouteExecutor(PROFILES.ground, PROFILES.jump_up, PROFILES.step,
                                         PROFILES.ground_modes)
        entry = MovementStateClass(MovementMode.WALK, "standing", 0.,
                                   PROFILES.ground.maximum_speed_blocks_per_second)
        transition = MovementTransition("narrow-walk", PROFILES.ground.environment_id,
            MovementMode.WALK, entry, (entry,), 1., (), ResourceChange(),
            CancellationMode.GROUND_STOP, CancellationMode.GROUND_STOP,
            trajectory_profile_id=PROFILES.ground.profile_id)
        controller.start(ActionRoute("narrow-executor",
            (WalkSegment(narrow, ((1, 64, 2), (2, 64, 6)), (), transition),)),
            _frame(state, backend.world._world, 0))
    else:
        controller.start(narrow, _frame(state, backend.world._world, 0))
    for tick in (1, 2):
        proof = {"state_anchor": physics_anchor(state, tick)} if use_executor else {"physics_state": state}
        decision = controller.decide(_frame(state, backend.world._world, tick), **proof)
        assert decision.movement.sneak
        state = advance(state, decision.movement, backend)
    state = replace(state, position=(state.position[0], state.position[1], state.position[2] - .30))
    return backend, narrow, controller, state


class GroundRouteEdgeGuardTests(unittest.TestCase):
    def test_unproven_outside_stop_replan_retains_supervisor_body_owner(self):
        backend, route, executor, state = reverse_displacement_fixture(raised_floor=True, use_executor=True)
        stamp = ObservationStamp(state.session, 3, 3, "standing-clearance-lost", 150_000_000)
        backend.truth.invalidate(stamp, tuple((x, 66, z) for x in range(0, 4) for z in range(4, 9)))
        backend.world = PhysicsWorldView(backend.truth.view(), JAVA_1_21_RULESET)
        # Forward momentum would advance outside the permission. The controller
        # must hand back rather than relabel it as a permitted protective stop.
        state = replace(state, velocity_blocks_per_tick=(state.velocity_blocks_per_tick[0],
                                                        state.velocity_blocks_per_tick[1], .05))
        frame = _frame(state, backend.world._world, 3)
        anchor, ledger = physics_anchor(state, 3), InputApplicationLedger()
        nodes = ((1, 64, 2), (2, 64, 6))
        active = ActiveRoute("narrow-active", 1, "request", "goal", 1,
            state.session.value, route, 4., 0., (), ExecutableCorridor(nodes, (), 4., nodes[-1]), executor.route)
        control, supervisor = RouteControl(active, executor), ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(control, frame, ledger, anchor))
        decision = executor.decide(frame, state_anchor=anchor, input_ledger=ledger)
        self.assertIs(decision.state, ActionRouteState.NEEDS_REPLAN)
        self.assertEqual(decision.movement, MovementV1())
        self.assertNotIn(decision.state, {ActionRouteState.COMPLETE, ActionRouteState.CANCELLED})
        self.assertEqual(executor.action_index, 0)
        self.assertIs(control.activity(frame, decision).phase, BodyControlPhase.STOPPING)
        self.assertIs(supervisor.evaluate_quiescence(frame, ledger, anchor).disposition,
                      HandoffDisposition.RETAIN)
        self.assertFalse(supervisor.retire_route(frame, ledger, anchor))
        self.assertIs(supervisor.incumbent_route, control)
        self.assertTrue(supervisor.has_owned_body_control(route_source_bound=True))
        # Even a later stopped observation cannot release an unproved stand-up.
        state = replace(state, velocity_blocks_per_tick=(0., state.velocity_blocks_per_tick[1], 0.))
        frame, anchor = _frame(state, backend.world._world, 4), physics_anchor(state, 4)
        self.assertIs(supervisor.evaluate_quiescence(frame, ledger, anchor).disposition,
                      HandoffDisposition.RETAIN)
        self.assertFalse(supervisor.retire_route(frame, ledger, anchor))
        self.assertIs(supervisor.incumbent_route, control)

    def test_reverse_displacement_releases_from_actual_position_then_replans(self):
        backend, route, controller, state = reverse_displacement_fixture()
        points = [[p.x, p.y, p.z] for p in route.points]
        actual = project_route(points, state.position)[0]
        self.assertLess(actual, 3.88)
        decision = controller.decide(_frame(state, backend.world._world, 3), physics_state=state)
        self.assertEqual(decision.progress_blocks, 4.)
        self.assertEqual(decision.movement, MovementV1(), decision)
        self.assertEqual(decision.edge_guard_phase.value, "outside_releasing")
        for tick in range(4, 40):
            state = advance(state, decision.movement, backend)
            self.assertTrue(state.on_ground)
            self.assertAlmostEqual(project_route(points, state.position)[0], actual)
            decision = controller.decide(_frame(state, backend.world._world, tick), physics_state=state)
            self.assertLessEqual(decision.full_candidates, 3)
            self.assertEqual(decision.movement, MovementV1())
            if decision.state is FixedRouteState.NEEDS_REPLAN:
                self.assertEqual(state.pose, "standing")
                self.assertFalse(state.sneaking)
                break
        else:
            self.fail("outside release did not hand back for replanning")

    def test_outside_retention_is_neutral_proved_and_bounded(self):
        backend, route, controller, state = reverse_displacement_fixture(raised_floor=True)
        # Crouching beneath y=66 is known; standing needs newly unknown cells.
        stamp = ObservationStamp(state.session, 3, 3, "standing-clearance-lost", 150_000_000)
        missing = tuple((x, 66, z) for x in range(0, 4) for z in range(4, 9))
        backend.truth.invalidate(stamp, missing)
        backend.world = PhysicsWorldView(backend.truth.view(), JAVA_1_21_RULESET)
        frame = _frame(state, backend.world._world, 3)
        release = verified_ground_route_candidate(frame, state, MovementV1(), control_ticks=2,
            tail_ticks=30, minimum_support=.15, profile=PROFILES.ground,
            query_cache=WorldQueryCache(frame.world), edge_guard=True)
        self.assertIs(release.status, QueryStatus.NEEDS_INFORMATION)
        points = [[p.x, p.y, p.z] for p in route.points]
        actual = project_route(points, state.position)[0]
        for tick in range(3, 45):
            decision = controller.decide(_frame(state, backend.world._world, tick), physics_state=state)
            self.assertEqual(decision.movement, MovementV1(sneak=True), decision)
            self.assertEqual(decision.edge_guard_phase.value, "outside_stopping")
            self.assertLessEqual(decision.full_candidates, 3)
            self.assertEqual(decision.progress_blocks, 4.)
            self.assertNotIn(decision.state, {FixedRouteState.RUNNING, FixedRouteState.SUCCEEDED})
            state = advance(state, decision.movement, backend)
            self.assertTrue(state.on_ground)
            self.assertAlmostEqual(project_route(points, state.position)[0], actual)
            if decision.state is FixedRouteState.NEEDS_REPLAN:
                self.assertLessEqual(tick, 3 + controller.config.maximum_recovery_ticks)
                break
        else:
            self.fail("protective retention did not hand back within its bound")
    def test_declared_edge_completes_without_safety_violation(self):
        row = run_route(case("declared_edge"))
        self.assertTrue(row["success"], row)
        self.assertEqual(row["gate_violations"], [])
        self.assertLessEqual(row["full_candidates_max"], 3)

    def test_all_frozen_directions_and_first_late_inputs(self):
        for item in json.loads(MANIFEST.read_text("utf-8"))["tasks"]:
            if item["family"] not in {"declared_edge", "undeclared_edge"}:
                continue
            with self.subTest(case=item["id"]):
                row = run_route(item)
                self.assertEqual(row["success"], item["family"] == "declared_edge", row)
                self.assertEqual(row["gate_violations"], [])
                self.assertLessEqual(row["full_candidates_max"], 3)
                self.assertEqual(row["lost_ground_frames"], 0)
                if item["family"] == "declared_edge":
                    length = sum(math.dist(a[::2], b[::2]) for a, b in
                                 zip(item["route_points"], item["route_points"][1:]))
                    self.assertEqual(row["original_intervals"], [[1.7, 6.2]])
                    self.assertEqual(row["execution_contract"]["capability_intervals"][0]["end_progress_blocks"], length)
                    self.assertGreater(row["sneak_frames"], 0)
                    self.assertGreater(row["edge_guard_phase_frames"].get("releasing", 0), 0)

    def test_ordinary_candidate_enters_interval_before_any_sneak_input(self):
        records = []
        original = FixedRouteController.decide
        def record(controller, frame, **kwargs):
            decision = original(controller, frame, **kwargs)
            records.append((frame.body, decision))
            return decision
        with patch.object(FixedRouteController, "decide", record):
            self.assertTrue(run_route(case("declared_edge"))["success"])
        points = case("declared_edge")["route_points"]
        self.assertTrue(any(b.position[2] < 2.3 and project_route(points, b.position)[0] >= 1.7
                            and not d.movement.sneak for b, d in records))
        guarded = [(b, d) for b, d in records if d.movement.sneak]
        self.assertTrue(guarded)
        self.assertTrue(all(1.7 <= project_route(points, b.position)[0] <= 6.1189620100417095
                            for b, _ in guarded))
        self.assertEqual(records[-1][0].pose, "standing")
        self.assertFalse(records[-1][0].is_sneaking)
        self.assertIsNot(records[-3][1].state, FixedRouteState.SUCCEEDED)

    def test_contract_is_typed_immutable_ordered_and_bounded(self):
        for first, last in ((-1, 1), (2, 1), (1, 1), (float("nan"), 1), (0, float("inf"))):
            with self.subTest(first=first, last=last), self.assertRaises(ContractViolation):
                Interval(first, last, EDGE)
        for capabilities in (set(EDGE), frozenset(), frozenset({"sneak_edge_guard"})):
            with self.assertRaises(ContractViolation):
                Interval(0, 1, capabilities)
        for intervals in ((Interval(0, 2, EDGE), Interval(1, 3, EDGE)),
                          (Interval(2, 3, EDGE), Interval(0, 1, EDGE))):
            with self.assertRaises(ContractViolation):
                Contract(intervals, (), PROFILES.ground.profile_id)
        backend, route, controller = fixture()
        with self.assertRaises(ContractViolation):
            FixedRoute("out-of-range", route.points,
                       Contract((Interval(1.7, 6.2, EDGE),), (), PROFILES.ground.profile_id))
        self.assertEqual(case("declared_edge")["capability_intervals"], [[1.7, 6.2]])
        with self.assertRaises(ContractViolation):
            Contract((), (), PROFILES.ground.profile_id, "unknown-ruleset")
        with self.assertRaises(ContractViolation):
            controller = FixedRouteController(PROFILES.ground)
            controller.start(replace(route, execution_contract=replace(route.execution_contract,
                                 profile_id="another-profile")), _frame(backend.state, backend.world._world, 0))
        interval = route.execution_contract.capability_intervals[0]
        self.assertTrue(interval.contains(interval.start_progress_blocks))
        self.assertTrue(interval.contains(interval.end_progress_blocks))
        self.assertFalse(interval.contains(interval.start_progress_blocks - 1e-9))

    def test_early_and_late_intervals_cannot_enable_outside_sneak(self):
        for bounds in ([[0., 1.]], [[6., 6.1189620100417095]], [[1.7, 3.]]):
            item = {**case("declared_edge"), "capability_intervals": bounds}
            row = run_route(item)
            self.assertFalse(row["success"], row)
            self.assertEqual(row["gate_violations"], [])

    def test_cancel_keeps_guard_until_stopped_and_observed_standing(self):
        items = [i for i in json.loads(MANIFEST.read_text("utf-8"))["tasks"] if i["family"] == "declared_edge"]
        for item in items:
            with self.subTest(case=item["id"]):
                backend, route, controller, state, tick = reach_guard(item)
                self.assertGreater(math.hypot(*state.velocity_blocks_per_tick[::2]), 0.)
                controller.cancel()
                phases = []
                for tick in range(tick+1, tick+40):
                    decision = controller.decide(_frame(state, backend.world._world, tick), physics_state=state)
                    phases.append(decision.edge_guard_phase)
                    if decision.state is FixedRouteState.CANCELLED:
                        self.assertEqual(state.pose, "standing")
                        self.assertFalse(state.sneaking)
                        break
                    if math.hypot(*state.velocity_blocks_per_tick[::2]) > 1e-9:
                        self.assertTrue(decision.movement.sneak)
                    state = advance(state, decision.movement, backend)
                    self.assertTrue(state.on_ground)
                else:
                    self.fail("cancel did not finish")
                self.assertIn(Phase.RELEASING, phases)

    def test_input_loss_and_external_displacement_use_existing_exit(self):
        backend, route, controller, state, tick = reach_guard()
        decision = controller.decide(_frame(state, backend.world._world, tick+1),
                                     input_confirmed=False, physics_state=state)
        self.assertIs(decision.state, FixedRouteState.INPUT_LOST)
        self.assertEqual(decision.movement, MovementV1())
        for change in ({"position": (3.5, 64., 6.5)},
                       {"velocity_blocks_per_tick": (.4, -.0784, 0.)},
                       {"on_ground": False, "velocity_blocks_per_tick": (0., .2, 0.)}):
            backend, route, controller, state, tick = reach_guard()
            state = replace(state, **change)
            decision = controller.decide(_frame(state, backend.world._world, tick+1), physics_state=state)
            self.assertIs(decision.state, FixedRouteState.UNSUPPORTED)
            self.assertEqual(decision.movement, MovementV1())

    def test_support_removal_rejects_guard_and_low_ceiling_rejects_success(self):
        for ceiling in (False, True):
            backend, route, controller, state, tick = reach_guard()
            world = backend.truth
            stamp = ObservationStamp(state.session, tick+1, tick+1, "edge-edit", (tick+1)*50_000_000)
            if ceiling:
                # A ceiling at y=65.5 admits crouching but prevents standing.
                p = (math.floor(state.position[0]), 65, math.floor(state.position[2]))
                world.observe_blocks(stamp, {p: BlockGeometry("minecraft:stone", "boxes",
                    (Aabb(0., .5, 0., 1., 1., 1.),))})
                backend.world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
                controller.cancel()
                for seq in range(tick+1, tick+40):
                    decision = controller.decide(_frame(state, backend.world._world, seq), physics_state=state)
                    if decision.state is FixedRouteState.BLOCKED:
                        break
                    state = advance(state, decision.movement, backend)
                self.assertIs(decision.state, FixedRouteState.BLOCKED)
                self.assertNotIn(decision.state, {FixedRouteState.SUCCEEDED, FixedRouteState.CANCELLED})
            else:
                world.confirm_air(stamp, tuple((x, 63, z) for x in range(1, 3)
                                               for z in range(5, 8)))
                backend.world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
                decision = controller.decide(_frame(state, backend.world._world, tick+1), physics_state=state)
                self.assertIs(decision.state, FixedRouteState.BLOCKED)
                self.assertEqual(decision.movement, MovementV1())

    def test_explicit_replay_preserves_sneak_tail_and_reports_real_clip(self):
        backend, route, controller, state, tick = reach_guard()
        clipped_state = replace(state, position=(2.28, 64., 6.5), velocity_blocks_per_tick=(.035, -.0784, 0.))
        clipped_frame = _frame(clipped_state, backend.world._world, tick+1)
        clip = verified_ground_route_candidate(clipped_frame, clipped_state,
            MovementV1(forward=1, strafe=1, sneak=True), control_ticks=2,
            tail_ticks=30, minimum_support=.15, profile=PROFILES.ground,
            query_cache=WorldQueryCache(clipped_frame.world), edge_guard=True)
        # The calculator event is real, but clipping alone cannot weaken the
        # production support threshold. This particular clip remains rejected.
        self.assertTrue(clip.sneak_edge_clipped)
        self.assertIs(clip.status, QueryStatus.BLOCKED)
        frame = _frame(state, backend.world._world, tick+1)
        command = MovementV1(forward=1, sneak=True)
        result = verified_ground_route_candidate(frame, state, command, control_ticks=2,
            tail_ticks=30, minimum_support=.15, profile=PROFILES.ground,
            query_cache=WorldQueryCache(frame.world), edge_guard=True)
        self.assertIs(result.status, QueryStatus.FEASIBLE, result)
        self.assertTrue(all(s.sneaking for s in result.trajectory))
        self.assertEqual(result.trajectory[-1].velocity_blocks_per_tick[::2], (0., 0.))
        ordinary = verified_ground_route_candidate(frame, state, command, control_ticks=2,
            tail_ticks=30, minimum_support=.15, profile=PROFILES.ground,
            query_cache=WorldQueryCache(frame.world))
        self.assertIs(ordinary.status, QueryStatus.UNSUPPORTED)
        self.assertEqual(ordinary.physics_steps, 0)

    def test_safe_ordinary_routes_do_not_prefer_sneak_when_permission_exists(self):
        item = case("offset")
        item = {**item, "capability_intervals": [[0., 6.]], "capability": "SNEAK_EDGE_GUARD"}
        movements = []
        original = FixedRouteController.decide
        def record(controller, *args, **kwargs):
            decision = original(controller, *args, **kwargs)
            movements.append(decision.movement)
            return decision
        with patch.object(FixedRouteController, "decide", record):
            row = run_route(item)
        self.assertTrue(row["success"], row)
        self.assertFalse(any(m.sneak for m in movements))
        self.assertEqual(row["full_candidates_max"], 0)

    def test_formal_action_route_propagates_contract_without_capability_branch(self):
        for interrupt in (None, "cancel", "replacement"):
            with self.subTest(interrupt=interrupt):
                backend, route, _ = fixture()
                executor = ActionRouteExecutor(PROFILES.ground, PROFILES.jump_up, PROFILES.step,
                                               PROFILES.ground_modes)
                entry = MovementStateClass(MovementMode.WALK, "standing", 0.,
                                           PROFILES.ground.maximum_speed_blocks_per_second)
                transition = MovementTransition("formal-walk", PROFILES.ground.environment_id,
                    MovementMode.WALK, entry, (entry,), 1., (), ResourceChange(),
                    CancellationMode.GROUND_STOP, CancellationMode.GROUND_STOP,
                    trajectory_profile_id=PROFILES.ground.profile_id)
                action_route = ActionRoute("formal-edge", (WalkSegment(route, ((1, 64, 0),), (), transition),))
                executor.start(action_route, _frame(backend.state, backend.world._world, 0))
                state, interrupted, saw_sneak = backend.state, False, False
                for tick in range(1, 150):
                    if interrupt and state.sneaking and not interrupted:
                        executor.cancel()
                        interrupted = True
                    anchor = StateAnchor(state.session, tick, state.movement_tick_id,
                        MotionTickPhase.AFTER_MOVEMENT, None, None, state.ruleset_id,
                        state.state_schema, "test-projection", state)
                    decision = executor.decide(_frame(state, backend.world._world, tick), state_anchor=anchor)
                    saw_sneak = saw_sneak or decision.movement.sneak
                    if decision.state in {ActionRouteState.COMPLETE, ActionRouteState.CANCELLED}:
                        self.assertEqual(state.pose, "standing")
                        self.assertFalse(state.sneaking)
                        break
                    self.assertNotIn(decision.state, {ActionRouteState.BLOCKED, ActionRouteState.UNSUPPORTED},
                                     (decision, state))
                    state = advance(state, decision.movement, backend)
                    self.assertTrue(state.on_ground)
                else:
                    self.fail("formal edge route did not finish")
                self.assertTrue(saw_sneak)
                self.assertEqual(decision.state, ActionRouteState.CANCELLED if interrupt else ActionRouteState.COMPLETE)
                if interrupt == "replacement":
                    replacement = FixedRoute("replacement", (RoutePoint(*state.position),))
                    executor.start(ActionRoute("replacement", (WalkSegment(replacement, ((1, 64, 6),), ()),)),
                                   _frame(state, backend.world._world, tick+1))
                    result = executor.decide(_frame(state, backend.world._world, tick+2), state_anchor=anchor)
                    self.assertIs(result.state, ActionRouteState.COMPLETE)
                    self.assertEqual(result.movement, MovementV1())

    def test_undeclared_edge_stays_safely_rejected(self):
        row = run_route(case("undeclared_edge"))
        self.assertFalse(row["success"], row)
        self.assertEqual(row["gate_violations"], [])


if __name__ == "__main__":
    unittest.main()
