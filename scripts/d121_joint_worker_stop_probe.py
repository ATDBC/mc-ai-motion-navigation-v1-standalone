"""Measure the test-side D121-S2 direct-copy stop without production changes."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import pickle
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.contracts.motion_activation import validate_frozen_value
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.motion_chain import FallbackTailProof
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_solver import VerifiedMotionResult
from mc2p.motion_nav.prepared_motion import (
    PreparedAdmissionStatus, PreparedComputeResult, PreparedComputeStatus,
    PreparedMotionProof, PreparedMotionRequest, PreparedScanBudget, StreamGeneration,
)
from mc2p.motion_nav.prepared_motion_proof import (
    MAXIMUM_MESSAGE_BYTES, PreparedMessageBudgetExceeded,
    canonical_motion_bytes, canonical_motion_digest, prepare_fixed_motion,
)
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.motion_worker import _execute_job
from mc2p.motion_nav.world_snapshot import SealedSnapshot, canonical_snapshot_bytes
from tests.motion_nav.test_d102_action_handoff import ActionHandoffTests


@dataclass(frozen=True, slots=True)
class DiagnosticIncumbentBinding:
    generation_id: str
    owner_id: str
    route_id: str
    action_index: int
    candidate_revision: int
    proof: VerifiedMotionResult
    exit_fallback: FallbackTailProof
    dependency_versions: tuple[tuple[tuple[int, int, int], int], ...]


@dataclass(frozen=True, slots=True)
class DiagnosticJointRequest:
    successor_request: PreparedMotionRequest
    incumbent: DiagnosticIncumbentBinding


@dataclass(frozen=True, slots=True)
class DiagnosticJointFact:
    incumbent: DiagnosticIncumbentBinding
    successor_stream: StreamGeneration
    successor_proof: PreparedMotionProof
    current_action_index: int
    successor_action_index: int
    dependency_versions: tuple[tuple[tuple[int, int, int], int], ...]
    incumbent_expected_damage_points: float
    fallback_expected_damage_points: float
    successor_expected_damage_points: float
    damage_remaining_points: float


@dataclass(frozen=True, slots=True)
class DiagnosticJointEnvelope:
    request: DiagnosticJointRequest
    inputs: tuple
    input_digest: str
    proof: PreparedMotionProof
    proof_digest: str
    snapshot_identity: object
    ruleset: object
    joint_fact: DiagnosticJointFact
    sealed_bytes: bytes


def _sealed_body(request, bundle, joint) -> bytes:
    return canonical_motion_bytes((
        "mc2p.d121-diagnostic-joint-bundle.v1",
        request,
        bundle.inputs,
        bundle.input_digest,
        bundle.proof,
        bundle.proof_digest,
        bundle.snapshot_identity,
        bundle.ruleset,
        joint,
    ))


def collect_stop_facts() -> dict:
    """Build one real gap successor and test-only duplicated joint envelope."""
    ActionHandoffTests.setUpClass()
    incumbent, successor_source = ActionHandoffTests.proofs
    source_snapshot = ActionHandoffTests.snapshot
    facts = tuple(sorted(source_snapshot.facts))
    fixed_versions = tuple((position, 1) for position, _ in facts)
    fixed_identity = replace(
        source_snapshot.identity,
        geometry_revision=1,
        evidence_revision=0,
        local_revision=1,
        digest="0" * 64,
    )
    snapshot_bytes = canonical_snapshot_bytes(
        fixed_identity, facts, fixed_versions)
    fixed_identity = replace(
        fixed_identity, digest=hashlib.sha256(snapshot_bytes).hexdigest())
    snapshot = SealedSnapshot(
        fixed_identity, snapshot_bytes, facts, fixed_versions)
    stream = StreamGeneration(
        "d121-s2-generation",
        "d121-s2-owner",
        "d121-s2-successor-request",
        "d121-s2-route",
        1,
        successor_source.tick_inputs,
        (successor_source.entry_state,),
        successor_source.execution_window,
        ("d121-s2-fixed-successor",),
        TickInput(0., 0., False, False, False,
                  successor_source.entry_state.yaw_radians),
    )
    versions = dict(snapshot.dependency_versions)
    incumbent_versions = tuple(
        (position, versions[position])
        for position in incumbent.world_dependencies
    )
    successor_request = PreparedMotionRequest(
        "d121-s2-joint-job",
        AsyncWorkIdentity(
            AsyncComputationScope(incumbent.entry_state.session.value,
                                  "d121-s2-task", 1),
            "d121-s2-worker-owner",
            AsyncWorkKind.MOTION_SOLVE,
            stream.request_id,
            1,
        ),
        stream,
        1,
        1,
        1,
        successor_source.commands,
        successor_source.tick_inputs,
        canonical_motion_digest(successor_source.tick_inputs),
        successor_source.entry_state,
        successor_source.exit_state,
        successor_source.execution_window,
        successor_source.execution_window.earliest_start_tick,
        "d121-s2-ledger",
        incumbent.entry_state.session.value,
        (),
        snapshot.identity,
        "d121-s2-snapshot-lease",
        incumbent_versions,
        JAVA_1_21_RULESET,
        TaskDamageBudget(),
        0.,
        PreparedScanBudget(65_536, 65_536, 1),
        stream.suffix_identity,
        successor_source.fallback_candidates,
    )
    successor = prepare_fixed_motion(successor_request, snapshot)
    if successor.status is not PreparedComputeStatus.PREPARED or successor.bundle is None:
        raise AssertionError("frozen gap successor no longer produces a prepared bundle")
    exit_fallback = incumbent.fallback_tails[-1]
    binding = DiagnosticIncumbentBinding(
        stream.generation_id,
        stream.owner_id,
        stream.route_id,
        0,
        1,
        incumbent,
        exit_fallback,
        incumbent_versions,
    )
    joint_request = DiagnosticJointRequest(successor_request, binding)
    successor_proof = successor.bundle.proof
    combined_versions = successor_proof.dependency_versions
    successor_damage = max(
        tail.expected_damage_points
        for branch in successor_proof.branches
        for tail in branch.safety_tails
    )
    joint = DiagnosticJointFact(
        binding,
        stream,
        successor_proof,
        0,
        1,
        combined_versions,
        incumbent.maximum_expected_damage_points,
        exit_fallback.expected_damage_points,
        successor_damage,
        successor_request.damage_remaining_points,
    )
    validate_frozen_value(joint)
    sealed = _sealed_body(joint_request, successor.bundle, joint)
    envelope = DiagnosticJointEnvelope(
        joint_request,
        successor.bundle.inputs,
        successor.bundle.input_digest,
        successor_proof,
        successor.bundle.proof_digest,
        successor.bundle.snapshot_identity,
        successor.bundle.ruleset,
        joint,
        sealed,
    )
    payload = pickle.dumps(envelope, protocol=5)

    case = ActionHandoffTests(
        methodName="test_parent_request_building_stays_on_existing_receiver")
    case.setUp()
    try:
        case.prepare()
        job = case.wait_for_current_prepared_job(action_index=1)

        def reject_oversized(_bundle):
            if len(payload) > MAXIMUM_MESSAGE_BYTES:
                raise PreparedMessageBudgetExceeded(
                    "test-side direct-copy payload exceeds 2 MiB")
            raise AssertionError("the frozen diagnostic unexpectedly fits")

        with patch("mc2p.motion_nav.motion_worker.seal_bundle",
                   side_effect=reject_oversized):
            message = _execute_job(job)
        case.worker.put(message)
        if not case.inbox.wait_for_receiver(timeout=10):
            raise AssertionError("receiver did not consume the typed worker failure")
        computation = case.inbox.prepared_compute_result(job.request.async_identity)
        admission = case.inbox.prepared_admission(job.request.async_identity)
        ready = tuple(
            handle for handle in case.inbox.registered_handles
            if handle.action_index == 1
            and handle.generation_id == job.request.stream.generation_id
        )
        if (type(computation) is not PreparedComputeResult
                or computation.status is not PreparedComputeStatus.BUDGET_EXHAUSTED
                or admission is None
                or admission.status is not PreparedAdmissionStatus.STALE
                or ready):
            raise AssertionError("oversized joint result did not stop as a typed non-READY result")

        parts = {
            "incumbent_proof_canonical_bytes": len(canonical_motion_bytes(incumbent)),
            "incumbent_binding_canonical_bytes": len(canonical_motion_bytes(binding)),
            "successor_request_canonical_bytes": len(canonical_motion_bytes(successor_request)),
            "joint_request_canonical_bytes": len(canonical_motion_bytes(joint_request)),
            "successor_proof_canonical_bytes": len(canonical_motion_bytes(successor_proof)),
            "joint_fact_canonical_bytes": len(canonical_motion_bytes(joint)),
        }
        sources = (
            Path(__file__).resolve(),
            Path(__file__).resolve().parents[1]
                / "mc2p/motion_nav/prepared_motion_proof.py",
            Path(__file__).resolve().parents[1]
                / "mc2p/motion_nav/motion_worker.py",
        )
        return {
            "schema_version": "mc2p.d121-s2-stop.v2",
            "status": "partial_stopped",
            "phase": "S2",
            "scenario": "gap_to_gap_test_side_direct_copy",
            "scope": "read_only_test_diagnostic_no_production_joint_type_or_message",
            "method": {
                "size_measurement": "test_side_direct_copy_DiagnosticJointEnvelope",
                "size_inputs": "real incumbent VerifiedMotionResult and real successor PreparedMotionProof",
                "worker_mapping": (
                    "normal PreparedMotionJob with an equivalent PreparedMessageBudgetExceeded "
                    "injected at seal_bundle"
                ),
                "production_joint_message_executed": False,
            },
            "measurement_boundary": {
                "direct_copy_size": (
                    "measures only the test-side DiagnosticJointEnvelope approach"
                ),
                "worker_status": (
                    "verifies exception-to-BUDGET_EXHAUSTED-to-STALE mapping and no READY only"
                ),
                "not_proved": (
                    "no production joint request, bundle, wire payload, worker submission, or READY margin"
                ),
            },
            "message_limit_bytes": MAXIMUM_MESSAGE_BYTES,
            "direct_copy_canonical_bytes": len(sealed),
            "direct_copy_canonical_limit_ratio": len(sealed) / MAXIMUM_MESSAGE_BYTES,
            "direct_copy_pickle_bytes": len(payload),
            "direct_copy_pickle_limit_ratio": len(payload) / MAXIMUM_MESSAGE_BYTES,
            "pickle_measurement": "fixed_snapshot_protocol5_sample_object_aliasing_may_vary",
            "parts": parts,
            "successor_input_digest": successor.bundle.input_digest,
            "injected_budget_failure_mapping": {
                "wire_schema": message.schema,
                "compute_status": computation.status.value,
                "admission_status": admission.status.value,
                "ready_handle_count": len(ready),
                "ready": False,
            },
            "background_ready_margin_ticks": None,
            "background_ready_status": "not_measured_no_production_joint_message",
            "stop_reason": "test_side_direct_copy_duplicates_complete_graphs_and_exceeds_message_budget",
            "history": {
                "s0": "complete",
                "s1": "complete",
                "s2": "stopped",
                "s3": "not_run",
                "s4": "not_run",
                "temporary_production_implementation": "withdrawn",
            },
            "source_sha256": {
                str(path.relative_to(Path(__file__).resolve().parents[1])).replace("\\", "/"):
                    hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sources
            },
        }
    finally:
        case.doCleanups()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite evidence: {args.output}")
    result = collect_stop_facts()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
