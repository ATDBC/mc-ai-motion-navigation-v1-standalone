"""E130：预排默认执行途中，若客户端收到一条普通 neutral 请求，身体会怎样。

D123-2 计划在 `local_not_dispatched` 后“另发一条有自己 sequence 的 observation-only／neutral
exchange 取得新观察”。现行 v2 合同里，普通请求被接纳时会清空整份安排（Java
`ClientBehaviorExecutor` 的 `input.clearTiming()`；计算器 `tests/sim/client_timing.py` 的
`ClientTimingSampler._activate` → `clear()`）。本脚本只调用项目函数：
  - 客户端：项目的 `ClientTimingSampler`，真实 `ActionSnapshotV1`／`SafeDefaultV1`；
  - 物理：D120 冻结的四方向 972 个落地入口与项目 `physics_1_21.step`。
父请求在 tick L=26 采样（前进+疾跑，valid_for_ticks=1），带 8 拍潜行默认，activation tick 27；
入口状态已是 tick 26 采样之后的状态，所以从 tick 27 起把 sampler 选出的输入送进物理。
对照：neutral 请求在 tick 27+k 被接纳（k=0…8），以及从不插入。
结果分类与 E123 相同：停在中间平台、掉落、other（40 tick 内无结论）。
用法：python e130_default_clear_by_plain_request.py   （在检出根目录运行）
"""
import collections
import itertools
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1, SafeDefaultV1  # noqa: E402
from mc2p.motion_nav.physics_1_21 import step  # noqa: E402
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput  # noqa: E402
from scripts import d120_momentum_entry_calibration as calibration  # noqa: E402
from tests.sim.client_timing import ClientTimingSampler  # noqa: E402

TICK_NS = 50_000_000
L = 26
DEFAULT_TICKS = 8
DEADLINE = 10**15


def tick_inputs(interrupt_tick):
    """Movements the project sampler selects for ticks 27..66, plus state names."""
    sampler = ClientTimingSampler("ep")
    parent = ActionSnapshotV1(
        "ep", 25, 0, DEADLINE, movement=MovementV1(forward=1, sprint=True), valid_for_ticks=1,
        safe_default=SafeDefaultV1(L + 1, MovementV1(sneak=True), DEFAULT_TICKS,
                                   DEFAULT_TICKS * TICK_NS))
    sampler.admit(parent, L, L * TICK_NS, 0)
    sampler.sample(L, L * TICK_NS)  # 已体现在 D120 落地入口里
    movements, states = [], []
    for tick in range(L + 1, L + 41):
        if tick == interrupt_tick:
            plain = ActionSnapshotV1("ep", 26, 0, DEADLINE, movement=MovementV1(), valid_for_ticks=1)
            sampler.admit(plain, tick, tick * TICK_NS, 0)
        selected = sampler.sample(tick, tick * TICK_NS)
        movements.append(selected.movement)
        states.append(selected.state)
    return tuple(movements), tuple(states)


def classify(entry, fixture, movements):
    feet = fixture.context.feet_y
    middle = (fixture.trial.first_gap_cells, fixture.trial.first_gap_cells + fixture.trial.middle_platform_cells)
    state = entry
    for movement in movements:
        values = (float(movement.forward), float(movement.strafe), movement.jump, movement.sneak,
                  movement.sprint)
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
    plans = {f"neutral_admitted_at_tick_{L + 1 + k}": L + 1 + k for k in range(DEFAULT_TICKS + 1)}
    plans["never_interrupted"] = None
    inputs = {name: tick_inputs(tick) for name, tick in plans.items()}
    totals = {name: collections.Counter() for name in plans}
    for direction in range(4):
        fixture = calibration.build_fixture(direction)
        nominal = calibration.nominal_entry(fixture)
        for offsets in itertools.product(*calibration.AXES):
            entry = calibration.perturb_entry(nominal, offsets, direction)
            for name in plans:
                totals[name][classify(entry, fixture, inputs[name][0])] += 1
    print(json.dumps({
        "entries": 4 * len(list(itertools.product(*calibration.AXES))),
        "default_ticks": DEFAULT_TICKS,
        "sampled_states_ticks_27_to_36": {name: list(inputs[name][1][:10]) for name in plans},
        "outcomes": {name: dict(counter) for name, counter in totals.items()},
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
