"""E17：不漏短风险的“少算尾迹”办法——逐边界都有判定，但多数判定不用推演。

两种模式，都对每个边界给出判定，不按事件点抽样：

A. exact-skip：只跳过旧 `_risks` 本身不看的边界。风险区间已经打开后，旧代码只在
   “边界状态着地、支撑安全、尾迹 SAFE”时关闭区间；空中或支撑不安全的边界无论尾迹
   结果如何都不改变区间。A 在这些边界不推演尾迹，其余边界一律调用原 `_tail`。
   这是对旧判定的等价变换（例外：被跳过的尾迹若本会触发 NEEDS_INFORMATION／未停稳，
   旧扫描整体拒绝，A 不会——见输出中的 superset 计数）。
B. certify：在 A 的基础上，着地边界先做保守停车包络检查（不推演）：
   - 状态着地、站立、不在流体／攀爬／飞行，脚底恰在整格高度，未低于 min_y，前缀伤害未超额；
   - 不可撤回输入不含跳跃；
   - 水平位移上界 D = (|v| + a) / (1 − 0.6×0.91) + 0.05，a 为该 tick 输入的最大地面加速度
     （movement_speed × 1.3 × 输入幅度；计算器只支持滑度 0.6 的普通材料）；
   - 身体盒向四周扩 D 覆盖的每一列：脚下一格是已知普通整方块，身体两格是已知 AIR。
   通过则判 SAFE（并把这些格加入依赖），否则回退原 `_tail`。

检查：
1. 28 个 A／C／D 样本：与旧 scan_commitment 的风险区间逐项比较，统计物理步与耗时；
2. K1 复现：把 `_tail` 在边界 5 改成 UNSAFE（原审查的 mock）；
3. 世界变异：在路径附近随机挖洞、放障碍、删成 UNKNOWN，凡 B 判 SAFE 的边界都再跑原 `_tail`，
   统计“包络判 SAFE 但真实尾迹不 SAFE”的次数（必须为 0），并统计真实出现的
   “SAFE→UNSAFE→SAFE 短风险”以及 K1 候选的事件点规则会不会漏掉它们。
只调用项目函数，不改项目文件。运行：PYTHONPATH=<项目根> python e17_sound_sparse_scan.py
"""
import importlib.util, math, random, statistics, sys, time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto import commitment as C
from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch
from experiments.motion_navigation.trajectory_proto.measurement import _set_a, _set_c, _set_d
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from mc2p.motion_nav import physics_1_21
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.world_model import BlockGeometry, CellFact, CellKnowledge, WorldView

FRICTION = 0.6 * 0.91
ORDINARY = physics_1_21._ORDINARY_MATERIALS
ROOT = Path(C.__file__).resolve().parents[3]


def certificate(state, irrevocable, world, minimum_y, prefix_damage, maximum_damage):
    if (not state.on_ground or state.pose != "standing" or state.swimming or state.submerged_in_water
            or state.climbing or state.fall_flying or state.flying or state.status_effects):
        return None
    y = state.position[1]
    level = round(y)
    if abs(y - level) > 1e-7 or y < minimum_y - 1e-7 or prefix_damage > maximum_damage + 1e-9:
        return None
    command = irrevocable[0] if irrevocable else None
    if command is not None and command.jump:
        return None
    magnitude = 0.0 if command is None else math.hypot(command.forward, command.strafe)
    accel = state.movement_speed_attribute * 1.3 * max(1.0, magnitude) if magnitude > 0 else 0.0
    reach = (math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) + accel) / (1 - FRICTION) + .05
    half = state.body_width / 2
    x, _, z = state.position
    cells = []
    for cx in range(math.floor(x - half - reach), math.floor(x + half + reach) + 1):
        for cz in range(math.floor(z - half - reach), math.floor(z + half + reach) + 1):
            below = world.cell((cx, level - 1, cz))
            if (below.knowledge is not CellKnowledge.BLOCK or below.block is None
                    or below.block.collision_kind != "full_cube" or below.block.fluid
                    or below.block.material_key not in ORDINARY):
                return None
            for dy in (0, 1):
                if world.cell((cx, level + dy, cz)).knowledge is not CellKnowledge.AIR:
                    return None
            cells += [(cx, level - 1, cz), (cx, level, cz), (cx, level + 1, cz)]
    return tuple(cells)


