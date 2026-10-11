"""E11：已证明的固定输入序列，对身体状态偏差有多少余量？

D099 要求输入包绑定“可接受的状态范围”并留有余量；逐 tick 监视只在超出范围时才重算。
本实验对 28 个 A／C／D 样本，取已证明的输入（只证准时分支），在两个位置扰动身体状态，
不改输入，用项目自己的 scan_commitment 判断是否仍然安全：

- entry：动作入口；
- commit：最后可放弃点（准时分支第一个风险区间的 last_abandon 边界），
  扰动后只重放剩余输入（即“起跳及之后的开环部分”）。

扰动方向：沿动作前进方向 ±d、横向 ±d、水平速度 ×(1±f)。
“余量”取网格上从 0 开始连续通过的最大值；网格外未测。只改进程内对象，不改项目文件。
运行：PYTHONPATH=<项目根> python e11_packet_margin.py
"""
import math, statistics
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch
from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence, _goal_check
from experiments.motion_navigation.trajectory_proto.measurement import _set_a, _set_c, _set_d
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics

POS = (.005, .01, .02, .04, .08, .16, .32)
SPEED = (.01, .02, .05, .1, .2)


def single(request, entry):
    return replace(request, entry_states=(entry,), timing_branches=(TimingBranch.ON_TIME,),
                   allowed_effect_ticks=(entry.movement_tick_id + 1,), branch_preludes=((),),
                   input_prefix=())


def check(request, inputs, world):
    scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs))
    if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
        return "unsafe", scan
    final = scan.proof.branches[0].states[-1]
    goal, missing = _goal_check(request, TimingBranch.ON_TIME, final, world, CountedPhysics(request.budget))
    return ("safe_goal" if goal is not None and goal.accepted and not missing else "safe_only"), scan


def perturb(state, forward, lateral, along, across, factor):
    x, y, z = state.position
    vx, vy, vz = state.velocity_blocks_per_tick
    return replace(state, position=(x + forward[0] * along + lateral[0] * across, y,
                                    z + forward[1] * along + lateral[1] * across),
                   velocity_blocks_per_tick=(vx * factor, vy, vz * factor))


def margin(request, inputs, world, state, forward, lateral, kind, sign, need_goal):
    best = 0.
    for value in (POS if kind != "speed" else SPEED):
        d = sign * value
        s = (perturb(state, forward, lateral, d, 0., 1.) if kind == "along" else
             perturb(state, forward, lateral, 0., d, 1.) if kind == "across" else
             perturb(state, forward, lateral, 0., 0., 1. + d))
        try:
            result, _ = check(single(request, s), inputs, world)
        except Exception as error:   # contract rejects (e.g. body inside a block)
            result = f"error:{type(error).__name__}"
        ok = result == "safe_goal" if need_goal else result in ("safe_goal", "safe_only")
        if not ok:
            return best
        best = value
    return best


cases = list(_set_a(4)) + list(_set_c(6)) + list(_set_d(18))
rows = []
for set_id, key, request, world, inputs, *_ in cases:
    base = single(request, request.entry_states[0])
    result, scan = check(base, inputs, world)
    assert result != "unsafe", (key, scan.status, scan.reason)
    branch = scan.proof.branches[0]
    start, end = branch.states[0].position, branch.states[-1].position
    fx, fz = end[0] - start[0], end[2] - start[2]
    norm = math.hypot(fx, fz) or 1.
    forward, lateral = (fx / norm, fz / norm), (-fz / norm, fx / norm)
    points = [("entry", 0)]
    if branch.risk_intervals:
        abandon = branch.risk_intervals[0].last_abandon_boundary
        if abandon is not None and abandon > 0:
            points.append(("commit", abandon))
    for label, boundary in points:
        state = branch.states[boundary]
        suffix = inputs[boundary:]
        out = {"case": key, "point": label, "boundary": boundary, "suffix_ticks": len(suffix),
               "goal_at_zero": result == "safe_goal"}
        for kind in ("along", "across", "speed"):
            for sign, tag in ((1, "+"), (-1, "-")):
                out[f"{kind}{tag}"] = margin(base, suffix, world, state, forward, lateral, kind, sign, False)
                out[f"{kind}{tag}_goal"] = margin(base, suffix, world, state, forward, lateral, kind, sign, True) \
                    if result == "safe_goal" else None
        rows.append(out)
        print(f"{key:28s} {label:6s} b={boundary:2d} n={len(suffix):2d} "
              + " ".join(f"{k}={out[k]:.3f}" for k in ("along+", "along-", "across+", "across-", "speed+", "speed-"))
              + "  | goal: " + (" ".join(f"{k}={out[k + '_goal']:.3f}" for k in ("along+", "along-", "across+", "across-"))
                                if out["goal_at_zero"] else "n/a"))

print("\n== summary (safety only; 0 means the first grid value already fails) ==")
for label in ("entry", "commit"):
    sel = [r for r in rows if r["point"] == label]
    print(f"{label}: n={len(sel)}")
    for k in ("along+", "along-", "across+", "across-", "speed+", "speed-"):
        values = [r[k] for r in sel]
        below = sum(v < .02 for v in values) if not k.startswith("speed") else sum(v < .02 for v in values)
        print(f"   {k:8s} min={min(values):.3f} median={statistics.median(values):.3f} "
              f"count(<0.02)={below} count(<0.08)={sum(v < .08 for v in values)}")
    tightest = [min(r["along+"], r["along-"], r["across+"], r["across-"]) for r in sel]
    print(f"   tightest position margin per case: min={min(tightest):.3f} median={statistics.median(tightest):.3f} "
          f"count(<0.02)={sum(t < .02 for t in tightest)} count(<0.08)={sum(t < .08 for t in tightest)}")
