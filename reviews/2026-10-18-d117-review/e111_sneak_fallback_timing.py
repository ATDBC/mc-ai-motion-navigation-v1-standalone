"""E111：第二跳没有获批时，潜行刹车何时开始才能停在 1 格中间平台上。

复用 E98／E103 的地形和计算器（只调用项目的 step()）。地形：平地 W1=1、P=1、W2=4。
取第一跳全部落到中间平台的轨迹（落地前一状态 + 落地状态），只看“现行时序可接续”的落点
（落地后 1 个只按前进的 tick，再起跳能过；即 E100 的 k1_forward，D116／D117 代表链的交接时序）。
对这些落点比较三种回退：
  sneak_L      落地那一 tick（L，仍在空中）就按潜行，之后保持潜行；
  sneak_L1     L 前进+疾跑（按原计划），L+1 起潜行；
  sneak_L2     L 前进+疾跑、L+1 只按前进（现行交接 tick 的输入），L+2 起潜行——回退迟到 1 tick；
  neutral_L1   L+1 起不按任何键（现行“释放后恢复”证明的回退）。
停稳指水平速度 < 0.003 b/t 且仍着地在中间平台上。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import Course, FORWARD, NEUTRAL, RUN, SNEAK, chain  # noqa: E402
from e103_jump_hold_timing import trajectories  # noqa: E402


def stops(course, before, prefix, then):
    state = before
    for tick_input in prefix:
        state = course.advance(state, tick_input)
        if state is None:
            return False
    for _ in range(30):
        state = course.advance(state, then)
        if state is None or not course.on(state, course.mid):
            return False
        if math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) < 0.003:
            return True
    return False


def main():
    course = Course(1, 1, 4, 0)
    found = [(bf, af) for bf, af in trajectories(course) if chain(course, af, 1, FORWARD) is not None]
    print(f"地形 W1=1 P=1 W2=4：现行时序可接续的第一跳落点 {len(found)} 个")
    cases = {
        "sneak_L": ((), SNEAK),
        "sneak_L1": ((RUN,), SNEAK),
        "sneak_L2": ((RUN, FORWARD), SNEAK),
        "neutral_L1": ((RUN,), NEUTRAL),
    }
    for name, (prefix, then) in cases.items():
        print(f"{name}: {sum(stops(course, bf, prefix, then) for bf, af in found)}/{len(found)} 停在中间平台上")


if __name__ == "__main__":
    main()