def sparse_scan(request, inputs, world, evidence, *, use_certificate):
    """Same acceptance structure as scan_commitment; returns (intervals per branch, counter, stats)."""
    counter = CountedPhysics(request.budget)
    options = C.ScanOptions()
    results, stats = [], dict(certified=0, exact=0, skipped=0)
    for index, rows in enumerate(evidence):
        counter.node()
        prelude, _ = C._prove_entry(counter, request, index, world)
        deps = set()
        states = [prelude[-1]]
        for command in inputs:
            states.append(C._calculated(counter, states[-1], command, world, deps))
        if C._damage(tuple(prelude[:-1]) + tuple(states)) > request.task_damage_budget.maximum_expected_damage_points:
            raise C._Incomplete(C.ScanStatus.CANDIDATE_REJECTED, C.CandidateRejection.DAMAGE_ALLOWANCE)
        if not states[-1].on_ground:
            raise C._Incomplete(C.ScanStatus.CANDIDATE_REJECTED, C.CandidateRejection.FINAL_EXIT_NOT_GROUNDED)
        minimum_y = min(states[0].position[1], states[-1].position[1])
        maximum = request.task_damage_budget.maximum_expected_damage_points
        first, intervals, last = None, [], len(inputs)
        for row in rows:
            b = row.boundary
            state = states[b]
            if first is not None and b != last:
                if not state.on_ground or not C._safe_support(state, world, deps):
                    stats["skipped"] += 1
                    continue
            prefix = tuple(prelude[:-1]) + tuple(states[:b + 1])
            cert = certificate(state, row.irrevocable_inputs, world, minimum_y, C._damage(prefix), maximum) \
                if use_certificate else None
            if cert is not None:
                stats["certified"] += 1
                safe = True
                deps.update(cert)
            else:
                stats["exact"] += 1
                tail = C._tail(counter, b, prefix, row.irrevocable_inputs, request.stop_input, world,
                               minimum_y, maximum, options)
                deps.update(tail.dependencies)
                safe = tail.status is C.TailStatus.SAFE_STOP
            if request.branch_preludes[index] and b == 0 and not safe:
                raise C._Incomplete(C.ScanStatus.CANDIDATE_REJECTED, C.CandidateRejection.PRELUDE_UNSAFE)
            if b == last and not safe:
                raise C._Incomplete(C.ScanStatus.CANDIDATE_REJECTED, C.CandidateRejection.FINAL_STOP_UNSAFE)
            if not safe and first is None:
                first = b
            elif safe and first is not None and state.on_ground and C._safe_support(state, world, deps):
                intervals.append((first - 1 if first else None, first, b))
                first = None
        if first is not None:
            raise C._Incomplete(C.ScanStatus.CANDIDATE_REJECTED, C.CandidateRejection.UNRECOVERED_RISK)
        results.append(tuple(intervals))
    return tuple(results), counter, stats


def old_intervals(scan):
    return tuple(tuple((r.last_abandon_boundary, r.first_committed_boundary, r.recovered_boundary)
                       for r in branch.risk_intervals) for branch in scan.proof.branches)


def timed(fn, n=5):
    out, value = [], None
    for _ in range(n):
        t = time.perf_counter_ns(); value = fn(); out.append((time.perf_counter_ns() - t) / 1e6)
    return statistics.median(out), value


