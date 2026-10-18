"""E103：第二跳的跳键早按一 tick（第一跳仍在空中时）会怎样，能否吸收 1 tick 交付延迟。

复用 E98 的地形和计算器（只调用项目的 step()）。地形：平地，W1=1、P=1、W2=4（E98／E100 中
静止不可过、带速度可过的组合）。取 E100 的方式收集第一跳所有落到中间平台的轨迹，但保留落地前
一个状态，比较四种按键时序（空中其余 tick 都是前进+疾跑）：
  on_time        L 不按跳（落地那一 tick），L+1 按跳            ——即 k=0；
  late_1         L、L+1 不按跳，L+2 按跳                         ——即 k=1（迟到 1 tick）；
  held_from_L    L 起连续按跳两 tick                             ——早按，不迟到；
  held_late_1    L+1 起连续按跳两 tick                           ——早按的命令整体迟到 1 tick；
  pressed_L_only 只在 L 按跳，L+1 不按                           ——检查空中按跳是否被保留。
L 指让身体着地的那一次 step。报告每种时序能落到目标平台的第一跳轨迹数与对应落点区间。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e98_momentum_feasibility import Course, JUMP, RUN  # noqa: E402


def trajectories(course):
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
            before = state
            for _ in range(40):
                after = course.advance(before, RUN)
                if after is None or after.position[1] < 62.0:
                    break
                if after.on_ground:
                    if course.on(after, course.mid):
                        out.append((before, after))
                    break
                before = after
        start = round(start + 0.02, 2)
    return out


def run(course, before, sequence):
    """从落地前一状态起依次施加 sequence 中的输入，然后空中前进+疾跑。"""
    state = before
    took_off = False
    for index, tick_input in enumerate(sequence):
        state = course.advance(state, tick_input)
        if state is None:
            return False
        if index >= 1 and not state.on_ground:
            took_off = True
            break
        if index >= 1 and not course.grounded(state):
            return False
    if not took_off:
        return False
    final = course.fly(state)
    return final is not None and course.on_target(final)


def main():
    course = Course(1, 1, 4, 0)
    found = trajectories(course)
    timings = {
        "on_time": (RUN, JUMP),
        "late_1": (RUN, RUN, JUMP),
        "held_from_L": (JUMP, JUMP),
        "held_late_1": (RUN, JUMP, JUMP),
        "pressed_L_only": (JUMP, RUN, RUN),
    }
    print(f"地形 W1=1 P=1 W2=4（中间平台 z∈[1,2)），第一跳落到中间平台的轨迹 {len(found)} 条")
    for name, sequence in timings.items():
        good = [after.position[2] for before, after in found if run(course, before, sequence)]
        window = [round(min(good), 3), round(max(good), 3)] if good else None
        print(f"{name}: {len(good)}/{len(found)} 条能落到目标平台，落点 z 区间 {window}")


if __name__ == "__main__":
    main()
