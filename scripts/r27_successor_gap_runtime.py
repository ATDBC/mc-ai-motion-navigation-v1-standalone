"""Real profile-4 body, formal Session creation, deliberately reordered solve results."""
from dataclasses import asdict
import time

from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.motion_nav.world_model import Aabb, CellKnowledge
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from mc2p.motion_nav.motion_worker import GapMotionSolveResult, _execute_job
from mc2p.motion_nav.motion_solver import SolveResult, SolveStatus
from scripts.control_probe_core import append_jsonl


class HeldSolveTestWorker:
    """Only delivery is controlled; solve uses the production job function."""
    def __init__(self):
        self.jobs, self.results = [], []
    def is_alive(self):
        return True
    def close(self):
        self.results.clear()
    def submit(self, job):
        self.jobs.append(job)
        return True
    def poll_available(self):
        result, self.results = tuple(self.results), []
        return result


def run_successor_gap_case(runtime, task, profile, profiles, directory, deadline_ns, fixture_writer, feet_y, diagnostic):
    fixture_writer((
        f"fill -3 {feet_y - 2} -3 3 {feet_y + 4} 8 minecraft:air replace",
        f"setblock 0 {feet_y - 1} 0 minecraft:stone",
        f"setblock 0 {feet_y - 1} 2 minecraft:stone",
        f"tp MC2PProbe 0.5 {feet_y} 0.5 0.0 30.0",
    ))
    request = ObservationRequestV3("navigation_v1", tuple(
        (x, y, z) for x in range(-1, 2) for y in range(feet_y - 2, feet_y + 4) for z in range(0, 4)))
    for _ in range(8):
        result = runtime.step(task, profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000), observation_request=request)
        diagnostic()
        if result.report.failure is not None:
            raise RuntimeError(f"R27 gap preparation failed: {result.report.failure}")
    # The jump reaches the third cell above the feet. Observe it through the
    # same profile-4 sensor; looking only at the supports cannot establish it.
    fixture_writer((f"tp MC2PProbe 0.5 {feet_y} 0.5 0.0 -60.0",))
    for _ in range(6):
        result = runtime.step(task, profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000), observation_request=request)
        diagnostic()
        if result.report.failure is not None:
            raise RuntimeError(f"R27 overhead observation failed: {result.report.failure}")
    fixture_writer((f"tp MC2PProbe 0.5 {feet_y} 0.5 0.0 0.0",))
    for _ in range(2):
        runtime.step(task, profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000), observation_request=request)
        diagnostic()
    frame = runtime.navigation_observation_adapter.latest_frame
    if any(frame.world.cell((0, feet_y - 1, z)).knowledge is not CellKnowledge.BLOCK for z in (0, 2)):
        raise RuntimeError("R27 gap preparation has not observed both real supports")
    worker = HeldSolveTestWorker()
    original = NavigationSession("r27-original", profiles,
        observation_adapter=runtime.navigation_observation_adapter, motion_worker=worker)
    driver = RuntimeNavigationDriver(runtime, original)
    goal = GoalState(Aabb(.3, feet_y - .05, 2.3, .7, feet_y + .05, 2.7), GoalSupport.SOLID,
                     frozenset({MovementMode.WALK}), frozenset({"standing"}), .6)
    sessions = [original]
    try:
        driver.start("r27-same-goal", 1, goal, time.perf_counter_ns())
        if driver.state in {"failed", "success", "cancelled"}:
            raise RuntimeError(f"R27 original start rejected: {original.report}; body={frame.body}")
        for _ in range(60):
            result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
            diagnostic()
            if result.report.failure is not None:
                raise RuntimeError(f"R27 original control failed: {result.report.failure}")
            if worker.jobs:
                break
            if driver.state in {"failed", "success", "cancelled"}:
                raise RuntimeError(f"R27 original ended before solve: {original.report}; "
                    f"head={[runtime.navigation_observation_adapter.latest_frame.world.cell((0, feet_y + y, z)).knowledge.value for y in range(4) for z in range(3)]}")
        if not worker.jobs:
            raise RuntimeError(f"R27 original never requested a solve: {original.report}")
        old = worker.jobs[-1]
        original.cancel("r27-successor-test")
        driver.release("r27-successor-test")
        for _ in range(20):
            if driver.source is None:
                break
            driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
            diagnostic()
        if driver.source is not None:
            raise RuntimeError("R27 original did not safely release")
        successor = original.spawn_successor("r27-successor")
        sessions.append(successor)
        driver = RuntimeNavigationDriver(runtime, successor)
        driver.start("r27-same-goal", 1, goal, time.perf_counter_ns())
        for _ in range(60):
            result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
            diagnostic()
            if result.report.failure is not None:
                raise RuntimeError(f"R27 successor control failed: {result.report.failure}")
            if len(worker.jobs) > 1:
                break
        new = worker.jobs[-1]
        if new.work_identity == old.work_identity:
            raise RuntimeError("R27 same-task successor reused identity")
        worker.results.append(GapMotionSolveResult(old.connection_id, old.candidate_revision,
            SolveResult(SolveStatus.INTERNAL_ERROR), 0, old.work_identity))
        result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
        diagnostic()
        if result.report.failure is not None or successor.report.terminal:
            raise RuntimeError("R27 old failure changed the successor")
        worker.results.extend((_execute_job(new), _execute_job(old)))
        for tick in range(1, 81):
            result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
            diagnostic()
            append_jsonl(directory / "r27-successor-gap-frames.jsonl", {
                "tick": tick, "observation": runtime.observation.sequence_id,
                "report": asdict(successor.report),
                "owners": [asdict(owner) for owner in successor.async_work_diagnostics]})
            if result.report.failure is not None or driver.state in {"success", "failed", "cancelled"}:
                break
        row = {"passed": driver.state == "success", "old": asdict(old.work_identity),
               "new": asdict(new.work_identity), "successor_report_reason": successor.diagnostics.reason,
               "state": driver.state, "reason": driver.reason}
        append_jsonl(directory / "r27-successor-gap-trials.jsonl", row)
        if not row["passed"]:
            raise RuntimeError(f"R27 successor gap failed: {row}")
        driver.release("r27-gap-complete")
        return row
    finally:
        for session in reversed(sessions):
            session.close()
