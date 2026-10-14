"""E34：D107 的“借用时补提交”在接收线程上持锁重建全部脏 section，控制线程会被挡多久。

`borrow_snapshot_version` 在持有 `_snapshot_version_lock` 时调用无参数的
`_commit_snapshot_sections()`，会重建世界里**所有**带待合并证据的 section，而不只是本次借用范围内的。
控制线程每次摄取观察（`observe_blocks` 等）都用阻塞方式取同一把锁。
实验：先建 N 个满 section（各 4096 个已知格），再在每个 section 刷新 1 格的时间戳（纯证据刷新，
D107 下这一步很便宜，但让 N 个 section 都变脏）；然后在后台线程借用一个只覆盖 1 个 section 的小范围，
主线程在借用开始重建后立刻摄取一批普通观察（刷新 1 格），记录主线程这一批的等待+执行时间。
对照：进程内把补提交限制为借用范围内的 section（只为说明修法，不改项目文件）。
只调用项目函数并计时。运行：PYTHONPATH=<检出> python e34_borrow_lock_stall.py
"""
import statistics, subprocess, threading, time

from mc2p.motion_nav import world_model as WM
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET

SESSION = WM.WorldSessionId("e34-world")
STONE = WM.BlockGeometry.full_cube("minecraft:stone")
ORIGINAL_COMMIT = WM.WorldKnowledge._commit_snapshot_sections
ORIGINAL_BORROW = WM.WorldKnowledge.borrow_snapshot_version
local = threading.local()


def stamp(sequence):
    return WM.ObservationStamp(SESSION, sequence, sequence, "e34-clock", sequence * 1_000_000)


def filled_world(sections):
    world = WM.WorldKnowledge(SESSION, max_known_cells=4096 * sections + 16)
    sequence = 1
    for index in range(sections):
        base = index * 16
        world.observe_blocks(stamp(sequence), {(base + x, y, z): STONE
                                               for x in range(16) for y in range(8) for z in range(16)})
        world.confirm_air(stamp(sequence + 1), tuple((base + x, y, z)
                                                    for x in range(16) for y in range(8, 16) for z in range(16)))
        sequence += 2
    return world, sequence


def bounded_commit(self, sections=None):
    """Variant: a borrow materializes only the sections its bounds reference."""
    bounds = getattr(local, "bounds", None)
    if sections is None and bounds is not None:
        sections = {(x, y, z)
                    for x in range(bounds.min_x // 16, bounds.max_x // 16 + 1)
                    for y in range(bounds.min_y // 16, bounds.max_y // 16 + 1)
                    for z in range(bounds.min_z // 16, bounds.max_z // 16 + 1)}
    return ORIGINAL_COMMIT(self, sections)


def bounded_borrow(self, bounds, *, ruleset):
    local.bounds = bounds
    try:
        return ORIGINAL_BORROW(self, bounds, ruleset=ruleset)
    finally:
        local.bounds = None


def trial(sections, variant):
    WM.WorldKnowledge._commit_snapshot_sections = ORIGINAL_COMMIT if variant == "project" else bounded_commit
    WM.WorldKnowledge.borrow_snapshot_version = ORIGINAL_BORROW if variant == "project" else bounded_borrow
    world, sequence = filled_world(sections)
    # One evidence-only refresh per section: cheap now, but every section becomes dirty.
    world.observe_blocks(stamp(sequence), {(index * 16, 0, 0): STONE for index in range(sections)})
    sequence += 1
    started_commit = threading.Event()
    commit = WM.WorldKnowledge._commit_snapshot_sections

    def signalling_commit(self, *args, **kwargs):
        if threading.current_thread().name == "receiver-like":
            started_commit.set()
        return commit(self, *args, **kwargs)
    WM.WorldKnowledge._commit_snapshot_sections = signalling_commit
    borrow_time = []

    def receiver():
        begun = time.perf_counter_ns()
        version = world.borrow_snapshot_version(PhysicsWorldBounds(0, 3, 0, 3, 0, 3), ruleset=JAVA_1_21_RULESET)
        borrow_time.append(((time.perf_counter_ns() - begun) / 1e6, version is not None))
    thread = threading.Thread(target=receiver, name="receiver-like")
    thread.start()
    started_commit.wait(5)
    begun = time.perf_counter_ns()
    world.observe_blocks(stamp(sequence), {(0, 1, 0): STONE})
    control_ms = (time.perf_counter_ns() - begun) / 1e6
    thread.join()
    WM.WorldKnowledge._commit_snapshot_sections = ORIGINAL_COMMIT
    WM.WorldKnowledge.borrow_snapshot_version = ORIGINAL_BORROW
    return control_ms, borrow_time[0][0], borrow_time[0][1]


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}：N 个满 section 都只有纯证据刷新待合并；后台借用 1 个 section 的范围，主线程同时摄取一批观察")
    for sections in (4, 16, 32):
        for variant in ("project", "bounded"):
            rows = [trial(sections, variant) for _ in range(3)]
            control = statistics.median(row[0] for row in rows)
            borrow = statistics.median(row[1] for row in rows)
            label = "项目现状（补提交全部脏 section）" if variant == "project" else "对照（只补提交借用范围）"
            print(f"  N={sections:>2} {label}：主线程这批观察 中位 {control:7.1f} ms（最大 {max(r[0] for r in rows):7.1f}）；"
                  f"后台借用 中位 {borrow:7.1f} ms；借用成功 {all(r[2] for r in rows)}")


if __name__ == "__main__":
    main()
