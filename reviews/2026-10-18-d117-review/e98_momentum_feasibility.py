"""E98：用项目的 1.21 运动计算器，找“停下来过不去、带速度能过去”的连续跨隙地形。

只调用项目函数：mc2p.motion_nav.physics_1_21.step、physics_adapter.build_physics_state、
world_model.WorldKnowledge 和测试夹具 valid_snapshot_v3（与 test_b09r_physics_step 相同的构造）。
不调用规划器或求解器，所以这里回答的是“物理上是否存在”，不是“项目现在能否规划和证明”。

地形沿 +z，宽 3 格，地面方块在 y=63，身体站在 y=64：
  起跑平台 z ∈ [-12, -1]，第一段间隙 W1 格，中间平台 P 格，第二段间隙 W2 格，
  目标平台 6 格，比中间平台高 DY 格（DY ∈ {-1, 0, +1}）。

策略（每 tick 一个 TickInput，朝向 +z）：
  A 静止起跳：身体静止站在中间平台上，起点 z 在平台内逐 0.05 格搜索（含后缘悬出 0.29 格），
    按住前进+疾跑，在第 j 个 tick 按跳（逐个搜索），空中继续前进+疾跑。
    报告：能落到目标平台的起点里最靠前的一个（“停在哪里还来得及”），以及后缘起跳的结果。
    后缘起跳不可行，就是“停下来过不去”。
  B 连续接续：从起跑平台静止、助跑 RUN_UP 格起步，按住前进+疾跑，第 j1 个 tick 起跳（搜索），
    落到中间平台后再着地 k 个 tick，然后下一 tick 按跳，空中前进+疾跑。三种着地输入：
      run      前进+疾跑；
      forward  只按前进（D116／D117 代表链在交接 tick 的实际输入）；
      neutral  空输入（现行普通 SOLVE 在两跳之间的实际输入）。
    k=0 表示观察到着地后的第一个输入就是跳。报告成功的第一跳落点数，以及成功时
    身体与目标平台方块的最大重叠余量（格，越大越稳）。
  C 刹车：B 的第一跳落到中间平台后，改用“后退”或“潜行不动”，看能否在中间平台上停稳
    （水平速度 < 0.003 b/t 且仍着地在中间平台上），报告所需 tick。

起跳资格一律以计算器报告的着地为准（grounded）：原版先按旧水平位置算竖直碰撞，所以身体中心
越过平台边缘（超出 0.3 格悬出）的那一 tick 仍可能着地，并且还能起跳。落点归属（落在哪个平台）
和“停稳”仍按身体与平台方块是否重叠判断。
"""
import itertools
import json
import math
import sys
from dataclasses import replace

from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockGeometry, WorldKnowledge
from tests.observation_v3_fixtures import valid_snapshot_v3

START_BACK = -12
TARGET_LEN = 6
RUN = TickInput(1, 0, False, False, True, 0.0)
JUMP = TickInput(1, 0, True, False, True, 0.0)
FORWARD = TickInput(1, 0, False, False, False, 0.0)
NEUTRAL = TickInput(0, 0, False, False, False, 0.0)
BACK = TickInput(-1, 0, False, False, False, 0.0)
SNEAK = TickInput(0, 0, False, True, False, 0.0)
GROUND_INPUTS = (("run", RUN), ("forward", FORWARD), ("neutral", NEUTRAL))

_frame = NavigationObservationAdapter().ingest(valid_snapshot_v3(sequence=7))
_BASE = replace(
    build_physics_state(_frame, JAVA_1_21_RULESET, dict(
        jumping_cooldown_ticks=0, movement_speed_attribute=0.1, step_height_blocks=0.6,
        gravity_attribute=0.08, jump_strength_attribute=0.42)).require_state(),
    yaw_radians=0.0, velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
    on_ground=True, vertical_collision=True, game_mode="survival",
    flying=False, allow_flying=False,
)


