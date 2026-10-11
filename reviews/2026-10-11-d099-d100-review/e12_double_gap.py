"""E12：连续两跳能否“空中算好下一跳、落地直接接上”，代价多大？

世界：y=0 地面；第一个坑 z∈[4,5)，平台 z∈[5,P)，第二个坑 z∈[P,P+G2)，之后是终点平台。
第一跳：D 样本中 gap1 模板的已证明输入（rest／walk／sprint 三种入口），只证准时分支。
第一跳着地 tick 的身体状态是确定的（无扰动时），因此可以在第一跳起跳回执之后、
空中飞行期间，从“预测着地状态”求解并证明第二跳：

- 第二跳控制器：项目自己的 template_controller（参数格点），候选顺序 =
  冻结表中 gap1 行的参数按出现次数排序后去重；找到第一个“控制器到达终点＋全量扫描通过”的就停；
- 第二跳证明：scan_commitment（单分支，准时）＋终点检查。

对比两种执行：
- 连续：第一跳输入截到着地 tick，然后直接接第二跳输入；
- 停走：第一跳完整执行到停稳，再从静止求解第二跳。
另外在“预测着地状态”上加横向／前向偏差，看预先算好的第二跳是否仍然成立（E11 式重放），
以及偏差后在着地节点重新求解要多少步。

只在进程内构造对象，不改项目文件。耗时为 Linux 墙钟；步数与平台无关。
运行：PYTHONPATH=<项目根> python e12_double_gap.py
"""
import json, math, statistics, time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch
from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence, _goal_check
from experiments.motion_navigation.trajectory_proto.frozen_templates import (
    EntrySpec, make_request, SESSION, GEOMETRY_REVISION, goal_for)
from experiments.motion_navigation.trajectory_proto.template_controller import (
    ControllerParams, ControllerTree, TemplateGeometry, evaluate, AIR_TICKS, TAKEOFF_GRID)
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.measurement import _input
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.world_model import (Aabb, BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldView)

DATA = Path("experiments/motion_navigation/trajectory_proto/data")


def world(platform_end, gap2):
    stamp = ObservationStamp(SESSION, 0, 0, "d097a-trial-v1", 0)
    facts = {}
    for x in range(-9, 10):
        for y in range(-50, 8):
            for z in range(-14, 26):
                hole = 4 <= z < 5 or platform_end <= z < platform_end + gap2
                solid = y == 0 and not hole
                facts[(x, y, z)] = CellFact(CellKnowledge.BLOCK if solid else CellKnowledge.AIR, stamp,
                                            BlockGeometry.full_cube("minecraft:stone") if solid else None)
    return PhysicsWorldView(WorldView.detached(SESSION, GEOMETRY_REVISION, 0, facts), JAVA_1_21_RULESET)


def single(request, entry, goal=None, centre=None):
    out = replace(request, entry_states=(entry,), timing_branches=(TimingBranch.ON_TIME,),
                  allowed_effect_ticks=(entry.movement_tick_id + 1,), branch_preludes=((),), input_prefix=())
    if goal is not None:
        out = replace(out, goal=goal, route_guidance=(centre,))
    return out


def prove(request, inputs, w):
    counter = CountedPhysics(request.budget)
    scan = scan_commitment(request, inputs, w, _boundary_evidence(request, inputs), counter=counter)
    if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
        return False, False, counter.physics_steps, scan
    goal, missing = _goal_check(request, TimingBranch.ON_TIME, scan.proof.branches[0].states[-1], w, counter)
    return True, bool(goal is not None and goal.accepted and not missing), counter.physics_steps, scan


def candidate_params():
    table = json.loads((DATA / "d097a-m1-table.json").read_text(encoding="utf-8"))
    counts = Counter()
    for row in table["rows"]:
        if row["template"] in ("gap1", "gap2"):
            for item in row["params"]:
                counts[json.dumps(item["params"], sort_keys=True)] += 1
    ordered = [ControllerParams.from_dict(json.loads(k)) for k, _ in counts.most_common()]
    # Sprint-first grid fallback, so the search is not limited to the table's start-line families.
    for d in TAKEOFF_GRID:
        for air in ("S", "W", "N"):
            for ticks in AIR_TICKS:
                params = ControllerParams("S", d, "SJ", air, ticks)
                if params not in ordered:
                    ordered.append(params)
    return ordered


