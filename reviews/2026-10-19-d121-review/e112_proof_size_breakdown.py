"""E112：D121-S2 体积止损里，字节都花在哪里。

只调用项目函数和项目夹具，不改项目文件：
  - tests.motion_nav.test_d102_action_handoff.ActionHandoffTests 的真实当前证明与后继源证明
    （与 scripts/d121_joint_worker_stop_probe.py 相同的夹具）；
  - 按该探针相同的方式构造后继 PreparedMotionRequest，并调用 prepare_fixed_motion；
  - 用项目的 canonical_motion_bytes 计算规范 JSON 字节，用 seal_bundle 得到现行正式线上消息。

报告：
  1. 当前证明（VerifiedMotionResult）与后继证明（PreparedMotionProof）按顶层字段的规范字节；
  2. 按值类型汇总：PhysicsState、FallbackTailProof／PreparedSafetyTail 等各占多少字节、多少个；
  3. 现行正式后继消息（只有后继，D117 已有路径）的 pickle 载荷、其中 sealed_bytes 的字节；
  4. 几种不重复完整图的表达的大小估算（只做计数，不实现任何新类型）。
用法：python e112_proof_size_breakdown.py
"""
import collections
import hashlib
import json
import pickle
import sys
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind  # noqa: E402
from mc2p.motion_nav.motion_risk import TaskDamageBudget  # noqa: E402
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput  # noqa: E402
from mc2p.motion_nav.prepared_motion import (  # noqa: E402
    PreparedComputeStatus, PreparedMotionRequest, PreparedScanBudget, StreamGeneration,
)
from mc2p.motion_nav.prepared_motion_proof import (  # noqa: E402
    MAXIMUM_MESSAGE_BYTES, _canonical, canonical_motion_bytes, canonical_motion_digest,
    prepare_fixed_motion, seal_bundle,
)
from mc2p.motion_nav.world_snapshot import SealedSnapshot, canonical_snapshot_bytes  # noqa: E402
from tests.motion_nav.test_d102_action_handoff import ActionHandoffTests  # noqa: E402


def size(value) -> int:
    return len(json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                          allow_nan=False, ensure_ascii=True).encode("utf-8"))


def by_field(value):
    return {f.name: size(getattr(value, f.name)) for f in fields(value)}


def by_type(value):
    """每种 dataclass 类型最外层出现的总字节和个数（嵌套在同类型里的不重复计）。"""
    totals, counts = collections.Counter(), collections.Counter()

    def walk(item, inside):
        if is_dataclass(item) and not isinstance(item, type):
            name = type(item).__name__
            if name not in inside:
                totals[name] += size(item)
                counts[name] += 1
            for f in fields(item):
                walk(getattr(item, f.name), inside | {name})
        elif type(item) in (tuple, list, frozenset):
            for element in item:
                walk(element, inside)
    walk(value, frozenset())
    return {name: [totals[name], counts[name]] for name, _ in totals.most_common(12)}


