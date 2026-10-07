"""Frozen F2 fixtures on the existing local Fabric Runtime navigation chain.

Server commands only prepare dedicated fixtures before the measured task, or
inject an explicitly recorded world change. The actor reads profile-4 sensors.
All measured movement goes through RuntimeNavigationDriver and Runtime.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.motion_nav.goal_observation import evaluate_observed_goal
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver, RuntimeNavigationDriverState
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.continuous_height_runtime import _self_health
from scripts.f2_ground_route_evidence import terminal_controller_evidence, timing_summary, digest
from scripts.r25_planning_information_runtime import _task
from scripts.r28_product_fabric_runtime import _rotate
from tests.sim.product_cases import player_layout
from tests.sim.runner import _goal
from scripts.f2_ground_route_quality import (
    REFERENCE_POINTS, motion_quality, run_continuous_reference, ground_start_evidence,
)

TERMINAL = (RuntimeNavigationDriverState.SUCCESS,
    RuntimeNavigationDriverState.FAILED, RuntimeNavigationDriverState.CANCELLED,
    RuntimeNavigationDriverState.STOPPED)
FAMILIES = ("offset_mid", "diagonal_mid", "tangent_mid", "open_noncenter",
    "player_wall_head", "player_wall_parallel", "player_corner",
    "player_corridor_end", "player_ledge_1", "player_ledge_2")
NEGATIVES = ("head_wall_mid", "hazard_wall", "undeclared_edge", "low_ceiling")
INTERRUPTIONS = ("revision", "cancel", "input_loss", "support_change", "external_force")


def frozen_plan(*, f2r=False):
    families = ('f2r_outer_corner', 'f2r_diagonal_pillar') if f2r else FAMILIES
    rows = [{"id": f"f2-{family}-{direction}-{condition}", "family": family,
        "direction": direction, "condition": condition, "expected": "success"}
        for family in families for direction in range(4)
        for condition in ("normal", "late_first")]
    if f2r:
        for row in rows:
            solids, start, target = fixture(row)
            row['scene_sha256'] = digest(sorted((list(p), m) for p, m in solids.items()))
            row['start_position'] = list(start)
            row['goal_bounds'] = list(_goal(target).region.as_tuple())
        return rows
    rows.extend({"id": "f2-"+family, "family": family, "direction": 0,
        "condition": "normal", "expected": "failed"} for family in NEGATIVES)
    rows.extend({"id": "f2-"+kind, "family": "offset_mid", "direction": 0,
        "condition": "normal", "injection": kind,
        "expected": ("cancelled" if kind == "cancel" else "failed"
            if kind == "support_change" else "success")}
        for kind in INTERRUPTIONS)
    for row in rows:
        if row.get("injection") == "input_loss":
            row["expected_terminals"] = ["success","failed","cancelled"]
        if row.get("injection") == "external_force":
            row["expected_terminals"] = ["success","failed","cancelled"]
            row["external_force_kind"] = "vanilla_no_ai_villager_collision"
            row["external_entity_count"] = 4
            row["external_entity_initial_motion"] = [-.08,0.,0.]
    rows.extend({"id":"f2-reference-"+family,"family":family,"direction":0,
        "condition":"normal","expected":"success","reference":True,
        "reference_point":list(point),"release_distance_blocks":.26}
        for family,point in REFERENCE_POINTS.items())
    rows.append({"id":"f2-window-outside","family":"offset_mid","direction":0,
        "condition":"late_two","expected":"failed",
        "expected_terminals":["success","failed","cancelled"]})
    for row in rows:
        solids,start,target = fixture(row)
        row["scene_sha256"] = digest(sorted((list(p),m) for p,m in solids.items()))
        row["start_position"] = list(start)
        row["goal_bounds"] = list(_goal(target).region.as_tuple())
    return rows


def fixture(row):
    """Freeze exact fixture geometry, start and original GoalState before run."""
    family = row["family"]
    if family.startswith('f2r_'):
        from tests.sim.f2r_cases import layout
        scene, start, target = layout(family.removeprefix('f2r_'))
        solids = dict(scene.solids)
    elif family.startswith("player_"):
        scene, start, target = player_layout(family)
        solids = dict(scene.solids)
    else:
        solids = {(x, 63, z): "minecraft:stone"
            for x in range(-4, 5) for z in range(-1, 11)}
        start, target = (.73, 64., .67), (.73, 64., 8.33)
        if family in {"diagonal_mid", "open_noncenter"}:
            target = (1.37, 64., 8.23)
        elif family == "tangent_mid":
            # Java's ordinary player width is .6f. This center touches x=2
            # without preparing the body inside stone. Actor dimensions still
            # come solely from the real observation, never from this fixture.
            touching_x = 1.699999988079071
            start, target = (touching_x, 64., .67), (touching_x, 64., 8.23)
            solids.update({(2, y, z): "minecraft:stone"
                for y in (64, 65, 66) for z in range(-1, 11)})
        elif family in {"head_wall_mid", "hazard_wall"}:
            material = "minecraft:magma_block" if family == "hazard_wall" else "minecraft:stone"
            # No bypass is available inside the finite supported fixture.
            solids.update({(x, y, 4): material for x in range(-4, 5)
                for y in (64, 65, 66)})
        elif family == "undeclared_edge":
            target = (.73, 64., 11.45)
        elif family == "low_ceiling":
            solids.update({(x, 65, z): "minecraft:stone"
                for x in range(-4, 5) for z in range(4, 11)})
    direction = row["direction"]
    if row.get("injection") == "external_force":
        start,target = (.55,64.,.67),(.55,64.,8.33)
    rotated = {}
    for (x, y, z), material in solids.items():
        px, pz = _rotate(x+.5, z+.5, direction)
        rotated[(math.floor(px), y+36, math.floor(pz))] = material
    def point(p):
        x, z = _rotate(p[0], p[2], direction)
        return (x, p[1]+36, z)
    return rotated, point(start), point(target)


def _step(runtime, task, profile, deadline, request=None):
    result = runtime.step(task, profile, min(deadline, time.perf_counter_ns()+500_000_000),
        observation_request=request)
    if result.report.failure is not None:
        raise RuntimeError(str(result.report.failure))
    return result


def _prepare_fixture(runtime, row, writer, task, profile, deadline, diagnostic):
    solids, start, target = fixture(row)
    assert row["scene_sha256"] == digest(sorted((list(p),m) for p,m in solids.items()))
    assert row["start_position"] == list(start)
    assert row["goal_bounds"] == list(_goal(target).region.as_tuple())
    writer(("kill @e[tag=mc2p_f2_push]",
        "fill -20 96 -20 20 106 20 minecraft:air replace"))
    # Acquire the support layer before walls cover it. This is real historical
    # surface-depth memory, not a truth-map seed or an unknown-as-air shortcut.
    floors = {p:m for p,m in solids.items() if p[1] < start[1]}
    covers = {p:m for p,m in solids.items() if p[1] >= start[1]}
    commands = tuple(f"setblock {x} {y} {z} {material}" for (x,y,z),material in sorted(floors.items()))
    for offset in range(0, len(commands), 100):
        writer(commands[offset:offset+100])
    # Request facts, rather than install TEST_ORACLE knowledge. All four views
    # are outside the measured actor episode and use the real surface sensor.
    cells = []
    for x in range(-5,6):
        for z in range(-2,13):
            px,pz = _rotate(x+.5,z+.5,row["direction"])
            cells.extend((math.floor(px),y,math.floor(pz)) for y in range(98,104))
    cells = tuple(cells)
    # Keep the preparation camera outside the wider F2-R building.
    right_back_z = 10.5 if row["family"] == "f2r_outer_corner" else 9.5
    exterior = tuple((x,z,yaw,pitch) for x,z,yaw in
        ((-2.5,.5,-30),(-2.5,9.5,-150),(3.5,.5,30),(3.5,right_back_z,150))
        for pitch in (0,45))
    def survey(views, covered):
        for x,z,yaw,pitch in views:
            if covered and row["family"] == "low_ceiling" and z > 4:
                z = 3.5
            px,pz = _rotate(x,z,row["direction"])
            writer((f"tp MC2PProbe {px} 100 {pz} {yaw-90*row['direction']} {pitch}",))
            for offset in range(0,len(cells),128):
                _step(runtime,task,profile,deadline,
                    ObservationRequestV3("navigation_v1",cells[offset:offset+128]))
                diagnostic()
    survey(exterior,False)
    commands = tuple(f"setblock {x} {y} {z} {material}" for (x,y,z),material in sorted(covers.items()))
    for offset in range(0,len(commands),100):
        writer(commands[offset:offset+100])
    views = (*exterior,(.5,.5,0,0),(.5,9.5,180,0))
    if row["family"] in {"player_ledge_1","player_ledge_2","undeclared_edge"}:
        # The lower air just beyond the platform cannot be seen through its
        # top surface. Observe it from the last supported cell before start.
        views += ((.5,10.5,0,75),)
    survey(views,True)
    writer((f"tp MC2PProbe {start[0]} {start[1]} {start[2]} {-90*row['direction']} 35",))
    for _ in range(4):
        _step(runtime,task,profile,deadline)
        diagnostic()
    return solids,start,target


def _danger_contact(box, solids):
    """Read-only fixture oracle for scoring; never feeds actor control."""
    for (x,y,z),material in solids.items():
        if material not in {"minecraft:magma_block", "minecraft:lava", "minecraft:water"}:
            continue
        if all(getattr(box,"min_"+axis) <= lower+1+.001
               and getattr(box,"max_"+axis) >= lower-.001
               for axis,lower in zip("xyz",(x,y,z))):
            return True
    return False


def run_f2_ground_route_runtime(runtime, backend, episode, directory, deadline_ns,
        fixture_writer, *, trace_owns_diagnostics=False):
    from scripts.navigation_coordination_metrics import source_fingerprint
    production_patterns = ["mc2p/**/*.py","config/motion-navigation/*.json"]
    harness_patterns = ["scripts/f2_ground_route_runtime.py",
        "scripts/f2_ground_route_quality.py",
        "scripts/f2_ground_route_evidence.py","scripts/probe_fabric_deployment_observation.py",
        "scripts/r28_product_fabric_runtime.py","tests/sim/product_cases.py","tests/sim/runner.py",
        "tests/sim/f2r_cases.py"]
    source_before = {"production":source_fingerprint(production_patterns),
        "harness":source_fingerprint(harness_patterns)}
    selected = json.loads(os.environ["MC2P_F2_SELECTED_PLAN"])
    plan_hash = digest(selected)
    write_json_atomic(directory/"f2-plan.json", {"schema_version":"mc2p.f2-fabric-plan.v1",
        "cases":selected, "sha256":plan_hash, "actor_information":"profile_4_surface_depth"})
    profile,task = BehaviorProfileV0(),_task(deadline_ns)
    profiles = NavigationSessionProfiles.load(ROOT/"config/motion-navigation")
    trials, diagnostics = [], []
    def diagnostic():
        row = {"episode_id":episode,"observation_sequence_id":runtime.observation.sequence_id,
            "diagnostics":backend.last_diagnostics}
        diagnostics.append(row)
        if not trace_owns_diagnostics:
            append_jsonl(directory/"diagnostics.jsonl",row)
        # The launcher owns persistence; it requires these paired rows for its
        # existing independent zero-image and per-observation association gate.
    diagnostic()
    for row in selected:
        solids,start,target = _prepare_fixture(runtime,row,fixture_writer,task,profile,deadline_ns,diagnostic)
        original_goal = _goal(target)
        current_goal = original_goal
        if row.get("reference"):
            assert row["reference_point"] == list(REFERENCE_POINTS[row["family"]])
            result_row = run_continuous_reference(runtime,row,original_goal,start,solids,
                task,profile,directory,deadline_ns,diagnostic,_danger_contact,_self_health)
            from mc2p.runtime.trace import trace_projection
            result_row = trace_projection(result_row)
            trials.append(result_row)
            append_jsonl(directory/"f2-trials.jsonl",result_row)
            print(f"F2_FABRIC {row['id']} reference passed={result_row['passed']}",flush=True)
            if not result_row["passed"]:
                write_json_atomic(directory/"f2-failed-trial.json",result_row)
                raise AssertionError(f"F2 reference fail fast: {row['id']}: {result_row['violations']}")
            continue
        session = NavigationSession(row["id"],profiles,
            observation_adapter=runtime.navigation_observation_adapter)
        driver = RuntimeNavigationDriver(runtime,session)
        driver.start(row["id"],1,original_goal,time.perf_counter_ns())
        frames,late = [],None
        routes = {}
        injected = False
        interruption_evidence = None
        initial_health = _self_health(runtime)
        with terminal_controller_evidence() as actual,ground_start_evidence() as starts:
            try:
                for index in range(360):
                    if driver.state in TERMINAL:
                        break
                    frame = runtime.navigation_observation_adapter.latest_frame
                    active = session._active_route
                    if active is not None:
                        for action in active.action_route.actions:
                            route = getattr(action,"fixed_route",None)
                            if route is not None:
                                routes[route.route_id] = [asdict(point) for point in route.points]
                    speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                        frame.body.velocity_blocks_per_second[2])
                    injection = row.get("injection")
                    trigger = speed > 1. and (injection != "external_force"
                        or 2.15 <= frame.body.position[2] <= 2.6)
                    if injection and not injected and trigger:
                        injected = True
                        interruption_evidence = {"kind":injection,"frame":index,
                            "movement_tick":frame.body.movement_tick_id,
                            "position":list(frame.body.position)}
                        if injection == "revision":
                            # New target is a valid ordinary point, expressed
                            # through the existing public revision interface.
                            tx,tz = _rotate(1.37,7.23,row["direction"])
                            current_goal = _goal((tx,100.,tz))
                            driver.replace_goal(row["id"],2,current_goal,time.perf_counter_ns())
                        elif injection == "cancel":
                            cancelled = driver.stop(profile,"f2_cancel")
                            diagnostic()
                            sequence = (None if cancelled.decision is None else
                                cancelled.decision.action.request_sequence_id)
                            receipt = None if sequence is None else runtime.input_ledger.record(sequence)
                            interruption_evidence["cancel_application_ticks"] = (
                                [] if receipt is None else list(receipt.applied_ticks))
                            interruption_evidence["cancel_request_sequence"] = sequence
                            continue
                        elif injection == "input_loss":
                            interruption_evidence["requested_silence_seconds"] = .60
                            time.sleep(.60)
                        elif injection == "support_change":
                            px,pz = _rotate(.5,8.5,row["direction"])
                            removed = (math.floor(px),99,math.floor(pz))
                            fixture_writer((f"setblock {removed[0]} 99 {removed[2]} minecraft:air",))
                            interruption_evidence["removed_support"] = list(removed)
                            solids.pop(removed)
                        elif injection == "external_force":
                            # Owned evaluator-only vanilla entity motion. No
                            # command writes the player's position or velocity.
                            x,y,z = frame.body.position
                            push = (f'summon minecraft:villager {x+.35} {y} {z+.10} '
                                '{NoAI:1b,PersistenceRequired:1b,Silent:1b,'
                                'Tags:["mc2p_f2_push"],Motion:[-0.08d,0.0d,0.0d]}')
                            commands = (push,)*row["external_entity_count"]
                            fixture_writer(commands)
                            interruption_evidence["commands"] = list(commands)
                            interruption_evidence["external_force_kind"] = row["external_force_kind"]
                            interruption_evidence["pre_push_body"] = asdict(frame.body)
                            interruption_evidence["pre_push_source"] = asdict(driver.source)
                            interruption_evidence["reentry"] = []
                            interruption_evidence["post_push_bodies"] = []
                            interruption_evidence["residuals"] = []
                    if injection == "external_force" and injected:
                        elapsed = index-interruption_evidence["frame"]
                        if elapsed == 8:
                            fixture_writer(("kill @e[tag=mc2p_f2_push]",))
                            interruption_evidence["external_entities_removed_frame"] = index
                        if elapsed > 0:
                            reentry = session.external_motion_reentry(runtime.observation)
                            interruption_evidence["reentry"].append(asdict(reentry))
                            interruption_evidence["post_push_bodies"].append(asdict(frame.body))
                    deadline = min(deadline_ns,time.perf_counter_ns()+500_000_000)
                    began = time.perf_counter_ns()
                    proposals = driver.prepare_proposals(deadline)
                    prepare_ns = time.perf_counter_ns()-began
                    nonneutral = any(e.intent.movement is not None and e.intent.movement != MovementV1()
                        for p in proposals for e in p.intents)
                    before = frame.body.movement_tick_id
                    delay = row["condition"] in {"late_first","late_two"} and late is None and nonneutral
                    if delay:
                        time.sleep(.055 if row["condition"] == "late_first" else .110)
                    result = runtime.control_frame(task,profile,deadline,proposals=proposals)
                    driver.adopt_result(result)
                    diagnostic()
                    frame = runtime.navigation_observation_adapter.latest_frame
                    if injection == "external_force" and injected:
                        interruption_evidence["post_push_bodies"].append(asdict(frame.body))
                        residual = session.motion_residual(runtime.observation,runtime.input_ledger)
                        if residual is not None:
                            interruption_evidence["residuals"].append(asdict(residual))
                    fd = driver.last_frame_diagnostics
                    sequence = None if fd is None else fd.action_request_sequence
                    record = None if sequence is None else runtime.input_ledger.record(sequence)
                    applications = [] if record is None else list(record.applied_ticks)
                    if delay:
                        late = {"before_tick":before,"requested_first_tick":None if record is None else record.requested_first_tick,
                            "requested_last_tick":None if record is None else record.requested_last_tick,
                            "latest_allowed_first_tick":None if record is None else record.latest_allowed_first_tick,
                            "valid_for_ticks":None if record is None else record.action.valid_for_ticks,
                            "actual_movement":None if record is None else asdict(record.action.movement),
                            "actual_ticks":applications,"actual_offset":None if not applications else min(applications)-before,
                            "status":None if record is None else record.status.value}
                    sample = {"index":index,"tick":frame.body.movement_tick_id,"position":frame.body.position,
                        "velocity":frame.body.velocity_blocks_per_second,"pose":frame.body.pose,
                        "on_ground":frame.body.is_on_ground,"sneaking":frame.body.is_sneaking,
                        "danger_contact":_danger_contact(frame.body.body_box,solids),
                        "health_points":_self_health(runtime),"state":driver.state.value,"reason":driver.reason,
                        "formal_goal_status":evaluate_observed_goal(frame,current_goal,
                            current_goal.risk_policy_id).status.value,
                        "in_original_goal_region":all(
                            getattr(current_goal.region,"min_"+axis) <= value <= getattr(current_goal.region,"max_"+axis)
                            for axis,value in zip("xyz",frame.body.position)),
                        "prepare_ns":prepare_ns,"actual_application_ticks":applications,
                        "input_status":None if record is None else record.status.value,
                        "requested_first_tick":None if record is None else record.requested_first_tick,
                        "latest_allowed_first_tick":None if record is None else record.latest_allowed_first_tick,
                        "request_sequence":sequence,"source":None if driver.source is None else asdict(driver.source),
                        "movement":None if result.decision is None else asdict(result.decision.action.movement),
                        "selected_intents":None if result.decision is None else result.decision.selected_intents,
                        "activity":None if fd is None or fd.movement_activity is None else asdict(fd.movement_activity)}
                    frames.append(sample)
                    append_jsonl(directory/"f2-frames.jsonl",{"trial":row["id"],**sample})
                    if result.report.failure is not None:
                        raise RuntimeError(str(result.report.failure))
                frame = runtime.navigation_observation_adapter.latest_frame
                formal_goal = evaluate_observed_goal(frame,current_goal,current_goal.risk_policy_id)
                violations = []
                expected = tuple(RuntimeNavigationDriverState(value)
                    for value in row.get("expected_terminals",(row["expected"],)))
                if driver.state not in expected:
                    violations.append("unexpected_terminal")
                if driver.state is RuntimeNavigationDriverState.SUCCESS and formal_goal.status.value != "satisfied":
                    violations.append("original_goal_not_satisfied")
                if not frame.body.is_on_ground or frame.body.pose != "standing" or frame.body.is_sneaking:
                    violations.append("unstable_terminal_body")
                if any(not f["on_ground"] or f["position"][1] < start[1]-.01 for f in frames):
                    violations.append("drop")
                if initial_health-_self_health(runtime) > 1e-6:
                    violations.append("damage")
                if any(f["danger_contact"] for f in frames):
                    violations.append("danger_contact")
                if row["family"] == "head_wall_mid" and any(f["position"][2] > 3.7+1e-6 for f in frames):
                    violations.append("head_wall_crossed")
                if row["family"] == "undeclared_edge" and any(f["sneaking"]
                        or (f["movement"] is not None and f["movement"]["sneak"]) for f in frames):
                    violations.append("undeclared_sneak")
                if row["condition"] == "late_first" and (late is None
                        or late["requested_first_tick"] != late["before_tick"]+1
                        or late["actual_offset"] != 2 or late["status"] != "applied"
                        or late["actual_movement"] == asdict(MovementV1())
                        or late["latest_allowed_first_tick"] != late["requested_first_tick"]+1):
                    violations.append("late_first_not_confirmed")
                if row["condition"] == "late_two" and (late is None
                        or late["actual_offset"] != 3 or late["status"] != "applied_outside_window"
                        or late["latest_allowed_first_tick"] != late["requested_first_tick"]+1):
                    violations.append("outside_window_not_confirmed")
                if row.get("injection") and not injected:
                    violations.append("injection_not_applied")
                if row.get("injection") == "external_force" and injected:
                    pushed = max((abs(body["position"][0]-interruption_evidence["pre_push_body"]["position"][0])
                        for body in interruption_evidence["post_push_bodies"]),default=0.)
                    interruption_evidence["maximum_lateral_displacement_blocks"] = pushed
                    if pushed < .3:
                        violations.append("external_push_not_confirmed")
                    fixture_writer(("kill @e[tag=mc2p_f2_push]",))
                quality = motion_quality(frames,start)
                quality["driver_terminal_tick"] = (frame.body.movement_tick_id
                    if driver.state in TERMINAL else None)
                if quality["stagnant_windows"]:
                    violations.append("non_neutral_stagnation")
                control = actual["frames"]
                maximum = max((f["full_candidates"] for f in control),default=0)
                if maximum > 3:
                    violations.append("candidate_limit")
                result_row = {**row,"episode_id":episode,"passed":not violations,"violations":violations,
                    "task_success":driver.state is RuntimeNavigationDriverState.SUCCESS,
                    "state":driver.state.value,"reason":driver.reason,"original_goal":asdict(original_goal),
                    "original_goal_bounds":original_goal.region.as_tuple(),"current_goal_bounds":current_goal.region.as_tuple(),
                    "completion_contracts":actual["contracts"],"actual_routes":routes,
                    "formal_goal_checks":actual["formal_goal_checks"],
                    "formal_goal_status":formal_goal.status.value,"final_body":asdict(frame.body),
                    "late_input":late,"injection_applied":injected,
                    "start_prefix_proofs":starts,
                    "motion_quality":quality,
                    "interruption_evidence":interruption_evidence,"frames":len(frames),
                    "trajectory_sha256":digest([{k:v for k,v in f.items() if k not in {"prepare_ns","source","activity"}}
                        for f in frames]),"max_full_candidates":maximum,
                    "physics_steps":sum(f["physics_steps"] for f in control),
                    "control_ms":timing_summary([f["control_ms"] for f in control]),
                    "control_samples_ms":[f["control_ms"] for f in control],
                    "prepare_ms":timing_summary([f["prepare_ns"]/1e6 for f in frames]),
                    "drop_frames":sum(not f["on_ground"] for f in frames),
                    "damage_points":max(0.,initial_health-_self_health(runtime)),
                    "danger_contact_frames":sum(f["danger_contact"] for f in frames)}
                result_row["input_deadline_miss_count"] = sum(
                    bool(f["actual_application_ticks"]) and f["latest_allowed_first_tick"] is not None
                    and min(f["actual_application_ticks"]) > f["latest_allowed_first_tick"]
                    for f in frames)
                if (row["condition"] != "late_two" and not row.get("injection")
                        and result_row["input_deadline_miss_count"]):
                    violations.append("input_deadline_miss")
                    result_row["passed"] = False
                # Serialize typed contract enums using the established trace
                # projection; their values are evidence, never control keys.
                from mc2p.runtime.trace import trace_projection
                result_row = trace_projection(result_row)
                trials.append(result_row)
                append_jsonl(directory/"f2-trials.jsonl",result_row)
                print(f"F2_FABRIC {row['id']} {driver.state.value} passed={not violations}",flush=True)
                if violations:
                    write_json_atomic(directory/"f2-failed-trial.json",result_row)
                    raise AssertionError(f"F2 fail fast: {row['id']}: {violations}")
            finally:
                if driver.source is not None:
                    if driver.state in TERMINAL:
                        driver.release("f2_trial_end")
                    else:
                        driver.stop(profile,"f2_trial_cleanup")
                        diagnostic()
                session.close()
    source_after = {"production":source_fingerprint(production_patterns),
        "harness":source_fingerprint(harness_patterns)}
    source_unchanged = source_before == source_after
    summary = {"schema_version":"mc2p.f2-ground-route-fabric.v1","cases":len(trials),
        "passed":all(r["passed"] for r in trials) and source_unchanged,
        "trials":trials,"plan_sha256":plan_hash,"sources_unchanged":source_unchanged,
        "actor_information":"profile_4_surface_depth","fixture_commands_are_task_inputs":False,
        "production":source_after["production"],"harness":source_after["harness"],
        "source_before":source_before}
    write_json_atomic(directory/"f2-summary.json",summary)
    return summary,diagnostics,[{"name":r["id"],"passed":r["passed"]} for r in trials]+[
        {"name":"f2_sources_unchanged","passed":source_unchanged}]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root",type=Path,default=ROOT/"artifacts/f2-ground-route")
    parser.add_argument("--smoke",action="store_true")
    parser.add_argument("--ids",nargs="+")
    parser.add_argument("--plan-only",action="store_true")
    parser.add_argument('--f2r', action='store_true', help='Frozen 16-case F2-R outer-corner/pillar matrix')
    args,launcher = parser.parse_known_args(argv)
    selected = frozen_plan(f2r=args.f2r)
    if args.smoke:
        selected = [r for r in selected if r["direction"] == 0 and (
            r["condition"] == "normal" or r["id"] == "f2-offset_mid-0-late_first"
            or r["condition"] == "late_two")]
    if args.ids:
        selected = [r for r in selected if r["id"] in args.ids]
        if {r["id"] for r in selected} != set(args.ids):
            parser.error("unknown frozen IDs")
    if args.plan_only:
        print(json.dumps(selected,indent=2))
        return 0
    args.output_root.mkdir(parents=True,exist_ok=True)
    from scripts.bounded_process import make_run_id
    stamp = make_run_id()
    path = args.output_root/(stamp+"-plan.json")
    write_json_atomic(path,{"cases":selected,"sha256":digest(selected)})
    os.environ["MC2P_F2_GROUND_ROUTE_PROBE"] = "1"
    os.environ["MC2P_F2_SELECTED_PLAN"] = json.dumps(selected)
    from scripts.probe_fabric_deployment_observation import main as launch
    cached_launch = ROOT/"deployment/fabric-observation-probe/build/launch/client-launch.json"
    if (cached_launch.is_file() and not any(value.split("=",1)[0] == "--launch-json"
            for value in launcher)):
        # The launcher still validates every cached classpath and asset hash.
        # Reuse the deployed build instead of rebuilding unchanged Java/native
        # components through a shell that has not activated the Conda JDK.
        launcher = ["--launch-json",str(cached_launch),*launcher]
    deployment = ROOT/"artifacts/fabric-deployment"
    batches = []
    for offset in range(0,len(selected),8):
        batch = selected[offset:offset+8]
        os.environ["MC2P_F2_SELECTED_PLAN"] = json.dumps(batch)
        previous = set(deployment.iterdir()) if deployment.exists() else set()
        code = launch(["--r25-planning-information-probe","--time-diagnostics",*launcher])
        created = sorted(set(deployment.iterdir())-previous)
        batches.append({"ids":[r["id"] for r in batch],"return_code":code,
            "raw_directories":[str(p.absolute()) for p in created]})
        write_json_atomic(path.with_name(path.stem.replace("-plan","-runs")+".json"),
            {"batches":batches,"plan_sha256":digest(selected)})
        if code:
            return code
    # This compares three actual actors with their separately recorded hold-
    # forward references. It never changes a controller, fixture or label.
    trials = {}
    for batch in batches:
        for raw in batch["raw_directories"]:
            for trial_file in Path(raw).glob("client-*/f2-trials.jsonl"):
                for line in trial_file.read_text("utf-8").splitlines():
                    trial = json.loads(line)
                    trials[trial["id"]] = trial
    pairs = []
    for family in REFERENCE_POINTS:
        actor = trials.get("f2-"+family+"-0-normal")
        reference = trials.get("f2-reference-"+family)
        if actor is None or reference is None:
            continue
        same_inputs = all(actor[k] == reference[k]
            for k in ("scene_sha256","start_position","goal_bounds","original_goal"))
        actor_ticks = actor["motion_quality"]["completion_ticks"]
        reference_ticks = reference["motion_quality"]["completion_ticks"]
        ratio = (None if actor_ticks is None or not reference_ticks
            else actor_ticks/reference_ticks)
        pairs.append({"family":family,"same_scene_start_and_original_goal":same_inputs,
            "actor":actor["id"],"reference":reference["id"],
            "actor_timing":actor["motion_quality"],"reference_timing":reference["motion_quality"],
            "actor_reference_tick_ratio":ratio,
            "passed":same_inputs and ratio is not None and ratio <= 1.3})
    quality = {"pairs":pairs,"complete":len(pairs)==3,
        "passed":all(pair["passed"] for pair in pairs),
        "timing":"first actual non-neutral input through first neutral stopped original-GoalState observation",
        "stopped_speed_blocks_per_second":.1,"raw_plan":str(path.absolute())}
    write_json_atomic(path.with_name(path.stem.replace("-plan","-quality")+".json"),quality)
    if not quality["passed"]:
        print("F2_QUALITY_FAILED",flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
