"""E106：从长平台上任意助跑后单次疾跑跳，最远能跨几格（平地）；与“落地即跳”的第二跳对比。

复用 E98 的地形和计算器（只调用项目的 step()）。把 E98 的第一跳当成被测对象：起跑平台
z ∈ [-12, -1]（12 格），助跑起点逐 0.1 格扫过整个平台，起跳 tick 逐个搜索，第一段间隙 W1=1..5，
第一跳能落到对面平台即算可过。输出每个 W1 能否单跳跨过，以及最远落点的余量。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import Course, START_BACK, first_landings  # noqa: E402


def main():
    for w1 in range(1, 6):
        course = Course(w1, 3, 1, 0)
        best = None
        run_up = 0.29
        while run_up <= -START_BACK - 0.3:
            for _, landed in first_landings(course, run_up):
                margin = landed.position[2] + 0.3 - course.mid[0]
                speed = math.hypot(landed.velocity_blocks_per_tick[0], landed.velocity_blocks_per_tick[2])
                if best is None or margin > best[0]:
                    best = (round(margin, 3), round(run_up, 2), round(speed, 3))
            run_up = round(run_up + 0.1, 2)
        print(f"W1={w1}：单跳{'可过' if best else '不可过'}"
              + (f"；最大落点余量 {best[0]} 格（助跑 {best[1]} 格，落地速度 {best[2]} b/t）" if best else ""))


if __name__ == "__main__":
    main()
