"""Local test scheduling only; leader state is kept outside the follower's constructor."""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.runtime.player_runtime_v1 import RuntimeStateV1
from mc2p.runtime.trace import trace_projection
from mc2p.skills.follow import RuleFollower
from mc2p.skills.follow_driver import FollowDriver
from mc2p.skills.follow_types import FollowRequest
from mc2p.skills.local_perception import project_follow_view
from scripts.control_probe_core import append_jsonl, write_json_atomic


def follow_task(deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0("follow-probe", "follow", "{}",
        (SuccessCriterionV0("follow_distance", ComparisonOperatorV0.LESS_THAN_OR_EQUAL, 4, "blocks"),),
        200, deadline_ns, True, 0)


def scripted_leader_controls(case: str, slot: int) -> tuple[MovementV1, LookV1]:
    if case not in {"static", "moving", "obstacle", "hazard"} or type(slot) is not int or not 0 <= slot < 2400:
        raise ValueError("undeclared leader script position")
    if case in {"static", "obstacle", "hazard"}:
        return MovementV1(), LookV1()
    return (MovementV1(forward=1 if slot < 81 and slot % 4 == 1 else 0),
            LookV1(yaw_delta_degrees=15 if slot in (51, 55, 59) else 0))


def _neutral(runtime, task, profile, deadline):
    result = runtime.step(task, profile, min(deadline, time.perf_counter_ns()+1_000_000_000))
    if result.observation is None or result.report.failure is not None:
        raise RuntimeError("follow scene neutral failed: "+str(result.report))
    return result


def _scripted_leader_step(runtime, task, profile, deadline_ns, controls, slot):
    source = "scripted-leader"
    runtime.cancel_source(source)
    movement, look = controls
    if movement != MovementV1() or look != LookV1():
        now = time.perf_counter_ns()
        obs = runtime.observation
        runtime.submit_intent(ActionIntentV1(f"leader/{obs.episode_id}/{slot}", source, obs.episode_id, obs.sequence_id,
            ActionPriorityV0.TASK, now, min(deadline_ns, now+250_000_000), movement=movement, look=look, valid_for_ticks=1))
    return _neutral(runtime, replace(task, task_id="scripted-leader", task_type="leader_script"), profile, deadline_ns)


def paired_update(leader, driver, task, profile, deadline_ns, executor, *, controls=None, slot=None):
    """One independent Runtime per player; no truth or actions cross their arbiters."""
    pending = (executor.submit(_neutral, leader, task, profile, deadline_ns) if controls is None else
               executor.submit(_scripted_leader_step, leader, task, profile, deadline_ns, controls, slot))
    try:
        result = driver.tick(task, profile, deadline_ns)
    except BaseException as error:
        try:
            pending.result(timeout=max(.001, (deadline_ns-time.perf_counter_ns())/1e9))
        except BaseException as peer_error:
            error.add_note("paired leader update also failed: "+repr(peer_error))
        raise
    pending.result(timeout=max(.001, (deadline_ns-time.perf_counter_ns())/1e9))
    return result


def _warmup_follow(host, driver, task, profile) -> None:
    deadline = min(host.deadline_ns, time.perf_counter_ns()+15_000_000_000)
    write_json_atomic(host.run_dir/"warmup-plan.json", dict(deadline_ns=deadline, entry_distance_max=3.5,
                                                           purpose="bounded_predeclared_approach_before_moving_phase"))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="follow-warmup") as executor:
        while time.perf_counter_ns() < deadline:
            step = paired_update(host.leader, driver, task, profile,
                                 min(deadline,time.perf_counter_ns()+1_000_000_000),executor)
            if step is not None:
                append_jsonl(host.run_dir/"warmup.jsonl", trace_projection(step.report))
                if step.report.terminal: raise RuntimeError("moving warmup follow terminated: "+str(step.report))
                if step.report.target_status == "visible" and step.report.distance_blocks <= 3.5:
                    return
            time.sleep(.05)
    raise TimeoutError("moving phase predeclared warmup did not reach band")


