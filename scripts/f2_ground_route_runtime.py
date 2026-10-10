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
from mc2p.motion_nav.action_route import WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteState
from mc2p.motion_nav.body_control import StopCause
from mc2p.motion_nav.segment_entry import SegmentEntryWindow
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


def _original_goal(row, target):
    if row.get("target_kind"):
        from tests.sim.f2r_cases import goal_for
        return goal_for(target, row["target_kind"])
    return _goal(target)


def _entry_late_walk_gate(session, frame, proposals, *, route_decision=None):
    """Evaluator-only trigger; no reason label participates in the choice."""
    active = session._active_route
    index = session.diagnostics.action_index
    if (active is None or index is None
            or index + 1 >= len(active.action_route.actions)
            or type(active.action_route.actions[index]) is not WalkSegment
            or not frame.body.is_on_ground
            or not any(e.intent.movement is not None
                       and e.intent.movement != MovementV1()
                       and not e.intent.movement.jump
                       for p in proposals for e in p.intents)
            or (route_decision is not None
                and (route_decision.expected_movement_tick is not None
                     or route_decision.verified_command_index is not None))):
        return None
    window = getattr(active.action_route.actions[index + 1], "entry_window", None)
    if type(window) is not SegmentEntryWindow:
        return None
    speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                       frame.body.velocity_blocks_per_second[2])
    dx = frame.body.position[0] - window.reference_point[0]
    dz = frame.body.position[2] - window.reference_point[2]
    ux, uz = window.horizontal_approach_direction
    longitudinal = dx * ux + dz * uz
    lateral = abs(dx * uz - dz * ux)
    # Observe the approach before braking completes.  Projection onto the
    # declared entry direction makes the trigger identical after rotation.
    distance_before_entry = max(0., window.minimum_longitudinal_offset_blocks - longitudinal)
    if (speed <= window.maximum_speed_blocks_per_second
            or distance_before_entry > .8
            or longitudinal > window.maximum_longitudinal_offset_blocks
            or lateral > max(.30, window.maximum_lateral_offset_blocks)
            or not window.minimum_feet_y <= frame.body.position[1] <= window.maximum_feet_y):
        return None
    return {"movement_tick": frame.body.movement_tick_id,
            "speed_blocks_per_second": speed,
            "maximum_entry_speed_blocks_per_second": window.maximum_speed_blocks_per_second,
            "reference_point": window.reference_point,
            "position": frame.body.position, "on_ground": frame.body.is_on_ground,
            "profile_id": window.profile_id,
            "distance_before_entry_blocks": distance_before_entry,
            "estimated_ticks_before_entry": math.ceil(distance_before_entry / (speed / 20.))}


def _selected_input_has_declared_timing(prepared, decision):
    """Timing belongs to the winning movement, not every submitted proposal."""
    if (prepared is None or prepared.route_decision is None
            or prepared.control_frame is None or decision is None):
        return False
    route = prepared.route_decision
    movement_ids = {envelope.intent.intent_id
                    for envelope in prepared.control_frame.intents
                    if envelope.intent.movement is not None}
    return (any(group == "movement" and intent in movement_ids
                for group, intent in decision.selected_intents)
            and (route.expected_movement_tick is not None
                 or route.verified_command_index is not None))


def _landing_late_air_gate(session, frame, route_decision, *, command_index=3):
    """Inject only a declared command of the final action after departure."""
    active = session._active_route
    if (active is None or route_decision is None or frame.body.is_on_ground
            or route_decision.action_index != len(active.action_route.actions) - 1
            or route_decision.expected_movement_tick is None
            or route_decision.verified_command_index != command_index
            or not route_decision.submit_input):
        return None
    return {"movement_tick": frame.body.movement_tick_id,
            "position": frame.body.position, "on_ground": False,
            "action_index": route_decision.action_index,
            "verified_command_index": route_decision.verified_command_index,
            "expected_movement_tick": route_decision.expected_movement_tick,
            "latest_movement_tick": route_decision.latest_movement_tick}


def _should_inject_late_input(condition, late_record, delayed_candidate):
    """The same once-per-trial guard is used by the fixture and its tests."""
    return (condition in {"late_first", "late_two", "late_entry", "late_air"}
            and late_record is None and delayed_candidate)


