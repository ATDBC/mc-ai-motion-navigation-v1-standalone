"""TP Task 5A 审查探针：现有参考搜索在更宽跨隙和更大输入集上的规模。

运行：在仓库根目录 PYTHONPATH=. python <本文件>
只读运行。世界、入口、晚支和目标形状沿用 representative_fixture("jump_gap_1")，
只改变空隙宽度、目标位置、输入字母表和计数上限。节点／step 计数与平台无关；
墙钟只在外层测量，是 Linux 补充数据，不代表 Windows 性能。
"""
from dataclasses import replace
import time

from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.reference_search import reference_search
from experiments.motion_navigation.trajectory_proto.scenarios import P0_BUDGET, representative_fixture
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, CellFact, CellKnowledge, WorldView,
)

WALK = TickInput(1., 0., False, False, False, 0.)
WALK_JUMP = TickInput(1., 0., True, False, False, 0.)
NEUTRAL = TickInput(0., 0., False, False, False, 0.)
SPRINT = TickInput(1., 0., False, False, True, 0.)
SPRINT_JUMP = TickInput(1., 0., True, False, True, 0.)
BACK = TickInput(-1., 0., False, False, False, 0.)

ALPHABETS = {
    "A3 walk/jump/neutral": (WALK, WALK_JUMP, NEUTRAL),
    "A5 +sprint/sprint_jump": (WALK, WALK_JUMP, NEUTRAL, SPRINT, SPRINT_JUMP),
    "A6 +back": (WALK, WALK_JUMP, NEUTRAL, SPRINT, SPRINT_JUMP, BACK),
}


def gap_case(width, start=1):
    """Gap cells are z=start..start+width-1; the anchor stays at z=0.5."""
    base = representative_fixture("jump_gap_1")
    stamp = base.world.cell((0, 0, 0)).stamp
    facts = {}
    for x in range(-3, 4):
        for y in range(-5, 7):
            for z in range(-3, 13):
                solid = y == 0 and not start <= z < start + width
                facts[(x, y, z)] = CellFact(
                    CellKnowledge.BLOCK if solid else CellKnowledge.AIR, stamp,
                    BlockGeometry.full_cube("minecraft:stone") if solid else None)
    world = PhysicsWorldView(WorldView.detached(base.world.session, 3, 0, facts), JAVA_1_21_RULESET)
    landing = start + width
    goal = replace(base.request.goal, region=Aabb(.35, 1., landing + .3, .65, 1.05, landing + 1.3))
    return base.request, world, goal


def run(label, width, alphabet, budget):
    request, world, goal = gap_case(width)
    request = replace(request, goal=goal, supported_inputs=alphabet, budget=budget,
                      request_id=f"scale-gap{width}")
    started = time.perf_counter()
    result = reference_search(request, world)
    elapsed = (time.perf_counter() - started) * 1000.
    print(f"gap{width} {label:24s} budget={budget.max_nodes}/{budget.max_physics_steps} "
          f"-> {result.status.value}/{result.reason.value} expanded={result.expanded_nodes} "
          f"nodes={result.counts.nodes} steps={result.counts.physics_steps} "
          f"tail={result.counts.tail_ticks} ticks={result.candidate_ticks} {elapsed:.0f} ms")
    return result


if __name__ == "__main__":
    big = SearchBudget(200_000, 4_000_000, 40, 2)
    for name in ("flat_walk", "jump_up_straight", "jump_gap_1"):
        fixture = representative_fixture(name)
        started = time.perf_counter()
        result = reference_search(fixture.request, fixture.world)
        print(f"calibration {name:18s} {result.status.value} nodes={result.counts.nodes} "
              f"steps={result.counts.physics_steps} {(time.perf_counter() - started) * 1000.:.0f} ms")
    for width in (1, 2, 3):
        for label, alphabet in ALPHABETS.items():
            result = run(label, width, alphabet, P0_BUDGET)
            if result.status.value != "FOUND":
                run(label, width, alphabet, big)
