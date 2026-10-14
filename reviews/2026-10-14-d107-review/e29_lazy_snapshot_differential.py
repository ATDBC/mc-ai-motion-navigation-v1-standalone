"""E29：D107 惰性提交与 D106 即时提交的快照内容是否逐字节一致。

同一随机操作序列（固定种子）分别在 98a0542（D106，每批观察都重建 section）和 8167d92
（D107，语义变化即时重建、纯证据刷新推迟到借用时）上运行。操作包括：
  - observe_blocks：新方块、换材质、同材质刷新时间戳（纯证据刷新）、乱序旧时间戳；
  - confirm_air：带或不带视觉证据，也会刷新已有空气的证据；
  - invalidate：墓碑删除；
  - 小容量触发整 section 淘汰；
  - 借用快照 → 封存 → 发布 → 按随机范围取租约 → 随机归还；
  - dependency_versions／dependency_changes 查询。
每一步把可观察结果（更新结果、封存快照的规范字节摘要、发布与取租约结果、依赖查询）写成一行摘要。
两个检出的输出应完全相同。为让两边可比，把 world_model 里的 uuid4 换成确定性计数器。

另外在 D107 检出上，每步检查一个不变量：已提交的不可变 section 与实时世界的“位置集合、
知识类型、几何”一致（只允许时间戳和视觉证据滞后），依赖版本也一致。
灵敏度对照：加参数 `--mutate-skip-borrow-commit` 时，在进程内把“借用时补提交”换成空操作，
模拟忘记合并纯证据刷新；此时摘要应与正常运行不同，说明本实验能发现快照滞后。
修法探针：加参数 `--defer-semantic-commit` 时，观察批次结束后只让已发布快照失效、不立即重建
section（几何变化也推迟到借用时合并）。若摘要仍与 D106／D107 相同，说明“几何变化立即重建”
对可观察结果不是必需的，重建可以整体移出控制线程（此模式下跳过“section 不滞后”的不变量检查）。
只调用项目函数，不改项目文件。运行：PYTHONPATH=<检出> python e29_lazy_snapshot_differential.py [种子数] [--mutate-skip-borrow-commit]
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


def check_invariant(world):
    """D107-only: committed immutable sections never lag in collision semantics."""
    if not hasattr(WM.ImmutableWorldSection, "_from_validated_observation"):
        return 0
    checked = 0
    live_sections = {position for position, cells in world._sections.items() if cells}
    if set(world._snapshot_sections) != live_sections:
        raise AssertionError(f"committed sections differ: {sorted(set(world._snapshot_sections) ^ live_sections)}")
    for position, section in world._snapshot_sections.items():
        live = world._sections[position]
        if semantic_view(section.facts) != semantic_view(live.items()):
            raise AssertionError(f"section {position} lags in collision semantics")
        expected = tuple((cell, world._snapshot_epoch + world._cell_semantic_versions[cell])
                         for cell, _ in sorted(live.items()))
        if section.dependency_versions != expected:
            raise AssertionError(f"section {position} dependency versions differ")
        checked += 1
    return checked


def run(seed, steps):
    rng = random.Random(seed)
    world = WM.WorldKnowledge(SESSION, max_known_cells=240, protection_radius_blocks=4.0)
    world.set_protection_center((8.0, 8.0, 8.0))
    sequence = 10
    leases = []
    lines = []
    outcomes = {}
    known = []
    invariant_checks = 0
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
        invariant_checks += check_invariant(world)
        lines.append(f"{seed}:{step}:{line!r}")
    return lines, invariant_checks, outcomes


def main():
    tree = pathlib.Path(WM.__file__).resolve().parents[2]
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if "--mutate-skip-borrow-commit" in sys.argv:
        original_commit = WM.WorldKnowledge._commit_snapshot_sections

        def skip_borrow_commit(self, sections=None):
            if sections is not None:
                return original_commit(self, sections)
        WM.WorldKnowledge._commit_snapshot_sections = skip_borrow_commit
        head += "（变异：借用时不补提交）"
    if "--defer-semantic-commit" in sys.argv:
        original_commit = WM.WorldKnowledge._commit_snapshot_sections

        def defer_batch_commit(self, sections=None):
            if sections is None:
                return original_commit(self)
        WM.WorldKnowledge._commit_snapshot_sections = defer_batch_commit
        global check_invariant
        check_invariant = lambda world: 0
        head += "（修法探针：几何变化也推迟到借用时重建）"
    total = hashlib.sha256()
    counts = {}
    checks = 0
    numbers = [value for value in sys.argv[1:] if value.isdigit()]
    for seed in range(int(numbers[0]) if numbers else 8):
        lines, checked, outcomes = run(seed, 600)
        checks += checked
        for name, value in outcomes.items():
            counts["借用:" + name] = counts.get("借用:" + name, 0) + value
        for line in lines:
            total.update(line.encode())
            kind = line.split(":", 2)[2].split("'")[1]
            counts[kind] = counts.get(kind, 0) + 1
    print(f"检出 {head}：操作计数 {dict(sorted(counts.items()))}")
    print(f"  全部可观察结果摘要 {total.hexdigest()}")
    if "--defer-semantic-commit" in sys.argv:
        print("  （修法探针模式：section 按设计可滞后到借用时，不做不变量检查）")
    else:
        print(f"  D107 不变量检查（section 语义与依赖版本不滞后）：{checks} 次" if checks else
              "  （该检出没有惰性提交，不做不变量检查）")


if __name__ == "__main__":
    main()