def _input_timing_evidence(frames, *, task_success, expected_late_sequence=None):
    """Keep ledger facts unchanged while distinguishing measured obligations."""
    explicit_misses = []
    unwindowed_delays = []
    for frame in frames:
        ticks = frame["actual_application_ticks"]
        requested = frame["requested_first_tick"]
        latest = frame["latest_allowed_first_tick"]
        if not ticks or requested is None:
            continue
        if frame["explicit_input_timing"]:
            if latest is not None and min(ticks) > latest:
                explicit_misses.append(frame["request_sequence"])
        elif min(ticks) > requested:
            unwindowed_delays.append({
                "request_sequence": frame["request_sequence"],
                "requested_first_tick": requested,
                "actual_application_ticks": ticks,
                "delay_ticks": min(ticks) - requested,
                "input_status": frame["input_status"],
                "observation_tick": frame["tick"],
                "position": frame["position"], "on_ground": frame["on_ground"],
                "formal_goal_status": frame["formal_goal_status"],
                "task_success": task_success,
            })
    return {
        "input_deadline_miss_count": len(explicit_misses),
        "expected_injected_explicit_miss_count": sum(
            sequence == expected_late_sequence for sequence in explicit_misses),
        "unexpected_explicit_deadline_miss_count": sum(
            sequence != expected_late_sequence for sequence in explicit_misses),
        "explicit_deadline_miss_sequences": explicit_misses,
        "raw_applied_outside_window_count": sum(
            frame["input_status"] == "applied_outside_window" for frame in frames),
        "unwindowed_input_delay_count": len(unwindowed_delays),
        "unwindowed_input_delays": unwindowed_delays,
        "violations": (["input_deadline_miss"] if any(
            sequence != expected_late_sequence for sequence in explicit_misses) else []),
    }


def frozen_plan(*, f2r=False, f2rec=False, handoff=False, handoff_entry_late=False,
                d094_landing_late=False):
    if d094_landing_late:
        rows = []
        for direction in range(4):
            row = {"id": f"d094-landing-late-{direction}",
                   "family": "handoff_column_top", "direction": direction,
                   "condition": "late_air", "expected": "success",
                   "d094_source_family": "column_landing_turn",
                   "injected_late_ticks": 1, "late_air_command_index": 3}
            solids, start, target = fixture(row)
            row["scene_sha256"] = digest(sorted((list(p), m) for p, m in solids.items()))
            row["start_position"] = list(start)
            row["goal_bounds"] = list(_original_goal(row, target).region.as_tuple())
            rows.append(row)
        return rows
    if handoff or handoff_entry_late:
        from tests.sim.f2s_cases import support_region_cases
        rows = []
        for case in support_region_cases():
            if case["family"] != "column_top" or case["target"] != "product":
                continue
            if handoff_entry_late and case["condition"] != "normal":
                continue
            direction = ("south", "east", "north", "west").index(
                case["direction"],
            )
            row = {
                "id": "handoff-" + case["id"].replace("/", "-"),
                "family": "handoff_column_top",
                "direction": direction,
                "condition": (
                    "late_first"
                    if case["condition"] == "first_late" else "normal"
                ),
                "expected": "success",
                "target_kind": case["target"],
                "frozen_v9_id": case["id"],
                "frozen_v9_input_sha256": digest(case),
            }
            if handoff_entry_late:
                row["id"] = "entry-late-" + row["id"]
                row["condition"] = "late_entry"
            solids, start, target = fixture(row)
            row["scene_sha256"] = digest(sorted(
                (list(position), material)
                for position, material in solids.items()
            ))
            row["start_position"] = list(start)
            row["goal_bounds"] = list(
                _original_goal(row, target).region.as_tuple()
            )
            rows.append(row)
        assert len(rows) == (4 if handoff_entry_late else 8)
        return rows
    if f2rec:
        from tests.sim.f2s_cases import support_region_cases
        selected = {("platform_outer_corner", "melee"),
                    ("platform_outer_corner", "follow"), ("bridge_head", "melee")}
        rows = []
        for case in support_region_cases():
            if (case["family"], case["target"]) not in selected:
                continue
            direction = ("south", "east", "north", "west").index(case["direction"])
            row = {"id": "f2rec-" + case["id"].replace("/", "-"),
                "family": "f2rec_"+case["family"], "direction": direction,
                "condition": "late_first" if case["condition"] == "first_late" else "normal",
                "expected": "success", "target_kind": case["target"],
                "frozen_v9_id": case["id"], "frozen_v9_input_sha256": digest(case)}
            solids, start, target = fixture(row)
            row["scene_sha256"] = digest(sorted((list(p),m) for p,m in solids.items()))
            row["start_position"] = list(start)
            row["goal_bounds"] = list(_original_goal(row, target).region.as_tuple())
            rows.append(row)
        assert len(rows) == 24
        return rows
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
    if row.get("frozen_v9_id") is not None:
        from tests.sim.f2s_cases import support_region_cases
        case = next(case for case in support_region_cases() if case["id"]==row["frozen_v9_id"])
        assert digest(case) == row["frozen_v9_input_sha256"]
        return ({(x,y+36,z):material for x,y,z,material in case["solids"]},
                (case["start"][0],case["start"][1]+36,case["start"][2]),
                (case["goal"][0],case["goal"][1]+36,case["goal"][2]))
    if row.get("d094_source_family") is not None:
        from scripts.action_entry_late_hardening import scenario_for
        scenario = scenario_for(row["d094_source_family"], None)
        solids, start, target = dict(scenario.scene.solids), scenario.start, scenario.goal
    elif family.startswith('f2r_'):
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


