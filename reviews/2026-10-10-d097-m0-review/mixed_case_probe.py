"""D097 M0 审查探针：混合动作元反例为什么要 10,056 次 step，以及这笔成本属于在线还是离线。

运行：在 be38685 仓库根目录，PYTHONPATH=. python <本文件>
只读调用 m0_probe.run_case（与 M0 正式量尺同一份代码）：
1. 原样运行混合反例（正式口径与高度截断诊断口径），复现 physics step；
2. 把同一夹具的目标移到不可达处，量出同一网格“完整耗尽”的 step，
   看混合反例的解排在枚举顺序的什么位置；
3. 只按 D097 在线路径计算：已知参数时的两支 rollout（≤3 组参数时取 3 倍上界）。
这是测量工具，不是建议的实现。
"""
from dataclasses import replace

from experiments.motion_navigation.trajectory_proto import m0_probe
from mc2p.motion_nav.world_model import Aabb

ORIGINAL = m0_probe._fixture_for


def unreachable_fixture(scenario_id, tier_id):
    if scenario_id != "mixed_unreachable":
        return ORIGINAL(scenario_id, tier_id)
    fixture = m0_probe.build_mixed_action_fixture().fixture
    goal = replace(fixture.request.goal, region=Aabb(.5 - 1.e-9, 1., 15.5 - 1.e-9,
                                                     .5 + 1.e-9, 1.05, 15.5 + 1.e-9))
    return replace(fixture, request=replace(fixture.request, goal=goal))


def label(command):
    if command.jump:
        return "SJ" if command.sprint else "J"
    if command.sprint:
        return "S"
    return "W" if command.forward or command.strafe else "N"


if __name__ == "__main__":
    m0_probe._fixture_for = unreachable_fixture
    mixed = m0_probe.build_mixed_action_fixture()
    for name, options in (("formal", m0_probe.FORMAL_OPTIONS),
                          ("floor_diagnostic", m0_probe.FLOOR_DIAGNOSTIC_OPTIONS)):
        found = m0_probe.run_case("mixed_ground_jump_air", None, options=options)
        exhausted = m0_probe.run_case("mixed_unreachable", None, options=options)
        shape = " ".join(label(c) for c in found.inputs)
        print(f"{name:17s} mixed: {found.status.value} steps={found.physics_steps} "
              f"completions={found.completed_candidates} scans={found.scan_count} "
              f"same_as_expected={found.inputs == mixed.expected_inputs}")
        print(f"{'':17s} same grid, unreachable goal: {exhausted.status.value} "
              f"steps={exhausted.physics_steps} completions={exhausted.completed_candidates} "
              f"-> mixed solution found after {found.physics_steps / exhausted.physics_steps:.1%} "
              f"of full-grid steps")
        print(f"{'':17s} inputs: {shape}")
    cost = m0_probe.measure_known_input("mixed_ground_jump_air", None, mixed.expected_inputs)
    print(f"online path with known parameters: rollout={cost.rollout_steps} steps "
          f"(<=3 parameter sets: <= {3 * cost.rollout_steps}), lazy per-tick={cost.estimated_per_tick_steps}, "
          f"commit={cost.estimated_commit_steps}, full scan (offline/acceptance only)={cost.full_scan_steps}")
