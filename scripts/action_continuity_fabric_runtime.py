"""Small real-Fabric continuity probe using the formal Runtime navigation path.

Fixture commands only build/teleport on the separately owned local test server.
The actor acquires every navigation fact through its profile-4 depth observation.
No fixture geometry or TEST_ORACLE memory is supplied to the navigation session.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path
import time

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_worker import MotionSolverWorker
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from scripts.control_probe_core import write_json_atomic
from scripts.r25_planning_information_runtime import _task


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "mc2p/motion_nav/motion_solver.py", "mc2p/motion_nav/motion_candidate.py",
    "mc2p/motion_nav/motion_worker.py", "mc2p/motion_nav/motion_coordination.py",
    "mc2p/motion_nav/action_route_executor.py", "mc2p/motion_nav/fixed_route.py",
    "mc2p/motion_nav/route_admission.py", "mc2p/motion_nav/navigation_session.py",
    "mc2p/skills/navigation_session_driver.py", "scripts/action_continuity_fabric_runtime.py",
    "mc2p/motion_nav/segment_entry.py", "mc2p/motion_nav/ground_traversal.py",
    "mc2p/motion_nav/known_map_planner.py",
)
TERMINAL = {"success", "failed", "cancelled", "stopped", "interaction_required"}


class _ObservedMotionWorker:
    """Read-only transport timing through the existing real worker port."""

    def __init__(self, path, trial, *, cold):
        self.path, self.trial = path, trial
        self.worker = None if cold else MotionSolverWorker(max_pending=4)

    def _record(self, **values):
        with self.path.open('a', encoding='utf8') as stream:
            stream.write(json.dumps(dict(trial=self.trial, at_ns=time.perf_counter_ns(), **values))+'\n')

    def submit(self, job):
        if self.worker is None:
            self.worker = MotionSolverWorker(max_pending=4)
        submitted = self.worker.submit(job)
        self._record(event='submit', submitted=submitted,
            identity=asdict(job.work_identity), source_tick=job.anchor.movement_tick_id,
            source_state=asdict(job.anchor.physics_state), prefix=[asdict(c) for c in job.entry_prefix],
            window=asdict(job.request.execution_window), operation=job.operation.value)
        return submitted

    def poll_available(self):
        results = () if self.worker is None else self.worker.poll_available()
        for result in results:
            proof = result.solve_result.proof
            self._record(event='result', identity=asdict(result.work_identity),
                status=result.solve_result.status.value, reasons=result.solve_result.reasons,
                compute_ns=result.elapsed_ns, window=None if proof is None else asdict(proof.execution_window),
                preparation=None if proof is None or proof.preparation is None else
                dict(source_tick=proof.preparation.source_anchor.movement_tick_id,
                     prefix_ticks=len(proof.preparation.commands), entry_tick=proof.anchor_movement_tick_id))
        return results

    def is_alive(self):
        return self.worker is None or self.worker.is_alive()

    def close(self):
        if self.worker is not None:
            self.worker.close()


def _rot(x, z, direction):
    """Rotate about the centre of cell (0,0), matching the component fixture."""
    x, z = x - .5, z - .5
    for _ in range(direction):
        x, z = z, -x
    return x + .5, z + .5


def _hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def _metrics(rows):
    samples = {sample["movement_tick_id"]: (sample, row)
               for row in rows for sample in row["input_samples"]}
    ordered = sorted(samples)
    jumps = [tick for tick in ordered if samples[tick][0]["jump"]]
    entries = []
    for tick in jumps:
        before = next((row for row in reversed(rows) if row["movement_tick"] < tick), None)
        entries.append(None if before is None else math.hypot(before["velocity"][0], before["velocity"][2]))
    approach_gap = landing_gap = None
    air_reverse_ticks, air_neutral_ticks = [], []
    landing_position = landing_speed = None
    if jumps:
        active = [tick for tick in ordered if tick < jumps[0]
                  and any(samples[tick][0][name] for name in ("forward", "strafe", "jump", "sneak", "sprint"))]
        if active:
            approach_gap = jumps[0] - active[-1] - 1
        landing = next((row["movement_tick"] for row in rows
                        if row["movement_tick"] > jumps[0] and row["on_ground"]), None)
        if landing is not None:
            air_reverse_ticks = [tick for tick in ordered if jumps[0] < tick < landing
                                 and samples[tick][0]["forward"] < 0]
            air_neutral_ticks = [tick for tick in ordered if jumps[0] < tick < landing
                                 and not any(samples[tick][0][name]
                                             for name in ("forward", "strafe", "jump", "sneak", "sprint"))]
            landing_row = next(row for row in rows if row["movement_tick"] == landing)
            landing_position = landing_row["position"]
            landing_speed = math.hypot(landing_row["velocity"][0], landing_row["velocity"][2])
        following = [] if landing is None else [tick for tick in ordered if tick >= landing
                    and any(samples[tick][0][name] for name in ("forward", "strafe"))]
        if following:
            landing_gap = following[0] - landing
    return dict(jump_ticks=jumps, jump_entry_speed_blocks_per_second=entries,
                movement_gap_before_jump_ticks=approach_gap,
                landing_to_movement_gap_ticks=landing_gap,
                airborne_reverse_ticks=air_reverse_ticks,
                airborne_neutral_ticks=air_neutral_ticks,
                first_landing_position=landing_position,
                first_landing_motion_speed_blocks_per_second=landing_speed,
                unowned_active_input_ticks=sorted({tick for row in rows for tick in row["unowned_active_input_ticks"]}),
                outside_window_sequences=sorted({seq for row in rows for seq in row["outside_window_sequences"]}))


def run_action_continuity_runtime(runtime, backend, episode, directory, deadline_ns,
                                 fixture_writer, *, trace_owns_diagnostics=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source_hashes = _hashes()
    selected = os.environ.get("MC2P_ACTION_CONTINUITY_DIRECTION")
    directions = range(4) if selected is None else (int(selected),)
    if any(direction not in range(4) for direction in directions):
        raise ValueError("continuity direction must be 0,1,2,3")
    profile, task = BehaviorProfileV0(), _task(deadline_ns)
    profiles = NavigationSessionProfiles.load(ROOT / "config/motion-navigation")
    trials, diagnostic_rows = [], []
    summary = {"probe": "action-continuity-formal-fabric", "sources": source_hashes,
               "trials": trials, "passed": False, "simulation_is_not_fabric": True}

    def next_deadline():
        return min(deadline_ns, time.perf_counter_ns() + 500_000_000)

    def diagnostic():
        # The launcher owns writing these records, but still needs the returned
        # rows to independently check that every sample remained image-free.
        diagnostic_rows.append(dict(episode_id=episode,
            observation_sequence_id=runtime.observation.sequence_id,
            diagnostics=backend.last_diagnostics))

    def observe(request=None):
        result = runtime.step(task, profile, next_deadline(), observation_request=request)
        diagnostic()
        if result.report.failure is not None:
            raise RuntimeError(result.report.failure)

    def teleport(x, z, yaw, pitch, feet_y=100.):
        fixture_writer((f"tp MC2PProbe {x} {feet_y} {z} {yaw} {pitch}",))
        for _ in range(10):
            observe()
            body = runtime.navigation_observation_adapter.latest_frame.body
            if math.dist(body.position, (x, feet_y, z)) < .08 and body.is_on_ground:
                return
        raise RuntimeError("fixture teleport was not observed on its support")

    diagnostic()  # Include reset observation zero in the continuous chain.
    delivery_probe = os.environ.get('MC2P_MOTION_START_DELIVERY_PROBE') == '1'
    selected_kind = os.environ.get('MC2P_MOTION_START_DELIVERY_KIND')
    kinds = (selected_kind,) if selected_kind is not None else ('moving_gap', 'cold_gap', 'jump_up', 'drop_2', 'drop_5')
    if any(kind not in {'moving_gap', 'cold_gap', 'jump_up', 'drop_2', 'drop_5'} for kind in kinds):
        raise ValueError('undeclared start-delivery family')
    cases = (tuple((kind, direction, late) for kind in kinds
                   for direction in directions for late in (False, True))
             if delivery_probe else tuple(('moving_gap', direction, False) for direction in directions))
    summary['frozen_cases'] = cases
    with (directory / "action-continuity-frames.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        for kind, direction, late_first in cases:
            name = (f"start-delivery-{kind}-{direction}-{'late' if late_first else 'normal'}"
                    if delivery_probe else f"continuity-gap-{direction}")
            session = driver = worker = None
            # Warm-process trials start transport before fixture observation.
            # Cold-gap trials deliberately let the session start its worker.
            if delivery_probe:
                worker = _ObservedMotionWorker(directory / 'motion-start-timing.jsonl', name,
                                               cold=kind == 'cold_gap')
            rows, activity_by_sequence = [], {}
            trial = dict(id=name, kind=kind, direction=direction, late_first=late_first,
                         worker_start='cold-on-demand' if kind == 'cold_gap' else 'before-fixture',
                         outcome="not_started", passed=False)
            delay_injected = False
            started = time.perf_counter_ns()
            try:
                if deadline_ns - started < 15_000_000_000:
                    raise RuntimeError("insufficient deadline for fixture and safe cleanup")
                fixture_writer(("forceload add -16 -16 16 16",
                    "fill -18 88 -18 18 107 18 minecraft:air replace",
                    "fill -18 89 -18 18 89 18 minecraft:stone replace",
                    "difficulty peaceful", "gamerule naturalRegeneration false",
                    "gamemode survival MC2PProbe",
                    "effect give MC2PProbe minecraft:instant_health 1 8 true",
                    "effect clear MC2PProbe"))
                drop = int(kind[-1]) if kind.startswith('drop_') else 0
                def feet_at(z):
                    return 100 + (1 if kind == 'jump_up' and z >= 3 else 0) - (drop if drop and z >= 3 else 0)
                supports = [(px, feet_at(z), pz) for x in range(-2, 3)
                            for z in range(-2, 10) if kind not in {'cold_gap', 'moving_gap'} or z != 3
                            for px, pz in (_rot(x + .5, z + .5, direction),)]
                fixture_writer(tuple(f"setblock {math.floor(x)} {y-1} {math.floor(z)} minecraft:grass_block"
                                     for x, y, z in supports))
                requested = tuple((math.floor(px), y, math.floor(pz))
                    for x in range(-2, 3) for z in range(-2, 10)
                    for px, pz in (_rot(x + .5, z + .5, direction),) for y in range(98-drop, 105))
                for x, z, yaw, pitch in ((-1.5, 2.5, -45, 45), (1.5, 4.5, 135, 45),
                                          (-1.5, 2.5, -45, -30), (1.5, 4.5, 135, -30)):
                    px, pz = _rot(x, z, direction)
                    teleport(px, pz, yaw - 90 * direction, pitch, feet_at(math.floor(z)))
                    for offset in range(0, len(requested), 128):
                        observe(ObservationRequestV3("navigation_v1", requested[offset:offset + 128]))
                sx, sz = _rot(.5, 2.5, direction) if kind == 'cold_gap' else _rot(.55, .65, direction)
                # Direct-drop startup requires the landing support to remain
                # freshly visible. Looking down preserves that existing gate.
                initial_pitch = 45. if drop else 0.
                trial['initial_pitch_degrees'] = initial_pitch
                teleport(sx, sz, -90 * direction, initial_pitch)
                for _ in range(4):
                    observe()
                gx, gz = _rot(.5, 8.5, direction)
                goal_y = feet_at(8)
                policy = 'd055-declared-fall-damage' if drop == 5 else 'no_expected_damage'
                budget = TaskDamageBudget(policy, 2. if drop == 5 else 0.)
                goal = GoalState(Aabb(gx-.2, goal_y-.08, gz-.2, gx+.2, goal_y+.08, gz+.2),
                    GoalSupport.SOLID, frozenset({MovementMode.WALK}), frozenset({"standing"}), .6)
                goal = replace(goal, risk_policy_id=policy)
                session = NavigationSession(name, profiles,
                    observation_adapter=runtime.navigation_observation_adapter,
                    motion_worker=worker)
                driver = RuntimeNavigationDriver(runtime, session)
                driver.start(name, 1, goal, time.perf_counter_ns(), damage_budget=budget)
                first_tick = runtime.navigation_observation_adapter.latest_frame.body.movement_tick_id
                task_started = time.perf_counter_ns()

                def tick(phase):
                    nonlocal delay_injected
                    before = runtime.navigation_observation_adapter.latest_frame.body.movement_tick_id
                    begun = time.perf_counter_ns()
                    controls = driver.prepare_proposals(next_deadline())
                    prepared = time.perf_counter_ns()
                    windows = [envelope.intent.movement_tick_window
                               for proposal in controls for envelope in proposal.intents
                               if envelope.intent.movement_tick_window is not None]
                    if late_first and not delay_injected and windows:
                        if windows[0].latest_tick - windows[0].earliest_tick != 1:
                            raise RuntimeError('new motion grant lost first-input delay slack')
                        trial['injected_first_window'] = asdict(windows[0])
                        time.sleep(.055)
                        delay_injected = True
                    result = runtime.control_frame(task, profile, next_deadline(), proposals=controls)
                    driver.adopt_result(result)
                    finished = time.perf_counter_ns()
                    diagnostic()
                    frame, fd = runtime.navigation_observation_adapter.latest_frame, driver.last_frame_diagnostics
                    if fd.action_request_sequence is not None:
                        activity_by_sequence[fd.action_request_sequence] = fd.movement_activity
                        if len(activity_by_sequence) > 128:
                            del activity_by_sequence[next(iter(activity_by_sequence))]
                    samples = runtime.input_ledger.samples_between(before + 1, frame.body.movement_tick_id)
                    unowned, outside = [], []
                    records = []
                    for sample in samples:
                        active = any((sample.forward, sample.strafe, sample.jump, sample.sneak, sample.sprint))
                        if active and activity_by_sequence.get(sample.request_sequence_id) is None:
                            unowned.append(sample.movement_tick_id)
                        record = None if sample.request_sequence_id is None else runtime.input_ledger.record(sample.request_sequence_id)
                        if record is not None:
                            records.append(asdict(record))
                            if record.status.value == "applied_outside_window":
                                outside.append(record.control_sequence)
                    row = dict(trial=name, phase=phase, frame=len(rows),
                        observation_sequence=frame.body.sequence_id, movement_tick=frame.body.movement_tick_id,
                        position=frame.body.position, velocity=frame.body.velocity_blocks_per_second,
                        on_ground=frame.body.is_on_ground, pose=frame.body.pose, yaw=frame.body.yaw_radians,
                        health=frame.body.health_points, state=driver.state, reason=driver.reason,
                        controller_ids=session.diagnostics.controller_ids, source_bound=driver.source is not None,
                        input_samples=[asdict(sample) for sample in samples], input_records=records,
                        winning_activity=None if fd.movement_activity is None else asdict(fd.movement_activity),
                        selected_intents=fd.selected_intents, suppressed_intents=fd.suppressed_intents,
                        prepare_ns=prepared-begun, roundtrip_ns=finished-begun,
                        unowned_active_input_ticks=unowned, outside_window_sequences=outside,
                        runtime_failure=None if result.report.failure is None else str(result.report.failure))
                    if delivery_probe:
                        row['motion_admissions'] = [asdict(record) for owner in
                            session.async_work_diagnostics for record in owner.admissions]
                    rows.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
                    if len(rows) % 20 == 0:
                        stream.flush()
                    if result.report.failure is not None or unowned or outside:
                        raise RuntimeError(row["runtime_failure"] or "input ownership/window violation")

                for _ in range(300):
                    if driver.state in TERMINAL or deadline_ns - time.perf_counter_ns() < 8_000_000_000:
                        break
                    tick("navigation")
                trial.update(outcome=driver.state, reason=driver.reason, passed=driver.state == "success",
                    late_injection_applied=delay_injected,
                    movement_ticks=runtime.navigation_observation_adapter.latest_frame.body.movement_tick_id-first_tick,
                    task_elapsed_ns=time.perf_counter_ns()-task_started)
            except Exception as error:
                trial.update(outcome="error", error=f"{type(error).__name__}:{error}")
            finally:
                if driver is not None and driver.source is not None:
                    try:
                        if driver.has_prepared_frame:
                            driver.discard_prepared()
                        driver.release("action_continuity_probe_end")
                        for _ in range(120):
                            if driver.source is None:
                                break
                            tick("cleanup")
                        if driver.source is not None:
                            raise RuntimeError("body owner did not release within cleanup limit")
                    except Exception as error:
                        trial.update(passed=False, cleanup_error=f"{type(error).__name__}:{error}")
                if session is not None and (driver is None or driver.source is None):
                    session.close()
                if worker is not None and (driver is None or driver.source is None):
                    worker.close()
                stream.flush()
                trial.update(metrics=_metrics(rows), elapsed_ns=time.perf_counter_ns()-started,
                             final_position=runtime.navigation_observation_adapter.latest_frame.body.position,
                             source_released=driver is None or driver.source is None)
                if trial["passed"] and (((not drop and not trial["metrics"]["jump_ticks"])
                                         or not trial["source_released"])
                                        or (late_first and not delay_injected)):
                    trial.update(passed=False, evidence_error="arrival lacked actual jump or safe source release")
                trials.append(trial)
                summary.update(passed=all(value["passed"] for value in trials), sources_after=_hashes())
                if summary["sources_after"] != source_hashes:
                    summary.update(passed=False, source_changed_during_run=True)
                write_json_atomic(directory / "action-continuity-summary.json", summary)
            if not trial["passed"]:
                break  # Fail fast; do not rebuild another fixture under a failed task.
    checks = [dict(name=row["id"], passed=row["passed"]) for row in trials]
    return summary, diagnostic_rows, checks
