"""E131b：南向 (0,62,0) 未知时，父动作的 NEEDS_WORLD 落在哪个边界、哪些 fallback 候选上。

只调用项目函数：`scripts.d123_dependency_probe.fixed_case／world_for／fact_for`、生产
`_replay_verified_commands`。包装 `_prove_fallback_candidate` 和 `_release_recovery_evidence`
只为记录调用参数与返回状态，不改返回值。分别对已知 AIR 与 UNKNOWN 两种世界报告：
  - 每个边界上三个候选（neutral／back／sneak）都没证明出来的边界及其失败状态；
  - 失败状态里带缺失格的边界。
用法：python e131b_failing_boundary.py   （在检出根目录运行）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav import motion_solver as ms  # noqa: E402
from scripts import d123_dependency_probe as d123  # noqa: E402

CELL = (0, 62, 0)


def run(mode):
    case = d123.fixed_case(0)
    request, _fixture, proof, _spec, snapshot = case
    world = d123.world_for(snapshot, {CELL: d123.fact_for(snapshot, CELL, mode)})
    attempts, lengths = [], []
    original_candidate, original_evidence = ms._prove_fallback_candidate, ms._release_recovery_evidence

    def candidate(index, release_state, fallback, *args, **kwargs):
        result = original_candidate(index, release_state, fallback, *args, **kwargs)
        attempts.append((index, fallback.candidate_id,
                         "proved" if result.proof is not None else result.status.value,
                         [] if result.proof is not None else [list(c) for c in result.missing_cells]))
        return result

    def evidence(trajectory, *args, **kwargs):
        lengths.append(len(trajectory))
        return original_evidence(trajectory, *args, **kwargs)

    ms._prove_fallback_candidate, ms._release_recovery_evidence = candidate, evidence
    try:
        _variant, failure = ms._replay_verified_commands(proof.entry_state, proof.commands, world, request,
                                                         start_tick=1)
    finally:
        ms._prove_fallback_candidate, ms._release_recovery_evidence = original_candidate, original_evidence
    by_boundary = {}
    for index, candidate_id, status, missing in attempts:
        by_boundary.setdefault(index, []).append({"candidate": candidate_id, "status": status, "missing": missing})
    unproved = {str(i): rows for i, rows in by_boundary.items() if all(r["status"] != "proved" for r in rows)}
    return {
        "parent_status": "solved" if failure is None else failure.status.value,
        "reasons": list(failure.reasons) if failure is not None else [],
        "allow_committed_main_suffix": request.allow_committed_main_suffix,
        "release_recovery_trajectory_lengths": lengths,
        "boundaries_without_any_fallback": unproved,
        "boundaries_with_missing_cells": sorted({i for i, _c, s, m in attempts if m}),
    }


def main():
    print(json.dumps({mode: run(mode) for mode in ("air", "unknown")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
