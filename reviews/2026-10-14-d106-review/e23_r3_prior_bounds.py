"""E23：R3 的“局部快照”效率数字用的是答案导出的范围，换成事前可得的范围会怎样。

R3 的 18 格范围取自完整快照求解后 `proof.dependency_facts` 的包围盒，属于事后最优。
真实的准备链必须在求解前选范围。本脚本只用请求里事前已知的量：
  入口状态与出口状态的身体 AABB，连成一段水平扫掠；水平外扩 m 格；
  竖直从脚下 d 格到头顶以上 h 格（跳跃最高约 1.25 格）。
对 m∈{0,1,2}、d∈{1,2}、h=2 分别裁剪 D102 冻结夹具的完整快照，调用 `prepare_fixed_motion`，
记录格数、是否 PREPARED、结果是否与完整快照一致、以及封存与求解耗时（Linux）。
第 2 节：在 90 格事前范围上拆分 `prepare_fixed_motion`（worker 侧求解）的耗时：
  构造器里的整图不可变遍历（validate_frozen_value）、规范 bytes／摘要、其余（物理与组装）。
  方法是在进程内把对应函数临时换成计时包装或空操作，用完恢复；仅用于计时。
只调用项目函数并计时，不改项目文件。运行：PYTHONPATH=<D106 检出> python e23_r3_prior_bounds.py
"""
import math, statistics, time
from dataclasses import replace

from mc2p.motion_nav.prepared_motion_proof import prepare_fixed_motion
from mc2p.motion_nav.prepared_motion import PreparedComputeStatus
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.world_snapshot import validate_sealed_snapshot
from tests.motion_nav import test_d102_prepared_proof as F
from tests.motion_nav.test_d106_local_snapshot import _clip


def timed(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter_ns(); value = fn(); out.append((time.perf_counter_ns() - t) / 1e6)
    return statistics.median(out), value


def body_cells(state, m, d, h):
    x, y, z = state.position
    half = state.body_width / 2
    return (math.floor(x - half) - m, math.floor(y) - d, math.floor(z - half) - m,
            math.floor(x + half) + 1 + m, math.floor(y + state.body_height) + 1 + h, math.floor(z + half) + 1 + m)


def split_worker(request, snapshot):
    from mc2p.contracts import motion_activation as MA
    from mc2p.motion_nav import prepared_motion as PM
    from mc2p.motion_nav import prepared_motion_proof as PP
    local = _clip(snapshot, Aabb(-1, -1, -1, 2, 5, 4))
    local_request = replace(request, snapshot_identity=local.identity)
    total, result = timed(lambda: prepare_fixed_motion(local_request, local), 7)
    assert result.status is PreparedComputeStatus.PREPARED
    spent = {"validate_frozen_value": 0, "canonical_motion_bytes": 0}
    originals = {"validate_frozen_value": MA.validate_frozen_value,
                 "canonical_motion_bytes": PP.canonical_motion_bytes}

    def wrap(name, fn):
        def inner(*args, **kwargs):
            started = time.perf_counter_ns()
            try:
                return fn(*args, **kwargs)
            finally:
                spent[name] += time.perf_counter_ns() - started
        return inner

    runs = 7
    PM.validate_frozen_value = wrap("validate_frozen_value", originals["validate_frozen_value"])
    PP.canonical_motion_bytes = wrap("canonical_motion_bytes", originals["canonical_motion_bytes"])
    try:
        for _ in range(runs):
            prepare_fixed_motion(local_request, local)
    finally:
        PM.validate_frozen_value = originals["validate_frozen_value"]
        PP.canonical_motion_bytes = originals["canonical_motion_bytes"]
    validate = spent["validate_frozen_value"] / runs / 1e6
    canonical = spent["canonical_motion_bytes"] / runs / 1e6
    print(f"\n== 2. worker 侧 prepare_fixed_motion 拆分（90 格事前范围，Linux，{runs} 次平均）")
    print(f"  总计中位 {total:.1f} ms")
    print(f"  prepared_motion 构造器里的整图不可变遍历（外层构造会重走已验证过的内层对象）{validate:.1f} ms")
    print(f"  canonical_motion_bytes（规范 bytes，含摘要所需部分）{canonical:.1f} ms")
    print(f"  其余（物理推演、尾迹、组装等）约 {max(0.0, total - validate - canonical):.1f} ms")


def main():
    F.ProofTests.setUpClass()
    request, snapshot = F.ProofTests.request, F.ProofTests.snapshot
    full_ms, full = timed(lambda: prepare_fixed_motion(request, snapshot), 3)
    deps = [p for p, _ in full.bundle.proof.dependency_facts]
    entry, exit_ = request.entry_state, request.exit_state
    print(f"夹具：入口 {entry.position}，出口 {exit_.position}，完整快照 {len(snapshot.facts)} 格；"
          f"完整求解 {full_ms:.1f} ms；实际依赖 {len(deps)} 格")
    seal_full, _ = timed(lambda: validate_sealed_snapshot(snapshot), 3)
    print(f"完整快照复核（validate_sealed_snapshot）{seal_full:.1f} ms")

    oracle = Aabb(min(p[0] for p in deps), min(p[1] for p in deps), min(p[2] for p in deps),
                  max(p[0] for p in deps) + 1, max(p[1] for p in deps) + 1, max(p[2] for p in deps) + 1)
    rows = [("R3 答案范围（依赖包围盒）", oracle)]
    h = 2
    for m in (0, 1, 2):
        for d in (1, 2):
            a, b = body_cells(entry, m, d, h), body_cells(exit_, m, d, h)
            rows.append((f"事前范围 m={m} d={d} h={h}", Aabb(min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]),
                                                     max(a[3], b[3]), max(a[4], b[4]), max(a[5], b[5]))))
    print(f"\n{'范围':26s} {'格数':>6s} {'状态':>22s} {'与完整一致':>8s} {'复核 ms':>8s} {'求解 ms':>8s}")
    for name, bounds in rows:
        local = _clip(snapshot, bounds)
        local_request = replace(request, snapshot_identity=local.identity)
        seal_ms, _ = timed(lambda: validate_sealed_snapshot(local), 5)
        solve_ms, result = timed(lambda: prepare_fixed_motion(local_request, local), 3)
        same = (result.status is PreparedComputeStatus.PREPARED
                and result.bundle.proof == full.bundle.proof and result.counts == full.counts)
        missing = sorted(result.missing_cells)[:4] if result.status is not PreparedComputeStatus.PREPARED else ""
        print(f"{name:26s} {len(local.facts):6d} {result.status.name:>22s} {str(same):>8s} {seal_ms:8.2f} {solve_ms:8.1f}  {missing}")
    print(f"\n实际依赖：{sorted(deps)}")
    split_worker(request, snapshot)
    print("说明：PREPARED 且与完整一致＝首次命中；MISSING_INFORMATION＝需要补取，不会产生假 SAFE。")


if __name__ == "__main__":
    main()