def solve(request, w, geometry):
    """First candidate whose controller rollout reaches the goal and whose full scan verifies."""
    start = time.perf_counter_ns()
    steps = tried = 0
    for params in PARAMS:
        tried += 1
        tree = ControllerTree(request, w, geometry, CountedPhysics(request.budget))
        node, reason = evaluate(tree, params)
        steps += tree.counter.physics_steps
        if node is None:
            continue
        safe, ok, scan_steps, scan = prove(request, node.inputs, w)
        steps += scan_steps
        if safe and ok:
            SOLVE_DETAIL.update(controller_steps=tree.counter.physics_steps, scan_steps=scan_steps,
                                key_only_proof_steps=key_only_steps(scan, len(node.inputs)))
            return params, node.inputs, scan, tried, steps, (time.perf_counter_ns() - start) / 1e6
    return None, None, None, tried, steps, (time.perf_counter_ns() - start) / 1e6


SOLVE_DETAIL = {}


def key_only_steps(scan, n):
    """E13 bookkeeping: main rollout + stop tails only at entry, last abandon, landing recovery and end."""
    branch = scan.proof.branches[0]
    cost = {t.boundary: len(t.inputs) for t in branch.tails}
    nodes = {0, n}
    for risk in branch.risk_intervals:
        if risk.last_abandon_boundary is not None:
            nodes.add(risk.last_abandon_boundary)
        nodes.add(risk.recovered_boundary)
    return n + sum(cost[b] for b in nodes)


def horizontal_speed(state):
    return 20. * math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])


PARAMS = candidate_params()
rows = json.loads((DATA / "d097a-accepted-sample.json").read_text(encoding="utf-8"))["rows"]
first_jumps = [r for r in rows if r["template_id"] == "gap1"]
COURSES = ((8, 1), (8, 2), (7, 1), (9, 2))   # (platform end z, second gap width)
print(f"candidate params: {len(PARAMS)} (table gap1/gap2 families first, then sprint grid)")
summary = []
for platform_end, gap2 in COURSES:
    w = world(platform_end, gap2)
    goal0 = goal_for("gap1")
    shift = platform_end + gap2 - 5
    region = (.3, 1., 5.02 + shift, .7, 1.05, 6.15 + shift)
    goal2 = replace(goal0, region=Aabb(*region))
    centre = (.5, 1., (region[2] + region[5]) / 2.)
    geometry = TemplateGeometry(float(platform_end), centre)
    for row in first_jumps:
        spec = EntrySpec(row["template_id"], row["gait"], row["ticks"], row["offset"], row["heading_deg"])
        request, _ = make_request(spec)
        inputs1 = tuple(_input(item) for item in row["inputs"])
        req1 = single(request, request.entry_states[0])
        safe1, ok1, steps1, scan1 = prove(req1, inputs1, w)
        label = f"course(P={platform_end},G2={gap2}) first={row['bin']}"
        if not ok1:
            print(f"{label}: first jump not verified on this course ({scan1.status}/{scan1.reason})")
            continue
        states1 = scan1.proof.branches[0].states
        jump_index = next(i for i, c in enumerate(inputs1) if c.jump)
        touch = next(i for i in range(jump_index + 2, len(states1)) if states1[i].on_ground)
        air_ticks = touch - jump_index
        landing = states1[touch]
        # Continuous: solve the second jump from the predicted touchdown state.
        req2 = single(request, landing, goal2, centre)
        SOLVE_DETAIL.clear()
        params2, inputs2, scan2, tried, steps2, ms2 = solve(req2, w, geometry)
        detail2 = dict(SOLVE_DETAIL)
        # Stop-and-go: full first jump to rest, then solve from rest.
        rest = states1[-1]
        req2r = single(request, rest, goal2, centre)
        params_r, inputs_r, scan_r, tried_r, steps_r, ms_r = solve(req2r, w, geometry)
        line = {"course": [platform_end, gap2], "first": row["bin"], "air_ticks": air_ticks,
                "landing_z": round(landing.position[2], 3), "landing_speed_bps": round(horizontal_speed(landing), 2),
                "first_proof_steps": steps1}
        if params2 is None:
            line["continuous"] = {"found": False, "tried": tried, "steps": steps2, "ms": round(ms2, 1)}
        else:
            s2 = scan2.proof.branches[0].states
            jump2 = next(i for i, c in enumerate(inputs2) if c.jump)
            stops_before_jump = sum(1 for c in inputs2[:jump2] if c.forward == 0.)
            line["continuous"] = {
                "found": True, "params": params2.to_dict(), "tried": tried, "steps": steps2, "ms": round(ms2, 1),
                **detail2,
                "ground_ticks_before_jump2": jump2, "stop_inputs_before_jump2": stops_before_jump,
                "speed_at_jump2_bps": round(horizontal_speed(s2[jump2]), 2),
                "total_ticks_to_rest": touch + len(inputs2)}
        if params_r is not None:
            line["stop_and_go"] = {"tried": tried_r, "steps": steps_r, "ms": round(ms_r, 1),
                                   "total_ticks_to_rest": len(inputs1) + len(inputs_r)}
        else:
            line["stop_and_go"] = {"found": False, "tried": tried_r, "steps": steps_r}
        # Deviation at touchdown: does the precomputed second jump survive? If not, what does a re-solve cost?
        deviations = []
        if params2 is not None:
            for name, dx, dz in (("lateral+0.1", .1, 0.), ("lateral+0.25", .25, 0.),
                                 ("forward+0.05", 0., .05), ("forward-0.05", 0., -.05),
                                 ("forward+0.15", 0., .15), ("forward-0.15", 0., -.15)):
                p = landing.position
                moved = replace(landing, position=(p[0] + dx, p[1], p[2] + dz))
                req_m = single(request, moved, goal2, centre)
                safe_m, goal_m, check_steps, _ = prove(req_m, inputs2, w)
                still = safe_m and goal_m
                entry = {"deviation": name, "precomputed_safe": safe_m, "precomputed_goal": goal_m,
                         "precomputed_still_valid": still, "recheck_steps": check_steps}
                if not still:
                    pr, _, _, tr, st, ms = solve(req_m, w, geometry)
                    entry.update({"resolve_found": pr is not None, "resolve_tried": tr, "resolve_steps": st,
                                  "resolve_ms": round(ms, 1)})
                deviations.append(entry)
        line["touchdown_deviation"] = deviations
        summary.append(line)
        print(json.dumps(line, ensure_ascii=False))

