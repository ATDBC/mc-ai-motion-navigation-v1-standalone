"""TP Task 5A 审查探针：同一组小矩阵在不同搜索版本上的结果。

运行：在仓库根目录 PYTHONPATH=.:<本目录> python <本文件>
矩阵：三个默认代表；空隙起点 z=1（无助跑）与 z=4（3.5 格助跑）；
宽 1—3；字母表 A3／A5／A15。只用 P0 默认预算。
墙钟只在外层测量，是 Linux 补充数据。
"""
from dataclasses import replace
import time

from experiments.motion_navigation.trajectory_proto.reference_search import reference_search
from experiments.motion_navigation.trajectory_proto.scenarios import P0_BUDGET, representative_fixture

import scale_probe
from alphabet_probe import ALPHABET15

ALPHABETS = (("A3", scale_probe.ALPHABETS["A3 walk/jump/neutral"]),
             ("A5", scale_probe.ALPHABETS["A5 +sprint/sprint_jump"]),
             ("A15", ALPHABET15))


def go(label, request, world):
    started = time.perf_counter()
    result = reference_search(request, world)
    print(f"{label:24s} -> {result.status.value}/{result.reason.value} expanded={result.expanded_nodes} "
          f"nodes={result.counts.nodes} steps={result.counts.physics_steps} "
          f"ticks={result.candidate_ticks} {(time.perf_counter() - started) * 1000.:.0f} ms", flush=True)


if __name__ == "__main__":
    for name in ("flat_walk", "jump_up_straight", "jump_gap_1"):
        fixture = representative_fixture(name)
        go(f"repr {name}", fixture.request, fixture.world)
    for start in (1, 4):
        for width in (1, 2, 3):
            if start == 1 and width == 3:
                continue  # no run-up: a 3-block gap is out of reach from rest
            for label, alphabet in ALPHABETS:
                request, world, goal = scale_probe.gap_case(width, start=start)
                go(f"start{start} gap{width} {label}",
                   replace(request, goal=goal, supported_inputs=alphabet, budget=P0_BUDGET), world)
