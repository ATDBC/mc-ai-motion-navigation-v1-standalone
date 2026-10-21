"""E128b：南向 (0,62,2) 放 1.5 高栅栏柱时，父动作具体在哪一步失败。

只调用项目函数：`scripts.d123_dependency_probe.fixed_case／world_for／fact_for` 与生产
`_replay_verified_commands`。包装 `_prove_fallback_candidate` 只为记录每个边界上 fallback 候选的
返回状态和放手时的身体位置，不改返回值。比较 AIR 与栅栏柱两种世界：父动作状态与原因、
主轨迹在 z≥1.7 的最低点，以及第 26 步（出口边界，身体中心越过中间平台边缘）三个候选的结果。
用法：python e128b_z2_exit_tail.py   （在检出根目录运行）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav import motion_solver as ms  # noqa: E402
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, CellFact, CellKnowledge  # noqa: E402
from scripts import d123_dependency_probe as d123  # noqa: E402

CELL = (0, 62, 2)
FENCE_POST = (Aabb(.375, 0, .375, .625, 1.5, .625),)


def run(case, fact):
    request, _fixture, proof, _spec, snapshot = case
    attempts = []
    original = ms._prove_fallback_candidate

    def candidate(index, release_state, fallback, *args, **kwargs):
        result = original(index, release_state, fallback, *args, **kwargs)
        attempts.append({"boundary": index, "candidate": fallback.candidate_id,
                         "status": "proved" if result.proof is not None else result.status.value,
                         "reasons": [] if result.proof is not None else list(result.reasons),
                         "release_y": round(release_state.position[1], 3),
                         "release_z": round(release_state.position[2], 3)})
        return result

    ms._prove_fallback_candidate = candidate
    try:
        _variant, failure = ms._replay_verified_commands(
            proof.entry_state, proof.commands, d123.world_for(snapshot, {CELL: fact}), request, start_tick=1)
    finally:
        ms._prove_fallback_candidate = original
    last = max(a["boundary"] for a in attempts)
    return {
        "parent_status": "solved" if failure is None else failure.status.value,
        "parent_reasons": [] if failure is None else list(failure.reasons),
        "main_lowest_point_beyond_z_1_7": min(
            ([round(s.position[1], 3), round(s.position[2], 3)] for s in proof.trajectory if s.position[2] > 1.7),
            default=None),
        "exit_boundary": last,
        "exit_boundary_candidates": [a for a in attempts if a["boundary"] == last],
    }


def main():
    case = d123.fixed_case(0)
    snapshot = case[4]
    stamp = dict(snapshot.facts)[CELL].stamp
    fence = CellFact(CellKnowledge.BLOCK, stamp, BlockGeometry("minecraft:oak_fence", "boxes", FENCE_POST))
    print(json.dumps({"air": run(case, d123.fact_for(snapshot, CELL, "air")), "fence_post": run(case, fence)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
