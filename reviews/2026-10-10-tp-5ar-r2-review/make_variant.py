"""5A-R R2 审查变体：在已应用 r2-independent-gja-failed.diff 的 reference_search.py 上加入三项去浪费修改。

运行：python <本文件> <reference_search.py 路径> v1|v2
v1：失败前缀也缓存；B 段在全部分支着地且水平速度为零、或速度已低于最小终速后不再延长。
v2：v1 再加“低于锚点与目标较低者即失败”（模板限制，不是有证明的剪枝）。
结果见 variant-v1.diff、variant-v2.diff。保留原文件换行符。只用于测量，不是建议的实现。
"""
import sys
path, variant = sys.argv[1], sys.argv[2]
s = open(path, newline="").read()
nl = "\r\n" if "\r\n" in s else "\n"
s = s.replace("\r\n", "\n")
old = '''        def execute(inputs):
            if inputs in state_cache:
                return state_cache[inputs], ()
            parent = inputs[:-1]
            states, missing = execute(parent)
            if states is None:
                return None, missing
            next_states = []
            for state in states:
                result = counter.step(state, inputs[-1], world)
                if result.status is CalculationStatus.NEEDS_WORLD:
                    return None, result.missing_cells
                if (result.status is not CalculationStatus.OK
                        or result.next_state.horizontal_collision):
                    return None, ()
                next_states.append(result.next_state)
            state_cache[inputs] = tuple(next_states)
            return state_cache[inputs], ()
'''
floor = '''
                if FLOOR is not None and result.next_state.position[1] < FLOOR - 1.e-7:
                    failed[inputs] = ()
                    return None, ()''' if variant == "v2" else ""
new = '''        failed = {}
        FLOOR = min(request.anchor_state.position[1], request.goal.region.min_y)

        def execute(inputs):
            # Review variant: memoize failed prefixes too; a failure is final.
            if inputs in state_cache:
                return state_cache[inputs], ()
            if inputs in failed:
                return None, failed[inputs]
            parent = inputs[:-1]
            states, missing = execute(parent)
            if states is None:
                failed[inputs] = missing
                return None, missing
            next_states = []
            for state in states:
                result = counter.step(state, inputs[-1], world)
                if result.status is CalculationStatus.NEEDS_WORLD:
                    return None, result.missing_cells
                if (result.status is not CalculationStatus.OK
                        or result.next_state.horizontal_collision):
                    failed[inputs] = ()
                    return None, ()''' + floor + '''
                next_states.append(result.next_state)
            state_cache[inputs] = tuple(next_states)
            return state_cache[inputs], ()
'''
assert old in s, "execute"
s = s.replace(old, new)
old = '''                    result = consider(brake_inputs, brake_states, tier)
                    if result is not None:
                        return result
'''
new = '''                    result = consider(brake_inputs, brake_states, tier)
                    if result is not None:
                        return result
                    # Review variant: stop lengthening B once it cannot change the
                    # verdict: every branch is grounded and stopped (a fixed point
                    # under stop_input), or horizontal speed has fallen below the
                    # goal minimum (it only decays under stop_input).
                    speeds = [20. * math.hypot(state.velocity_blocks_per_tick[0],
                                               state.velocity_blocks_per_tick[2])
                              for state in brake_states]
                    if (all(state.on_ground for state in brake_states) and max(speeds) == 0.) or (
                            request.minimum_terminal_speed_blocks_per_second > 0.
                            and max(speeds) + 1.e-12 < request.minimum_terminal_speed_blocks_per_second):
                        break
'''
assert old in s, "brake"
s = s.replace(old, new)
open(path, "w", newline="").write(s.replace("\n", nl))
