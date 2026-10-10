"""连续运动方法建议的成本探针：如果输入方案不在线搜索，在线只做一次复核，要多少 physics step。

运行：在 ebc4129 仓库根目录，
PYTHONPATH=.:reviews/2026-10-10-tp-5ar-r2-review python <本文件> [场景名 ...]

对每个冻结正例：先用 steer_probe（地面限制＋着地起跳）找到输入，模拟“离线表已经给出方案”；
再用一个新的计数器，只做在线必须做的事并分别计数：
- rollout：两支时序各执行一次候选；
- goal：逐支 _goal_check；
- scan：完整 scan_commitment（含两支重放、每个边界的停车尾迹和风险区间）。
另报告扫描里停车尾迹的 tick 数和边界数。

lazy 列估算“逐次提交时才证明”的单次决策成本（取自同一份扫描证明，不另算物理）：
- per_tick：提交一条非承诺命令前，只需下一边界在两支上的停车尾迹，取全程最大值；
- commit：提交越过承诺点的命令前，需要该风险区间恢复边界在两支上的停车尾迹，取最大值；
两者都不含计划 rollout（已在 rollout 列）。这是测量工具，不是建议的实现。
"""
from dataclasses import replace
import sys
import time

from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence, _goal_check
from experiments.motion_navigation.trajectory_proto.scenarios import primitive_fixture

import steer_probe
from tier_cost_probe import POSITIVES

UNBOUNDED = SearchBudget(10_000_000, 100_000_000, 40, 2)


def online_cost(request, world, inputs):
    request = replace(request, budget=UNBOUNDED)
    counter = CountedPhysics(UNBOUNDED)
    started = time.perf_counter()
    finals = []
    for state in request.entry_states:
        for command in inputs:
            state = counter.step(state, command, world).next_state
        finals.append(state)
    rollout = counter.physics_steps
    for branch, state in zip(request.timing_branches, finals):
        check, _ = _goal_check(request, branch, state, world, counter)
        assert check is not None and check.accepted
    goal = counter.physics_steps - rollout
    scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs), counter=counter)
    assert scan.status is ScanStatus.VERIFIED_CANDIDATE, scan.status
    elapsed = (time.perf_counter() - started) * 1000.
    tails = sum(len(b.tails) for b in scan.proof.branches)
    branches = scan.proof.branches
    committed = {r.first_committed_boundary for b in branches for r in b.risk_intervals}
    per_tick = max(sum(len(b.tails[k].inputs) for b in branches)
                   for k in range(len(branches[0].tails)) if k not in committed)
    commit = max((sum(len(b.tails[r.recovered_boundary].inputs) for b in branches)
                  for r in branches[0].risk_intervals), default=0)
    intervals = len(branches[0].risk_intervals)
    return (rollout, goal, counter.physics_steps - rollout - goal, counter.counts.tail_ticks, tails, elapsed,
            per_tick, commit, intervals)


if __name__ == "__main__":
    steer_probe.GROUNDED_JUMP = True
    wanted = set(sys.argv[1:])
    seen = set()
    for name, tier in POSITIVES:
        if (wanted and name not in wanted) or (name, tier) in seen:
            continue
        seen.add((name, tier))
        fixture = primitive_fixture(name, tier)
        status, found, _, _ = steer_probe.steer_search(fixture.request, fixture.world)
        assert status == "found", (name, status)
        (rollout, goal, scan, tail_ticks, tails, elapsed,
         per_tick, commit, intervals) = online_cost(fixture.request, fixture.world, found)
        print(f"{name:30s} {tier:4s} ticks={len(found):2d} rollout={rollout:3d} goal={goal} scan={scan:4d} "
              f"total={rollout + goal + scan:4d} tail_ticks={tail_ticks:4d} tails={tails:3d} {elapsed:.0f} ms "
              f"| lazy per_tick={per_tick:3d} commit={commit:3d} intervals={intervals}", flush=True)
