"""State-triggered R27 combinations; physical movement remains calculator-driven."""
from dataclasses import asdict, replace
from pathlib import Path
import random

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.async_work import AsyncWorkKind
from mc2p.motion_nav.bridge_planner import BridgePlacementPolicy
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_interaction import PlacementState
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.world_change_navigation_driver import RuntimeWorldChangeNavigationDriver
from tests.sim.async_monitor import AsyncCoverageRequirement, AsyncInvariantMonitor, ObservedAsyncActivity
from tests.sim.backend import Perturbations, Scene
from tests.sim.placement_backend import PlacementCalculatorBackend
from tests.sim.runner import InlinePlannerWorker, InlineMotionWorker, _goal, seed_memory
from tests.test_player_runtime import _RecordingTrace
from tests.sim.monitor import InvariantMonitor


def run_information_sequence(seed):
    from unittest.mock import patch
    from mc2p.motion_nav.planning_coordinator import PlanningCoordinator, PlanningUpdateKind
    from tests.sim.runner import Event, run
    from tests.sim.scenarios import SCENARIOS
    base = next(item for item in SCENARIOS if item.name == "flat_walk")
    notification = []
    dispatched = []
    getter = PlanningCoordinator.current_information_update.fget
    from tests.sim.backend import CalculatorBackend, _air_result

    class HeldFactsBackend(CalculatorBackend):
        def observation(self, **kwargs):
            snapshot = super().observation(**kwargs)
            hidden = {(x, 63, 7) for x in (-1, 0, 1)}
            perception = snapshot.perception.value
            requested = set(kwargs.get("air_positions", ())).intersection(hidden)
            results = tuple(result for result in perception.air_query_results if result.position not in hidden)
            results += tuple(_air_result({"position": list(cell), "status": "occluded",
                "observer_distance_blocks": None, "lower_region_visible": None}) for cell in sorted(requested))
            return replace(snapshot, perception=replace(snapshot.perception, value=replace(perception,
                blocks=tuple(block for block in perception.blocks if block.position not in hidden),
                air_query_results=results)))

    def capture(owner):
        update = getter(owner)
        if update is not None:
            notification.append((owner, update))
        return update

    def interrupt(context):
        owner, old = notification[-1]
        context.driver.release("async_information_cancel")
        dispatched.append("cancel_information_work")
        for _ in range(1 + seed % 3):
            result = owner.reconcile_information(old, context.runtime_frame)
            if result.kind is not PlanningUpdateKind.DISCARDED or result.information_identity != old.information_identity:
                raise AssertionError("old information notification was not independently discarded")
            dispatched.append("old_notification_discarded")

    # The context has the formal Runtime-owned latest frame through the driver.
    def action(context):
        context.runtime_frame = context.driver.runtime.navigation_observation_adapter.latest_frame
        interrupt(context)

    scenario = replace(base, name=f"information-exit-{seed}",
        initial_unknown_cells=frozenset({(x, 63, 7) for x in (-1, 0, 1)}),
        events=[Event("information_exit", lambda c: c.session.planning_information_update is not None, action)],
        expect="cancelled", async_coverage=AsyncCoverageRequirement(
            (AsyncWorkKind.PLANNING, AsyncWorkKind.INFORMATION)))
    with patch.object(PlanningCoordinator, "current_information_update", property(capture)):
        result = run(scenario, backend_factory=HeldFactsBackend)
    return {"seed": seed, "passed": result.verdict == "PASS" and bool(dispatched),
            "task_outcome": result.outcome, "events": dispatched,
            "verification": asdict(result.verification), "trace": result.trace}


