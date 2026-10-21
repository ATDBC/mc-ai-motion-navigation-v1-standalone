"""E131：(0,62,0) 未知时，父动作 NEEDS_WORLD 是否只来自“可选放弃点”的候选尝试。

只调用项目函数：`tests.motion_nav.test_d121_successor_binding.fixed_case`（D122 冻结父 proof），
`scripts.d123_dependency_probe.world_for／fact_for`，以及生产 `_replay_verified_commands`
（父动作重放，含 `_release_recovery_evidence`）。在原样树和对照树（e131 diff）上分别运行，
四方向都取 D123-0 对应的旋转格（由 `_canonical_fabric_dependency_manifest` 的 12 个下层格中，
与南向 z=0 对应的那一格）。报告：父动作状态、fallback 尾段边界、出口尾段是否与已知 AIR 时相同、
该格是否进入父证明依赖。
用法：python e131_optional_abandon_point.py   （在检出根目录运行）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav.motion_solver import _replay_verified_commands  # noqa: E402
from scripts import d123_dependency_probe as d123  # noqa: E402
from scripts.d118_momentum_calibration_runtime import _local_to_world  # noqa: E402

LOCAL = (0.5, 0.5)  # 南向 (0,62,0) 的格中心，按方向旋转


def cell_for(fixture, direction):
    x, z = _local_to_world(fixture.context.origin, direction, *LOCAL)
    import math
    return (math.floor(x), fixture.context.feet_y - 2, math.floor(z))


def run(direction):
    case = d123.fixed_case(direction)
    request, fixture, proof, _spec, snapshot = case
    cell = cell_for(fixture, direction)
    rows = {}
    for mode in ("air", "unknown"):
        world = d123.world_for(snapshot, {cell: d123.fact_for(snapshot, cell, mode)})
        variant, failure = _replay_verified_commands(proof.entry_state, proof.commands, world, request,
                                                     start_tick=1)
        rows[mode] = {
            "status": "solved" if failure is None else failure.status.value,
            "reasons": list(failure.reasons) if failure is not None else [],
            "fallback_tail_boundaries": ([t.boundary_index for t in variant.fallback_tails]
                                         if variant else None),
            "release_safe_command_indices": (list(variant.release_safe_command_indices)
                                             if variant else None),
            "exit_tail": (d123.digest(variant.fallback_tails[-1]) if variant and variant.fallback_tails
                          else None),
            "cell_in_parent_dependencies": (cell in set(variant.world_dependencies)
                                            | {p for t in variant.fallback_tails for p in t.dependencies}
                                            if variant else None),
        }
    rows["unknown"]["exit_tail_same_as_air"] = rows["unknown"]["exit_tail"] == rows["air"]["exit_tail"]
    for row in rows.values():
        row.pop("exit_tail")
    return {"cell": cell, **rows}


def main():
    print(json.dumps({str(direction): run(direction) for direction in range(4)}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