def build():
    """与 d121_joint_worker_stop_probe.collect_stop_facts 前半段相同的构造。"""
    ActionHandoffTests.setUpClass()
    incumbent, successor_source = ActionHandoffTests.proofs
    source_snapshot = ActionHandoffTests.snapshot
    facts = tuple(sorted(source_snapshot.facts))
    fixed_versions = tuple((position, 1) for position, _ in facts)
    fixed_identity = replace(source_snapshot.identity, geometry_revision=1, evidence_revision=0,
                             local_revision=1, digest="0" * 64)
    snapshot_bytes = canonical_snapshot_bytes(fixed_identity, facts, fixed_versions)
    fixed_identity = replace(fixed_identity, digest=hashlib.sha256(snapshot_bytes).hexdigest())
    snapshot = SealedSnapshot(fixed_identity, snapshot_bytes, facts, fixed_versions)
    stream = StreamGeneration(
        "d121-s2-generation", "d121-s2-owner", "d121-s2-successor-request", "d121-s2-route", 1,
        successor_source.tick_inputs, (successor_source.entry_state,),
        successor_source.execution_window, ("d121-s2-fixed-successor",),
        TickInput(0., 0., False, False, False, successor_source.entry_state.yaw_radians))
    versions = dict(snapshot.dependency_versions)
    incumbent_versions = tuple((p, versions[p]) for p in incumbent.world_dependencies)
    request = PreparedMotionRequest(
        "d121-s2-joint-job",
        AsyncWorkIdentity(AsyncComputationScope(incumbent.entry_state.session.value, "d121-s2-task", 1),
                          "d121-s2-worker-owner", AsyncWorkKind.MOTION_SOLVE, stream.request_id, 1),
        stream, 1, 1, 1, successor_source.commands, successor_source.tick_inputs,
        canonical_motion_digest(successor_source.tick_inputs), successor_source.entry_state,
        successor_source.exit_state, successor_source.execution_window,
        successor_source.execution_window.earliest_start_tick, "d121-s2-ledger",
        incumbent.entry_state.session.value, (), snapshot.identity, "d121-s2-snapshot-lease",
        incumbent_versions, JAVA_1_21_RULESET, TaskDamageBudget(), 0.,
        PreparedScanBudget(65_536, 65_536, 1), stream.suffix_identity,
        successor_source.fallback_candidates)
    result = prepare_fixed_motion(request, snapshot)
    assert result.status is PreparedComputeStatus.PREPARED and result.bundle is not None
    return incumbent, result.bundle


def main():
    incumbent, bundle = build()
    proof = bundle.proof
    wire = seal_bundle(bundle)
    one_state = size(incumbent.entry_state)
    report = {
        "message_limit_bytes": MAXIMUM_MESSAGE_BYTES,
        "incumbent_VerifiedMotionResult": {
            "canonical_bytes": size(incumbent),
            "by_field_top": dict(sorted(by_field(incumbent).items(), key=lambda kv: -kv[1])[:8]),
            "by_type": by_type(incumbent),
            "trajectory_ticks": len(incumbent.trajectory),
            "fallback_tails": len(incumbent.fallback_tails),
            "fallback_tail_ticks": [len(t.trajectory) for t in incumbent.fallback_tails]
            if incumbent.fallback_tails and hasattr(incumbent.fallback_tails[0], "trajectory") else None,
            "delayed_start_variants": len(incumbent.delayed_start_variants),
        },
        "successor_PreparedMotionProof": {
            "canonical_bytes": size(proof),
            "by_field_top": dict(sorted(by_field(proof).items(), key=lambda kv: -kv[1])[:8]),
            "by_type": by_type(proof),
        },
        "one_PhysicsState_canonical_bytes": one_state,
        "one_PhysicsState_pickle_bytes": len(pickle.dumps(incumbent.entry_state, protocol=5)),
        "existing_successor_wire_message": {
            "pickle_payload_bytes": len(wire.payload),
            "sealed_bytes_inside_payload": len(bundle.sealed_bytes),
            "payload_limit_ratio": round(len(wire.payload) / MAXIMUM_MESSAGE_BYTES, 4),
        },
        "estimates_without_duplicate_graphs": {
            # 引用方案：消息只多出当前证明的摘要和出口 fallback 的身份，现行后继消息本身不变。
            "reference_digest_exit_fallback_extra_bytes": len(canonical_motion_digest(incumbent))
            + size(incumbent.fallback_tails[-1]) if incumbent.fallback_tails else None,
            # 如果坚持把当前证明也随消息带一份（只带一次，pickle 共享对象）：
            "successor_message_plus_one_incumbent_pickle_bytes":
                len(pickle.dumps((bundle, incumbent), protocol=5)),
            "same_plus_incumbent_canonical_once_bytes":
                len(pickle.dumps((bundle, incumbent), protocol=5)) + size(incumbent),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