def main():
    global k1proto
    # The K1 prototype is archived as evidence; load it under its original package name.
    _spec = importlib.util.spec_from_file_location(
        "experiments.motion_navigation.trajectory_proto.key_boundary_proof",
        ROOT / "evidence/motion_navigation/key-boundary-motion-proof-d104-v1/prototype-key-boundary-proof.py")
    k1proto = importlib.util.module_from_spec(_spec)
    k1proto.__package__ = "experiments.motion_navigation.trajectory_proto"
    sys.modules[_spec.name] = k1proto
    _spec.loader.exec_module(k1proto)

    # ---------- 1. frozen samples ----------
    print("== 1. 28 frozen A/C/D samples, both start branches ==")
    cases = list(_set_a(4)) + list(_set_c(6)) + list(_set_d(18))
    totals = {"old": 0, "A": 0, "B": 0}
    times = {"old": 0., "A": 0., "B": 0.}
    mismatch = 0
    d18 = {"old": 0, "A": 0, "B": 0}
    for set_id, key, request, world, inputs, *_ in cases:
        evidence = _boundary_evidence(request, inputs)
        t_old, old = timed(lambda: C.scan_commitment(request, inputs, world, evidence))
        assert old.status is C.ScanStatus.VERIFIED_CANDIDATE, key
        row = [f"{key:26s} old {old.counts.physics_steps:4d}"]
        totals["old"] += old.counts.physics_steps; times["old"] += t_old
        if set_id == "D":
            d18["old"] += old.counts.physics_steps
        for mode, flag in (("A", False), ("B", True)):
            t, (intervals, counter, stats) = timed(lambda: sparse_scan(request, inputs, world, evidence, use_certificate=flag))
            same = intervals == old_intervals(old)
            mismatch += not same
            totals[mode] += counter.physics_steps; times[mode] += t
            if set_id == "D":
                d18[mode] += counter.physics_steps
            row.append(f"{mode} {counter.physics_steps:4d} (cert {stats['certified']:2d} exact {stats['exact']:2d} skip {stats['skipped']:2d}) {'=' if same else 'DIFF'}")
        print(" | ".join(row), old_intervals(old)[0])
    print(f"physics steps total: old {totals['old']}, A {totals['A']} ({100*totals['A']/totals['old']:.0f}%), "
          f"B {totals['B']} ({100*totals['B']/totals['old']:.0f}%); interval mismatches vs old: {mismatch}")
    print(f"D18 (K0 set) physics steps: old {d18['old']}, A {d18['A']}, B {d18['B']}  (K1 candidate on Windows: 11,072 -> 5,263)")
    print(f"Linux wall time sum of medians: old {times['old']:.0f} ms, A {times['A']:.0f} ms, B {times['B']:.0f} ms")

    fixture = m0_probe._fixture_for("jump_gap_1", "A3")
    import json
    frozen = json.loads((ROOT / "experiments/motion_navigation/trajectory_proto/data/d097a-set-a-sample.json").read_text())
    from mc2p.motion_nav.physics_types import TickInput
    gap_inputs = tuple(TickInput(*r) for r in frozen["inputs"]["jump_gap_1:A3"])
    gap_ev = _boundary_evidence(fixture.request, gap_inputs)
    t_old, old = timed(lambda: C.scan_commitment(fixture.request, gap_inputs, fixture.world, gap_ev), 10)
    for mode, flag in (("A", False), ("B", True)):
        t, (iv, counter, stats) = timed(lambda: sparse_scan(fixture.request, gap_inputs, fixture.world, gap_ev, use_certificate=flag), 10)
        print(f"jump_gap_1:A3 (D101 M0 action) mode {mode}: {counter.physics_steps} steps vs old {old.counts.physics_steps}; "
              f"Linux {t:.1f} ms vs {t_old:.1f} ms -> Windows est. {48.35 * t / t_old:.1f} ms; intervals {iv} "
              f"{'equal' if iv == old_intervals(old) else 'DIFF'}; {stats}")

    t_k1, k1 = timed(lambda: k1proto.compact_scan_commitment(fixture.request, gap_inputs, fixture.world, gap_ev), 10)
    print(f"jump_gap_1:A3 K1 candidate (unsound, for calibration): {k1.counts.physics_steps} steps, Linux {t_k1:.1f} ms "
          f"= {100 * t_k1 / t_old:.0f}% of old; its Windows M0 P99 was 18.25 ms (old 48.61 ms)")

    # ---------- 2. K1 mock ----------
    print("\n== 2. K1 reproduction: _tail forced UNSAFE at boundary 5 on flat ground ==")
    flat = m0_probe._fixture_for("flat_walk", "A3")
    neutral = (flat.request.stop_input,) * 10
    ev = _boundary_evidence(flat.request, neutral)
    original = C._tail
    def forced(*args, **kwargs):
        tail = original(*args, **kwargs)
        return replace(tail, status=C.TailStatus.UNSAFE) if args[1] == 5 else tail
    with patch.object(C, "_tail", side_effect=forced):
        old = C.scan_commitment(flat.request, neutral, flat.world, ev)
        for mode, flag in (("A", False), ("B", True)):
            iv, _, stats = sparse_scan(flat.request, neutral, flat.world, ev, use_certificate=flag)
            print(f"mode {mode}: intervals {iv} vs old {old_intervals(old)} -> {'equal' if iv == old_intervals(old) else 'DIFF'}; {stats}")
    print("   (B's envelope proves boundary 5 safe from geometry and never calls the mocked _tail there;"
          " the mock is not a physical world, so B must be tested with world mutations instead — part 3)")

    # ---------- 3. world mutations ----------
    print("\n== 3. world-mutation property test ==")


    def mutated(world, cells, rng):
        view = world._world
        facts = dict(view._snapshot_facts)
        stamp = next(iter(facts.values())).stamp
        stone = CellFact(CellKnowledge.BLOCK, stamp, BlockGeometry.full_cube("minecraft:stone"))
        air = CellFact(CellKnowledge.AIR, stamp, None)
        kinds = []
        for position, kind in cells:
            if kind == "hole":
                facts[position] = air
            elif kind == "obstacle":
                facts[position] = stone
            else:
                facts.pop(position, None)
            kinds.append(kind)
        return PhysicsWorldView(WorldView.detached(view.session, view.geometry_revision,
                                                   view.evidence_revision, facts), world.ruleset)


    rng = random.Random(20261013)
    counts = dict(trials=0, main_ok=0, certified=0, violations=0, hidden_short=0, hidden_missed_by_k1=0,
                  old_ok=0, a_equal=0, b_equal=0, a_superset=0, b_superset=0, b_reject_old_ok=0)
    for set_id, key, request, world, inputs, *_ in cases:
        single = replace(request, entry_states=request.entry_states[:1], timing_branches=(TimingBranch.ON_TIME,),
                         allowed_effect_ticks=request.allowed_effect_ticks[:1], branch_preludes=request.branch_preludes[:1])
        ev1 = _boundary_evidence(single, inputs)
        base = C.scan_commitment(single, inputs, world, ev1).proof.branches[0].states
        xs = [s.position[0] for s in base]; zs = [s.position[2] for s in base]
        level = round(base[0].position[1])
        for trial in range(15):
            counts["trials"] += 1
            cells = []
            for _ in range(rng.randint(1, 4)):
                kind = rng.choice(("hole", "hole", "obstacle", "unknown"))
                dy = {"hole": -1, "unknown": rng.choice((-1, 0)), "obstacle": rng.choice((0, 1))}[kind]
                position = (rng.randint(math.floor(min(xs)) - 2, math.floor(max(xs)) + 2), level + dy,
                            rng.randint(math.floor(min(zs)) - 2, math.floor(max(zs)) + 2))
                cells.append((position, kind))
            w = mutated(world, cells, rng)
            try:
                counter = CountedPhysics(single.budget)
                states = [single.entry_states[0]]
                for command in inputs:
                    states.append(C._calculated(counter, states[-1], command, w, set()))
            except Exception:
                continue
            counts["main_ok"] += 1
            minimum_y = min(states[0].position[1], states[-1].position[1])
            maximum = single.task_damage_budget.maximum_expected_damage_points
            statuses = []
            support_safe = []
            for b, row in enumerate(ev1[0]):
                prefix = tuple(states[:b + 1])
                try:
                    tail = C._tail(CountedPhysics(single.budget), b, prefix, row.irrevocable_inputs, single.stop_input,
                                   w, minimum_y, maximum, C.ScanOptions())
                    status = "SAFE" if tail.status is C.TailStatus.SAFE_STOP else "UNSAFE"
                except C._Incomplete as error:
                    status = "INCOMPLETE"
                except Exception:
                    status = "INCOMPLETE"
                statuses.append(status)
                try:
                    support_safe.append(C._safe_support(states[b], w, set()))
                except Exception:
                    support_safe.append(False)
                if certificate(states[b], row.irrevocable_inputs, w, minimum_y, C._damage(prefix), maximum) is not None:
                    counts["certified"] += 1
                    if status != "SAFE":
                        counts["violations"] += 1
                        print("   VIOLATION", key, trial, b, status, cells)
            # short hidden risk: SAFE ... UNSAFE ... SAFE where the unsafe run touches no K1 seed
            seeds = set(k1proto._event_boundaries(inputs, ev1[0], support_safe))
            b = 1
            while b < len(statuses):
                if statuses[b] == "UNSAFE" and statuses[b - 1] == "SAFE":
                    end = b
                    while end < len(statuses) and statuses[end] == "UNSAFE":
                        end += 1
                    if end < len(statuses) and statuses[end] == "SAFE":
                        counts["hidden_short"] += 1
                        if not any(i in seeds for i in range(b, end)):
                            counts["hidden_missed_by_k1"] += 1
                    b = end
                b += 1
            # full comparison when the old scanner accepts
            ev = _boundary_evidence(request, inputs)
            old = C.scan_commitment(request, inputs, w, ev)
            for mode, flag in (("a", False), ("b", True)):
                try:
                    iv = sparse_scan(request, inputs, w, ev, use_certificate=flag)[0]
                except C._Incomplete:
                    iv = None
                if old.status is C.ScanStatus.VERIFIED_CANDIDATE:
                    if mode == "a":
                        counts["old_ok"] += 1
                    if iv == old_intervals(old):
                        counts[f"{mode}_equal"] += 1
                    elif iv is None and mode == "b":
                        counts["b_reject_old_ok"] += 1
                elif iv is not None:
                    counts[f"{mode}_superset"] += 1
    print(counts)
    print("violations must be 0: an envelope-certified boundary whose real tail is not SAFE")
    print("a_equal/b_equal count mutated worlds where the old scanner accepts and the intervals are identical (out of old_ok);")
    print("*_superset counts worlds the old scanner rejects but the sparse scan accepts (skipped tails would have hit UNKNOWN/unsettled).")


if __name__ == "__main__":
    main()
