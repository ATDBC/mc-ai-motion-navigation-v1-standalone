"""E24：R4 失败在 B10 旧夹具的“带速入口准备器”。它对“一次观察跨两个 movement tick”有多敏感。

复刻 `scripts/b10_gap_solver_runtime.py` 中 `prepare_moving_entry` 的控制律（逐字搬用打分公式、
四个离散输入、80 步上限、成功窗口 along∈[-0.18, 0.05]、速度带、着地；along>0.07 判越过），
在 jump_gap_1 夹具的地面上离线运行，物理用项目的 `physics_1_21.step`。
每一步按概率 p 让本次观察跨两个 movement tick：第二个 tick 施加中性输入（对应 D105 根因记录中的
`lease_exhausted` 中性 tick），或者保持同一输入（另一种可能的客户端行为），两种都测。
对三个速度带、p∈{0, 0.05, 0.1, 0.2} 各运行 400 次，统计 80 步内成功率和越过入口次数。
另统计速度带 (0.5, 1.0) 每次“在带内”的连续 tick 数，说明窗口有多窄。
只调用项目函数，不改项目文件。运行：PYTHONPATH=<D106 检出> python e24_r4_approach_fragility.py
"""
import random
from dataclasses import replace

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture

CONTROLS = (MovementV1(), MovementV1(forward=1), MovementV1(forward=1, sprint=True), MovementV1(forward=-1))
BANDS = (((.5, 1.0), .75), ((1.0, 2.0), 1.5), ((2.0, 3.0), 2.5))
ORIGIN_Z = 0  # 南向：dx=0, dz=1；入口格 (0, 0, 0)，缺口在 z=1


def advance(state, movement, world):
    projected = project_movement_command(state, movement, movement_yaw_radians=state.yaw_radians)
    if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
        return None
    calculated = physics_step(state, projected.tick_input, world, JAVA_1_21_RULESET)
    return calculated.next_state if calculated.next_state is not None and calculated.status.name == "OK" else None


def along_speed(state):
    return state.position[2] - (ORIGIN_Z + .5), 20.0 * state.velocity_blocks_per_tick[2]


def choose(state, world, band, target_speed):
    along, _ = along_speed(state)
    weight = 5.0 if along > -.5 else 1.5
    ranked = []
    for movement in CONTROLS:
        nxt = advance(state, movement, world)
        if nxt is None:
            continue
        next_along, next_speed = along_speed(nxt)
        score = abs(next_along - (-.05)) * weight + abs(next_speed - target_speed) * .8
        if next_along > .06:
            score += 100.0 + (next_along - .06) * 100.0
        if next_speed < -.01:
            score += 20.0
        ranked.append((score, movement))
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1] if ranked else None


def run(start, world, band, target_speed, p, second_tick, rng):
    state = start
    for _ in range(80):
        along, speed = along_speed(state)
        if -.18 <= along <= .05 and band[0] - 1e-6 <= speed <= band[1] + 1e-6 and state.on_ground:
            return "ok"
        if along > .07:
            return "passed"
        movement = choose(state, world, band, target_speed)
        if movement is None:
            return "no_input"
        state = advance(state, movement, world)
        if state is None:
            return "physics"
        if rng.random() < p:
            state = advance(state, MovementV1() if second_tick == "neutral" else movement, world)
            if state is None:
                return "physics"
    return "timeout"


def main():
    fixture = representative_fixture("jump_gap_1")
    world = fixture.world
    anchor = fixture.request.anchor_state
    start = replace(anchor, position=(.5, anchor.position[1], ORIGIN_Z + .5 - 3.0),
                    velocity_blocks_per_tick=(0.0, anchor.velocity_blocks_per_tick[1], 0.0))
    print("== 1. 速度带 (0.5, 1.0) 有多窄：从静止按住前进若干 tick 再松开，带内连续 tick 数")
    for hold in (1, 2, 3, 6):
        state, speeds = start, []
        for i in range(hold + 6):
            state = advance(state, MovementV1(forward=1) if i < hold else MovementV1(), world)
            speeds.append(round(along_speed(state)[1], 3))
        print(f"  前进 {hold} tick 后松开：速度序列 {speeds}；带内 tick 数 {sum(.5 <= v <= 1.0 for v in speeds)}")

    print("\n== 2. 准备器成功率（每格 400 次）")
    print(f"  {'速度带':12s} {'第二 tick':8s} " + " ".join(f"p={p:<5}" for p in (0, .05, .1, .2)))
    for band, target in BANDS:
        for second in ("neutral", "held"):
            cells = []
            for p in (0, .05, .1, .2):
                rng = random.Random(21001)
                outcomes = [run(start, world, band, target, p, second, rng) for _ in range(400 if p else 1)]
                ok = sum(o == "ok" for o in outcomes) / len(outcomes)
                passed = sum(o == "passed" for o in outcomes)
                cells.append(f"{ok:5.0%}/{passed:<3d}")
            print(f"  {str(band):12s} {second:8s} " + " ".join(cells))
    print("  （格式：成功率/越过入口次数；p=0 为确定性单次运行）")


if __name__ == "__main__":
    main()
