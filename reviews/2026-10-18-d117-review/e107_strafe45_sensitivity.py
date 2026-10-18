"""E107：把空中输入换成原版的“45° 侧移”后，静止起跳与连续接续的结论是否改变。

复用 E98 的地形和计算器（只调用项目的 step()）。原版输入向量先乘 0.98、长度超过 1 时再归一化，
所以“前进+侧移、朝向偏 45°”在空中比单按前进多约 2% 加速度；起跳加速仍按起跳 tick 的朝向。
这里起跳 tick 仍正对 +z（拿满起跳加速），空中每 tick 改用 TickInput(1, 1, 跳=否, 疾跑=是, yaw=45°)，
合成方向沿 +z。对平地 W1=1、P=1/2、W2=3/4/5 比较：
  - 静止起跳（E98 策略 A，起点和起跳 tick 全搜索）在两种空中输入下是否可过；
  - 连续接续 k=0／k=1（前进+疾跑）在两种空中输入下成功的第一跳落点数。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e98_momentum_feasibility as base  # noqa: E402
from mc2p.motion_nav.physics_types import TickInput  # noqa: E402

STRAFE45 = TickInput(1, 1, False, False, True, math.pi / 4)
_straight_fly = base.Course.fly


def _fly45(self, state, limit=40):
    for _ in range(limit):
        state = self.advance(state, STRAFE45)
        if state is None or state.position[1] < 62.0:
            return None
        if state.on_ground:
            return state
    return None


def evaluate(p, w2):
    course = base.Course(1, p, w2, 0)
    out = {}
    for name, fly in (("straight", _straight_fly), ("strafe45", _fly45)):
        base.Course.fly = fly
        stand = base.standstill(course)
        landings = base.first_landings(course, 8.0)
        out[name] = {
            "standstill_back_edge_jump_tick": stand["back_edge_jump_tick"],
            "standstill_frontmost_start_z": stand["frontmost_feasible_start_z"],
            "chain_k0": f"{sum(base.chain(course, s, 0, base.RUN) is not None for _, s in landings)}/{len(landings)}",
            "chain_k1_run": f"{sum(base.chain(course, s, 1, base.RUN) is not None for _, s in landings)}/{len(landings)}",
        }
    base.Course.fly = _straight_fly
    return out


def main():
    for p in (1, 2):
        for w2 in (3, 4, 5):
            print(f"W1=1 P={p} W2={w2}:", evaluate(p, w2), flush=True)


if __name__ == "__main__":
    main()
