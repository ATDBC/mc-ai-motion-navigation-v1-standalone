"""E108：静止起跳在 1 格中间平台上能否跨 4 格间隙，逐 0.01 格列出可行静止起点与余量。

复用 E98 的地形和计算器（只调用项目的 step()）。地形：平地，W1=1，P=1 或 2，W2=4。
对中间平台上每个静止起点（含两侧 0.29 格悬出），按住前进+疾跑、搜索起跳 tick（以计算器报告的
着地为准，包括越过边缘的那一 tick），报告：可行起点、所用起跳 tick、起跳时身体中心 z、
目标平台重叠余量。再与连续接续（E100 的 k=0／k=1）的余量对照。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import Course, JUMP, RUN  # noqa: E402


def best_from(course, z):
    best = None
    for j in range(0, 14):
        state = course.state_at(z)
        alive = True
        for _ in range(j):
            state = course.advance(state, RUN)
            if state is None or not course.grounded(state):
                alive = False
                break
        if not alive:
            break
        takeoff = state.position[2]
        state = course.advance(state, JUMP)
        if state is None or state.on_ground:
            continue
        landed = course.fly(state)
        if landed is not None and course.on_target(landed):
            margin = course.margin(landed)
            if best is None or margin > best[2]:
                best = (j, round(takeoff, 3), round(margin, 3))
    return best


def main():
    for p in (1, 2):
        course = Course(1, p, 4, 0)
        a, b = course.mid
        rows = []
        for i in range(int(round((b - a + 0.58) / 0.01)) + 1):
            z = round(a - 0.29 + 0.01 * i, 2)
            best = best_from(course, z)
            if best is not None:
                rows.append((z, *best))
        span = (rows[0][0], rows[-1][0]) if rows else None
        print(f"P={p}（中间平台 z∈[{a},{b})）：可行静止起点 {len(rows)} 个（0.01 格步长），范围 {span}")
        for z, j, takeoff, margin in rows:
            print(f"  起点 z={z}：跑 {j} tick 后起跳，起跳时 z={takeoff}，目标重叠余量 {margin}")


if __name__ == "__main__":
    main()
