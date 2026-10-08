"""Formal owner creation and independent delivery/time regression cases."""

from mc2p.motion_nav.async_work import AsyncComputationScope
from dataclasses import replace, asdict
import unittest
import math
import random
from unittest.mock import patch

from mc2p.motion_nav.async_work import (
    AsyncWorkIdentity, AsyncWorkKind, AsyncWorkLifecycle, AsyncWorkWindow, WorkCheck,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveResult, MotionResultInbox, MotionWorkerCancelStatus,
    MotionWorkerHealth, MotionWorkerReadiness, _execute_job,
)
from mc2p.motion_nav.motion_solver import SolveResult, SolveStatus
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.retry_ledger import RetryLedger
from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.support_surfaces import (
    SurfaceNodeId, SupportSurface, HorizontalRegion,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.movement_transition import MovementMode
from tests.motion_nav.test_b10_gap_solver import fixture
from tests.motion_nav import test_b10_motion_candidate as candidate_fixtures
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav import test_planning_coordinator as planning_fixtures
from mc2p.motion_nav.planning_coordinator import InformationOutcome, PlanningUpdateKind
from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry, WorldKnowledge, WorldSessionId
from mc2p.motion_nav.world_interaction import BlockPlacementTransaction, PlacementState
from tests.motion_nav import test_b11_block_placement as placement_fixtures
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.block_placement_driver import RuntimeBlockPlacementDriver
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.observation import Vec3V0
from tests.test_player_runtime import _RecordingTrace
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.observation_v3 import TargetingStateV3
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.action_v1 import MovementV1
from tests.follow_v3_fixtures import observed_block
from tests.sim.backend import CalculatorBackend, Scene, Perturbations
from tests.sim.async_monitor import AsyncInvariantMonitor
from tests.motion_nav import test_runtime_navigation_verified_handoff as runtime_gap_fixtures
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
from tests.motion_nav.test_navigation_session import _InlineMotionWorker
from tests.motion_nav import test_navigation_session as session_fixtures
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.motion_nav.async_work import AsyncOwnerDiagnostics, AsyncWorkEvent, AsyncAdmissionRecord, AsyncAdmissionDisposition


class DeferredMotionWorker:
    def __init__(self):
        self.jobs = []
        self.results = []
    def submit(self, job):
        self.jobs.append(job)
        return True
    def poll_available(self):
        results, self.results = tuple(self.results), []
        return results
    @property
    def health(self):
        return MotionWorkerHealth(
            MotionWorkerReadiness.READY, None, 0, None, None,
        )
    def cancel(self, identity, _status=None):
        return MotionWorkerCancelStatus.ACCEPTED
    def close(self):
        pass
    def is_alive(self):
        return True


def gap_owner(worker, *, inbox=None, clock_ns=lambda: 100_000_000):
    anchor, world, _, _ = fixture()
    start = SupportSurface(
        SurfaceNodeId(0, 0, 64, 0), (.5, 64., .5),
        HorizontalRegion(0, 0, 1, 1), 1., ("minecraft:grass_block",), (),
    )
    end = SupportSurface(
        SurfaceNodeId(0, 2, 64, 0), (.5, 64., 2.5),
        HorizontalRegion(0, 2, 1, 3), 1., ("minecraft:grass_block",), (),
    )
    route = ActiveRoute(
        "same-route", 1, "request", "goal", 1, anchor.session.value,
        None, 2., 0., (),
        ExecutableCorridor((start.node_id, end.node_id), (), 2., end.node_id),
        ActionRoute("same-route", (JumpGapSegment(
            JumpGapEdge(start.node_id, end.node_id, "gap", .9, ()),
            start, end, (),
        ),)), planning_generation=1,
    )
    executor = ActionRouteExecutor(
        ground_profile(), jump_profile(), step_profile(),
        air_profiles=(air_profile(MovementMode.JUMP_GAP),),
    )
    frame = candidate_fixtures.VerifiedMotionRouteIntegrationTests.frame(world._world, anchor.physics_state, 1)
    owner = MotionRouteCoordinator(
        route, executor, worker, retry_ledger=RetryLedger(route.goal_id),
        result_inbox=inbox, clock_ns=clock_ns,
        computation_scope=AsyncComputationScope((route).world_session, (RetryLedger(route.goal_id)).task_id, 1),
    )
    owner.start(frame)
    return owner, frame, anchor, world, InputApplicationLedger(max_records=64)


class R27IdentityTests(unittest.TestCase):
    def test_information_verification_crossing_deadline_cannot_issue_progress_permit(self):
        world = planning_fixtures._world(unknown=frozenset({(0, 0, 0)}))
        request = planning_fixtures._request(world)
        clock = [1_000_000_000]
        coordinator = planning_fixtures.PlanningCoordinatorTests().coordinator(clock_ns=lambda: clock[0])
        current = planning_fixtures.frame(world, 0, (-.5, 1., .5))
        coordinator.begin(request, current, permit=planning_fixtures._permit(), state_anchor=None,
                          remaining_damage_budget=request.damage_budget)
        notification = coordinator.advance(current, state_anchor=None, edge_probe=None,
                                           remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        world.observe_blocks(ObservationStamp(world.session, 2, 2, "clock", clock[0]),
                             {(0, 0, 0): BlockGeometry.full_cube("minecraft:stone")})
        original = coordinator.information_fact_is_acquired
        def expensive_query(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 2_100_000_000
            return result
        with patch.object(coordinator, "information_fact_is_acquired", expensive_query):
            result = coordinator.reconcile_information(notification, planning_fixtures.frame(world, 1, (-.5, 1., .5)), current_scope=coordinator._request_ledger.current_computation_scope)
        self.assertIs(result.kind, PlanningUpdateKind.FAILED)
        self.assertFalse(coordinator.has_owned_work)
        self.assertFalse(any(r.disposition is AsyncAdmissionDisposition.APPLIED
                             and r.identity.work_kind is AsyncWorkKind.INFORMATION
                             for r in coordinator.admission_records))

    def test_motion_verification_crossing_deadline_cannot_install_proof(self):
        import mc2p.motion_nav.motion_coordination as coordination
        clock = [100_000_000]
        worker = DeferredMotionWorker()
        owner, frame, anchor, world, ledger = gap_owner(worker, clock_ns=lambda: clock[0])
        owner.decide(frame, anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
        worker.results.append(_execute_job(worker.jobs[-1]))
        original = coordination.prepare_planned_gap_motion
        def expensive_verify(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 1_000_000_000
            return result
        with patch.object(coordination, "prepare_planned_gap_motion", expensive_verify):
            decision = owner.decide(frame, anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
        self.assertFalse(decision.movement.jump)
        self.assertFalse(any(r.disposition is AsyncAdmissionDisposition.APPLIED for r in owner.admission_records))

    def test_retirement_history_eviction_does_not_allow_identity_restart(self):
        from mc2p.contracts.common import ContractViolation
        lifecycle = AsyncWorkLifecycle(history_limit=2)
        old = AsyncWorkIdentity(AsyncComputationScope("world", "task", 1), "owner", AsyncWorkKind.MOTION_SOLVE, "edge", 1)
        for revision in range(1, 12):
            identity = replace(old, revision=revision)
            lifecycle.begin(identity, AsyncWorkWindow(revision, revision * 100, revision * 100 + 99))
            lifecycle.finish(identity, "cancel", revision * 100 + 1)
        with self.assertRaises(ContractViolation):
            lifecycle.begin(old, AsyncWorkWindow(12, 1200, 1299))

    def test_truncated_world_change_history_falls_back_to_actual_route_facts(self):
        world = planning_fixtures._world()
        request = planning_fixtures._request(world)
        coordinator = planning_fixtures.PlanningCoordinatorTests().coordinator(planning_fixtures._DeferredPlanner())
        current = planning_fixtures.frame(world, 0, (-.5, 1., .5))
        coordinator.begin(request, current, permit=planning_fixtures._permit(), state_anchor=None,
                          remaining_damage_budget=request.damage_budget)
        coordinator.advance(current, state_anchor=None, edge_probe=None,
                            remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        revision = world.view().geometry_revision
        world.confirm_air(ObservationStamp(world.session, 2, 2, "clock", 2),
                          tuple((10_000 + index, 0, 0) for index in range(4100)))
        self.assertIsNone(world.changes_since(revision))
        result = coordinator.advance(planning_fixtures.frame(world, 1, (-.5, 1., .5)),
                                     state_anchor=None, edge_probe=None, remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        self.assertIs(result.kind, PlanningUpdateKind.ROUTE_READY)

    def test_monitor_rejects_information_apply_without_determined_query(self):
        identity = AsyncWorkIdentity(AsyncComputationScope("world", "task", 1), "owner", AsyncWorkKind.INFORMATION, "fact", 1)
        window = AsyncWorkWindow(1, 100, 200)
        evidence = AsyncOwnerDiagnostics("owner", identity, window, (
            AsyncWorkEvent(identity, window, "begin", 100),
            AsyncWorkEvent(identity, window, "apply", 150),
        ), (AsyncAdmissionRecord(identity, 150, 200, True, True, AsyncAdmissionDisposition.APPLIED),))
        monitor = AsyncInvariantMonitor()
        monitor.check((evidence,))
        self.assertEqual({code for code, _ in monitor.violations}, {"I21"})

    def test_current_entry_rejection_does_not_retry_each_settling_velocity_sample(self):
        from tests.sim.event_sequences import GeneratedEvent, GeneratedSequence, EventKind, run_sequence
        for scenario in ("direct_drop_2", "direct_drop_5_budget_2"):
            with self.subTest(scenario=scenario):
                result = run_sequence(GeneratedSequence(23089, scenario, 400, (
                    GeneratedEvent(EventKind.EXTERNAL_PUSH_BACKWARD, 35),
                )))
                self.assertIsNone(result.exception)
                self.assertEqual(result.result.violations, [])
                self.assertEqual(result.result.reason, "motion_unsolvable:needs_state")

    def test_session_consumes_information_failure_instead_of_reopening_without_permit(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                world, gap = session_fixtures._known_endpoints_with_unknown_gap()
                start, goal = session_fixtures._nodes(world, (-1, 1))
                clock = [1_000_000_000]
                session = NavigationSession("late-fact", session_fixtures.NavigationSessionTests().profiles(),
                    planner_worker=_InlinePlanner(), motion_worker=_InlineMotionWorker(), clock_ns=lambda: clock[0])
                session.bind_source(session_fixtures._source())
                current = planning_fixtures.frame(world, 0, start.position)
                session.start(planning_fixtures.SurfacePlanningRequest(1, "request", "goal", 1,
                    world.session.value, start.node_id, goal.node_id, goal_state=_goal(goal.position)), current)
                session.propose(current, None, clock[0] + 500_000_000)
                self.assertEqual(session.report.state.value, "needs_information")
                initial_generation = session.diagnostics.request_generation
                world.observe_blocks(ObservationStamp(world.session, 2, 2, "clock", clock[0]),
                                     {gap: BlockGeometry.full_cube("minecraft:stone")})
                clock[0] += 2_100_000_000 if expired else 50_000_000
                current = planning_fixtures.frame(world, 1, start.position)
                session.observe(current, (gap,))
                self.assertEqual(session.report.state.value, "failed" if expired else "planning")
                if expired:
                    self.assertEqual(session.diagnostics.request_generation, initial_generation)
                else:
                    self.assertGreater(session.diagnostics.request_generation, initial_generation)
                session.close()

    def test_native_frontier_three_and_full_batch_expire_once(self):
        for large in (False, True):
            with self.subTest(large=large):
                radius = 10 if large else 1
                minimum, maximum = (-16, 27) if large else (-3, 5)
                world = WorldKnowledge(WorldSessionId("native-information-batch"))
                stamp = ObservationStamp(world.session, 1, 1, "clock", 1)
                unknown = {(x, 0, z) for x in range(minimum, maximum + 1)
                           for z in range(minimum, maximum + 1)
                           if (max(abs(x), abs(z)) == radius if large else z == 1)}
                world.confirm_air(stamp, tuple((x, y, z)
                    for x in range(minimum, maximum + 1) for y in range(-1, 4)
                    for z in range(minimum, maximum + 1) if (x, y, z) not in unknown))
                world.observe_blocks(stamp, {(x, 0, z): BlockGeometry.full_cube("minecraft:stone")
                    for x in range(minimum, maximum + 1) for z in range(minimum, maximum + 1)
                    if (x, 0, z) not in unknown})
                goal_column = (11, 11) if large else (0, 2)
                start = planning_fixtures.query_support_surfaces(world.view(), 0, 0, 1, 1).surfaces[0]
                goal = planning_fixtures.query_support_surfaces(world.view(), *goal_column, 1, 1).surfaces[0]
                request = planning_fixtures.SurfacePlanningRequest(1, "native", "goal", 1,
                    world.session.value, start.node_id, goal.node_id, maximum_planning_seconds=5.)
                clock = [1_000_000_000]
                coordinator = planning_fixtures.PlanningCoordinator("task",
                    planning_fixtures.PlanningCapabilities(ordinary_profile(), step_profile(), jump_profile(), (), None),
                    planner_worker=_InlinePlanner(), route_admitter=planning_fixtures.RouteAdmitter(),
                    retry_ledger=planning_fixtures.RetryLedger("task"), clock_ns=lambda: clock[0],
                    snapshot_cells_per_step=100_000, planning_margin_cells=16 if large else 1)
                current = planning_fixtures.frame(world, 0, start.position)
                permit = replace(planning_fixtures._permit(), task_id="task")
                coordinator.begin(request, current, permit=permit, state_anchor=None,
                                  remaining_damage_budget=request.damage_budget)
                update = coordinator.advance(current, state_anchor=None, edge_probe=None,
                                             remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
                self.assertIs(update.kind, PlanningUpdateKind.NEEDS_INFORMATION)
                need = update.information_need
                self.assertEqual(len(need.blockers), 64 if large else 3)
                clock[0] += 2_100_000_000
                result = coordinator.reconcile_information(update, current,
                    outcomes=tuple((b.blocker_key, InformationOutcome.TIMED_OUT) for b in need.blockers), current_scope=coordinator._request_ledger.current_computation_scope)
                self.assertIs(result.kind, PlanningUpdateKind.FAILED)
                info_finishes = [e for e in coordinator.async_diagnostics.events
                                 if e.operation == "finish" and e.identity.work_kind is AsyncWorkKind.INFORMATION]
                self.assertEqual(len(info_finishes), 1)

    def test_formal_runtime_successor_rejects_old_same_goal_gap_failure(self):
        self._run_formal_successor_delivery(0)

    def test_seeded_formal_successor_delivery_orders(self):
        for seed in range(1, 13):
            with self.subTest(seed=seed):
                self._run_formal_successor_delivery(seed)

    def _run_formal_successor_delivery(self, seed):
        class ClockedGapBackend(runtime_gap_fixtures._GapRuntimeBackend):
            def observation(self, **kwargs):
                observation = super().observation(**kwargs)
                # This transport-only fixture remains stationary on support.
                # Report the collision state that a real neutral tick produces.
                return replace(observation, self_state=replace(observation.self_state,
                    value=replace(observation.self_state.value,
                                  vertical_collision=not self.airborne)))

            def step(self, action, deadline, **kwargs):
                result = super().step(action, deadline, **kwargs)
                receipt = asdict(result.receipt)
                receipt["input_applications"] = list(receipt["input_applications"])
                if not receipt["input_applications"]:
                    receipt["leased_input_samples"] = 1
                    receipt["input_applications"] = [{
                        "schema_version": "mc2p.input-application.v1",
                        "movement_tick_id": self.movement_tick, "episode_id": action.episode_id,
                        "request_sequence_id": action.request_sequence_id, "sampled_at_jvm_ns": self.movement_tick,
                        "state": "leased", "forward": 0., "strafe": 0.,
                        "jump": False, "sneak": False, "sprint": False,
                    }]
                return replace(result, receipt=behavior_receipt_from_mapping(receipt))
        clock = [100_000_000]
        backend = ClockedGapBackend(clock)
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        reset = runtime.reset(ResetRequestV0("reset", "episode-verified-runtime", "test", 1, 10_000_000_000))
        self.assertTrue(reset.succeeded, reset.failure)
        self.addCleanup(runtime.close)
        profiles = NavigationSessionProfiles(
            replace(ordinary_profile(), support_materials=frozenset({"minecraft:grass_block"})),
            jump_profile(), step_profile(), air=(air_profile(MovementMode.JUMP_GAP),))
        worker = DeferredMotionWorker()
        session = NavigationSession("original", profiles, planner_worker=_InlinePlanner(),
                                    motion_worker=worker, clock_ns=lambda: clock[0])
        driver = RuntimeNavigationDriver(runtime, session, clock_ns=lambda: clock[0])
        goal = _goal((.5, 64., 2.5))
        driver.start("same-goal", 1, goal, clock[0])
        for _ in range(10):
            result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            self.assertIsNone(result.report.failure)
            if worker.jobs:
                break
        self.assertTrue(worker.jobs)
        old = worker.jobs[-1]
        session.cancel("replace-session")
        driver.release("replace-session")
        for _ in range(10):
            if driver.source is None:
                break
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertIsNone(driver.source)
        successor = session.spawn_successor("successor", task_id="successor-task")
        driver = RuntimeNavigationDriver(runtime, successor, clock_ns=lambda: clock[0])
        driver.start("same-goal", 1, goal, clock[0], task_id="successor-task")
        for _ in range(10):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if len(worker.jobs) > 1:
                break
        new = worker.jobs[-1]
        self.assertNotEqual(old.work_identity, new.work_identity)
        old_failure = GapMotionSolveResult(old.connection_id, old.candidate_revision,
            SolveResult(SolveStatus.INTERNAL_ERROR), 1, old.work_identity)
        rng = random.Random(seed)
        worker.results.extend([old_failure] * rng.randint(1, 3))
        result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertIsNone(result.report.failure)
        self.assertFalse(successor.report.terminal)
        self.assertTrue(successor.active_motion_mailboxes)
        new_result = _execute_job(new)
        deliveries = [new_result, old_failure, _execute_job(old), new_result]
        rng.shuffle(deliveries)
        worker.results.extend(deliveries)
        result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertIsNone(result.report.failure)
        # Two transport ticks have passed: the old start window is expired.
        # The formal control path must request worker replay, never replay on
        # the control thread or let an old duplicate cancel that new job.
        from mc2p.motion_nav.motion_worker import MotionJobOperation
        refresh = worker.jobs[-1]
        self.assertIs(refresh.operation, MotionJobOperation.REVALIDATE)
        self.assertNotEqual(refresh.work_identity, new.work_identity)
        self.assertFalse(result.decision.action.movement.jump)
        worker.results.extend((_execute_job(refresh), old_failure, new_result))
        result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertIsNone(result.report.failure)
        for _ in range(4):
            if result.decision.action.movement.jump:
                break
            result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            self.assertIsNone(result.report.failure)
        self.assertTrue(result.decision.action.movement.jump)
        monitor = AsyncInvariantMonitor()
        monitor.check(successor.async_work_diagnostics, successor.active_motion_mailboxes)
        self.assertEqual(monitor.violations, [])
        self.assertGreater(monitor.coverage["applied"], 0)

    def test_monitor_detects_expired_retired_application_and_orphan_mailbox(self):
        identity = AsyncWorkIdentity(AsyncComputationScope("world", "task", 1), "owner", AsyncWorkKind.MOTION_SOLVE, "edge", 1)
        window = AsyncWorkWindow(1, 100, 200)
        evidence = AsyncOwnerDiagnostics("owner", None, None, (
            AsyncWorkEvent(identity, window, "begin", 100),
            AsyncWorkEvent(identity, window, "finish", 150, "cancel"),
        ), (AsyncAdmissionRecord(identity, 210, 200, True, True, AsyncAdmissionDisposition.APPLIED),),
            ((identity, "pending"),))
        monitor = AsyncInvariantMonitor()
        monitor.check((evidence,), (identity,))
        self.assertEqual({code for code, _ in monitor.violations}, {"I19", "I20", "I22"})

    def test_monitor_does_not_hide_resources_after_identity_is_cleared(self):
        monitor = AsyncInvariantMonitor()
        monitor.check((AsyncOwnerDiagnostics("owner", None, None, (), (), ((None, "pending_job"),)),))
        self.assertEqual({code for code, _ in monitor.violations}, {"I20"})

    def test_cancelled_placement_drains_real_inertia_and_lands_before_release(self):
        class ContactBackend(CalculatorBackend):
            def observation(self, **kwargs):
                observation = super().observation(**kwargs)
                box = self.state.body_box
                contacts = {b.position: b for b in observation.perception.value.blocks}
                for x in range(math.floor(box.min_x), math.ceil(box.max_x)):
                    for y in range(math.floor(box.min_y), math.ceil(box.max_y)):
                        for z in range(math.floor(box.min_z), math.ceil(box.max_z)):
                            if (x, y, z) not in self.scene.solids:
                                contacts[(x, y, z)] = observed_block((x, y, z), "minecraft:air", kind="empty", sources=("body_contact",))
                perception = replace(observation.perception,
                    value=replace(observation.perception.value, blocks=tuple(contacts[p] for p in sorted(contacts))))
                if self.sequence == 0:
                    return replace(observation, perception=perception)
                return replace(observation, field_profile="interaction_v1",
                    targeting=replace(observation.targeting, status=FieldStatusV0.VALID,
                                      reason_code=None, value=TargetingStateV3("miss", None, None, None, None, None)),
                    perception=perception)
        for airborne in (False, True):
            for late in (False, True):
                with self.subTest(airborne=airborne, late=late):
                    clock = [100_000_000]
                    scene = Scene({(x, 63, z): "minecraft:stone" for x in range(-2, 3) for z in range(-2, 3)},
                                  ((-2, 3), (60, 70), (-2, 3)))
                    backend = ContactBackend(clock, scene, (.5, 64.35 if airborne else 64., .5),
                        episode="episode-1", perturbations=Perturbations(late_ticks=frozenset({2}) if late else frozenset()))
                    backend.state = replace(backend.state, on_ground=not airborne,
                                            velocity_blocks_per_tick=(.04, -.0784, 0.))
                    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
                    reset = runtime.reset(ResetRequestV0("reset", "episode-1", "test", 1, 10_000_000_000))
                    self.assertTrue(reset.succeeded, reset.failure)
                    self.addCleanup(runtime.close)
                    transaction = BlockPlacementTransaction(placement_fixtures.requirement(
                        world_session=runtime.navigation_observation_adapter.latest_frame.session.value), computation_scope=placement_fixtures.placement_scope(placement_fixtures.requirement(
                        world_session=runtime.navigation_observation_adapter.latest_frame.session.value)))
                    driver = RuntimeBlockPlacementDriver(runtime, transaction, clock_ns=lambda: clock[0])
                    driver.start()
                    driver.cancel("cancel-with-inertia")
                    self.assertIsNotNone(driver.source)
                    for _ in range(30):
                        result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                        if result is not None:
                            self.assertIsNone(result.report.failure)
                        if driver.source is None:
                            break
                    self.assertIsNone(driver.source)
                    self.assertTrue(backend.state.on_ground)
                    self.assertLessEqual(math.hypot(*backend.state.velocity_blocks_per_tick[::2]) * 20, .10)
                    self.assertTrue(all(a.operation is None for a in backend.actions))

    def test_placement_cancel_retains_source_while_grounded_body_has_velocity(self):
        class MovingBackend(placement_fixtures._PlacementBackend):
            def _observation(self, **kwargs):
                observation = super()._observation(**kwargs)
                return replace(observation, self_state=replace(observation.self_state,
                    value=replace(observation.self_state.value, velocity=Vec3V0(.04, 0., 0.))))
        clock = [100_000_000]
        runtime = PlayerRuntimeV1(MovingBackend(clock), _RecordingTrace(), lambda: clock[0])
        runtime.reset(ResetRequestV0("reset", "episode-1", "test", 1, 10_000_000_000))
        transaction = BlockPlacementTransaction(placement_fixtures.requirement(), computation_scope=placement_fixtures.placement_scope(placement_fixtures.requirement()))
        driver = RuntimeBlockPlacementDriver(runtime, transaction, clock_ns=lambda: clock[0])
        self.addCleanup(runtime.close)
        driver.start()
        source = driver.source
        driver.cancel("cancel-moving-placement")
        self.assertIs(driver.source, source)
        self.assertIs(transaction.report.state, PlacementState.CANCELLED)

    def test_placement_uses_dispatch_and_processing_clock_even_without_new_observation(self):
        for new_observation in (False, True):
            with self.subTest(new_observation=new_observation):
                clock = [1_000_000_000]
                adapter = NavigationObservationAdapter()
                transaction = BlockPlacementTransaction(placement_fixtures.requirement(maximum_attempts=1),
                                                        clock_ns=lambda: clock[0], computation_scope=placement_fixtures.placement_scope(placement_fixtures.requirement(maximum_attempts=1)))
                old = placement_fixtures.observation(1)
                proposal = transaction.propose(old, adapter.ingest(old))
                transaction.register_dispatch(proposal, selected=True,
                                              receipt_status="pending_confirmation", receipt_reason="dispatch",
                                              control_sequence=1, dispatched_monotonic_ns=clock[0])
                self.assertEqual(transaction.work_window.started_monotonic_ns, 1_000_000_000)
                confirmation = (placement_fixtures.observation(2, destination="minecraft:dirt", count=2)
                                if new_observation else old)
                clock[0] = 1_200_000_000
                result = transaction.propose(confirmation, adapter.ingest(confirmation))
                self.assertIs(result.state, PlacementState.FAILED)
                self.assertEqual(result.reason, "confirmation_timeout")

    def test_revoked_partial_placement_evidence_cannot_be_combined(self):
        for world_first in (True, False):
            with self.subTest(world_first=world_first):
                adapter = NavigationObservationAdapter()
                transaction = BlockPlacementTransaction(placement_fixtures.requirement(), computation_scope=placement_fixtures.placement_scope(placement_fixtures.requirement()))
                first = placement_fixtures.observation(1)
                proposal = transaction.propose(first, adapter.ingest(first))
                transaction.register_dispatch(proposal, selected=True,
                                              receipt_status="pending_confirmation",
                                              receipt_reason="test-dispatch", control_sequence=1)
                partial = (placement_fixtures.without_inventory(placement_fixtures.observation(
                    2, destination="minecraft:dirt")) if world_first else
                    placement_fixtures.observation(2, count=2))
                transaction.propose(partial, adapter.ingest(partial))
                revoked = placement_fixtures.observation(
                    3, destination="air" if world_first else "minecraft:dirt",
                    count=2 if world_first else 3,
                )
                result = transaction.propose(revoked, adapter.ingest(revoked))
                self.assertNotEqual(result.state, PlacementState.COMPLETE)

    def test_unrelated_change_inside_search_margin_does_not_discard_valid_route(self):
        world = planning_fixtures._world()
        request = planning_fixtures._request(world)
        coordinator = planning_fixtures.PlanningCoordinatorTests().coordinator(planning_fixtures._DeferredPlanner())
        current = planning_fixtures.frame(world, 0, (-.5, 1., .5))
        coordinator.begin(request, current, permit=planning_fixtures._permit(),
                          state_anchor=None, remaining_damage_budget=request.damage_budget)
        coordinator.advance(current, state_anchor=None, edge_probe=None, remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        position = (-2, 2, -1)
        world.observe_blocks(ObservationStamp(world.session, 2, 2, "clock", 2),
                             {position: BlockGeometry.full_cube("minecraft:stone")})
        coordinator.observe_changes((position,))
        result = coordinator.advance(planning_fixtures.frame(world, 1, (-.5, 1., .5)),
                                     state_anchor=None, edge_probe=None, remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        self.assertIs(result.kind, PlanningUpdateKind.ROUTE_READY)

    def test_negative_planning_detects_world_change_even_when_caller_drops_exact_change_list(self):
        world = planning_fixtures._world()
        world.confirm_air(ObservationStamp(world.session, 2, 2, "clock", 2), ((0, 0, 0),))
        request = planning_fixtures._request(world)
        coordinator = planning_fixtures.PlanningCoordinatorTests().coordinator(planning_fixtures._DeferredPlanner())
        current = planning_fixtures.frame(world, 0, (-.5, 1., .5))
        coordinator.begin(request, current, permit=planning_fixtures._permit(),
                          state_anchor=None, remaining_damage_budget=request.damage_budget)
        coordinator.advance(current, state_anchor=None, edge_probe=None, remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        world.observe_blocks(ObservationStamp(world.session, 3, 3, "clock", 3),
                             {(0, 0, 0): BlockGeometry.full_cube("minecraft:stone")})
        result = coordinator.advance(planning_fixtures.frame(world, 1, (-.5, 1., .5)),
                                     state_anchor=None, edge_probe=None, remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        self.assertIs(result.kind, PlanningUpdateKind.RUNNING)
        self.assertTrue(coordinator.has_owned_work)

    def test_expiring_natural_information_batch_finishes_once_and_late_fact_cannot_reopen(self):
        clock = [1_000_000_000]
        world = planning_fixtures._world(unknown=frozenset({(0, 0, 0)}))
        request = planning_fixtures._request(world)
        coordinator = planning_fixtures.PlanningCoordinatorTests().coordinator(clock_ns=lambda: clock[0])
        current = planning_fixtures.frame(world, 0, (-.5, 1., .5))
        coordinator.begin(request, current, permit=planning_fixtures._permit(),
                          state_anchor=None, remaining_damage_budget=request.damage_budget)
        update = coordinator.advance(current, state_anchor=None, edge_probe=None,
                                     remaining_damage_budget=request.damage_budget, current_scope=coordinator._request_ledger.current_computation_scope)
        need = update.information_need
        self.assertIsNotNone(need)
        clock[0] += 2_100_000_000
        outcomes = tuple((b.blocker_key, InformationOutcome.TIMED_OUT) for b in need.blockers)
        result = coordinator.reconcile_information(update, current, outcomes=outcomes, current_scope=coordinator._request_ledger.current_computation_scope)
        self.assertIs(result.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(result.failure.reason, "information_timeout")
        for _ in range(3):
            late = coordinator.reconcile_information(update, current, outcomes=outcomes, current_scope=coordinator._request_ledger.current_computation_scope)
            self.assertIs(late.kind, PlanningUpdateKind.DISCARDED)
        self.assertFalse(coordinator.has_owned_work)

    def test_motion_result_expiring_during_delivery_never_submits_jump(self):
        clock = [100_000_000]
        worker = DeferredMotionWorker()
        owner, frame, anchor, world, ledger = gap_owner(worker, clock_ns=lambda: clock[0])
        owner.decide(frame, anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
        result = _execute_job(worker.jobs[-1])
        worker.results.append(result)
        original = worker.poll_available
        def late_delivery():
            clock[0] += 1_000_000_000
            return original()
        worker.poll_available = late_delivery
        decision = owner.decide(frame, anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
        self.assertFalse(decision.movement.jump)
        self.assertFalse(any(r.disposition.value == "applied" for r in owner.admission_records))

    def test_old_negative_entry_is_recomputed_under_current_body(self):
        worker = DeferredMotionWorker()
        owner, frame, anchor, world, ledger = gap_owner(worker)
        old_anchor = replace(anchor, physics_state=replace(
            anchor.physics_state, velocity_blocks_per_tick=(0., 0., .30),
        ))
        old_frame = candidate_fixtures.VerifiedMotionRouteIntegrationTests.frame(world._world, old_anchor.physics_state, 1)
        owner.decide(old_frame, old_anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
        job = worker.jobs[-1]
        worker.results.append(GapMotionSolveResult(
            job.connection_id, job.candidate_revision,
            SolveResult(SolveStatus.NEEDS_STATE, reasons=("entry_speed_outside_trial",)),
            1, job.work_identity,
        ))
        decision = owner.decide(frame, anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
        self.assertEqual(decision.state.value, "running")
        self.assertGreaterEqual(len(worker.jobs), 2)

    def test_lifecycle_checks_actual_time_and_old_finish_does_not_touch_new_work(self):
        owner = AsyncWorkLifecycle(history_limit=2)
        identity = AsyncWorkIdentity(AsyncComputationScope("world", "task", 1), "owner", AsyncWorkKind.MOTION_SOLVE, "edge", 1)
        owner.begin(identity, AsyncWorkWindow(1, 100, 200))
        self.assertIs(owner.check(identity, 199, current_scope=identity.scope), WorkCheck.READY)
        self.assertIs(owner.check(identity, 200, current_scope=identity.scope), WorkCheck.EXPIRED)
        self.assertFalse(owner.finish(identity, "timeout", 200).already_retired)
        next_identity = replace(identity, revision=2)
        owner.begin(next_identity, AsyncWorkWindow(2, 200, 300))
        self.assertTrue(owner.finish(identity, "late", 210).already_retired)
        self.assertEqual(owner.identity, next_identity)
        self.assertEqual(len([e for e in owner.events if e.operation == "finish"]), 1)
    def test_recreating_same_route_allocates_distinct_execution_identity(self):
        worker = DeferredMotionWorker()
        identities = []
        for _ in range(2):
            owner, frame, anchor, world, ledger = gap_owner(worker)
            owner.decide(frame, anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
            identities.append(worker.jobs[-1].work_identity)
            owner.cancel_work()
        self.assertNotEqual(identities[0], identities[1])

    def test_same_owner_name_in_different_worlds_never_shares_mail(self):
        worker = DeferredMotionWorker()
        inbox = MotionResultInbox(max_results=2)
        first = AsyncWorkIdentity(AsyncComputationScope("world-a", "task", 1), "owner", AsyncWorkKind.MOTION_SOLVE, "edge", 1)
        second = replace(first, scope=replace(first.scope, world_session_id="world-b"))
        for identity in (first, second):
            self.assertTrue(inbox.register(identity))
            worker.results.append(GapMotionSolveResult(
                "edge", 1, SolveResult(SolveStatus.INTERNAL_ERROR), 0, identity,
            ))
        inbox.drain_once(worker, 1)
        self.assertEqual(tuple(r.work_identity for r in inbox.take(first)), (first,))
        self.assertEqual(tuple(r.work_identity for r in inbox.take(second)), (second,))

    def test_unknown_duplicates_and_retired_results_cannot_evict_active_result(self):
        inbox = MotionResultInbox(max_results=1)
        worker = DeferredMotionWorker()
        identity = AsyncWorkIdentity(AsyncComputationScope("world", "task", 1), "owner", AsyncWorkKind.MOTION_SOLVE, "edge", 1)
        self.assertTrue(inbox.register(identity))
        current = GapMotionSolveResult("edge", 1, SolveResult(SolveStatus.INTERNAL_ERROR), 0, identity)
        worker.results.extend([current, current])
        worker.results.extend(replace(current, work_identity=replace(identity, revision=i)) for i in range(2, 150))
        inbox.drain_once(worker, 1)
        self.assertEqual(inbox.take(identity), (current,))
        inbox.retire(identity)
        worker.results.append(current)
        inbox.drain_once(worker, 2)
        self.assertEqual(inbox.take(identity), ())


if __name__ == "__main__":
    unittest.main()
