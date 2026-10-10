"""TP Task 5A 审查探针：留出 3.5 格助跑后的 2、3 格跨隙。

运行：在仓库根目录 PYTHONPATH=.:<本目录> python <本文件>
空隙为 z=4..4+width-1，入口仍在 z=0.5 静止，所以起跳前有 3.5 格助跑。
先用 P0 默认预算，未找到再用 4 倍预算。墙钟只在外层测量，是 Linux 补充数据。
"""
from dataclasses import replace
import time

from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.reference_search import reference_search
from experiments.motion_navigation.trajectory_proto.scenarios import P0_BUDGET

import scale_probe
from alphabet_probe import ALPHABET15


def run(label, width, alphabet, budget):
    request, world, goal = scale_probe.gap_case(width, start=4)
    request = replace(request, goal=goal, supported_inputs=alphabet, budget=budget,
                      request_id=f"runup-gap{width}")
    started = time.perf_counter()
    result = reference_search(request, world)
    elapsed = (time.perf_counter() - started) * 1000.
    print(f"runup gap{width} {label:24s} budget={budget.max_nodes}/{budget.max_physics_steps} "
          f"-> {result.status.value}/{result.reason.value} expanded={result.expanded_nodes} "
          f"nodes={result.counts.nodes} steps={result.counts.physics_steps} "
          f"tail={result.counts.tail_ticks} ticks={result.candidate_ticks} {elapsed:.0f} ms", flush=True)
    return result


if __name__ == "__main__":
    larger = SearchBudget(16384, 262144, 40, 2)
    alphabets = dict(scale_probe.ALPHABETS)
    alphabets["A15 3 yaws x 5"] = ALPHABET15
    for width in (2, 3):
        for label, alphabet in alphabets.items():
            if width == 3 and label.startswith("A3"):
                continue
            result = run(label, width, alphabet, P0_BUDGET)
            if result.status.value != "FOUND":
                run(label, width, alphabet, larger)
