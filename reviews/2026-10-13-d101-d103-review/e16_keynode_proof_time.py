"""E16：只在关键节点证明停车尾迹时，D101 M0 的动作开始证明要多久？

同一动作 jump_gap_1:A3、同一机器、两条起点分支，比较：
- full：项目 scan_commitment（每个边界一条尾迹，D101 M0 的初始证明）；
- key：同样的两条主轨迹（含晚一 tick 等待前缀），只在关键边界调用项目的 _tail：
  入口 0、最后可放弃点、起跳后首次着地到恢复点之间的每个边界、终点。
  关键边界从 full 的风险区间读取（实际实现可从“首个跳跃输入”和“着地”直接得到）。
key 只是计时口径，不是新的证明器；它复用 commitment._prove_entry／_tail，不改项目文件。
Windows 估算 = D101 M0 Windows 初始证明 P50 48.35 ms × (key / full)。
运行：PYTHONPATH=<项目根> python e16_keynode_proof_time.py
"""
import json, statistics, time
from pathlib import Path
from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto import commitment as C
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from mc2p.motion_nav.physics_types import TickInput

ROOT = Path(m0_probe.__file__).resolve().parents[3]
fixture = m0_probe._fixture_for("jump_gap_1", "A3")
frozen = json.loads((ROOT / "experiments/motion_navigation/trajectory_proto/data/d097a-set-a-sample.json").read_text())
inputs = tuple(TickInput(*row) for row in frozen["inputs"]["jump_gap_1:A3"])
request, world = fixture.request, fixture.world
evidence = _boundary_evidence(request, inputs)
full = C.scan_commitment(request, inputs, world, evidence)
assert full.status is C.ScanStatus.VERIFIED_CANDIDATE
key_boundaries = []
for branch in full.proof.branches:
    risk, = branch.risk_intervals
    airborne = next(i for i in range(risk.first_committed_boundary, len(branch.states)) if not branch.states[i].on_ground)
    landed = next(i for i in range(airborne, len(branch.states)) if branch.states[i].on_ground)
    nodes = {0, len(inputs), risk.last_abandon_boundary, *range(landed, risk.recovered_boundary + 1)}
    key_boundaries.append(tuple(sorted(nodes)))


def key_only():
    counter = CountedPhysics(request.budget)
    options = C.ScanOptions()
    for index, nodes in enumerate(key_boundaries):
        prelude, _ = C._prove_entry(counter, request, index, world)
        states, deps = [prelude[-1]], set()
        for command in inputs:
            states.append(C._calculated(counter, states[-1], command, world, deps))
        minimum_y = min(states[0].position[1], states[-1].position[1])
        for b in nodes:
            tail = C._tail(counter, b, tuple(prelude[:-1]) + tuple(states[:b + 1]), inputs[b:b + 1],
                           request.stop_input, world, minimum_y,
                           request.task_damage_budget.maximum_expected_damage_points, options)
            expected = full.proof.branches[index].tails[b].status
            assert tail.status is expected, (index, b, tail.status, expected)
    return counter.physics_steps


def timed(fn, n=15):
    out, value = [], None
    for _ in range(n):
        t = time.perf_counter_ns(); value = fn(); out.append((time.perf_counter_ns() - t) / 1e6)
    return statistics.median(out), value


t_full, _ = timed(lambda: C.scan_commitment(request, inputs, world, evidence))
t_key, steps_key = timed(key_only)
print(f"key boundaries per branch: {key_boundaries}")
print(f"full scan_commitment: {full.counts.physics_steps} physics steps, Linux median {t_full:.1f} ms")
print(f"key-node tails only : {steps_key} physics steps, Linux median {t_key:.1f} ms "
      f"({100 * t_key / t_full:.0f}% of full)")
print(f"Windows estimate from D101 M0 (48.35 ms for the full proof): key-node ~ {48.35 * t_key / t_full:.1f} ms")