def placement_fixture(*, perturbations=None):
    clock = [100_000_000]
    solids = {(x, 63, 0): "minecraft:stone" for x in (-1, 0, 2, 3)}
    solids.update({(x, y, z): "minecraft:stone" for x in range(-1, 4)
                   for y in range(63, 68) for z in (-1, 1)})
    scene = Scene(solids, ((-2, 4), (60, 70), (-2, 2))).with_floor()
    backend = PlacementCalculatorBackend(clock, scene, (.5, 64., .5), 90.,
                                         perturbations=perturbations)
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
    reset = runtime.reset(ResetRequestV0("reset-placement", backend.episode, "test", 1, 10_000_000_000))
    if not reset.succeeded:
        raise AssertionError(reset.failure)
    seed_memory(runtime, scene)
    planner = InlinePlannerWorker()
    motion = InlineMotionWorker()
    profiles = replace(NavigationSessionProfiles.load(Path("config/motion-navigation")), air=())
    session = NavigationSession("placement-sim", profiles, planner_worker=planner,
        motion_worker=motion, bridge_policy=BridgePlacementPolicy(maximum_blocks=1), clock_ns=lambda: clock[0])
    driver = RuntimeWorldChangeNavigationDriver(runtime, session, clock_ns=lambda: clock[0])
    driver.start("placement-goal", 1, _goal((2.5, 64., .5)), clock[0])
    return clock, backend, runtime, session, driver, planner


