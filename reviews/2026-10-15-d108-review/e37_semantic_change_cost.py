"""E37：沿用 D107 审查 E33，复测“语义变化（新看到一格、方块被换掉）”在观察线程上的成本。

F1 缩短场景用 TEST_ORACLE 预先灌入整张地图，运行中几乎没有语义变化（E28：53 批里只有 5 批
重建，其中 3 批在灌图阶段），所以它测不到“边走边看到新地形”的成本。本实验直接调用
WorldKnowledge：先把 k 个 section 各填满 16×16×16 个已知格（方块与空气各半），然后每轮在
这 k 个 section 里各把一格换成另一种方块（语义变化），计时一次 `observe_blocks_with_changes`。
同时给出“同样 k 格只刷新时间戳”（纯证据刷新）的成本作对照。
在 5c1485d（D103 前，无 section 快照）、8167d92（D107）、a9d51a2（D108）上比较。
只调用项目函数并计时，不改项目文件。运行：PYTHONPATH=<检出> python e37_semantic_change_cost.py
"""
import statistics, subprocess, time

from mc2p.motion_nav import world_model as WM

SESSION = WM.WorldSessionId("e33-world")
STONE = WM.BlockGeometry.full_cube("minecraft:stone")
DIRT = WM.BlockGeometry.full_cube("minecraft:dirt")


def stamp(sequence):
    return WM.ObservationStamp(SESSION, sequence, sequence, "e33-clock", sequence * 1_000_000)


def filled_world(sections):
    world = WM.WorldKnowledge(SESSION, max_known_cells=4096 * sections + 16)
    sequence = 1
    for index in range(sections):
        base = index * 16
        blocks = {(base + x, y, z): STONE for x in range(16) for y in range(8) for z in range(16)}
        air = tuple((base + x, y, z) for x in range(16) for y in range(8, 16) for z in range(16))
        world.observe_blocks(stamp(sequence), blocks)
        world.confirm_air(stamp(sequence + 1), air)
        sequence += 2
    return world, sequence


def measure(sections, rounds=15):
    world, sequence = filled_world(sections)
    semantic, refresh = [], []
    for round_index in range(rounds):
        geometry = DIRT if round_index % 2 == 0 else STONE
        cells = {(index * 16 + round_index % 16, 0, 0): geometry for index in range(sections)}
        started = time.perf_counter_ns()
        world.observe_blocks(stamp(sequence), cells)
        semantic.append((time.perf_counter_ns() - started) / 1e6)
        sequence += 1
        same = {position: geometry for position in cells}
        started = time.perf_counter_ns()
        world.observe_blocks(stamp(sequence), same)
        refresh.append((time.perf_counter_ns() - started) / 1e6)
        sequence += 1
    return statistics.median(semantic), max(semantic), statistics.median(refresh)


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}：每个 section 4096 个已知格；每批在 k 个 section 各改 1 格")
    for sections in (1, 2, 4, 8):
        rounds = 15 if sections <= 4 else 7
        median, worst, refresh = measure(sections, rounds)
        print(f"  k={sections}：语义变化一批 中位 {median:7.2f} ms（最大 {worst:7.2f} ms，约 {median / sections:5.2f} ms/section）；"
              f"同样 k 格只刷新时间戳 中位 {refresh:6.3f} ms")


if __name__ == "__main__":
    main()
