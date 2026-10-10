"""5A-R R2 审查探针：取消计数预算，量出独立 G／J／A 下每一层“确认没有结果”的真实成本。

运行：在应用 r2-independent-gja-failed.diff（或本目录变体补丁）的仓库根目录，
PYTHONPATH=. python <本文件> [场景名 ...]
只把 request.budget 换成 10M nodes／100M steps，轨迹上限和分支数不变；其余与 tier_cost_probe 相同。
这是测量工具，不是建议的预算。
"""
from dataclasses import replace
import sys

from experiments.motion_navigation.trajectory_proto import scenarios
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget

import tier_cost_probe as probe

UNBOUNDED = SearchBudget(10_000_000, 100_000_000, 40, 2)
CASES = (("turn_90", "A15"), ("jump_up_after_turn", "A15"), ("jump_up_after_turn_continue", "A15"),
         ("flat_sprint", "A5"), ("gap_start_4_width_3", "A5"))
original_fixture = scenarios.primitive_fixture


def unbounded_fixture(*args, **kwargs):
    fixture = original_fixture(*args, **kwargs)
    return replace(fixture, request=replace(fixture.request, budget=UNBOUNDED))


if __name__ == "__main__":
    probe.primitive_fixture = unbounded_fixture
    wanted = set(sys.argv[1:])
    for name, tier in CASES:
        if not wanted or name in wanted:
            probe.run(name, tier)
