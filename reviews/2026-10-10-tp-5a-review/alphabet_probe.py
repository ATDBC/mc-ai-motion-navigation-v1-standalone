"""TP Task 5A 审查探针：输入字母表加入朝向后，参考搜索的规模。

运行：在仓库根目录 PYTHONPATH=. python <本文件>
只读运行。字母表为 3 个朝向 × 5 种动作 = 15 个逐 tick 输入；
目标仍要求最终朝向 0，因此最后几 tick 必须选回朝向 0。
只用 P0 默认预算 4096／65536／40／2。墙钟只在外层测量，是 Linux 补充数据。
"""
from dataclasses import replace
import math
import time

from experiments.motion_navigation.trajectory_proto.reference_search import reference_search
from experiments.motion_navigation.trajectory_proto.scenarios import P0_BUDGET, representative_fixture
from mc2p.motion_nav.physics_types import TickInput

import scale_probe

YAWS = (-math.pi / 4, 0., math.pi / 4)
MODES = ((1., False, False), (1., True, False), (0., False, False), (1., False, True), (1., True, True))
ALPHABET15 = tuple(TickInput(forward, 0., jump, False, sprint, yaw)
                   for yaw in YAWS for forward, jump, sprint in MODES)
# Same 15 inputs, yaw-0 inputs first: only the expansion order differs.
ALPHABET15_ZERO_FIRST = tuple(sorted(ALPHABET15, key=lambda command: abs(command.movement_yaw_radians)))


def report(label, request, world):
    started = time.perf_counter()
    result = reference_search(request, world)
    elapsed = (time.perf_counter() - started) * 1000.
    print(f"{label:34s} -> {result.status.value}/{result.reason.value} expanded={result.expanded_nodes} "
          f"nodes={result.counts.nodes} steps={result.counts.physics_steps} "
          f"ticks={result.candidate_ticks} {elapsed:.0f} ms")


if __name__ == "__main__":
    for order, alphabet in (("A15", ALPHABET15), ("A15 yaw0-first", ALPHABET15_ZERO_FIRST)):
        fixture = representative_fixture("jump_up_straight")
        report(f"jump_up {order}",
               replace(fixture.request, supported_inputs=alphabet, budget=P0_BUDGET), fixture.world)
        for width in (2, 3):
            request, world, goal = scale_probe.gap_case(width)
            report(f"gap{width} {order}",
                   replace(request, goal=goal, supported_inputs=alphabet, budget=P0_BUDGET), world)
