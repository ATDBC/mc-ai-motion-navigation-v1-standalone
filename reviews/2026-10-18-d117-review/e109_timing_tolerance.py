"""E109：平地 W1=1、P=1、W2=4 上，“停下再跳”和“落地即接”各自对起跳时刻的容差。

复用 E98 的地形和计算器（只调用项目的 step()）。两种策略都只用正前方前进+疾跑：
  停下再跳：中间平台上每个静止起点（0.01 格步长），列出所有能跨过的起跳 tick j；
            再检查“计划在 j 起跳、跳键迟到 1 tick”（即 j+1 才按跳）是否仍能跨过。
  落地即接：E100 方式收集第一跳的全部落点；对每个落点，计划在 L+1 起跳（k=0），
            分别用“只在 L+1 按跳”和“从 L 起按住跳”（E103）两种命令，检查迟到 1 tick 是否仍能跨过。
输出：静止起点中有多少个能容忍 1 tick 迟到；落点中两种命令各有多少个能容忍 1 tick 迟到。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import Course, JUMP, RUN  # noqa: E402
from e103_jump_hold_timing import run, trajectories  # noqa: E402


def standstill_succeeds(course, z, j):
    state = course.state_at(z)
    for _ in range(j):
        state = course.advance(state, RUN)
        if state is None or not course.grounded(state):
            return False
    state = course.advance(state, JUMP)
    if state is None or state.on_ground:
        return False
    landed = course.fly(state)
    return landed is not None and course.on_target(landed)


def main():
    course = Course(1, 1, 4, 0)
    a, b = course.mid
    feasible = tolerant = 0
    for i in range(int(round((b - a + 0.58) / 0.01)) + 1):
        z = round(a - 0.29 + 0.01 * i, 2)
        good = [j for j in range(14) if standstill_succeeds(course, z, j)]
        if good:
            feasible += 1
            if any(j + 1 in good for j in good):
                tolerant += 1
    print(f"停下再跳：可行静止起点 {feasible} 个，其中能容忍跳键迟到 1 tick 的 {tolerant} 个")
    found = trajectories(course)
    on_time = [(bf, af) for bf, af in found if run(course, bf, (RUN, JUMP))]
    late_plain = sum(run(course, bf, (RUN, RUN, JUMP)) for bf, af in on_time)
    held = [(bf, af) for bf, af in found if run(course, bf, (JUMP, JUMP))]
    late_held = sum(run(course, bf, (RUN, JUMP, JUMP)) for bf, af in held)
    print(f"落地即接：第一跳落点 {len(found)} 个；L+1 准点起跳可过 {len(on_time)} 个，"
          f"其中跳键只按 L+1、迟到 1 tick 仍可过 {late_plain} 个")
    print(f"落地即接：从 L 起按住跳可过 {len(held)} 个，整条命令迟到 1 tick 仍可过 {late_held} 个")


if __name__ == "__main__":
    main()
