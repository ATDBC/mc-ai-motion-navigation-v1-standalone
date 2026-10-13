"""Pure, complete parent-side admission of controlled motion-worker messages.

The returned handle is provisional. Only the existing inbox's atomic publish
boundary can give that exact object registry authority. This module owns no
thread, queue, world, pending work, generation or body responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import hashlib
import math
import pickle
import uuid

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.motion_activation import require_immutable_graph, validate_frozen_value
from mc2p.motion_nav.prepared_motion import (
    PreparedAdmissionResult, PreparedAdmissionStatus, PreparedComputeResult, PreparedComputeStatus,
    PreparedCurrentFacts, PreparedMotionBundle, PreparedMotionHandle, PreparedMotionHotView,
    PreparedMotionRequest, PreparedRejectReason,
)
from mc2p.motion_nav.prepared_motion_proof import (
    MAXIMUM_MESSAGE_BYTES, MAXIMUM_PHYSICS_STEPS, MAXIMUM_TAIL_TICKS, MAXIMUM_TRAJECTORY_TICKS,
    PREPARED_BUNDLE_SCHEMA, PREPARED_FAILURE_SCHEMA, PreparedWireMessage, canonical_bundle_bytes,
    canonical_motion_bytes, canonical_motion_digest,
)
from mc2p.motion_nav.world_model import CellKnowledge
from mc2p.motion_nav.world_snapshot import SealedSnapshot, validate_sealed_snapshot


@dataclass(frozen=True, slots=True)
class _ValidatedPrepared:
    admission: PreparedAdmissionResult
    bundle: PreparedMotionBundle | None = None
    computation: PreparedComputeResult | None = None


def _reject(reason: PreparedRejectReason) -> _ValidatedPrepared:
    return _ValidatedPrepared(PreparedAdmissionResult(PreparedAdmissionStatus.REJECTED, reason=reason))


def _validate_values(root: object) -> None:
    """Repeat native constructors that pickle slot restoration skips."""
    checked: set[int] = set()
    def visit(value):
        if value is None or type(value) in (str, bytes, bool, int, float) or isinstance(value, Enum):
            return
        if id(value) in checked:
            return
        checked.add(id(value))
        if type(value) in (tuple, frozenset):
            for item in value:
                visit(item)
        elif is_dataclass(value):
            for field in fields(value):
                visit(getattr(value, field.name))
            validate_frozen_value(value)
            constructor = getattr(type(value), "__post_init__", None)
            if constructor is not None:
                constructor(value)
    visit(root)


def _parent_mismatch(bundle: PreparedMotionBundle, expected: PreparedMotionRequest):
    actual, wanted = bundle.request, expected
    if (bundle.inputs != wanted.expected_inputs or bundle.input_digest != wanted.expected_input_digest
            or actual.expected_inputs != wanted.expected_inputs
            or actual.stream.expected_inputs != wanted.expected_inputs
            or bundle.proof.stream.expected_inputs != wanted.expected_inputs
            or any(branch.inputs != wanted.expected_inputs for branch in bundle.proof.branches)
            or actual.expected_input_digest != wanted.expected_input_digest
            or canonical_motion_digest(wanted.expected_inputs) != wanted.expected_input_digest
            or canonical_motion_digest(bundle.inputs) != wanted.expected_input_digest):
        return PreparedRejectReason.INPUTS
    if (actual.stream.generation_id != wanted.stream.generation_id
            or actual.async_identity.scope != wanted.async_identity.scope):
        return PreparedRejectReason.GENERATION
    if actual.stream.owner_id != wanted.stream.owner_id:
        return PreparedRejectReason.OWNER
    if (actual.stream.route_id != wanted.stream.route_id
            or actual.stream.action_index != wanted.stream.action_index
            or actual.route_revision != wanted.route_revision
            or actual.action_revision != wanted.action_revision):
        return PreparedRejectReason.ROUTE_ACTION
    if (actual.ledger_id != wanted.ledger_id
            or actual.boundary_applications != wanted.boundary_applications):
        return PreparedRejectReason.LEDGER
    if (actual.world_session_id != wanted.world_session_id
            or actual.snapshot_identity != wanted.snapshot_identity
            or actual.snapshot_lease_token != wanted.snapshot_lease_token
            or bundle.snapshot_identity != wanted.snapshot_identity):
        return PreparedRejectReason.WORLD_SNAPSHOT
    if actual.ruleset != wanted.ruleset:
        return PreparedRejectReason.RULESET
    # The comparisons above classify important failures. Equality below binds
    # every other field, including commands, entry/exit, permits, damage,
    # budget, suffix, inherited dependencies and complete computation identity.
    if actual != wanted:
        return PreparedRejectReason.REQUEST
    return None


def _validate_for_registration(message: PreparedWireMessage, expected: PreparedMotionRequest,
                               snapshot: SealedSnapshot, current: PreparedCurrentFacts) -> _ValidatedPrepared:
    """One decode for the receiver; public validation exposes only the typed result."""
    if (type(expected) is not PreparedMotionRequest or type(snapshot) is not SealedSnapshot
            or type(current) is not PreparedCurrentFacts):
        raise ContractViolation("prepared validation requires independent typed parent facts")
    try:
        if (type(message) is not PreparedWireMessage or type(message.payload) is not bytes
                or type(message.job_token) is not str
                or type(message.schema) is not str or type(message.payload_digest) is not str
                or message.schema not in (PREPARED_BUNDLE_SCHEMA, PREPARED_FAILURE_SCHEMA)
                or not 0 < len(message.payload) <= MAXIMUM_MESSAGE_BYTES
                or hashlib.sha256(message.payload).hexdigest() != message.payload_digest):
            return _reject(PreparedRejectReason.MESSAGE)
        if message.job_token != expected.job_token:
            return _reject(PreparedRejectReason.REQUEST)
        # No object restoration is allowed before the byte integrity check.
        decoded = pickle.loads(message.payload)
    except Exception:
        return _reject(PreparedRejectReason.MESSAGE)

    if message.schema == PREPARED_FAILURE_SCHEMA:
        try:
            if type(decoded) is not PreparedComputeResult or decoded.status is PreparedComputeStatus.PREPARED:
                return _reject(PreparedRejectReason.MESSAGE)
            require_immutable_graph(decoded)
            _validate_values(decoded)
            if decoded.job_token != expected.job_token:
                return _reject(PreparedRejectReason.REQUEST)
            if (decoded.counts.nodes_scanned > expected.scan_budget.maximum_nodes
                    or decoded.counts.physics_steps > min(MAXIMUM_PHYSICS_STEPS, expected.scan_budget.maximum_physics_steps)
                    or decoded.counts.completed_candidates > expected.scan_budget.maximum_candidates):
                return _reject(PreparedRejectReason.PROOF)
            return _ValidatedPrepared(PreparedAdmissionResult(PreparedAdmissionStatus.STALE), computation=decoded)
        except (ContractViolation, AttributeError, TypeError, ValueError):
            return _reject(PreparedRejectReason.PROOF)

    bundle = decoded
    if type(bundle) is not PreparedMotionBundle:
        return _reject(PreparedRejectReason.MESSAGE)
    try:
        if (bundle.proof.ruleset != bundle.ruleset or bundle.ruleset != snapshot.identity.ruleset
                or bundle.ruleset != current.ruleset or bundle.ruleset != expected.ruleset
                or bundle.request.ruleset != bundle.ruleset
                or bundle.snapshot_identity.ruleset != bundle.ruleset):
            return _reject(PreparedRejectReason.RULESET)
    except (AttributeError, TypeError):
        return _reject(PreparedRejectReason.RULESET)
    try:
        require_immutable_graph(bundle)
    except (ContractViolation, AttributeError, TypeError, ValueError):
        return _reject(PreparedRejectReason.MUTABLE_GRAPH)
    try:
        proof = bundle.proof
        if (len(proof.branches) not in (1, 2) or proof.physics_steps > MAXIMUM_PHYSICS_STEPS
                or any(len(branch.inputs) > MAXIMUM_TRAJECTORY_TICKS
                       or not branch.trajectory[-1].on_ground
                       or any(len(tail.inputs) > MAXIMUM_TAIL_TICKS
                              or not tail.trajectory[-1].on_ground
                              or math.hypot(tail.trajectory[-1].velocity_blocks_per_tick[0],
                                            tail.trajectory[-1].velocity_blocks_per_tick[2]) > 1.e-9
                              for tail in branch.safety_tails)
                       for branch in proof.branches)):
            return _reject(PreparedRejectReason.PROOF)
        _validate_values(proof)
        canonical = canonical_motion_bytes(proof)
        if (len(canonical) > MAXIMUM_MESSAGE_BYTES
                or hashlib.sha256(canonical).hexdigest() != bundle.proof_digest
                or type(bundle.sealed_bytes) is not bytes
                or len(bundle.sealed_bytes) > MAXIMUM_MESSAGE_BYTES
                or canonical_bundle_bytes(bundle) != bundle.sealed_bytes):
            return _reject(PreparedRejectReason.PROOF)
    except (ContractViolation, AttributeError, TypeError, ValueError):
        return _reject(PreparedRejectReason.PROOF)
    try:
        mismatch = _parent_mismatch(bundle, expected)
        if mismatch is not None:
            return _reject(mismatch)
        _validate_values(bundle)
    except (ContractViolation, AttributeError, TypeError, ValueError):
        return _reject(PreparedRejectReason.REQUEST)
    if current.scope != expected.async_identity.scope or current.stream != expected.stream:
        return _ValidatedPrepared(PreparedAdmissionResult(PreparedAdmissionStatus.STALE))
    if current.body_owner_id != expected.stream.owner_id:
        return _reject(PreparedRejectReason.OWNER)
    if current.ledger_id != expected.ledger_id:
        return _reject(PreparedRejectReason.LEDGER)
    if (snapshot.identity != expected.snapshot_identity
            or current.world_session_id != expected.world_session_id):
        return _reject(PreparedRejectReason.WORLD_SNAPSHOT)
    try:
        validate_sealed_snapshot(snapshot)
        facts, versions = dict(snapshot.facts), dict(snapshot.dependency_versions)
        if (any(facts.get(position) != fact or fact.knowledge is CellKnowledge.UNKNOWN
                for position, fact in proof.dependency_facts)
                or any(versions.get(position) != version for position, version in proof.dependency_versions)
                or any(versions.get(position) != version for position, version in expected.inherited_dependencies)):
            return _reject(PreparedRejectReason.DEPENDENCY)
        current_versions = dict(current.dependency_versions)
        if any(current_versions.get(position) != version for position, version in proof.dependency_versions):
            return _reject(PreparedRejectReason.DEPENDENCY)
    except (ContractViolation, AttributeError, TypeError, ValueError):
        return _reject(PreparedRejectReason.WORLD_SNAPSHOT)
    if (current.movement_tick > expected.deadline_tick
            or current.permitted_window != expected.requested_window
            or current.damage_remaining_points < expected.damage_remaining_points):
        return _ValidatedPrepared(PreparedAdmissionResult(PreparedAdmissionStatus.STALE))
    hot = PreparedMotionHotView.from_bundle(bundle)
    stream = expected.stream
    handle = PreparedMotionHandle(uuid.uuid4().hex, stream.request_id, stream.generation_id,
                                  stream.owner_id, stream.route_id, stream.action_index,
                                  bundle.proof_digest, proof.dependency_versions, hot)
    return _ValidatedPrepared(PreparedAdmissionResult(PreparedAdmissionStatus.REGISTERED, handle), bundle)


def validate_prepared_message(message: PreparedWireMessage, expected: PreparedMotionRequest,
                              snapshot: SealedSnapshot, current: PreparedCurrentFacts) -> PreparedAdmissionResult:
    return _validate_for_registration(message, expected, snapshot, current).admission