class Course:
    def __init__(self, w1, p, w2, dy=0):
        self.mid = (w1, w1 + p)                 # 中间平台方块覆盖的 z 区间 [a, b)
        self.target = (w1 + p + w2, w1 + p + w2 + TARGET_LEN)
        self.target_y = 64.0 + dy
        end = self.target[1]
        world = WorldKnowledge(_BASE.session)
        world.confirm_air(_frame.body.stamp, tuple(
            (x, y, z) for x in range(-2, 3) for y in range(56, 72)
            for z in range(START_BACK - 2, end + 3)))
        blocks = {(x, 63, z): BlockGeometry.full_cube("minecraft:stone")
                  for x in range(-1, 2)
                  for z in [*range(START_BACK, 0), *range(*self.mid)]}
        blocks.update({(x, 63 + dy, z): BlockGeometry.full_cube("minecraft:stone")
                       for x in range(-1, 2) for z in range(*self.target)})
        world.observe_blocks(_frame.body.stamp, blocks)
        self.world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)

    def state_at(self, z):
        return replace(_BASE, position=(0.5, 64.0, z))

    def advance(self, state, tick_input):
        result = step(state, tick_input, self.world, JAVA_1_21_RULESET)
        if result.status is not CalculationStatus.OK:
            return None
        return result.next_state

    @staticmethod
    def grounded(state, y=64.0):
        # 计算器报告着地且脚在中间平台高度：仍可按跳。
        return state.on_ground and abs(state.position[1] - y) < 1e-6

    @staticmethod
    def on(state, span, y=64.0):
        # 站立判定：脚在平台顶面，身体（宽 0.6）与平台方块的 z 区间有重叠。
        return (state.on_ground and abs(state.position[1] - y) < 1e-6
                and span[0] - 0.3 < state.position[2] < span[1] + 0.3)

    def on_target(self, state):
        return self.on(state, self.target, self.target_y)

    def margin(self, state):
        # 身体与目标平台方块在 z 方向的重叠（0 表示刚好擦边）。
        return state.position[2] + 0.3 - self.target[0]

    def fly(self, state, limit=40):
        """空中按住前进+疾跑直到着地或掉落；返回着地状态或 None。"""
        for _ in range(limit):
            state = self.advance(state, RUN)
            if state is None or state.position[1] < 62.0:
                return None
            if state.on_ground:
                return state
        return None


def standstill_from(course, z):
    """A：从中间平台上静止的 z 出发，最好的起跳 tick；不可行返回 None。"""
    for j in range(0, 14):
        state = course.state_at(z)
        for _ in range(j):
            state = course.advance(state, RUN)
            if state is None or not course.grounded(state):
                return None
        state = course.advance(state, JUMP)
        if state is None or state.on_ground:
            continue
        landed = course.fly(state)
        if landed is not None and course.on_target(landed):
            return j
    return None


def standstill(course):
    a, b = course.mid
    back = a - 0.29
    positions = [round(back + 0.05 * i, 2) for i in range(int((b - a + 0.58) / 0.05) + 1)]
    feasible = [z for z in positions if standstill_from(course, z) is not None]
    return {"back_edge_jump_tick": standstill_from(course, back),
            "frontmost_feasible_start_z": max(feasible) if feasible else None,
            "platform_z": [a, b]}


def first_landings(course, run_up):
    """B 第一跳：所有能落到中间平台的起跳 tick，以及落地状态。"""
    out = []
    for j1 in range(0, 40):
        state = course.state_at(-run_up)
        for _ in range(j1):
            state = course.advance(state, RUN)
            if state is None or not state.on_ground:
                return out
        state = course.advance(state, JUMP)
        if state is None or state.on_ground:
            continue
        landed = course.fly(state)
        if landed is not None and course.on(landed, course.mid):
            out.append((j1, landed))
    return out


def chain(course, landed, k, ground_input):
    """成功返回目标平台余量，失败返回 None。"""
    state = landed
    for _ in range(k):
        state = course.advance(state, ground_input)
        if state is None or not course.grounded(state):
            return None
    state = course.advance(state, JUMP)
    if state is None or state.on_ground:
        return None
    final = course.fly(state)
    return course.margin(final) if final is not None and course.on_target(final) else None


def brake(course, landed, brake_input):
    state = landed
    for tick in range(30):
        state = course.advance(state, brake_input)
        if state is None or not course.on(state, course.mid):
            return None
        if math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) < 0.003:
            return tick + 1
    return None


def main():
    run_up = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    dys = tuple(int(v) for v in sys.argv[2].split(",")) if len(sys.argv) > 2 else (0,)
    widths = tuple(int(v) for v in sys.argv[3].split(",")) if len(sys.argv) > 3 else (1, 2, 3, 4)
    for dy, w1, p, w2 in itertools.product(dys, (1, 2, 3), (1, 2), widths):
        course = Course(w1, p, w2, dy)
        landings = first_landings(course, run_up)
        chains = {}
        for name, ground_input in GROUND_INPUTS:
            for k in (0, 1, 2):
                margins = [chain(course, s, k, ground_input) for _, s in landings]
                good = [m for m in margins if m is not None]
                chains[f"k{k}_{name}"] = [len(good), round(max(good), 3) if good else None]
        row = {"DY": dy, "W1": w1, "P": p, "W2": w2, "standstill": standstill(course),
               "first_landings": [[j, round(s.position[2], 3),
                                   round(math.hypot(s.velocity_blocks_per_tick[0], s.velocity_blocks_per_tick[2]), 3)]
                                  for j, s in landings],
               "chain_successes_and_best_margin": chains,
               "brake_ticks": {name: [brake(course, s, inp) for _, s in landings]
                               for name, inp in (("back", BACK), ("sneak", SNEAK))}}
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
