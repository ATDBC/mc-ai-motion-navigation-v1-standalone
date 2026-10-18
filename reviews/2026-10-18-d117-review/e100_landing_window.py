"""E100：“只有带速度才能过”的地形里，第一跳必须落在中间平台的哪一段。

复用 E98 的地形和计算器（同目录 e98_momentum_feasibility.py，只调用项目的 step()）。
对每个地形，把起跑点在起跑平台上按 0.02 格细扫（助跑 0.29–8 格），每个起跑点再搜索起跳 tick，
收集所有落到中间平台的第一跳。对每个落点检查：
  k0       观察到着地后的第一个输入就按跳（前进+疾跑），能否落到目标平台；
  k1_run   多着地 1 tick（前进+疾跑）再跳；
  k1_fwd   多着地 1 tick（只按前进）再跳，即 D116／D117 代表链的现行交接时序；
  neutral  不按任何键，身体是否仍停在中间平台上（否则滑落）；
  sneak    潜行不动，能否停稳。
输出：落点 z 的范围、可接续（k0）的落点 z 区间和占比、其它时序的可接续数、
不潜行时滑落的比例、潜行停稳的比例。
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import (  # noqa: E402
    Course, FORWARD, JUMP, NEUTRAL, RUN, SNEAK, brake, chain,
)

SCENARIOS = (  # (目标高度 DY, W1, P, W2)：E98 中静止不可过、带速度可过的组合
    (0, 1, 1, 4), (0, 2, 1, 4), (0, 1, 2, 4), (0, 2, 2, 4),
    (1, 1, 1, 3), (1, 2, 1, 3), (1, 1, 2, 3), (1, 2, 2, 3),
)


def landings(course):
    out = []
    start = 0.29
    while start <= 8.0:
        for j1 in range(0, 40):
            state = course.state_at(-start)
            ok = True
            for _ in range(j1):
                state = course.advance(state, RUN)
                if state is None or not state.on_ground:
                    ok = False
                    break
            if not ok:
                break
            state = course.advance(state, JUMP)
            if state is None or state.on_ground:
                continue
            landed = course.fly(state)
            if landed is not None and course.on(landed, course.mid):
                out.append(landed)
        start = round(start + 0.02, 2)
    return out


def stays_without_sneak(course, landed):
    state = landed
    for _ in range(30):
        state = course.advance(state, NEUTRAL)
        if state is None or not course.on(state, course.mid):
            return False
        if math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) < 0.003:
            return True
    return False


def main():
    for dy, w1, p, w2 in SCENARIOS:
        course = Course(w1, p, w2, dy)
        found = landings(course)
        k0 = [s for s in found if chain(course, s, 0, RUN) is not None]
        k1_run = [s for s in found if chain(course, s, 1, RUN) is not None]
        k1_fwd = [s for s in found if chain(course, s, 1, FORWARD) is not None]
        slide = [s for s in found if not stays_without_sneak(course, s)]
        sneak = [s for s in found if brake(course, s, SNEAK) is not None]
        k0_slide = [s for s in k0 if not stays_without_sneak(course, s)]
        z = lambda states: [round(min(s.position[2] for s in states), 3),
                            round(max(s.position[2] for s in states), 3)] if states else None
        print(json.dumps({
            "DY": dy, "W1": w1, "P": p, "W2": w2, "mid_platform_z": list(course.mid),
            "first_landings": len(found), "landing_z_range": z(found),
            "landing_speed_range": [round(min(math.hypot(s.velocity_blocks_per_tick[0], s.velocity_blocks_per_tick[2]) for s in found), 3),
                                    round(max(math.hypot(s.velocity_blocks_per_tick[0], s.velocity_blocks_per_tick[2]) for s in found), 3)] if found else None,
            "k0": [len(k0), z(k0)], "k1_run": [len(k1_run), z(k1_run)], "k1_forward": [len(k1_fwd), z(k1_fwd)],
            "slides_off_without_sneak": len(slide), "k0_landings_that_slide_without_sneak": len(k0_slide),
            "sneak_stops": len(sneak),
        }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