print("\n== summary ==")
found = [l for l in summary if l["continuous"]["found"]]
print(f"first jumps verified on course: {len(summary)}; continuous second jump found: {len(found)}")
if found:
    for key in ("tried", "steps", "ms"):
        values = [l["continuous"][key] for l in found]
        print(f"   continuous solve {key}: median={statistics.median(values)} max={max(values)}")
    for key in ("controller_steps", "scan_steps", "key_only_proof_steps"):
        values = [l["continuous"][key] for l in found]
        print(f"   continuous {key}: median={statistics.median(values)} max={max(values)}")
    print("   ground ticks before jump2:", sorted(l["continuous"]["ground_ticks_before_jump2"] for l in found))
    print("   air ticks available:", sorted(l["air_ticks"] for l in found))
    print("   stop inputs before jump2:", sorted(l["continuous"]["stop_inputs_before_jump2"] for l in found))
    pairs = [(l["continuous"]["total_ticks_to_rest"], l["stop_and_go"].get("total_ticks_to_rest")) for l in found]
    print("   ticks to rest continuous vs stop-and-go:", pairs)
    dev = [d for l in found for d in l["touchdown_deviation"]]
    print(f"   touchdown deviations: {len(dev)}; precomputed still valid: {sum(d['precomputed_still_valid'] for d in dev)}")
    for name in sorted({d['deviation'] for d in dev}):
        sel = [d for d in dev if d["deviation"] == name]
        print(f"     {name:14s} safe={sum(d['precomputed_safe'] for d in sel)}/{len(sel)} "
              f"safe+goal={sum(d['precomputed_still_valid'] for d in sel)}/{len(sel)} "
              f"resolve_found={sum(bool(d.get('resolve_found')) for d in sel if not d['precomputed_still_valid'])}"
              f"/{sum(not d['precomputed_still_valid'] for d in sel)} "
              f"resolve_steps={[d.get('resolve_steps') for d in sel if not d['precomputed_still_valid']]}")
    recheck = [d["recheck_steps"] for d in dev]
    print(f"   recheck of precomputed packet at touchdown: steps median={statistics.median(recheck)} max={max(recheck)}")