def run_follow_scene(host) -> tuple[list[dict], dict]:
    task, profile = follow_task(host.deadline_ns), BehaviorProfileV0()
    binding_deadline = min(host.deadline_ns, time.perf_counter_ns()+10_000_000_000)
    while True:
        _neutral(host.follower, task, profile, binding_deadline)
        observation = host.follower.observation
        now = time.perf_counter_ns()
        initial = project_follow_view(observation, now, observation.controller_clock_id)
        players = [e for e in initial.entities if e.entity_type == "minecraft:player"]
        if initial.available and len(players) == 1:
            break
        if now >= binding_deadline:
            raise TimeoutError("no uniquely visible legal target for follow binding")
        time.sleep(.05)
    req = FollowRequest(f"{host.scenario}-{host.seed}", initial.episode_id, players[0].track_id, now,
                        min(now+120_000_000_000, host.deadline_ns), initial.controller_clock_id)
    driver = FollowDriver(host.follower, RuleFollower(req, initial))
    if host.scenario == "moving":
        try:
            _warmup_follow(host, driver, task, profile)
        except BaseException:
            if host.follower.state is RuntimeStateV1.READY:
                driver.stop(task, profile, time.perf_counter_ns()+1_000_000_000, "warmup_failed")
            raise
    interval, duration = 100_000_000, 12_000_000_000 if host.scenario == "moving" else 18_000_000_000
    phase = dict(case=host.scenario, duration_ns=duration, interval_ns=interval,
                 declared_at_ns=now, started_at_ns=time.perf_counter_ns())
    if host.scenario == "obstacle":
        phase["barrier"] = dict(x=host.seed-21001, z_min=3, z_max=5)  # EVALUATOR ONLY, never passed to the skill.
    write_json_atomic(host.run_dir/"phase.json", phase)
    write_json_atomic(host.run_dir/"follow-request.json", trace_projection(req))
    rows = []
    def sample(slot, step=None):
        obs = host.follower.observation
        legal = project_follow_view(obs, time.perf_counter_ns(), obs.controller_clock_id)
        target = next((e for e in legal.entities if e.track_id == req.target_track_id), None)
        own = obs.position.value
        leader = host.leader.observation.position.value  # EVALUATOR ONLY, below completed policy decision.
        row = dict(slot=slot, observation_sequence=obs.sequence_id, target_visible=target is not None,
                   distance=None if target is None else math.hypot(target.position.x-own.x, target.position.z-own.z),
                   follower=[own.x, own.y, own.z], leader=[leader.x, leader.y, leader.z],
                   collision=obs.self_state.value.horizontal_collision,
                   state="initial" if step is None else step.report.state,
                   reason=None if step is None else step.report.reason,
                   elapsed_ns=time.perf_counter_ns()-phase["started_at_ns"])
        rows.append(row)
        append_jsonl(host.run_dir/"samples.jsonl", row)
    sample(0)
    next_slot = 1
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="follow-leader")
    try:
        while next_slot*interval < duration:
            deadline = phase["started_at_ns"]+next_slot*interval
            remaining = deadline-time.perf_counter_ns()
            if remaining > 0:
                time.sleep(min(remaining/1e9, .05))
                continue
            if time.perf_counter_ns() >= deadline+interval:
                append_jsonl(host.run_dir/"missed-slots.jsonl", dict(slot=next_slot, reason="scheduler_late"))
                next_slot += 1
                continue
            step = paired_update(host.leader, driver, task, profile,
                                 min(host.deadline_ns, time.perf_counter_ns()+1_000_000_000), executor,
                                 controls=scripted_leader_controls(host.scenario, next_slot), slot=next_slot)
            if step is None:
                append_jsonl(host.run_dir/"missed-slots.jsonl", dict(slot=next_slot, reason="cadence_guard"))
                next_slot += 1
                continue
            append_jsonl(host.run_dir/"follow-decisions.jsonl", dict(decision=trace_projection(step.decision),
                report=trace_projection(step.report), movement_selected=step.movement_selected, look_selected=step.look_selected,
                actual_action=trace_projection(step.runtime_result.decision.action) if step.runtime_result.decision else None,
                result_observation_sequence=None if step.runtime_result.observation is None else step.runtime_result.observation.sequence_id))
            if time.perf_counter_ns() < deadline+interval:
                sample(next_slot, step)
            else:
                append_jsonl(host.run_dir/"missed-slots.jsonl", dict(slot=next_slot, reason="observation_late"))
            if next_slot % 10 == 0:
                print(f"FOLLOW_SAMPLE={next_slot}:{step.report.state}:{step.report.reason}:distance={step.report.distance_blocks}", flush=True)
            next_slot += 1
            if step.report.terminal:
                raise RuntimeError("follow terminated before static phase completed: "+str(step.report))
    finally:
        executor.shutdown(wait=True, cancel_futures=True)  # Each I/O is bounded; outer parent owns a hard deadline.
        if host.follower.state is RuntimeStateV1.READY:
            released = driver.stop(task, profile, time.perf_counter_ns()+1_000_000_000, "scenario_end")
            write_json_atomic(host.run_dir/"follow-release.json", trace_projection(released))
            for _ in range(4):
                _neutral(host.follower, task, profile, time.perf_counter_ns()+1_000_000_000)
                time.sleep(.05)
    return rows, phase
