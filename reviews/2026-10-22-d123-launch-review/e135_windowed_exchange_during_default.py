"""E135：D123-2 实现的 local_not_dispatched 观察交换，在安全默认执行期内到达时身体会怎样。

上一轮 E130 发现：默认执行期内若接纳一条普通（无窗口）neutral 请求，会清空整份安排，
972 个入口在 tick 27–32 接纳时全部掉落。7f1a95a2 的 `PlayerRuntimeV1.complete_local_not_dispatched`
改为发送带首效窗口的 neutral：窗口 = `[activation_tick + valid_for_ticks]`（默认结束后的第一拍）。
本脚本只调用项目函数：
  - 客户端：项目 `ClientTimingSampler`，真实 `ActionSnapshotV1`／`SafeDefaultV1`／`MovementTickWindowV1`；
  - 物理：D120 冻结的四方向 972 个落地入口与项目 `physics_1_21.step`。
父请求与 E130 相同（tick 26 采样前进+疾跑，8 拍潜行默认，activation 27）。
对照三组，各在 tick 27…35 接纳：
  plain     —— E130 的普通 neutral；
  windowed  —— 与 `complete_local_not_dispatched` 相同的窗口 neutral，窗口 [35,35]；
另报告 windowed 组在采样器里产生的事件序列（检查 default_exhausted 是否出现）。
用法：python e135_windowed_exchange_during_default.py   （在检出根目录运行）
"""
import collections
import itertools
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementTickWindowV1, MovementV1, SafeDefaultV1  # noqa: E402
from mc2p.motion_nav.physics_1_21 import step  # noqa: E402
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput  # noqa: E402
from scripts import d120_momentum_entry_calibration as calibration  # noqa: E402
from tests.sim.client_timing import ClientTimingSampler  # noqa: E402

TICK_NS = 50_000_000
L = 26
DEFAULT_TICKS = 8
EXCHANGE_TICK = L + 1 + DEFAULT_TICKS
DEADLINE = 10**15


def tick_inputs(kind, admit_tick):
    sampler = ClientTimingSampler("ep")
    parent = ActionSnapshotV1(
        "ep", 25, 0, DEADLINE, movement=MovementV1(forward=1, sprint=True), valid_for_ticks=1,
        safe_default=SafeDefaultV1(L + 1, MovementV1(sneak=True), DEFAULT_TICKS, DEFAULT_TICKS * TICK_NS))
    sampler.admit(parent, L, L * TICK_NS, 0)
    sampler.sample(L, L * TICK_NS)
    movements, states = [], []
    for tick in range(L + 1, L + 41):
        if tick == admit_tick:
            window = (MovementTickWindowV1(EXCHANGE_TICK, EXCHANGE_TICK) if kind == "windowed" else None)
            exchange = ActionSnapshotV1("ep", 26, 0, DEADLINE, movement=MovementV1(), first_effect_window=window)
            sampler.admit(exchange, tick, tick * TICK_NS, 0)
        selected = sampler.sample(tick, tick * TICK_NS)
        movements.append(selected.movement)
        states.append(selected.state)
    events = [(e.status.value, e.movement_tick_id, e.request_sequence_id) for e in sampler.events]
    return tuple(movements), states, events


def classify(entry, fixture, movements):
    feet = fixture.context.feet_y
    middle = (fixture.trial.first_gap_cells, fixture.trial.first_gap_cells + fixture.trial.middle_platform_cells)
    state = entry
    for movement in movements:
        values = (float(movement.forward), float(movement.strafe), movement.jump, movement.sneak, movement.sprint)
        result = step(state, TickInput(*values, entry.yaw_radians), fixture.context.world, JAVA_1_21_RULESET)
        if result.status is not CalculationStatus.OK:
            return "fell"
        state = result.next_state
        if state.position[1] < feet - 3:
            return "fell"
        _, z, _, _ = calibration._axes(state, fixture)
        speed = math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
        on_floor = state.on_ground and abs(state.position[1] - feet) < 1e-7
        if on_floor and speed < .003 and middle[0] - .3 < z < middle[1] + .3:
            return "middle"
    return "other"


def main():
    plans = {f"{kind}_admitted_at_tick_{t}": (kind, t) for kind in ("plain", "windowed")
             for t in range(L + 1, EXCHANGE_TICK + 1)}
    inputs = {name: tick_inputs(*plan) for name, plan in plans.items()}
    totals = {name: collections.Counter() for name in plans}
    entries = 0
    for direction in range(4):
        fixture = calibration.build_fixture(direction)
        nominal = calibration.nominal_entry(fixture)
        for offsets in itertools.product(*calibration.AXES):
            entries += 1
            entry = calibration.perturb_entry(nominal, offsets, direction)
            for name in plans:
                totals[name][classify(entry, fixture, inputs[name][0])] += 1
    print(json.dumps({
        "entries": entries,
        "exchange_window": [EXCHANGE_TICK, EXCHANGE_TICK],
        "outcomes": {name: dict(counter) for name, counter in totals.items()},
        "windowed_states_ticks_27_to_37": {name: inputs[name][1][:11] for name in plans if name.startswith("windowed")},
        "windowed_events": {name: inputs[name][2] for name in plans if name.startswith("windowed")},
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
