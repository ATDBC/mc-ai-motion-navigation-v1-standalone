"""E114：现行正式 prepared 后继消息有多大，D121-S1 前后各多大。

只调用项目夹具和项目 worker 函数：tests.motion_nav.test_d102_action_handoff 的
ActionHandoffTests（与 scripts/d121_joint_worker_stop_probe.py 相同的 gap -> gap 夹具），
用它的正式 prepare() 得到第二跳的 PreparedMotionJob，再调用 motion_worker._execute_job。
只把 seal_bundle 包一层用来记录字节数，返回值原样交回，不改行为。
同一脚本分别在 dce230f（D117，S1 前）和 3889d10（D121）检出中运行。

报告：当前证明（第一跳 VerifiedMotionResult）的规范字节和 pickle 字节；
后继正式消息的 pickle 载荷、其中 sealed_bytes 的字节、proof 的规范字节、安全尾段个数与状态数。
用法：python e114_existing_wire_size.py
"""
import json
import pickle
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav import motion_worker  # noqa: E402
from mc2p.motion_nav.prepared_motion_proof import (  # noqa: E402
    MAXIMUM_MESSAGE_BYTES, canonical_motion_bytes, seal_bundle,
)
from tests.motion_nav.test_d102_action_handoff import ActionHandoffTests  # noqa: E402


def main():
    ActionHandoffTests.setUpClass()
    incumbent = ActionHandoffTests.proofs[0]
    seen = {}

    def recording_seal(bundle):
        message = seal_bundle(bundle)
        tails = [tail for branch in bundle.proof.branches for tail in branch.safety_tails]
        seen.update(
            pickle_payload_bytes=len(message.payload),
            sealed_bytes=len(bundle.sealed_bytes),
            proof_canonical_bytes=len(canonical_motion_bytes(bundle.proof)),
            branches=len(bundle.proof.branches),
            safety_tails=len(tails),
            safety_tail_states=sum(len(tail.trajectory) for tail in tails),
            main_trajectory_states=sum(len(branch.trajectory) for branch in bundle.proof.branches),
        )
        return message

    case = ActionHandoffTests(methodName="test_parent_request_building_stays_on_existing_receiver")
    case.setUp()
    try:
        case.prepare()
        job = case.wait_for_current_prepared_job(action_index=1)
        with patch("mc2p.motion_nav.motion_worker.seal_bundle", side_effect=recording_seal):
            message = motion_worker._execute_job(job)
    finally:
        case.doCleanups()
    tails = getattr(incumbent, "fallback_tails", ())
    print(json.dumps({
        "message_limit_bytes": MAXIMUM_MESSAGE_BYTES,
        "wire_schema": message.schema,
        "incumbent_canonical_bytes": len(canonical_motion_bytes(incumbent)),
        "incumbent_pickle_bytes": len(pickle.dumps(incumbent, protocol=5)),
        "incumbent_trajectory_states": len(incumbent.trajectory),
        "incumbent_fallback_tails": len(tails),
        "incumbent_fallback_tail_states": sum(len(t.trajectory) for t in tails) if tails else 0,
        "incumbent_delayed_start_variants": len(incumbent.delayed_start_variants),
        "successor_message": seen,
        "successor_payload_limit_ratio": round(seen["pickle_payload_bytes"] / MAXIMUM_MESSAGE_BYTES, 4),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
