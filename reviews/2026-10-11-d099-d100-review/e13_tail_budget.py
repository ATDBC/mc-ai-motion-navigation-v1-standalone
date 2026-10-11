"""E13：性能到底花在哪里？逐 tick 比较，还是逐边界证明停车尾迹？

对 28 个 A／C／D 样本，用项目自己的 scan_commitment 得到证明，然后只做记账（不新增物理计算）：
每条停车尾迹的物理步数 = 该尾迹的输入条数，主轨迹步数 = 输入条数。

比较几种“需要证明哪些停车尾迹”的口径：
- D098：两条时序分支，每个边界一条尾迹（scan 的实际计数）；
- ON_TIME：只证准时分支，每个边界一条尾迹；
- 包长 k（k=3、6）：只在包起点（地面且未承诺）、最后可放弃点、落地恢复点和终点证明尾迹；
- 仅关键节点：入口、最后可放弃点、落地恢复点和终点。

“承诺决定”需要的步数：
- D098／ON_TIME：承诺前必须证完整个动作（剩余主轨迹＋剩余全部尾迹）；
- D099 口径：只需从承诺点到落地恢复点的主轨迹＋恢复点尾迹（之后在安全地面上按包继续证明）。
运行：PYTHONPATH=<项目根> python e13_tail_budget.py
"""
import math, statistics
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch
from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, TailStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.measurement import _set_a, _set_c, _set_d


def single(request):
    return replace(request, entry_states=request.entry_states[:1], timing_branches=(TimingBranch.ON_TIME,),
                   allowed_effect_ticks=request.allowed_effect_ticks[:1], branch_preludes=request.branch_preludes[:1])


def pct(values, q):
    values = sorted(values)
    return values[max(0, math.ceil(q * len(values)) - 1)]


cases = list(_set_a(4)) + list(_set_c(6)) + list(_set_d(18))
table = {k: [] for k in ("D098", "ON_TIME", "packet3", "packet6", "key_only")}
commit = {k: [] for k in ("D098_twobranch_full", "ON_TIME_full", "D099_to_landing")}
rows = []
for set_id, key, request, world, inputs, *_ in cases:
    both = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs))
    one_req = single(request)
    one = scan_commitment(one_req, inputs, world, _boundary_evidence(one_req, inputs))
    assert both.status is one.status is ScanStatus.VERIFIED_CANDIDATE, key
    branch = one.proof.branches[0]
    tails = branch.tails
    cost = {t.boundary: len(t.inputs) for t in tails}
    n = len(inputs)
    committed = set()
    key_nodes = {0, n}
    for risk in branch.risk_intervals:
        committed.update(range(risk.first_committed_boundary, risk.recovered_boundary))
        if risk.last_abandon_boundary is not None:
            key_nodes.add(risk.last_abandon_boundary)
        key_nodes.add(risk.recovered_boundary)
    def packet(k):
        chosen = {b for b in range(0, n + 1, k) if b not in committed} | key_nodes
        return n + sum(cost[b] for b in chosen)
    row = {"case": key, "ticks": n,
           "D098": both.counts.physics_steps, "ON_TIME": one.counts.physics_steps,
           "packet3": packet(3), "packet6": packet(6), "key_only": n + sum(cost[b] for b in key_nodes),
           "unsafe_tail_steps": sum(len(t.inputs) for t in tails if t.status is TailStatus.UNSAFE),
           "risk": [(r.last_abandon_boundary, r.first_committed_boundary, r.recovered_boundary)
                    for r in branch.risk_intervals]}
    for k in table:
        table[k].append(row[k])
    if branch.risk_intervals:
        risk = branch.risk_intervals[0]
        c = risk.last_abandon_boundary if risk.last_abandon_boundary is not None else 0
        both_tails = sum(len(t.inputs) for b in both.proof.branches for t in b.tails if t.boundary >= c)
        commit["D098_twobranch_full"].append(2 * (n - c) + both_tails)
        commit["ON_TIME_full"].append((n - c) + sum(cost[b] for b in cost if b >= c))
        commit["D099_to_landing"].append((risk.recovered_boundary - c) + cost[risk.recovered_boundary])
        row["commit_steps"] = {k: v[-1] for k, v in commit.items()}
    rows.append(row)
    print(row)

print("\n== whole-action proof steps (28 cases) ==")
for k, v in table.items():
    print(f"   {k:9s} median={statistics.median(v):6.1f} P95={pct(v, .95):5d} max={max(v):5d} sum={sum(v)}")
print("\n== steps that must be done at the commit decision (cases with a risk interval) ==")
for k, v in commit.items():
    print(f"   {k:20s} n={len(v)} median={statistics.median(v):6.1f} P95={pct(v, .95):5d} max={max(v):5d}")
gap = next(r for r in rows if r["case"] == "jump_gap_1:A3")
print("\njump_gap_1:A3:", gap)
