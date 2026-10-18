"""E110：从长平台助跑起跳（不靠上一跳的速度）跨 W 格间隙时，对跳键迟到 1 tick 的容差。

复用 E98 的地形和计算器（只调用项目的 step()）。起跑平台 z ∈ [-12, -1]，第一段间隙 W1=W，
对面平台 3 格。助跑起点逐 0.02 格扫过整个起跑平台，按住前进+疾跑，起跳 tick 全搜索。
对每个可行的（起点, 起跳 tick j），检查同一起点在 j+1 起跳是否仍能落到对面平台。
输出每个 W：可行组合数、能容忍 1 tick 迟到的组合数、起跳时身体中心相对平台边缘（z=0）的范围。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import Course, JUMP, RUN  # noqa: E402


def lands(course, start, j):
    state = course.state_at(-start)
    for _ in range(j):
        state = course.advance(state, RUN)
        if state is None or not course.grounded(state):
            return None
    takeoff = state.position[2]
    state = course.advance(state, JUMP)
    if state is None or state.on_ground:
        return None
    landed = course.fly(state)
    return takeoff if landed is not None and course.on(landed, course.mid) else None


def main():
    for w in (1, 2, 3, 4):
        course = Course(w, 3, 1, 0)
        feasible = tolerant = 0
        takeoffs = []
        start = 0.29
        while start <= 11.7:
            for j in range(0, 45):
                takeoff = lands(course, start, j)
                if takeoff is None:
                    continue
                feasible += 1
                takeoffs.append(takeoff)
                if lands(course, start, j + 1) is not None:
                    tolerant += 1
            start = round(start + 0.02, 2)
        span = [round(min(takeoffs), 3), round(max(takeoffs), 3)] if takeoffs else None
        print(f"W={w}：可行（起点, 起跳 tick）{feasible} 个，能容忍迟到 1 tick 的 {tolerant} 个；起跳时身体中心 z 范围 {span}（平台边缘 z=0，悬出极限 0.3）")


if __name__ == "__main__":
    main()
