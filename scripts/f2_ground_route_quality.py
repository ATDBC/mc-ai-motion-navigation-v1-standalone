"""Read-only F2 motion quality accounting and fixed continuous-input references."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
import math
import time
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1, LookV1, ActionIntentV1
from mc2p.contracts.action import ActionPriorityV0
from mc2p.motion_nav.goal_observation import evaluate_observed_goal
from mc2p.motion_nav.fixed_route import FixedRouteConfig
from scripts.control_probe_core import append_jsonl


REFERENCE_POINTS = {
    "offset_mid": (.73,100.,8.23),
    "player_wall_head": (1.6,100.,6.4),
    "player_ledge_2": (.5,100.,10.78),
}
STOP_SPEED = FixedRouteConfig().stopped_speed_blocks_per_second


@contextmanager
def ground_start_evidence():
    """Record the actual production prefix calculation and returned window."""
    from mc2p.motion_nav import action_route_executor as executor
    original_calculate=executor.verified_ground_route_candidate
    original_window=executor._ordinary_walk_start_window
    evidence={"prefixes":[],"windows":[]}
    def calculate(frame,state,command,**kwargs):
        result=original_calculate(frame,state,command,**kwargs)
        evidence["prefixes"].append({"observation_sequence":frame.body.sequence_id,
            "input_state":asdict(state),"command":asdict(command),
            "control_ticks":kwargs["control_ticks"],"tail_ticks":kwargs["tail_ticks"],
            "result":asdict(result)})
        return result
    def window(action,decision,frame,**kwargs):
        result=original_window(action,decision,frame,**kwargs)
        if result is not None:
            evidence["windows"].append({"observation_sequence":frame.body.sequence_id,
                "earliest_tick":result[0],"latest_tick":result[1],
                "first_command":kwargs["first_command"],
                "candidate_lease_ticks":decision.input_lease_ticks,
                "candidate_movement":asdict(decision.movement)})
        return result
    with patch.object(executor,"verified_ground_route_candidate",calculate),\
            patch.object(executor,"_ordinary_walk_start_window",window):
        yield evidence


def motion_quality(frames, start_position):
    """Use actual game ticks and observations; wall time never changes control."""
    first = next((min(f["actual_application_ticks"]) for f in frames
        if f["actual_application_ticks"] and f["movement"] is not None
        and f["movement"] != asdict(MovementV1())),None)
    satisfied = next((f["tick"] for f in frames
        if f.get("formal_goal_status") == "satisfied"),None)
    complete = next((f["tick"] for f in frames
        if f.get("formal_goal_status") == "satisfied"
        and f.get("movement") == asdict(MovementV1())
        and math.hypot(f["velocity"][0],f["velocity"][2]) <= STOP_SPEED),None)
    region_entry = next((f["tick"] for f in frames if f.get("in_original_goal_region")),None)
    samples = [{"tick":first-1,"position":start_position,
        "movement":asdict(MovementV1())}] if first is not None else []
    samples += frames
    stagnant = []
    for end in range(1,len(samples)):
        after = samples[end]
        before = next((f for f in reversed(samples[:end])
            if after["tick"]-f["tick"] >= 10),None)
        if before is None or after["tick"]-before["tick"] != 10:
            continue
        window = [f for f in samples if before["tick"] < f["tick"] <= after["tick"]]
        if (len(window) == 10 and all(f.get("movement") is not None
                and f["movement"] != asdict(MovementV1()) for f in window)):
            net = math.dist(before["position"],after["position"])
            if net < .1:
                stagnant.append({"first_tick":before["tick"],"last_tick":after["tick"],
                    "net_displacement_blocks":net})
    return {"first_actual_non_neutral_tick":first,"first_region_entry_tick":region_entry,
        "first_satisfied_observation_tick":satisfied,"first_stopped_goal_tick":complete,
        "stopped_speed_threshold_blocks_per_second":STOP_SPEED,
        "final_recorded_tick":None if not frames else frames[-1]["tick"],
        "completion_ticks":None if first is None or complete is None else complete-first+1,
        "stagnant_windows":stagnant}


def run_continuous_reference(runtime, row, goal, start, solids, task, profile,
        directory, deadline_ns, diagnostic, danger_contact, health):
    """A fixed hold-forward then release reference, through the normal Runtime.

    The original GoalState and scene stay fixed. Reference points and release
    distance are frozen before running; no navigation result picks the input.
    """
    point = tuple(row["reference_point"])
    dx,dz = point[0]-start[0],point[2]-start[2]
    length = math.hypot(dx,dz)
    ux,uz = dx/length,dz/length
    heading = math.degrees(math.atan2(-dx,dz))
    source = "f2-continuous-reference/"+row["id"]
    frames = []
    released = False
    initial_health = health(runtime)
    for index in range(180):
        before = runtime.navigation_observation_adapter.latest_frame
        distance = ((before.body.position[0]-start[0])*ux
            +(before.body.position[2]-start[2])*uz)
        released = released or distance >= length-.26
        movement = MovementV1() if released else MovementV1(forward=1)
        look = None
        if index == 0:
            delta = (heading-math.degrees(before.body.yaw_radians)+180)%360-180
            look = LookV1(delta,0.)
        now = time.perf_counter_ns()
        runtime.cancel_source(source)
        runtime.submit_intent(ActionIntentV1(
            row["id"]+f"/{index}",source,runtime.observation.episode_id,
            runtime.observation.sequence_id,ActionPriorityV0.TASK,
            now,min(deadline_ns,now+750_000_000),movement=movement,
            look=look,valid_for_ticks=1))
        result = runtime.step(task,profile,min(deadline_ns,now+500_000_000))
        diagnostic()
        if result.report.failure is not None or result.decision is None:
            raise RuntimeError(f"F2 reference Runtime failure: {result.report}")
        if result.decision.action.movement != movement:
            raise AssertionError("F2 reference input lost arbitration")
        frame = runtime.navigation_observation_adapter.latest_frame
        record = runtime.input_ledger.record(result.decision.action.request_sequence_id)
        formal = evaluate_observed_goal(frame,goal,goal.risk_policy_id)
        sample = {"index":index,"tick":frame.body.movement_tick_id,
            "position":frame.body.position,"velocity":frame.body.velocity_blocks_per_second,
            "pose":frame.body.pose,"sneaking":frame.body.is_sneaking,
            "on_ground":frame.body.is_on_ground,"danger_contact":danger_contact(frame.body.body_box,solids),
            "health_points":health(runtime),"movement":asdict(movement),
            "source":source,"actual_application_ticks":list(record.applied_ticks),
            "requested_first_tick":record.requested_first_tick,
            "latest_allowed_first_tick":record.latest_allowed_first_tick,
            "formal_goal_status":formal.status.value,
            "in_original_goal_region":all(
                getattr(goal.region,"min_"+axis) <= value <= getattr(goal.region,"max_"+axis)
                for axis,value in zip("xyz",frame.body.position))}
        frames.append(sample)
        append_jsonl(directory/"f2-reference-frames.jsonl",{"trial":row["id"],**sample})
        # Keep observing the released-input tail to exact rest. Timing uses
        # the common formal .1 b/s threshold, but later drift cannot fake a
        # success by briefly passing through the original goal.
        if (formal.status.value == "satisfied" and released
                and math.hypot(frame.body.velocity_blocks_per_second[0],
                    frame.body.velocity_blocks_per_second[2]) <= 1.e-9):
            break
    runtime.cancel_source(source)
    violations = []
    if not frames or frames[-1]["formal_goal_status"] != "satisfied":
        violations.append("reference_did_not_complete_original_goal")
    if any(not f["on_ground"] or f["danger_contact"] or f["sneaking"]
            or f["health_points"] < initial_health for f in frames):
        violations.append("reference_safety")
    quality = motion_quality(frames,start)
    if quality["stagnant_windows"]:
        violations.append("reference_stagnation")
    return {**row,"passed":not violations,"violations":violations,
        "reference_point":point,"release_distance_blocks":.26,
        "original_goal":asdict(goal),"final_body":asdict(frame.body),
        "formal_goal_status":frames[-1]["formal_goal_status"],
        "motion_quality":quality,"frames":len(frames),
        "drop_frames":sum(not f["on_ground"] for f in frames),
        "damage_points":max(0.,initial_health-health(runtime)),
        "danger_contact_frames":sum(f["danger_contact"] for f in frames)}
