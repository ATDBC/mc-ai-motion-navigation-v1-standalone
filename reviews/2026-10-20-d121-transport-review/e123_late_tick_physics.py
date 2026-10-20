"""E123：L+1 那一拍客户端实际用了什么，决定了迟到之后还能不能安全。

只调用项目函数：scripts/d120_momentum_entry_calibration 的 build_fixture、nominal_entry、
perturb_entry、AXES，以及项目的 physics_1_21.step；世界用 D120 夹具自带的 PhysicsWorldView。
入口取 D120 冻结的四方向 972 个落地 post-state（每方向 243 个联合扰动）。

对每个入口比较 L+1、L+2 两拍的输入组合，之后按组合规定继续（最多 40 tick）：
  sneak_then_sneak     L+1 潜行（客户端预先排好的默认），之后一直潜行            —— 预排默认、后继未到
  neutral_then_sneak   L+1 不按键（现行租约耗尽），L+2 起潜行                    —— 现行协议、迟到一拍的回退
  neutral_then_neutral L+1 不按键，之后一直不按键                               —— Task 5 实际发生的情况
  sneak_then_neutral   L+1 潜行，L+2 起不按键                                    —— 预排默认生效后，Python 错拍时又改发 neutral
  neutral_then_jump    L+1 不按键，L+2 起跳（前进+疾跑+跳），空中前进+疾跑         —— 现行协议、后继迟到一拍
  sneak_then_jump      L+1 潜行，L+2 起跳，空中前进+疾跑                         —— 预排默认、但过期后继仍被采样
  run_then_jump        L+1 前进+疾跑，L+2 起跳                                   —— D120 的 landing-immediate-late1
  run_then_sneak       L+1 前进+疾跑，L+2 起潜行                                 —— 若默认改成保持动量、后继又没到
  jump_on_time         L+1 起跳                                                —— 正常后继
另报告“从 L+1 起潜行 k 拍、之后不按键”对 k 的结果，用来确定预排默认至少要覆盖多少拍。
结果分四类：停在中间平台（着地、水平速度 < 0.003 且中心不越过边缘 0.3 格）、落到目标平台、掉落
（低于平台 3 格或越出声明世界）、other（40 tick 内没有以上结论）。
用法：python e123_late_tick_physics.py
"""
import collections
import itertools
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav.physics_1_21 import step  # noqa: E402
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput  # noqa: E402
from scripts import d120_momentum_entry_calibration as calibration  # noqa: E402

RUN = (1., 0., False, False, True)
JUMP = (1., 0., True, False, True)
NEUTRAL = (0., 0., False, False, False)
SNEAK = (0., 0., False, True, False)
PLANS = {
    "sneak_then_sneak": (SNEAK, SNEAK, "sneak"),
    "neutral_then_sneak": (NEUTRAL, SNEAK, "sneak"),
    "neutral_then_neutral": (NEUTRAL, NEUTRAL, "neutral"),
    "sneak_then_neutral": (SNEAK, NEUTRAL, "neutral"),
    "neutral_then_jump": (NEUTRAL, JUMP, "flight"),
    "sneak_then_jump": (SNEAK, JUMP, "flight"),
    "run_then_jump": (RUN, JUMP, "flight"),
    "run_then_sneak": (RUN, SNEAK, "sneak"),
    "jump_on_time": (JUMP, None, "flight"),
}
HOLDS = (1, 2, 4, 6, 7, 8, 9, 10, 12, 16, 20)


def classify(entry, fixture, plan, sneak_hold=None):
    first, second, mode = plan
    feet = fixture.context.feet_y
    world = fixture.context.world
    middle = (fixture.trial.first_gap_cells, fixture.trial.first_gap_cells + fixture.trial.middle_platform_cells)
    target_start = fixture.trial.first_gap_cells + fixture.trial.middle_platform_cells + fixture.trial.second_gap_cells
    state, took_off, landed_target = entry, False, False
    for index in range(40):
        if sneak_hold is not None:
            values = SNEAK if index < sneak_hold else NEUTRAL
        elif index == 0:
            values = first
        elif index == 1 and second is not None:
            values = second
        elif mode == "flight":
            values = NEUTRAL if landed_target else RUN
        else:
            values = SNEAK if mode == "sneak" else NEUTRAL
        result = step(state, TickInput(*values, entry.yaw_radians), world, JAVA_1_21_RULESET)
        if result.status is not CalculationStatus.OK:
            return "fell"  # 越出声明世界即已掉出平台高度范围
        state = result.next_state
        _, z, _, _ = calibration._axes(state, fixture)
        if values[2] and not state.on_ground:
            took_off = True
        if state.position[1] < feet - 3:
            return "fell"
        speed = math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
        on_floor = state.on_ground and abs(state.position[1] - feet) < 1e-7
        if on_floor and took_off and z - .3 < target_start + 6 and z + .3 > target_start:
            landed_target = True
        if on_floor and speed < .003:
            if landed_target:
                return "target"
            if middle[0] - .3 < z < middle[1] + .3 and not took_off:
                return "middle"
            # 速度已低于阈值但中心越过平台边缘 0.3 格：继续模拟，下一拍通常失去支撑。
    return "other"


def main():
    manifest = calibration.build_manifest()
    totals = {name: collections.Counter() for name in PLANS}
    holds = {k: collections.Counter() for k in HOLDS}
    for direction in range(4):
        fixture = calibration.build_fixture(direction)
        nominal = calibration.nominal_entry(fixture)
        for offsets in itertools.product(*calibration.AXES):
            entry = calibration.perturb_entry(nominal, offsets, direction)
            for name, plan in PLANS.items():
                totals[name][classify(entry, fixture, plan)] += 1
            for k in HOLDS:
                holds[k][classify(entry, fixture, PLANS["sneak_then_neutral"], sneak_hold=k)] += 1
    print(json.dumps({
        "manifest_sha256": manifest["sha256"],
        "entries": 4 * len(list(itertools.product(*calibration.AXES))),
        "outcomes": {name: dict(counter) for name, counter in totals.items()},
        "sneak_k_ticks_then_neutral": {str(k): dict(counter) for k, counter in holds.items()},
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
