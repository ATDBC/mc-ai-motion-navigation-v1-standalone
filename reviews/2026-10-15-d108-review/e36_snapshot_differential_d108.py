"""E36：D108 的“观察只失效、借用按范围物化、增量复用”与 D106／D107 的快照内容是否逐字节一致。

沿用 D107 审查 E29 的同一随机操作序列（固定种子，操作与随机数消耗完全相同），在 98a0542（D106）、
8167d92（D107）和 a9d51a2（D108）上运行，比较全部可观察结果的摘要：更新结果、封存快照规范字节、
发布与取租约、依赖查询。

D108 检出上另做两类检查：
  - 每步：缓存的不可变 section 只能是实时世界的子集，且不得在位置集合、知识类型、几何或依赖版本上
    滞后；待物化集合只含仍有事实的 section；逐格脏标记只挂在待物化 section 上（有界性）；
  - 每次借用：借出的 section 集合恰好等于范围内有实时事实的 section；借出的每个 section 与
    “按实时世界从头构造”的结果逐项相等（含时间戳和视觉证据），
    直接检验新增的增量复用路径。并统计增量复用路径实际被走到的次数。
变异（只在进程内替换，验证本实验的灵敏度）：
  --mutate-skip-retire       语义变化后不撤下缓存（漏失效）
  --mutate-skip-materialize  借用时不物化（漏范围物化）
  --mutate-forget-dirty-cell 纯证据刷新不登记逐格脏标记（增量复用会沿用旧事实）
只调用项目函数，不改项目文件。
运行：PYTHONPATH=<检出> python e36_snapshot_differential_d108.py [种子数] [变异参数]
"""
import hashlib, pathlib, random, subprocess, sys, uuid

from mc2p.motion_nav import world_model as WM
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.world_snapshot import seal_snapshot_version


class DeterministicUuid:
    def __init__(self):
        self.counter = 0

    def uuid4(self):
        self.counter += 1
        return uuid.UUID(int=(0x5EED << 64) + self.counter)

    def __getattr__(self, name):
        return getattr(uuid, name)


WM.uuid = DeterministicUuid()

SESSION = WM.WorldSessionId("e29-world")
GEOMETRIES = (
    WM.BlockGeometry.full_cube("minecraft:stone"),
    WM.BlockGeometry.full_cube("minecraft:dirt"),
    WM.BlockGeometry("minecraft:oak_slab", "boxes", (WM.Aabb(0, 0, 0, 1, 0.5, 1),)),
    WM.BlockGeometry.empty("minecraft:short_grass"),
)


def stamp(sequence):
    return WM.ObservationStamp(SESSION, sequence, sequence, "e29-clock", 1_000_000 * sequence)


def semantic_view(cells):
    return tuple(sorted((position, fact.knowledge, fact.block) for position, fact in cells))


D108 = hasattr(WM.WorldKnowledge, "_retire_changed_snapshot_sections")
D107 = hasattr(WM.ImmutableWorldSection, "_from_validated_observation") and not D108
stats = {"invariant_checks": 0, "borrow_checks": 0, "incremental_reuse": 0, "full_build": 0}
failures = []


def expected_section(world, position):
    live = world._sections[position]
    facts = tuple(sorted(live.items()))
    versions = tuple((cell, world._snapshot_epoch + world._cell_semantic_versions[cell]) for cell, _ in facts)
    return facts, versions


def check_invariant(world):
    if not (D107 or D108):
        return
    live_sections = {position for position, cells in world._sections.items() if cells}
    if D107 and set(world._snapshot_sections) != live_sections:
        raise AssertionError("committed sections differ from live sections")
    if D108:
        if not set(world._snapshot_sections) <= live_sections:
            raise AssertionError("cache retains a section without live facts")
        if not world._snapshot_dirty_sections <= live_sections:
            raise AssertionError("pending set retains a section without live facts")
        if not set(world._snapshot_dirty_positions) <= world._snapshot_dirty_sections:
            raise AssertionError("per-cell dirty marks outside the pending set")
        if any(not cells <= set(world._sections.get(section, {}))
               for section, cells in world._snapshot_dirty_positions.items()):
            raise AssertionError("per-cell dirty mark names a removed cell")
    for position, section in world._snapshot_sections.items():
        live = world._sections[position]
        if semantic_view(section.facts) != semantic_view(live.items()):
            raise AssertionError(f"cached section {position} lags in collision semantics")
        if section.dependency_versions != expected_section(world, position)[1]:
            raise AssertionError(f"cached section {position} dependency versions lag")
    stats["invariant_checks"] += 1