def run_placement_sequence(seed, *, mode=None, body=None, repeated=None, partial=False):
    rng = random.Random(seed)
    mode = mode or ("complete", "failed", "cancelled")[seed % 3]
    body = body or ("inertia", "pending", "air")[seed // 3 % 3]
    repeated = repeated or (1 if seed % 2 else 3)
    clock, backend, runtime, session, driver, planner = placement_fixture()
    if mode != "normal":
        backend.dispatch_impulse = (.025 + rng.random() * .01, .05 if body == "air" else -.0784, 0.)
        backend.dispatch_lift = .25 if body == "air" else 0.
    backend.inventory_delivery_missing = mode == "cancelled"
    backend.report_wrong_destination = mode == "failed" and not partial
    if partial:
        backend.partial_world_only = True
    body_monitor = InvariantMonitor()
    monitor = body_monitor.async_monitor
    activity = []
    events = []
    trace = []
    retained = None
    original_state = None
    stop_triggered = False
    partial_seen = False
    clock_expired = False
    revoked_identity = None
    after_release = 0
    try:
        for tick in range(1, 181):
            if driver.report.terminal:
                driver.release()
                backend.free_tick()
                after_release += 1
            else:
                before_tick = backend.movement_tick
                result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                if backend.movement_tick == before_tick:
                    backend.free_tick()
                if result is not None and result.report.failure is not None:
                    raise AssertionError(result.report.failure)
                active = driver.placement
                if active is not None:
                    retained = active
                    transaction = active.transaction
                    if transaction.work_identity is not None:
                        activity.append(ObservedAsyncActivity(transaction.work_identity, "dispatch"))
                    if (backend.placements and partial and not partial_seen
                            and backend.movement_tick > backend.confirmation_started):
                        # World-only confirmation is delivered through the ordinary observation.
                        partial_seen = transaction.report.state is PlacementState.AWAITING_CONFIRMATION
                        if partial_seen:
                            destination = backend.placements[0]
                            backend.scene = Scene({cell: block for cell, block in backend.scene.solids.items()
                                                   if cell != destination}, backend.scene.volume)
                            backend._build_truth()
                            backend.partial_world_only = False
                            revoked_identity = transaction.work_identity
                            clock[0] = transaction.work_window.deadline_monotonic_ns + 1 + seed % 17
                            clock_expired = True
                            events.extend(("world_confirmation_revoked", "processing_deadline_crossed"))
                    if (mode == "cancelled" and backend.placements
                            and not transaction.report.terminal):
                        driver.cancel("cancel_after_dispatch")
                    if (mode != "normal" and transaction.report.terminal
                            and active.source is not None and not stop_triggered):
                        original_state = transaction.report.state
                        if body == "pending":
                            backend.perturbations.late_ticks = frozenset({backend.movement_tick + 1})
                        for _ in range(repeated):
                            driver.cancel("cancel_during_terminal_body_stop")
                            events.append("repeat_stop")
                        stop_triggered = True
                    elif transaction.report.terminal and mode == "normal":
                        original_state = transaction.report.state
            owners = session.async_work_diagnostics + (() if retained is None else (retained.transaction.async_diagnostics,))
            monitor.check(owners, session.active_motion_mailboxes)
            body_sources = tuple(source.source_id for source in (
                driver.navigation.source, None if retained is None else retained.source) if source is not None)
            body_monitor.check_body_responsibility(backend.movement_tick,
                on_ground=backend.state.on_ground, source_bound=bool(body_sources), controller_ids=body_sources)
            trace.append({"tick": tick, "movement_tick": backend.movement_tick,
                "position": backend.state.position, "velocity": backend.state.velocity_blocks_per_tick,
                "yaw": backend.state.yaw_radians,
                "session": asdict(session.report),
                "on_ground": backend.state.on_ground, "world_driver": asdict(driver.report),
                "placement": None if retained is None else asdict(retained.transaction.report),
                "source_owned": retained is not None and retained.source is not None,
                "events": list(events), "async_events": [asdict(event) for event in monitor.last_events],
                "actual_applied": backend.applied_commands[-1] if backend.applied_commands else None})
            if after_release >= 12:
                break
        requirement = AsyncCoverageRequirement(
            (AsyncWorkKind.PLANNING, AsyncWorkKind.PLACEMENT_CONFIRMATION),
            (AsyncWorkKind.PLANNING,) + (() if mode != "normal" else (AsyncWorkKind.PLACEMENT_CONFIRMATION,)))
        verification = body_monitor.finalize(requirement, (*planner.activity, *activity))
        terminal = driver.report.terminal
        safe = (terminal and (retained is None or retained.source is None)
                and backend.state.on_ground and math_speed(backend.state.velocity_blocks_per_tick) <= .10)
        expected = (PlacementState.FAILED if body == "air" else PlacementState.COMPLETE) if partial else {
            "complete": PlacementState.COMPLETE, "normal": PlacementState.COMPLETE,
            "failed": PlacementState.FAILED, "cancelled": PlacementState.CANCELLED}[mode]
        passed = (safe and not body_monitor.violations and retained is not None and retained.transaction.report.state is expected
                  and verification.complete and (mode == "normal" or stop_triggered)
                  and (not partial or (partial_seen and clock_expired)))
        if original_state is not None:
            passed &= retained.transaction.report.state is original_state
        passed &= driver.report.confirmed_placements == (1 if expected is PlacementState.COMPLETE else 0)
        passed &= driver.report.state == ("success" if mode == "normal" else "cancelled")
        if partial:
            from mc2p.motion_nav.async_work import AsyncAdmissionDisposition
            passed &= not any(record.identity == revoked_identity
                and record.disposition is AsyncAdmissionDisposition.APPLIED
                for record in retained.transaction.async_diagnostics.admissions)
        return {"seed": seed, "mode": mode, "body": body, "repeated": repeated,
                "passed": passed, "task_outcome": driver.report.state, "safe_release": safe,
                "stop_triggered": stop_triggered, "events": events, "partial_seen": partial_seen,
                "inventory_count": backend.items, "placements": backend.placements,
                "body_violations": body_monitor.violations,
                "verification": asdict(verification), "trace": trace}
    finally:
        session.close()
        runtime.close()


def math_speed(velocity):
    return (velocity[0] ** 2 + velocity[2] ** 2) ** .5 * 20


def run_gap_sequence(seed, *, service_followup=True):
    from mc2p.motion_nav.motion_worker import GapMotionSolveResult, _execute_job
    from mc2p.motion_nav.motion_solver import SolveResult, SolveStatus
    from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
    from tests.sim.backend import CalculatorBackend
    rng = random.Random(seed)

    class HeldWorker(InlineMotionWorker):
        def __init__(self):
            super().__init__()
            self.jobs = []
            self.deliveries = []

        def submit(self, job):
            self.jobs.append(job)
            self.activity.append(ObservedAsyncActivity(job.work_identity, "submit"))
            return True

        def poll_available(self):
            done, self.deliveries = tuple(self.deliveries), []
            self.activity.extend(ObservedAsyncActivity(result.work_identity, "poll") for result in done)
            return done

    clock = [100_000_000]
    scene = Scene({(x, 63, z): "minecraft:grass_block" for x in (-1, 0, 1) for z in (0, 2, 3)},
                  ((-2, 2), (60, 70), (-1, 4))).with_floor()
    backend = CalculatorBackend(clock, scene, (.5, 64., .5))
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
    runtime.reset(ResetRequestV0("reset-gap", backend.episode, "test", 1, 10_000_000_000))
    seed_memory(runtime, scene)
    planner, worker = InlinePlannerWorker(), HeldWorker()
    original = NavigationSession("old-gap", NavigationSessionProfiles.load(Path("config/motion-navigation")),
        planner_worker=planner, motion_worker=worker, clock_ns=lambda: clock[0])
    current = original
    driver = RuntimeNavigationDriver(runtime, current, clock_ns=lambda: clock[0])
    goal = _goal((.5, 64., 2.5))
    driver.start("same-goal", 1, goal, clock[0])
    monitor = AsyncInvariantMonitor()
    events, trace = [], []
    old = None
    new = None
    old_delivered = False
    delivered = False
    serviced_job_count = 0
    followup_operations = []
    after_release = 0
    try:
        for tick in range(1, 161):
            before_tick = backend.movement_tick
            if driver.source is None:
                if old is not None and current is original:
                    current = original.spawn_successor("new-gap")
                    driver = RuntimeNavigationDriver(runtime, current, clock_ns=lambda: clock[0])
                    driver.start("same-goal", 1, goal, clock[0])
                    events.append("same_goal_successor_started")
                else:
                    backend.free_tick()
                    after_release += 1
            if driver.source is not None:
                if current is not original and len(worker.jobs) >= 2 and not old_delivered:
                    new = worker.jobs[-1]
                    worker.deliveries = [GapMotionSolveResult(old.connection_id, old.candidate_revision,
                        SolveResult(SolveStatus.INTERNAL_ERROR), 0, old.work_identity)]
                    old_delivered = True
                    events.append("old_failure_delivered")
                elif new is not None and not delivered:
                    deliveries = [_execute_job(new), _execute_job(old), _execute_job(new)]
                    rng.shuffle(deliveries)
                    worker.deliveries.extend(deliveries)
                    delivered = True
                    serviced_job_count = len(worker.jobs)
                    events.append("interleaved_results_delivered")
                elif delivered and service_followup and len(worker.jobs) > serviced_job_count:
                    # Interleaving is the injected fault, not permanent worker
                    # starvation. Service later solve/revalidation jobs through
                    # the same real implementation, on the following frame.
                    pending = worker.jobs[serviced_job_count:]
                    worker.deliveries.extend(_execute_job(job) for job in pending)
                    followup_operations.extend(job.operation.value for job in pending)
                    serviced_job_count = len(worker.jobs)
                result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                if result is not None and result.report.failure is not None:
                    raise AssertionError(result.report.failure)
                if old is None and worker.jobs:
                    old = worker.jobs[0]
                    driver.release("replace_gap_session")
                    events.append("old_owner_stopped")
                elif driver.source is not None and driver.state in {"success", "failed", "cancelled"}:
                    driver.release("gap_sequence_terminal")
            if backend.movement_tick == before_tick:
                backend.free_tick()
            owners = original.async_work_diagnostics
            if current is not original:
                owners += current.async_work_diagnostics
            monitor.check(owners, current.active_motion_mailboxes)
            trace.append({"tick": tick, "position": backend.state.position, "on_ground": backend.state.on_ground,
                "source_owned": driver.source is not None, "driver_state": driver.state,
                "driver_reason": driver.reason, "events": list(events),
                "async_events": [asdict(event) for event in monitor.last_events],
                "actual_applied": backend.applied_commands[-1] if backend.applied_commands else None})
            if after_release >= 12:
                break
        verification = monitor.finalize(AsyncCoverageRequirement(
            (AsyncWorkKind.PLANNING, AsyncWorkKind.MOTION_SOLVE),
            (AsyncWorkKind.PLANNING, AsyncWorkKind.MOTION_SOLVE)), (*planner.activity, *worker.activity))
        passed = (delivered and driver.state == "success" and driver.source is None
                  and backend.state.on_ground and verification.complete
                  and old.work_identity != new.work_identity)
        return {"seed": seed, "passed": passed, "task_outcome": driver.state,
                "events": events, "followup_operations": followup_operations,
                "verification": asdict(verification), "trace": trace}
    finally:
        current.close()
        if current is not original:
            original.close()
        runtime.close()
