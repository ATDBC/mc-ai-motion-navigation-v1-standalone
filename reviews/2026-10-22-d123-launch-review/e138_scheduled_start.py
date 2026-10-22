"""E138：“预约起跳”——父动作按 anchor 后第 K+1 拍证明并绑定后继，是否与 anchor+1 等价、成本是否随 K 增长。

问题：D121/D122 的固定后继要求父动作窗口恰为一拍（`bind_fixed_successor` 拒绝 delayed variants），
正式 `_prove_delayed_starts` 又要求窗口从 anchor+1 开始。于是父证明只能登记在 anchor+1，
而证明工作本身（重放、绑定）发生在 anchor 之后。本实验检查另一种登记方式：
从同一个静止 anchor（tick 0）出发，先按 `_prove_delayed_starts` 同样的方式推进 K 拍 neutral 前奏，
再在 start_tick=K+1 重放同一组 26 条命令，并对结果调用正式 `bind_fixed_successor`。
只调用项目函数：`tests.motion_nav.test_d121_successor_binding.fixed_case`（D122 冻结父 proof）、
`project_movement_command`、`physics_1_21.step`、`_replay_verified_commands`、`bind_fixed_successor`。
对每个 K 报告：
  - 前奏是否为不动点（每拍状态除 movement_tick_id 外与 anchor 相同、着地、无碰撞变化）；
  - 重放出的主轨迹、fallback 尾段、放手安全点、依赖，是否与 K=0 在“忽略 tick 编号”后完全相同；
  - 后继绑定状态、后继窗口，以及后继输入与终态是否与 K=0 等价；
  - 前奏+重放、绑定各自耗时（单核）。
proof 对象里用 anchor_movement_tick_id=K 表示“预约起点”，只为本实验调用绑定函数；正式表示方式是设计问题，不在此决定。
用法：python e138_scheduled_start.py   （在检出根目录运行）
"""
import dataclasses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command  # noqa: E402
from mc2p.contracts.action_v1 import MovementV1  # noqa: E402
from mc2p.motion_nav.motion_solver import SolveStatus, _replay_verified_commands, bind_fixed_successor  # noqa: E402
from mc2p.motion_nav.online_motion import CandidateExecutionWindow  # noqa: E402
from mc2p.motion_nav.physics_1_21 import step  # noqa: E402
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, PhysicsState  # noqa: E402
from scripts import d123_dependency_probe as d123  # noqa: E402

KS = (0, 1, 5, 10, 20, 40)


def untimed(value):
    """Replace every PhysicsState.movement_tick_id by 0, recursively, so shifted proofs compare equal."""
    if isinstance(value, PhysicsState):
        return dataclasses.replace(value, movement_tick_id=0)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple((f.name, untimed(getattr(value, f.name))) for f in dataclasses.fields(value)
                     if f.name not in ("start_tick", "boundary_index_tick"))
    if isinstance(value, (tuple, list)):
        return tuple(untimed(item) for item in value)
    return value


def prelude(entry, world, ticks):
    current, dependencies, fixed_point = entry, set(), True
    for _ in range(ticks):
        projected = project_movement_command(current, MovementV1(), movement_yaw_radians=current.yaw_radians)
        if projected.status is not ProjectionStatus.READY:
            raise RuntimeError(projected.reasons)
        calculated = step(current, projected.tick_input, world, JAVA_1_21_RULESET)
        if calculated.status is not CalculationStatus.OK:
            raise RuntimeError(calculated.status)
        dependencies.update(calculated.dependencies)
        current = calculated.next_state
        fixed_point &= (dataclasses.replace(current, movement_tick_id=entry.movement_tick_id) == entry)
    return current, tuple(sorted(dependencies)), fixed_point


def main():
    request, _fixture, template, spec, snapshot = d123.fixed_case(0)
    world = d123.world_for(snapshot, {})
    entry = template.entry_state
    rows, base = {}, None
    for k in KS:
        started = time.perf_counter_ns()
        state, prelude_dependencies, fixed_point = prelude(entry, world, k)
        start_tick = entry.movement_tick_id + k + 1
        window = CandidateExecutionWindow(start_tick, start_tick)
        request_k = dataclasses.replace(request, execution_window=window)
        variant, failure = _replay_verified_commands(state, template.commands, world, request_k,
                                                     start_tick=start_tick,
                                                     inherited_dependencies=prelude_dependencies)
        replay_ms = (time.perf_counter_ns() - started) / 1e6
        if failure is not None:
            rows[str(k)] = {"replay": failure.status.value, "reasons": list(failure.reasons)}
            continue
        proof = dataclasses.replace(
            template, anchor_movement_tick_id=state.movement_tick_id, entry_state=state,
            tick_inputs=variant.tick_inputs, trajectory=variant.trajectory, step_events=variant.step_events,
            exit_state=variant.exit_state, release_safe_command_indices=variant.release_safe_command_indices,
            world_dependencies=variant.world_dependencies,
            resource_incomplete_reasons=variant.resource_incomplete_reasons,
            execution_window=window, fallback_tails=variant.fallback_tails, successor_binding=None)
        started = time.perf_counter_ns()
        bound = bind_fixed_successor(proof, request_k, spec, snapshot, damage_remaining_points=0.)
        bind_ms = (time.perf_counter_ns() - started) / 1e6
        binding = bound.proof.successor_binding if bound.status is SolveStatus.SOLVED else None
        summary = {
            "trajectory": untimed(variant.trajectory),
            "tick_inputs": untimed(variant.tick_inputs),
            "fallback_tails": untimed(variant.fallback_tails),
            "release_safe": variant.release_safe_command_indices,
            "dependencies": variant.world_dependencies,
            "successor_inputs": None if binding is None else untimed(binding.expected_inputs),
            "successor_exit": None if binding is None else untimed(binding.exit_state),
        }
        if base is None:
            base = summary
        rows[str(k)] = {
            "prelude_fixed_point": fixed_point,
            "start_tick": start_tick,
            "exit_tick": variant.exit_state.movement_tick_id,
            "same_as_k0_ignoring_tick_ids": {key: summary[key] == base[key] for key in summary},
            "successor_binding": bound.status.value,
            "successor_first_start_tick": None if binding is None else binding.first_start_tick,
            "successor_window": None if binding is None else [binding.requested_window.earliest_start_tick,
                                                              binding.requested_window.latest_start_tick],
            "prelude_plus_replay_ms": round(replay_ms, 1),
            "bind_fixed_successor_ms": round(bind_ms, 1),
        }
    print(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
