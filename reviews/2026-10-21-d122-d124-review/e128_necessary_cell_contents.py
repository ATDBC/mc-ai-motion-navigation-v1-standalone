"""E128：D124 剩下的南向 3 个必要格里放不同的原版内容，证明结论会怎样变。

只调用项目函数：`scripts/d123_dependency_probe` 的 `fixed_case`（D122 冻结的父 proof、
固定后继和快照）、`replay`（重放父动作并绑定后继，即 D123-0 用的同一入口）。
世界变化同样只作用在 detached 副本上。

每格依次放入：
  air / cube / unknown      —— D123-0 已比较过的三态
  fence_post                —— 原版栅栏柱碰撞箱 x,z∈[0.375,0.625]，高 1.5
  fence_x                   —— 东西向连接的栅栏，x∈[0,1]、z∈[0.375,0.625]，高 1.5
  wall_post                 —— 原版墙柱 x,z∈[0.25,0.75]，高 1.5
  bottom_slab / top_slab / tall_ordinary —— D123-0 探针已有的形状；tall_ordinary 是两格高石块，
                               只在项目 BlockGeometry 合同（Y 到 2）里合法，原版没有这种方块
报告父动作状态、后继绑定状态、联合依赖门禁，以及主轨迹、出口和 fallback 行为签名
是否与 air 相同；fallback 预期伤害取 replay 给出的最大值。
用法：python e128_necessary_cell_contents.py   （在检出根目录运行）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav.world_model import Aabb, BlockGeometry, CellFact, CellKnowledge  # noqa: E402
from scripts import d123_dependency_probe as d123  # noqa: E402

CELLS = ((0, 62, 0), (0, 62, 2), (0, 62, 5))
SHAPES = {
    "fence_post": (Aabb(.375, 0, .375, .625, 1.5, .625),),
    "fence_x": (Aabb(0, 0, .375, 1, 1.5, .625),),
    "wall_post": (Aabb(.25, 0, .25, .75, 1.5, .75),),
}


def fact(snapshot, position, mode):
    if mode in ("air", "cube", "unknown", "bottom_slab", "top_slab", "tall_ordinary"):
        return d123.fact_for(snapshot, position, mode)
    stamp = dict(snapshot.facts)[position].stamp
    return CellFact(CellKnowledge.BLOCK, stamp, BlockGeometry("minecraft:oak_fence", "boxes", SHAPES[mode]))


def main():
    case = d123.fixed_case(0)
    snapshot = case[4]
    report = {}
    for cell in CELLS:
        rows = {}
        baseline = None
        for mode in ("air", "cube", "unknown", *SHAPES, "bottom_slab", "top_slab", "tall_ordinary"):
            result = d123.replay(case, {cell: fact(snapshot, cell, mode)})
            if mode == "air":
                baseline = result
            rows[mode] = {
                "parent_status": result["parent_status"],
                "successor_binding_status": str(result["successor_binding_status"]),
                "successor_failure_reasons": list(result["successor_failure_reasons"] or ()),
                "offline_dependency_gate": result["offline_dependency_gate"],
                "main_same_as_air": result.get("main_signature") == baseline.get("main_signature"),
                "exit_same_as_air": result.get("exit_signature") == baseline.get("exit_signature"),
                "fallback_behavior_same_as_air": (result.get("fallback_behavior_signature")
                                                  == baseline.get("fallback_behavior_signature")),
                "max_fallback_damage": result.get("damage"),
            }
        report[str(cell)] = rows
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