def check_borrow(world, version, bounds):
    if not D108:
        return
    size = WM.WORLD_SECTION_SIZE
    requested = {
        (x, y, z)
        for x in range(bounds.min_x // size, bounds.max_x // size + 1)
        for y in range(bounds.min_y // size, bounds.max_y // size + 1)
        for z in range(bounds.min_z // size, bounds.max_z // size + 1)
    }
    live = {position for position, cells in world._sections.items() if cells}
    if {section.position for section in version.section_references} != requested & live:
        raise AssertionError("borrowed sections differ from the live sections inside the bounds")
    for section in version.section_references:
        facts, versions = expected_section(world, section.position)
        if section.facts != facts or section.dependency_versions != versions:
            raise AssertionError(f"borrowed section {section.position} differs from a fresh build")
    stats["borrow_checks"] += 1


def guarded(check, *args):
    if failures:
        return
    try:
        check(*args)
    except AssertionError as error:
        failures.append(str(error))


def run(seed, steps):
    rng = random.Random(seed)
    world = WM.WorldKnowledge(SESSION, max_known_cells=240, protection_radius_blocks=4.0)
    world.set_protection_center((8.0, 8.0, 8.0))
    sequence = 10
    leases = []
    lines = []
    outcomes = {}
    known = []
    for step in range(steps):
        sequence += 1
        kind = rng.choices(("blocks", "refresh", "air", "invalidate", "borrow", "release", "deps"),
                           weights=(22, 22, 16, 6, 18, 6, 10))[0]
        observed = stamp(sequence if rng.random() > 0.08 else max(1, sequence - rng.randint(1, 6)))
        if kind == "blocks":
            cells = {(rng.randrange(0, 40), rng.randrange(0, 20), rng.randrange(0, 20)): rng.choice(GEOMETRIES)
                     for _ in range(rng.randint(1, 30))}
            result, changed = world.observe_blocks_with_changes(observed, cells)
            known.extend(cells)
            line = (kind, result, changed)
        elif kind == "refresh" and known:
            # Same geometry with a newer stamp: an evidence-only refresh in both versions.
            cells = {}
            for position in rng.sample(known, min(len(known), rng.randint(1, 40))):
                fact = world.view().cell(position)
                if fact.knowledge is WM.CellKnowledge.BLOCK:
                    cells[position] = fact.block
            result, changed = world.observe_blocks_with_changes(observed, cells)
            line = (kind, result, changed)
        elif kind == "air":
            positions = tuple({(rng.randrange(0, 40), rng.randrange(0, 20), rng.randrange(0, 20))
                               for _ in range(rng.randint(1, 20))})
            evidence = {position: WM.VisualAirEvidence(observed, rng.choice((0.5, 2.0, 6.0)), rng.random() < 0.5)
                        for position in positions if rng.random() < 0.4}
            line = (kind, world.confirm_air(observed, positions, evidence))
            known.extend(positions)
        elif kind == "invalidate" and known:
            positions = tuple(rng.sample(known, min(len(known), rng.randint(1, 6))))
            world.invalidate(observed, positions)
            line = (kind, positions)
        elif kind == "borrow":
            x, y, z = rng.randrange(0, 34), rng.randrange(0, 14), rng.randrange(0, 14)
            bounds = PhysicsWorldBounds(x, x + rng.randint(1, 6), y, y + rng.randint(1, 6), z, z + rng.randint(1, 6))
            version = world.borrow_snapshot_version(bounds, ruleset=JAVA_1_21_RULESET)
            if version is None:
                line = (kind, None)
            else:
                guarded(check_borrow, world, version, bounds)
                sealed = seal_snapshot_version(version, JAVA_1_21_RULESET)
                published = world.publish_sealed_snapshot(version, sealed)
                probe = PhysicsWorldBounds(x, x + 1, y, y + 1, z, z + 1)
                acquired = world.acquire_published_snapshot(probe, ruleset=JAVA_1_21_RULESET)
                if acquired.lease is not None:
                    leases.append(acquired.lease)
                for name in ("published" if published else "not_published", f"lease_{acquired.status.value}"):
                    outcomes[name] = outcomes.get(name, 0) + 1
                line = (kind, hashlib.sha256(sealed.canonical_bytes).hexdigest(), published,
                        acquired.status, None if acquired.lease is None else
                        (acquired.lease.identity.local_revision,
                         hashlib.sha256(acquired.lease.snapshot.canonical_bytes).hexdigest()))
        elif kind == "release" and leases:
            lease = leases.pop(rng.randrange(len(leases)))
            line = (kind, world.release_snapshot(lease.lease_token))
        elif kind == "deps" and known:
            positions = tuple(rng.sample(known, min(len(known), rng.randint(1, 8))))
            versions = world.dependency_versions(positions)
            line = (kind, versions, world.dependency_changes(versions))
        else:
            line = ("skip", kind)
        guarded(check_invariant, world)
        lines.append(f"{seed}:{step}:{line!r}")
    return lines, outcomes


def install_mutations():
    labels = []
    if "--mutate-skip-retire" in sys.argv:
        WM.WorldKnowledge._retire_changed_snapshot_sections = lambda self, sections: None
        labels.append("语义变化后不撤下缓存")
    if "--mutate-skip-materialize" in sys.argv:
        WM.WorldKnowledge._commit_snapshot_sections = lambda self, sections: None
        labels.append("借用时不物化")
    if "--mutate-forget-dirty-cell" in sys.argv:
        original_apply = WM.WorldKnowledge._apply

        def forgetful_apply(self, position, incoming, **kwargs):
            outcome = original_apply(self, position, incoming, **kwargs)
            section = WM.block_section(position)
            marks = self._snapshot_dirty_positions.get(section)
            if marks is not None and position not in self._snapshot_changed_positions:
                marks.discard(position)
            return outcome
        WM.WorldKnowledge._apply = forgetful_apply
        labels.append("纯证据刷新不登记逐格脏标记")
    return labels


def count_build_paths():
    if not D108:
        return
    original = WM.WorldKnowledge._commit_snapshot_sections

    def counted(self, sections):
        for position in self._snapshot_dirty_sections.intersection(sections):
            cells = self._sections.get(position)
            previous = self._snapshot_sections.get(position)
            if not cells:
                continue
            if (previous is not None and len(previous.facts) == len(cells)
                    and all(cell in cells for cell, _ in previous.facts)):
                stats["incremental_reuse"] += 1
            else:
                stats["full_build"] += 1
        return original(self, sections)
    WM.WorldKnowledge._commit_snapshot_sections = counted


def main():
    tree = pathlib.Path(WM.__file__).resolve().parents[2]
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    count_build_paths()
    labels = install_mutations()
    total = hashlib.sha256()
    counts = {}
    numbers = [value for value in sys.argv[1:] if value.isdigit()]
    for seed in range(int(numbers[0]) if numbers else 8):
        lines, outcomes = run(seed, 600)
        for line in lines:
            total.update(line.encode())
            kind = line.split(":", 2)[2].split("'")[1]
            counts[kind] = counts.get(kind, 0) + 1
        for name, value in outcomes.items():
            counts["借用:" + name] = counts.get("借用:" + name, 0) + value
    suffix = f"（变异：{'、'.join(labels)}）" if labels else ""
    print(f"检出 {head}{suffix}：操作计数 {dict(sorted(counts.items()))}")
    print(f"  全部可观察结果摘要 {total.hexdigest()}")
    if D108:
        print(f"  D108 每步不变量 {stats['invariant_checks']} 次、借用逐项比对 {stats['borrow_checks']} 次；"
              f"借用时增量复用 {stats['incremental_reuse']} 个 section、整段构造 {stats['full_build']} 个")
    elif D107:
        print(f"  D107 不变量检查 {stats['invariant_checks']} 次")
    print("  检查失败：" + (failures[0] if failures else "无"))


if __name__ == "__main__":
    main()