def _survey_exterior(row):
    if row["family"] == "f2rec_bridge_head":
        # Every camera position is on the original bridge/base geometry.
        points = ((-1.5,.5,-30),(1.5,.5,30),(.5,4.5,0),(.5,8.5,0))
    else:
        right_back_z = 10.5 if row["family"] == "f2r_outer_corner" else 9.5
        points = ((-2.5,.5,-30),(-2.5,9.5,-150),(3.5,.5,30),(3.5,right_back_z,150))
    return tuple((x,z,yaw,pitch) for x,z,yaw in points for pitch in (0,45))


def _fixture_view_has_support(solids, row, x, z):
    """Fixture-only preflight; never supplies a fact to the navigation actor."""
    px,pz = _rotate(x,z,row["direction"])
    area = sum(max(0., min(px+.3,bx+1)-max(px-.3,bx)) *
               max(0., min(pz+.3,bz+1)-max(pz-.3,bz))
               for bx,by,bz in solids if by==99)
    return area >= .36-1e-9


def _prepare_fixture(runtime, row, writer, task, profile, deadline, diagnostic):
    solids, start, target = fixture(row)
    assert row["scene_sha256"] == digest(sorted((list(p),m) for p,m in solids.items()))
    assert row["start_position"] == list(start)
    assert row["goal_bounds"] == list(_original_goal(row,target).region.as_tuple())
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
    exterior = _survey_exterior(row)
    def survey(views, covered):
        for x,z,yaw,pitch in views:
            if covered and row["family"] == "low_ceiling" and z > 4:
                z = 3.5
            if row["family"] == "f2rec_bridge_head":
                assert _fixture_view_has_support(solids,row,x,z), (row['id'],x,z)
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
    if row["family"] in {"f2rec_platform_outer_corner", "f2rec_bridge_head"}:
        # Observe exposed lower air from an actually supported edge before the
        # actor starts. These views request real profile-4 facts only.
        views += (((3.5,9.5,0,75),(3.5,9.5,-90,75)) if
            row["family"] == "f2rec_platform_outer_corner" else ((.5,8.5,0,75),))
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
        original_goal = _original_goal(row,target)
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
        entry_yaws = {}
        injected = False
        interruption_evidence = None
        entry_late_gate = None
        landing_air_gate = None
        landing_recovery_evidence = []
        initial_health = _self_health(runtime)
        with terminal_controller_evidence() as actual,ground_start_evidence() as starts:
            try:
                for index in range(360):
                    if driver.state in TERMINAL:
                        break
                    frame = runtime.navigation_observation_adapter.latest_frame
                    active = session._active_route
                    if active is not None:
                        for action_index, action in enumerate(
                                active.action_route.actions):
                            route = getattr(action,"fixed_route",None)
                            if route is not None:
                                routes[route.route_id] = [asdict(point) for point in route.points]
                            window = getattr(action, "entry_window", None)
                            if (window is not None
                                    and window.required_yaw_radians is not None):
                                entry_yaws[action_index] = (
                                    window.required_yaw_radians
                                )
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
                    prepared_navigation = driver._prepared_proposal
                    prepared_route_decision = prepared_navigation.route_decision
                    if (prepared_route_decision is not None
                            and prepared_route_decision.state is ActionRouteState.NEEDS_REPLAN
                            and prepared_route_decision.failure_cause is StopCause.INPUT_LOST):
                        landing_recovery_evidence.append({
                            "movement_tick": frame.body.movement_tick_id,
                            "position": frame.body.position,
                            "on_ground": frame.body.is_on_ground,
                            "action_index": prepared_route_decision.action_index,
                            "failure_cause": prepared_route_decision.failure_cause.value})
                    prepare_ns = time.perf_counter_ns()-began
                    nonneutral = any(e.intent.movement is not None and e.intent.movement != MovementV1()
                        for p in proposals for e in p.intents)
                    before = frame.body.movement_tick_id
                    delayed_candidate = nonneutral
                    if row["family"] == "handoff_column_top":
                        # This matrix verifies the first strict action input,
                        # whose proof explicitly allows one late tick.  The
                        # preceding ordinary Walk does not declare a timing
                        # obligation and is not the strict boundary under test.
                        delayed_candidate = any(
                            envelope.intent.movement is not None
                            and envelope.intent.movement.jump
                            for proposal in proposals
                            for envelope in proposal.intents
                        )
                    if row["condition"] == "late_entry":
                        entry_late_gate = entry_late_gate or _entry_late_walk_gate(
                            session, frame, proposals,
                            route_decision=prepared_navigation.route_decision,
                        )
                        delayed_candidate = entry_late_gate is not None
                    if row["condition"] == "late_air":
                        landing_air_gate = landing_air_gate or _landing_late_air_gate(
                            session, frame, prepared_navigation.route_decision,
                            command_index=row["late_air_command_index"])
                        delayed_candidate = landing_air_gate is not None
                    delay = _should_inject_late_input(row["condition"], late,
                        delayed_candidate)
                    if delay:
                        time.sleep(.110 if row["condition"] == "late_two" else .055)
                    result = runtime.control_frame(task,profile,deadline,proposals=proposals)
                    explicit_input_timing = _selected_input_has_declared_timing(
                        prepared_navigation, result.decision,
                    )
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
                            "request_sequence":sequence,
                            "requested_last_tick":None if record is None else record.requested_last_tick,
                            "latest_allowed_first_tick":None if record is None else record.latest_allowed_first_tick,
                            "valid_for_ticks":None if record is None else record.action.valid_for_ticks,
                            "actual_movement":None if record is None else asdict(record.action.movement),
                            "actual_ticks":applications,"actual_offset":None if not applications else min(applications)-before,
                            "status":None if record is None else record.status.value,
                            "explicit_input_timing":explicit_input_timing,
                            "entry_gate":entry_late_gate, "air_gate":landing_air_gate}
                    activity = (None if fd is None
                                or fd.movement_activity is None
                                else fd.movement_activity)
                    required_yaw = (
                        None if activity is None else
                        entry_yaws.get(activity.action_index)
                    )
                    yaw_error = (
                        None if required_yaw is None else math.degrees(abs(
                            math.atan2(
                                math.sin(frame.body.yaw_radians - required_yaw),
                                math.cos(frame.body.yaw_radians - required_yaw),
                            )
                        ))
                    )
                    sample = {"index":index,"tick":frame.body.movement_tick_id,"position":frame.body.position,
                        "velocity":frame.body.velocity_blocks_per_second,"pose":frame.body.pose,
                        "yaw_radians":frame.body.yaw_radians,
                        "required_entry_yaw_radians":required_yaw,
                        "entry_yaw_error_degrees":yaw_error,
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
                        "explicit_input_timing":explicit_input_timing,
                        "expected_movement_tick":(
                            prepared_navigation.route_decision.expected_movement_tick
                            if explicit_input_timing else None),
                        "verified_command_index":(
                            prepared_navigation.route_decision.verified_command_index
                            if explicit_input_timing else None),
                        "latest_movement_tick":(
                            prepared_navigation.route_decision.latest_movement_tick
                            if explicit_input_timing else None),
                        "requested_first_tick":None if record is None else record.requested_first_tick,
                        "latest_allowed_first_tick":None if record is None else record.latest_allowed_first_tick,
                        "request_sequence":sequence,"source":None if driver.source is None else asdict(driver.source),
                        "movement":None if result.decision is None else asdict(result.decision.action.movement),
                        "selected_intents":None if result.decision is None else result.decision.selected_intents,
                        "activity":None if activity is None else asdict(activity)}
                    frames.append(sample)
                    if row["condition"] == "late_entry":
                        sample["pre_input_speed_blocks_per_second"] = speed
                        sample["maximum_entry_speed_blocks_per_second"] = (
                            None if entry_late_gate is None else
                            entry_late_gate["maximum_entry_speed_blocks_per_second"]
                        )
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
                if (row["family"] != "handoff_column_top"
                        and any(not f["on_ground"]
                                or f["position"][1] < start[1]-.01
                                for f in frames)):
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
                if row["condition"] == "late_entry" and (late is None
                        or late["actual_offset"] != 2 or late["entry_gate"] is None):
                    violations.append("late_entry_not_confirmed")
                if row["condition"] == "late_air":
                    if not landing_recovery_evidence:
                        violations.append("late_air_recovery_not_exercised")
                    if (late is None or late["air_gate"] is None
                            or not late["explicit_input_timing"]
                            or late["actual_offset"] != 2
                            or late["status"] != "applied_outside_window"):
                        violations.append("late_air_not_confirmed")
                    if late is not None:
                        landed_after_late = next((i for i, value in enumerate(frames)
                            if value["tick"] > late["before_tick"]
                            and value["on_ground"]), None)
                        if landed_after_late is None:
                            violations.append("late_air_landing_not_confirmed")
                        elif any(value["movement"] is not None and value["movement"]["jump"]
                                 for value in frames[landed_after_late:]):
                            violations.append("late_air_second_jump")
                if row["family"] == "handoff_column_top":
                    jump_frames = [
                        value for value in frames
                        if value["movement"] is not None
                        and value["movement"]["jump"]
                    ]
                    if not jump_frames:
                        violations.append("handoff_jump_not_applied")
                    elif any(
                        value["entry_yaw_error_degrees"] is None
                        or value["entry_yaw_error_degrees"] > 2.0 + 1.0e-9
                        for value in jump_frames
                    ):
                        violations.append("handoff_entry_yaw_outside_window")
                    if row["condition"] == "late_entry" and jump_frames and any(
                        value["maximum_entry_speed_blocks_per_second"] is None
                        or value["pre_input_speed_blocks_per_second"]
                            > value["maximum_entry_speed_blocks_per_second"] + 1.e-9
                        for value in jump_frames
                    ):
                        violations.append("handoff_entry_speed_outside_window")
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
                if (row["family"] != "handoff_column_top"
                        and quality["stagnant_windows"]):
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
                    "landing_recovery_evidence":landing_recovery_evidence,
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
                timing_evidence = _input_timing_evidence(frames,
                    task_success=result_row["task_success"],
                    expected_late_sequence=(
                        late["request_sequence"]
                        if row["condition"] in {"late_two", "late_air"}
                        and late is not None else None))
                result_row.update({key: value for key, value in timing_evidence.items()
                                   if key != "violations"})
                if (not row.get("injection") and timing_evidence["violations"]):
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
    parser.add_argument('--f2rec', action='store_true', help='Frozen 24-case recovery support-edge matrix')
    parser.add_argument('--handoff', action='store_true', help='Frozen 8-case Walk-to-JumpUp handoff matrix')
    parser.add_argument('--handoff-entry-late', action='store_true', help='Four typed entry-braking Walk late probes')
    parser.add_argument('--d094-landing-late', action='store_true',
        help='Four final JumpUp airborne-input-loss landing recovery probes')
    args,launcher = parser.parse_known_args(argv)
    if sum((args.f2r, args.f2rec, args.handoff, args.handoff_entry_late,
            args.d094_landing_late)) > 1:
        parser.error('choose only one frozen matrix')
    selected = frozen_plan(
        f2r=args.f2r, f2rec=args.f2rec, handoff=args.handoff,
        handoff_entry_late=args.handoff_entry_late,
        d094_landing_late=args.d094_landing_late,
    )
    if args.smoke:
        selected = [r for r in selected if r["direction"] == 0 and (
            r["condition"] == "normal" or r["id"] == "f2-offset_mid-0-late_first"
            or r["condition"] in {"late_two", "late_entry", "late_air"})]
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
