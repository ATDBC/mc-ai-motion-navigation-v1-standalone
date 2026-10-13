"""E21：T2 可信本地 receiver 的两个边界核对。

1. `_trusted_hot_view`／`_trusted_handle` 用 object.__new__ 绕过构造器。核对它们与完整路径
   `PreparedMotionHotView.from_bundle`／`PreparedMotionHandle(...)` 在同一 bundle 上逐字段相同
   （proof_token 是随机 uuid，不比较），并计时两条构造路径。
2. 局部快照越界读：快照外的格子由 `SealedSnapshot.cell` 返回 UNKNOWN，物理计算返回
   NEEDS_INFORMATION，而不是当作空气。也就是说“快照包络不够大”只会降低命中率，不会产生假 SAFE。
只调用项目函数，不改项目文件。运行：PYTHONPATH=<项目根> python e21_t2_trusted_projection.py
"""
import statistics, time
from dataclasses import fields, replace

from tests.motion_nav import test_d102_receiver as fixtures
from mc2p.motion_nav import prepared_motion_validation as V
from mc2p.motion_nav.prepared_motion import PreparedMotionHandle, PreparedMotionHotView
from mc2p.motion_nav.world_model import CellKnowledge


def ms(fn, n=20):
    out = []
    for _ in range(n):
        t = time.perf_counter_ns(); fn(); out.append((time.perf_counter_ns() - t) / 1e6)
    return min(out), statistics.median(out)


def main():
    fixtures.ReceiverTests.setUpClass()
    T = fixtures.ReceiverTests
    bundle, request, snapshot = T.bundle, T.request, T.snapshot

    print("== 1. 可信投影与完整构造逐字段比较")
    full = PreparedMotionHotView.from_bundle(bundle)
    trusted = V._trusted_hot_view(bundle)
    diffs = [f.name for f in fields(PreparedMotionHotView) if getattr(full, f.name) != getattr(trusted, f.name)]
    print(f"  hot view 字段 {len(fields(PreparedMotionHotView))} 个，不同 {diffs or '无'}")
    stream = request.stream
    full_handle = PreparedMotionHandle("x" * 32, stream.request_id, stream.generation_id, stream.owner_id,
                                       stream.route_id, stream.action_index, bundle.proof_digest,
                                       bundle.proof.dependency_versions, full)
    trusted_handle = V._trusted_handle(bundle, request, trusted)
    diffs = [f.name for f in fields(PreparedMotionHandle)
             if f.name != "proof_token" and getattr(full_handle, f.name) != getattr(trusted_handle, f.name)]
    print(f"  handle 字段（不含随机 proof_token）不同 {diffs or '无'}")
    print("  构造耗时 Linux（最小/中位 ms）：完整 from_bundle {:.2f}/{:.2f}，可信投影 {:.3f}/{:.3f}".format(
        *ms(lambda: PreparedMotionHotView.from_bundle(bundle)), *ms(lambda: V._trusted_hot_view(bundle))))
    print("  注意：项目测试只检查字段名漂移和拒绝矩阵，没有这条“同一 bundle 两条路径结果相同”的断言。")

    print("\n== 2. 快照越界读")
    xs = [p[0] for p, _ in snapshot.facts]; ys = [p[1] for p, _ in snapshot.facts]; zs = [p[2] for p, _ in snapshot.facts]
    outside = (max(xs) + 3, min(ys), min(zs))
    fact = snapshot.cell(outside)
    print(f"  快照 {len(snapshot.facts)} 格，范围 x[{min(xs)},{max(xs)}] y[{min(ys)},{max(ys)}] z[{min(zs)},{max(zs)}]；"
          f"越界格 {outside} → knowledge={fact.knowledge.name}（UNKNOWN 即不当空气）")
    assert fact.knowledge is CellKnowledge.UNKNOWN


if __name__ == "__main__":
    main()
