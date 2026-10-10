"""5A-R R2 审查探针：冻结矩阵的计数，以及预算按输入层花在哪里。

运行：在仓库根目录（可先应用 r2-independent-gja-failed.diff 或本目录的变体补丁），
PYTHONPATH=. python <本文件> [场景名 ...]
只读运行。按 r2-matrix.json 的 22 正例／6 负例逐项调用 reference_search()，
并在调用期间统计每个输入层消耗的物理 step 与完成尝试次数。
"""
import collections
import sys
import time

from experiments.motion_navigation.trajectory_proto import reference_search as rs
from experiments.motion_navigation.trajectory_proto.scenarios import primitive_fixture

POSITIVES = (
    ("flat_walk", "A3"), ("jump_up_straight", "A3"), ("jump_gap_1", "A3"),
    *((f"gap_start_1_width_{w}", t) for w in (1, 2) for t in ("A3", "A5", "A15")),
    *((f"gap_start_4_width_{w}", t) for w in (1, 2) for t in ("A3", "A5", "A15")),
    ("gap_start_4_width_3", "A5"), ("gap_start_4_width_3", "A15"),
    ("flat_sprint", "A5"), ("turn_90", "A15"), ("jump_up_after_turn", "A15"),
    ("jump_gap_continue", "A3"), ("jump_up_after_turn_continue", "A15"),
)
NEGATIVES = (("gap_start_4_width_3_a3", None), ("one_twelfth_support", None),
             ("resource_goal", None), ("unknown_landing", None), ("tiny_budget", None),
             ("collision_only", None))


def run(name, tier):
    fixture = primitive_fixture(name, tier) if tier else primitive_fixture(name)
    per_tier = collections.Counter()
    current = {"tier": None}
    original_prefixes = rs.primitive_prefixes

    def tracked(input_tier):
        for row in original_prefixes(input_tier):
            current["tier"] = input_tier.tier_id
            yield row

    original_step = rs.CountedPhysics.step

    def counted(self, *args, **kwargs):
        per_tier[current["tier"]] += 1
        return original_step(self, *args, **kwargs)

    rs.primitive_prefixes, rs.CountedPhysics.step = tracked, counted
    try:
        started = time.perf_counter()
        outcome = rs.reference_search(fixture.request, fixture.world)
        elapsed = (time.perf_counter() - started) * 1000.
    finally:
        rs.primitive_prefixes, rs.CountedPhysics.step = original_prefixes, original_step
    split = " ".join(f"{k}:{v}" for k, v in per_tier.items())
    print(f"{name:30s} {str(tier):4s} -> {outcome.status.value}/{outcome.reason.value} "
          f"tier={outcome.winning_tier} nodes={outcome.counts.nodes} steps={outcome.counts.physics_steps} "
          f"completions={outcome.completed_candidates} scans={outcome.commitment_scans} "
          f"ticks={outcome.candidate_ticks} {elapsed:.0f} ms | steps by tier {split}", flush=True)


if __name__ == "__main__":
    wanted = set(sys.argv[1:])
    for name, tier in POSITIVES + NEGATIVES:
        if not wanted or name in wanted:
            run(name, tier)
