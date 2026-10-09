"""Collect compact F2 component and formal-chain evidence.

Direct routes use an explicitly labelled TEST_ORACLE component world. Player and
regression references use the existing Runtime chain and v7 input identities.
Missing future diagnostics are RED, never inferred as zero. No raw trace files
are written. Timing is excluded from behavioral hashes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from contextlib import contextmanager
from unittest.mock import patch
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
MANIFEST = ROOT / "tests/sim/manifests/navigation-product-f2-ground-route-v1.json"
SCHEMA = "mc2p.f2-ground-route-evidence.v1"
MEASUREMENT_SCHEMA = "mc2p.f2-ground-route-measurement.v2"
FUTURE_DIAGNOSTICS = {"full_candidates": "Task2", "physics_steps": "Task2",
                      "execution_contract": "Task3", "completion_region": "Task4"}
PENDING_DIAGNOSTICS = {k: v for k, v in FUTURE_DIAGNOSTICS.items()
                       if k not in {"full_candidates", "physics_steps", "execution_contract", "completion_region"}}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def gate_violations(frames, *, corridor, intervals=()):
    """Check measured per-tick results, independent of controller reason strings."""
    violations = set()
    for row in frames:
        if row["moving"] and row["contact"] and row["progress_delta"] <= 1e-9 and not row["in_completion_region"]:
            violations.add("head_on_mid_route_wall")
        if row["unsafe_contact"]:
            violations.add("unsafe_contact")
        if row["route_responsibility"] in {"active", "settling"} and row["cross_track"] > corridor + 1e-9:
            violations.add("outside_corridor")
        if row["sneak"] and not any(start <= row["progress"] <= end for start, end in intervals):
            violations.add("undeclared_sneak")
        if row["terminal"] and row["sneak"]:
            violations.add("terminal_sneak")
        for field in ("full_candidates", "physics_steps"):
            if row[field] is None:
                violations.add(f"RED:{FUTURE_DIAGNOSTICS[field]}:{field}")
            elif type(row[field]) is not int or row[field] < 0:
                raise ValueError(f"invalid diagnostic: {field}")
        if row["full_candidates"] is not None and row["full_candidates"] > 3:
            violations.add("full_candidate_limit")
    return sorted(violations)


def project_route(points, position):
    """Independent nearest-polyline ruler, measured from actual calculator state."""
    best, total = None, 0.
    for first, second in zip(points, points[1:]):
        dx, dz = second[0] - first[0], second[2] - first[2]
        length = math.hypot(dx, dz)
        fraction = max(0., min(1., ((position[0] - first[0]) * dx + (position[2] - first[2]) * dz) / length**2))
        distance = math.hypot(position[0] - first[0] - fraction * dx,
                              position[2] - first[2] - fraction * dz)
        candidate = (distance, total + fraction * length)
        if best is None or candidate[0] < best[0] - 1e-9:
            best = candidate
        total += length
    return best[1], best[0]


def measure_frame(previous_state, state, applied, case, scene, *, tick,
                  responsibility, terminal=False, full_candidates=None,
                  physics_steps=None):
    """Derive measurements from raw states/inputs and frozen route/world facts.

    Responsibility is the harness's execution phase, not whether a key is held.
    An initial rejected entry is not admitted; every active or settling sample
    retains corridor responsibility, including neutral inertial motion.
    """
    from mc2p.contracts.action_v1 import MovementV1
    if responsibility not in {"not_admitted", "active", "settling", "released"}:
        raise ValueError("invalid route responsibility phase")
    progress, _ = project_route(case["route_points"], previous_state.position)
    next_progress, error = project_route(case["route_points"], state.position)
    goal = case["goal_box"]
    return dict(tick=tick, moving=applied != MovementV1(), progress=progress,
                progress_delta=next_progress-progress, cross_track=error,
                contact=state.horizontal_collision,
                unsafe_contact=_unsafe_contact(state.body_box, scene, case["unknown_cells"]),
                in_completion_region=all(goal[i] <= state.position[i] <= goal[i+3] for i in range(3)),
                sneak=applied.sneak or state.sneaking,
                terminal=terminal, route_responsibility=responsibility,
                full_candidates=full_candidates, physics_steps=physics_steps,
                position=list(state.position), movement=asdict(applied),
                body_sneaking=state.sneaking, on_ground=state.on_ground)


def _unsafe_contact(body_box, scene, unknown_cells):
    unknown = {tuple(p) for p in unknown_cells}
    return any((material in {"minecraft:magma_block", "minecraft:water", "minecraft:oak_fence"} or p in unknown)
               and any(all(getattr(box, f"min_{axis}") - .001 <= getattr(body_box, f"max_{axis}")
                           and getattr(box, f"max_{axis}") + .001 >= getattr(body_box, f"min_{axis}")
                           for axis in ("x", "y", "z"))
                       for box in scene.geometry(material).world_boxes(p))
               for p, material in scene.solids.items())


def _frame(state, world, tick):
    from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
    from mc2p.motion_nav.world_model import ObservationStamp
    stamp = ObservationStamp(state.session, tick, tick, "f2-component", tick * 50_000_000)
    body = BodyState(state.session, tick, stamp, state.position,
                     tuple(v * 20 for v in state.velocity_blocks_per_tick),
                     state.yaw_radians, state.pitch_radians, state.pose, state.body_box,
                     state.on_ground, state.horizontal_collision, state.vertical_collision,
                     is_sneaking=state.sneaking, movement_tick_id=state.movement_tick_id)
    return NavigationFrame(state.session, body, world, "fabric")


def run_route(case):
    from mc2p.contracts.action_v1 import MovementV1
    from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteConfig, FixedRouteController, RoutePoint
    from mc2p.motion_nav.ground_route_execution import (
        GroundRouteCapability, GroundRouteCapabilityInterval, GroundRouteExecutionContract,
    )
    from mc2p.motion_nav.ground_traversal import verify_ground_traversal
    from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
    from mc2p.motion_nav.online_motion import project_movement_command
    from mc2p.motion_nav.physics_1_21 import step
    from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET
    from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp, WorldKnowledge
    from tests.sim.backend import CalculatorBackend
    from tests.sim.product_cases import f2_ground_route_scene
    scene = f2_ground_route_scene(case)
    if digest(sorted((list(p), material) for p, material in scene.solids.items())) != case["scene_sha256"]:
        raise ValueError(f"frozen component geometry changed: {case['id']}")
    backend = CalculatorBackend([0], scene, tuple(case["start"]), case["yaw_degrees"])
    state = backend.state
    actor_world = WorldKnowledge(state.session)
    stamp = ObservationStamp(state.session, 0, 0, "f2-component", 0)
    unknown = {tuple(p) for p in case["unknown_cells"]}
    actor_world.observe_blocks(stamp, {
        p: BlockGeometry.empty(material, fluid=True) if material == "minecraft:water"
        else BlockGeometry.unsupported(material) if material == "minecraft:oak_fence"
        else scene.geometry(material)
        for p, material in scene.solids.items() if p not in unknown})
    actor_world.confirm_air(stamp, tuple(p for p in scene.air_cells() if p not in unknown))
    actor = actor_world.view()
    profiles = NavigationSessionProfiles.load(ROOT / "config/motion-navigation")
    config = FixedRouteConfig(maximum_cross_track_blocks=case["corridor"])
    # Task1 froze placeholder bounds before the typed contract existed. This
    # test adapter explicitly intersects them with valid route progress; the
    # production type itself rejects out-of-range intervals.
    length = sum(math.dist(a[::2], b[::2]) for a, b in zip(case["route_points"], case["route_points"][1:]))
    effective_intervals = [(max(0., a), min(length, b)) for a, b in case["capability_intervals"]]
    assert all(a < b for a, b in effective_intervals)
    contract = (GroundRouteExecutionContract(tuple(
        GroundRouteCapabilityInterval(a, b, frozenset({GroundRouteCapability.SNEAK_EDGE_GUARD}))
        for a, b in effective_intervals), (), profiles.ground.profile_id)
        if case["capability_intervals"] else None)
    route = FixedRoute(case["id"], tuple(RoutePoint(*p) for p in case["route_points"]), contract)
    controller = FixedRouteController(profiles.ground, config=config)
    proof = None
    if case["family"] == "small_height":
        proof = verify_ground_traversal(state, route, backend.world, profiles.ground, maximum_ticks=case["max_ticks"])
        if proof.plan is None:
            return {"id": case["id"], "family": case["family"], "kind": "component",
                    "outcome": proof.status.value, "reason": list(proof.reasons),
                    "failure_category": "route_tracking", "success": False,
                    "input_sha256": digest(case), "frames": 0, "gate_violations": [],
                    "diagnostics_status": {k: f"RED:{v}" for k, v in FUTURE_DIAGNOSTICS.items()}}
    controller.start(route, _frame(state, actor, 0), traversal_plan=None if proof is None else proof.plan)
    previous = MovementV1()
    frames, timings, fast_timings, slow_timings = [], [], [], []
    progress, _ = project_route(case["route_points"], state.position)
    admitted = False
    reason, outcome = "max_ticks", "bounded_timeout"
    for tick in range(1, case["max_ticks"] + 1):
        decision = controller.decide(_frame(state, actor, tick), physics_state=state)
        timings.append(decision.control_time_ns / 1e6)
        (slow_timings if decision.full_candidates else fast_timings).append(timings[-1])
        terminal = decision.state.value not in {"running", "braking", "cancelling", "waiting_input"}
        if not terminal:
            admitted = True
        requested = decision.movement
        applied = previous if tick in case["late_ticks"] else requested
        previous = requested
        projection = project_movement_command(state, applied)
        if projection.tick_input is None:
            raise ValueError("component command cannot be projected")
        result = step(state, projection.tick_input, backend.world, JAVA_1_21_RULESET)
        if result.status is not CalculationStatus.OK:
            outcome, reason = result.status.value, list(result.unsupported_reasons or result.invalid_reasons)
            break
        next_state = result.next_state
        measured = measure_frame(state, next_state, applied, case, scene, tick=tick,
                                 responsibility="settling" if terminal and admitted else "active" if admitted else "not_admitted",
                                 terminal=terminal, full_candidates=decision.full_candidates,
                                 physics_steps=decision.physics_steps)
        measured.update(controller_progress=decision.progress_blocks,
                        controller_cross_track=decision.cross_track_error_blocks,
                        reason=decision.reason)
        if contract is not None:
            measured.update(edge_guard_phase=decision.edge_guard_phase.value,
                            execution_contract={**{k:v for k,v in asdict(contract).items()
                                                    if k != 'completion_region' or v is not None}, "capability_intervals": [
                                {"start_progress_blocks": i.start_progress_blocks,
                                 "end_progress_blocks": i.end_progress_blocks,
                                 "capabilities": sorted(c.value for c in i.capabilities)}
                                for i in contract.capability_intervals]},
                            original_intervals=case["capability_intervals"],
                            effective_intervals=effective_intervals,
                            sneak_edge_clipped="sneak_edge_clipped" in result.events)
        frames.append(measured)
        state, progress = next_state, measured["progress"] + measured["progress_delta"]
        if terminal:
            outcome, reason = decision.state.value, decision.reason
            break
    violations = gate_violations(frames, corridor=case["corridor"], intervals=effective_intervals)
    categories = {"tangent": "wall_result", "corner": "wall_result", "edge": "edge_input",
                  "head_wall": "truly_unreachable", "hazard": "truly_unreachable",
                  "unknown": "truly_unreachable", "fluid": "truly_unreachable",
                  "unsupported": "truly_unreachable", "outside": "route_tracking"}
    return {"id": case["id"], "family": case["family"], "kind": "component",
            "outcome": outcome, "reason": reason, "success": outcome == "succeeded",
            "failure_category": None if outcome == "succeeded" else categories.get(case["layout"], "route_tracking"),
            "input_sha256": digest(case), "measurement_schema": MEASUREMENT_SCHEMA,
            "behavior_sha256": digest(frames), "frames": len(frames),
            "baseline_v2_behavior_sha256": digest([{**r, "full_candidates": None, "physics_steps": None}
                                                    for r in frames]),
            "final_position": list(state.position), "progress_blocks": progress,
            "maximum_cross_track_blocks": max((r["cross_track"] for r in frames), default=0),
            "full_candidates_max": max((r["full_candidates"] for r in frames), default=None)
            if all(r["full_candidates"] is not None for r in frames) else None,
            "physics_steps_total": sum(r["physics_steps"] for r in frames)
            if all(r["physics_steps"] is not None for r in frames) else None,
            "physics_steps_max": max((r["physics_steps"] for r in frames), default=0),
            "diagnostics_status": {"full_candidates": "production", "physics_steps": "production",
                                   "execution_contract": "production",
                                   **{k: f"RED:{v}" for k, v in PENDING_DIAGNOSTICS.items()}},
            "gate_violations": violations, "control_ms": timing_summary(timings),
            "execution_contract": frames[0].get("execution_contract") if frames else None,
            "original_intervals": case["capability_intervals"],
            "effective_intervals": effective_intervals,
            "edge_guard_phase_frames": dict(Counter(r.get("edge_guard_phase", "inactive") for r in frames)),
            "sneak_frames": sum(r["sneak"] for r in frames),
            "sneak_edge_clip_frames": sum(r.get("sneak_edge_clipped", False) for r in frames),
            "lost_ground_frames": sum(not r["on_ground"] for r in frames),
            "fast_control_ms": timing_summary(fast_timings), "fast_frames": len(fast_timings),
            "slow_control_ms": timing_summary(slow_timings), "slow_frames": len(slow_timings)}


def timing_summary(values):
    from scripts.navigation_coordination_metrics import quantile
    return {"p50": quantile(values, .5), "p95": quantile(values, .95),
            "p99": quantile(values, .99), "maximum": max(values, default=None)}


@contextmanager
def terminal_controller_evidence():
    """Observe the real FixedRoute objects and decisions; leave trace unchanged."""
    from mc2p.motion_nav.fixed_route import FixedRouteController
    from mc2p.motion_nav import action_route_executor
    from mc2p.motion_nav.ground_modes import observed_ground_mode
    original_start, original_decide = FixedRouteController.start, FixedRouteController.decide
    original_goal = action_route_executor.evaluate_observed_goal
    records = {"contracts": [], "frames": [], "formal_goal_checks": []}
    def start(controller, route, frame, **kwargs):
        if route.execution_contract is not None:
            value = asdict(route.execution_contract)
            value['capability_intervals'] = [{**asdict(i),
                'capabilities': sorted(c.value for c in i.capabilities)}
                for i in route.execution_contract.capability_intervals]
            records["contracts"].append(value)
        return original_start(controller, route, frame, **kwargs)
    def decide(controller, frame, **kwargs):
        decision = original_decide(controller, frame, **kwargs)
        records["frames"].append({"control_ms": decision.control_time_ns/1e6,
            "full_candidates": decision.full_candidates, "physics_steps": decision.physics_steps,
            "state": decision.state.value, "position": frame.body.position})
        return decision
    def goal_check(frame, goal, risk_policy_id):
        result = original_goal(frame, goal, risk_policy_id)
        records['formal_goal_checks'].append({
            'status': result.status.value, 'observation_sequence': frame.body.sequence_id,
            'movement_tick': frame.body.movement_tick_id, 'position': frame.body.position,
            'velocity_blocks_per_second': frame.body.velocity_blocks_per_second,
            'mode': None if observed_ground_mode(frame.body) is None else observed_ground_mode(frame.body).value,
            'pose': frame.body.pose, 'sneaking': frame.body.is_sneaking,
            'on_ground': frame.body.is_on_ground, 'yaw_radians': frame.body.yaw_radians,
            'food_points': frame.body.food_points, 'applied_risk_policy_id': risk_policy_id,
            'goal_bounds': goal.region.as_tuple(), 'goal_support': goal.support.value,
            'goal_modes': sorted(m.value for m in goal.allowed_modes),
            'goal_poses': sorted(goal.allowed_poses),
            'maximum_terminal_speed': goal.maximum_terminal_speed_blocks_per_second,
            'required_yaw': goal.required_yaw_radians,
            'maximum_yaw_error': goal.maximum_yaw_error_radians,
            'minimum_resources': goal.minimum_resources.values,
            'goal_risk_policy_id': goal.risk_policy_id})
        return result
    with patch.object(FixedRouteController, "start", start), patch.object(FixedRouteController, "decide", decide), \
            patch.object(action_route_executor, 'evaluate_observed_goal', goal_check):
        yield records


def run_reference(case, manifest):
    from scripts.navigation_coordination_metrics import trace_signatures
    from tests.sim.product_cases import product_scenario
    from tests.sim.runner import run
    from tests.sim.motion_delivery import DeterministicMotionWorker
    product = manifest["product_reference"]
    group = next(g for g in product["groups"] if g["id"] == case["group"])
    scenario, parameters = product_scenario(product, group, case["seed"])
    # Frozen input fingerprints detect any future fixture drift before running.
    if (digest(parameters) != case["parameters_sha256"] or list(scenario.goal) != case["goal"]
            or list(scenario.start) != case["start"]
            or digest(sorted((list(p), material) for p, material in scenario.scene.solids.items())) != case["scene_sha256"]):
        raise ValueError(f"frozen reference changed: {case['id']}")
    delivery = DeterministicMotionWorker(product["motion_delivery_profile"])
    trace = []
    with terminal_controller_evidence() as actual:
        result = run(scenario, trace_sink=trace.append, motion_factory=lambda: delivery,
                     control_step=delivery.control_step)
    goal_frame = next((row for row in trace if row.get("session_state") == "complete"
                       and row.get("goal_satisfied")), None)
    proof = (None if goal_frame is None else {k: goal_frame[k] for k in
        ("observation_sequence", "movement_tick", "position", "velocity", "pose", "sneaking",
         "on_ground", "support_fraction", "goal_position", "goal_satisfied")})
    return {"id": case["id"], "family": case["family"], "kind": "formal_chain",
            "outcome": result.outcome, "reason": result.reason,
            "success": result.outcome == "success", "input_sha256": digest(case),
            **trace_signatures(trace), "violations": list(result.violations),
            "verification_complete": result.verification_complete,
            "actual_execution_contracts": actual["contracts"],
            "strict_goal_completion_observation": proof,
            "formal_goal_checks": actual['formal_goal_checks'],
            "terminal_control_ms": timing_summary([f["control_ms"] for f in actual["frames"]]),
            "terminal_full_candidates_max": max((f["full_candidates"] for f in actual["frames"]), default=0),
            "terminal_physics_steps": sum(f["physics_steps"] for f in actual["frames"]),
            "frames": len(trace), "physical_reachability": case["physical_reachability"],
            "failure_category": None if result.outcome == "success" else "completion_region"}


def _execute(job):
    case, manifest = job
    return run_route(case) if case["kind"] == "component" else run_reference(case, manifest)


def collect(output, *, families=None, ids=None, workers=4, baseline=False):
    from scripts.navigation_coordination_metrics import source_fingerprint
    if output.exists():
        raise FileExistsError(output)
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    cases = manifest["tasks"]
    if families:
        unknown = set(families) - {c["family"] for c in cases}
        if unknown:
            raise ValueError(f"unknown families: {sorted(unknown)}")
        cases = [c for c in cases if c["family"] in families]
    if ids:
        unknown = set(ids) - {c['id'] for c in cases}
        if unknown:
            raise ValueError(f'unknown frozen IDs: {sorted(unknown)}')
        cases = [c for c in cases if c['id'] in ids]
    output.mkdir(parents=True)
    started = time.perf_counter()
    source = source_fingerprint(["mc2p/**/*.py", "config/motion-navigation/*.json"])
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        with (output / "runs.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for row in pool.map(_execute, [(c, manifest) for c in cases]):
                rows.append(row)
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                if len(rows) % 20 == 0:
                    print(f"F2: {len(rows)}/{len(cases)}", flush=True)
    summary = {"schema_version": SCHEMA, "measurement_schema": MEASUREMENT_SCHEMA,
               "baseline_only": baseline, "cases": len(rows),
               "by_family": {f: dict(Counter("success" if r["success"] else "failed" for r in rows if r["family"] == f))
                             for f in sorted({r["family"] for r in rows})},
               "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
               "source_worktree_clean": not subprocess.check_output(["git", "status", "--porcelain"], text=True).strip(),
               "production": source, "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
               "harness": source_fingerprint(["scripts/f2_ground_route_evidence.py", "tests/sim/product_cases.py"]),
               "platform": platform.platform(), "python": sys.version,
               "elapsed_seconds": time.perf_counter()-started,
               "coverage_limits": [limit for limit in manifest["coverage_limits"]
                                   if not limit.startswith(("Candidate and physics-step diagnostics are missing",
                                                            "Declared edge intervals are frozen placeholders"))],
               "interval_adaptation": "Test adapter intersects frozen placeholder intervals with route length; original/effective bounds retained.",
               "diagnostics_source": "Actual FixedRoute contract, FixedRouteDecision counters/guard phase, applied input and calculator state/events",
               "pending_red": PENDING_DIAGNOSTICS}
    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", "utf-8")
    index = {"schema_version": SCHEMA, "measurement_schema": MEASUREMENT_SCHEMA,
             "manifest_sha256": summary["manifest_sha256"],
             "cases": [{"id": r["id"], "input_sha256": r["input_sha256"],
                        "behavior_sha256": r.get("behavior_sha256", r.get("trajectory_sha256")),
                        "outcome": r["outcome"], "reason": r["reason"]} for r in rows]}
    (output / "index.json").write_text(json.dumps(index, indent=2) + "\n", "utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--families", nargs="+")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args(argv)
    summary = collect(args.output, families=args.families, ids=args.ids, workers=args.workers, baseline=args.baseline)
    print(json.dumps({k: v for k, v in summary.items() if k not in {"production", "harness"}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
