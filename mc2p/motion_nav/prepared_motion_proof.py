"""Complete fixed-input proof on a transmitted, Runtime-owned snapshot.

The scan order and counters retain the frozen commitment scanner's pure
semantics. This module performs no input search and imports no experiment.
Unsafe immediate stops are replaced by the already proved continuation to
the recovered boundary followed by that boundary's safe stopping tail.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import json
import math
import pickle

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.contracts.motion_activation import StreamStartBranch, validate_frozen_value
from mc2p.motion_nav.geometry import QueryStatus, query_support, required_cells_for_sweep
from mc2p.motion_nav.motion_risk import conservative_plain_fall_damage_points
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.prepared_motion import (
    PreparedBranchProof, PreparedComputeCounts, PreparedComputeResult, PreparedComputeStatus,
    PreparedMotionBundle, PreparedMotionProof, PreparedMotionRequest, PreparedSafetyTail,
)
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.world_model import CellKnowledge, WorldView
from mc2p.motion_nav.world_snapshot import SealedSnapshot, validate_sealed_snapshot


PREPARED_BUNDLE_SCHEMA = "mc2p.prepared-motion-bundle.v1"
PREPARED_FAILURE_SCHEMA = "mc2p.prepared-motion-compute-failure.v1"
MAXIMUM_MESSAGE_BYTES = 2 * 1024 * 1024
MAXIMUM_PHYSICS_STEPS = 65_536
MAXIMUM_TRAJECTORY_TICKS = 40
MAXIMUM_TAIL_TICKS = 80
_SCANNER_TAIL_TICKS = 40
_MINIMUM_SAFE_SUPPORT_FRACTION = .15


class PreparedMessageBudgetExceeded(ContractViolation):
    """Sealing exceeded the existing per-message byte budget."""


def _canonical(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if type(value) is bytes:
        return {"bytes_hex": value.hex()}
    if type(value) is tuple:
        return [_canonical(item) for item in value]
    if type(value) is frozenset:
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def canonical_motion_bytes(value) -> bytes:
    """Canonical immutable motion values, calculated only in the background."""
    validate_frozen_value(value)
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=True).encode("utf-8")


def canonical_motion_digest(value) -> str:
    return hashlib.sha256(canonical_motion_bytes(value)).hexdigest()


def _bundle_bytes(request, inputs, input_digest, proof, proof_digest) -> bytes:
    # Keep all bindings, without recursively including sealed_bytes itself.
    return canonical_motion_bytes((PREPARED_BUNDLE_SCHEMA, request, inputs, input_digest,
                                   proof, proof_digest, request.snapshot_identity, request.ruleset))


def canonical_bundle_bytes(bundle: PreparedMotionBundle) -> bytes:
    if type(bundle) is not PreparedMotionBundle:
        raise ContractViolation("bundle sealing requires the exact prepared bundle type")
    return _bundle_bytes(bundle.request, bundle.inputs, bundle.input_digest,
                         bundle.proof, bundle.proof_digest)


@dataclass(frozen=True, slots=True)
class PreparedWireMessage:
    job_token: str
    schema: str
    payload: bytes
    payload_digest: str

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        require_identifier(self.job_token, "prepared wire job token")
        if self.schema not in (PREPARED_BUNDLE_SCHEMA, PREPARED_FAILURE_SCHEMA):
            raise ContractViolation("prepared wire requires a supported schema")
        if not self.payload:
            raise ContractViolation("prepared wire payload must not be empty")
        if len(self.payload) > MAXIMUM_MESSAGE_BYTES:
            raise PreparedMessageBudgetExceeded("prepared wire payload exceeds its byte budget")
        if hashlib.sha256(self.payload).hexdigest() != self.payload_digest:
            raise ContractViolation("prepared wire digest differs from its sealed payload")


def seal_bundle(bundle: PreparedMotionBundle) -> PreparedWireMessage:
    """Seal a complete bundle; independent parent comparison still follows."""
    if type(bundle) is not PreparedMotionBundle:
        raise ContractViolation("bundle sealing requires the exact prepared bundle type")
    bundle.__post_init__()
    if (canonical_motion_digest(bundle.inputs) != bundle.input_digest
            or canonical_motion_digest(bundle.proof) != bundle.proof_digest
            or canonical_bundle_bytes(bundle) != bundle.sealed_bytes):
        raise ContractViolation("prepared bundle inputs, proof or sealed bytes have changed")
    payload = pickle.dumps(bundle, protocol=5)
    return PreparedWireMessage(bundle.request.job_token, PREPARED_BUNDLE_SCHEMA, payload,
                               hashlib.sha256(payload).hexdigest())


def seal_compute_failure(result: PreparedComputeResult) -> PreparedWireMessage:
    if type(result) is not PreparedComputeResult or result.status is PreparedComputeStatus.PREPARED:
        raise ContractViolation("failure sealing requires a typed unsuccessful computation")
    result.__post_init__()
    payload = pickle.dumps(result, protocol=5)
    return PreparedWireMessage(result.job_token, PREPARED_FAILURE_SCHEMA, payload,
                               hashlib.sha256(payload).hexdigest())


class _ScanFailure(Exception):
    def __init__(self, status, missing_cells=()):
        self.status = status
        self.missing_cells = tuple(sorted(set(missing_cells)))


class _RequestCounter:
    """All branch, prelude, rollout, stop and recovery work has one owner."""
    def __init__(self, budget):
        self.budget = budget
        self.nodes = self.physics_steps = self.tail_steps = self.candidates = 0

    @property
    def counts(self):
        return PreparedComputeCounts(self.nodes, self.physics_steps, self.candidates, self.tail_steps)

    def node(self):
        if self.nodes >= self.budget.maximum_nodes:
            raise _ScanFailure(PreparedComputeStatus.BUDGET_EXHAUSTED)
        self.nodes += 1

    def calculated(self, current, command, world, dependencies, *, tail=False):
        if self.physics_steps >= min(self.budget.maximum_physics_steps, MAXIMUM_PHYSICS_STEPS):
            raise _ScanFailure(PreparedComputeStatus.BUDGET_EXHAUSTED)
        self.physics_steps += 1
        self.tail_steps += int(tail)
        calculated = step(current, command, world, JAVA_1_21_RULESET)
        dependencies.update(calculated.dependencies)
        if calculated.status is CalculationStatus.NEEDS_WORLD:
            raise _ScanFailure(PreparedComputeStatus.MISSING_INFORMATION, calculated.missing_cells)
        if calculated.status is CalculationStatus.UNSUPPORTED:
            raise _ScanFailure(PreparedComputeStatus.UNSUPPORTED)
        if calculated.status is not CalculationStatus.OK:
            raise _ScanFailure(PreparedComputeStatus.BLOCKED)
        return calculated.next_state

    def complete_candidate(self):
        if self.candidates >= self.budget.maximum_candidates:
            raise _ScanFailure(PreparedComputeStatus.BUDGET_EXHAUSTED)
        self.candidates += 1


def _damage(states):
    # Preserve conservative multi-fall accounting, using the existing risk
    # function for every fall rather than reimplementing damage physics.
    total = peak = 0.
    for state in states:
        peak = max(peak, state.fall_distance_blocks)
        if state.on_ground:
            total += conservative_plain_fall_damage_points(peak)
            peak = 0.
    return total + conservative_plain_fall_damage_points(peak)


def _safe_support(state, world, dependencies):
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    facts = {position: world.cell(position) for position in cells}
    dependencies.update(cells)
    support = query_support(state.body_box, WorldView.detached(
        world.session, world.geometry_revision, 0, facts))
    dependencies.update(support.dependencies)
    if support.missing_cells:
        raise _ScanFailure(PreparedComputeStatus.MISSING_INFORMATION, support.missing_cells)
    if support.status is QueryStatus.UNSUPPORTED:
        raise _ScanFailure(PreparedComputeStatus.UNSUPPORTED)
    return (state.on_ground and support.status is QueryStatus.FEASIBLE
            and support.support_fraction >= _MINIMUM_SAFE_SUPPORT_FRACTION - 1.e-9)


@dataclass(frozen=True, slots=True)
class _StoppingTail:
    safe: bool
    inputs: tuple
    states: tuple
    dependencies: tuple
    damage: float


def _stop_tail(counter, prefix, irrevocable, stop_input, world, minimum_y, maximum_damage):
    counter.node()
    current = prefix[-1]
    states, inputs, dependencies = [current], [], set()
    for tick in range(_SCANNER_TAIL_TICKS):
        command = irrevocable[tick] if tick < len(irrevocable) else stop_input
        current = counter.calculated(current, command, world, dependencies, tail=True)
        states.append(current)
        inputs.append(command)
        damage = _damage(prefix[:-1] + tuple(states))
        if current.position[1] < minimum_y - 1.e-7 or damage > maximum_damage + 1.e-9:
            return _StoppingTail(False, tuple(inputs), tuple(states), tuple(sorted(dependencies)), damage)
        speed = math.hypot(current.velocity_blocks_per_tick[0], current.velocity_blocks_per_tick[2])
        if (tick + 1 >= len(irrevocable) and speed <= 1.e-9
                and current.position[1] >= minimum_y - 1.e-7
                and damage <= maximum_damage + 1.e-9
                and _safe_support(current, world, dependencies)):
            return _StoppingTail(True, tuple(inputs), tuple(states), tuple(sorted(dependencies)), damage)
    raise _ScanFailure(PreparedComputeStatus.BLOCKED)


def _risk_intervals(tails, counter, world, dependencies):
    intervals, first = [], None
    for boundary, tail in enumerate(tails):
        if not tail.safe and first is None:
            first = boundary
        elif tail.safe and first is not None and tail.states[0].on_ground:
            counter.node()
            if not _safe_support(tail.states[0], world, dependencies):
                continue
            intervals.append((first - 1 if first else None, first, boundary))
            first = None
    if first is not None or len(intervals) > 1:
        # D102's one air action does not widen into multi-risk composition.
        raise _ScanFailure(PreparedComputeStatus.BLOCKED)
    return tuple(intervals)


def _dependency_values(snapshot, positions):
    versions = dict(snapshot.dependency_versions)
    positions = tuple(sorted(positions))
    missing = tuple(position for position in positions if position not in versions)
    if missing:
        raise _ScanFailure(PreparedComputeStatus.MISSING_INFORMATION, missing)
    return (tuple((position, snapshot.cell(position)) for position in positions),
            tuple((position, versions[position]) for position in positions))


def _scan_branch(request, snapshot, world, counter, branch):
    start_nodes, start_steps, start_tails = counter.nodes, counter.physics_steps, counter.tail_steps
    counter.node()
    anchor = request.entry_state
    prelude, dependencies = [anchor], set()
    if branch is StreamStartBranch.LATE_ONE:
        current = counter.calculated(anchor, request.stream.late_start_hold_input, world, dependencies)
        prelude.append(current)
        if (not anchor.on_ground or not current.on_ground or current.horizontal_collision
                or not math.isclose(current.position[1], anchor.position[1], abs_tol=1.e-7)
                or not _safe_support(anchor, world, dependencies)
                or not _safe_support(current, world, dependencies)):
            raise _ScanFailure(PreparedComputeStatus.BLOCKED)
    first_tick = request.requested_window.earliest_start_tick + int(branch is StreamStartBranch.LATE_ONE)
    if prelude[-1].movement_tick_id + 1 != first_tick:
        raise _ScanFailure(PreparedComputeStatus.BLOCKED)
    states, step_dependencies = [prelude[-1]], []
    for command in request.expected_inputs:
        used = set()
        states.append(counter.calculated(states[-1], command, world, used))
        step_dependencies.append(used)
        dependencies.update(used)
    states = tuple(states)
    normal_damage = _damage(tuple(prelude[:-1]) + states)
    if normal_damage > request.damage_remaining_points or not states[-1].on_ground:
        raise _ScanFailure(PreparedComputeStatus.BLOCKED)
    minimum_y = min(states[0].position[1], states[-1].position[1])
    stop_input = TickInput(0., 0., False, False, False, anchor.yaw_radians)
    tails = []
    for boundary in range(len(request.expected_inputs) + 1):
        prefix = tuple(prelude[:-1]) + states[:boundary + 1]
        tail = _stop_tail(counter, prefix, request.expected_inputs[boundary:boundary + 1],
                          stop_input, world, minimum_y, request.damage_remaining_points)
        dependencies.update(tail.dependencies)
        if branch is StreamStartBranch.LATE_ONE and boundary == 0 and not tail.safe:
            raise _ScanFailure(PreparedComputeStatus.BLOCKED)
        tails.append(tail)
    if not tails[-1].safe:
        raise _ScanFailure(PreparedComputeStatus.BLOCKED)
    intervals = _risk_intervals(tails, counter, world, dependencies)
    safety_tails = []
    for boundary, tail in enumerate(tails):
        if tail.safe:
            inputs, trajectory, used, damage = tail.inputs, tail.states, set(tail.dependencies), tail.damage
        else:
            _, first, recovered = intervals[0]
            if not first <= boundary < recovered:
                raise _ScanFailure(PreparedComputeStatus.BLOCKED)
            recovered_tail = tails[recovered]
            inputs = request.expected_inputs[boundary:recovered] + recovered_tail.inputs
            trajectory = states[boundary:recovered] + recovered_tail.states
            used = set(recovered_tail.dependencies)
            for positions in step_dependencies[boundary:recovered]:
                used.update(positions)
            damage = recovered_tail.damage
        if len(inputs) > MAXIMUM_TAIL_TICKS:
            raise _ScanFailure(PreparedComputeStatus.BUDGET_EXHAUSTED)
        facts, versions = _dependency_values(snapshot, used)
        tail_tick = trajectory[0].movement_tick_id + 1
        safety_tails.append(PreparedSafetyTail(
            boundary, branch, inputs, trajectory, facts, versions, damage,
            CandidateExecutionWindow(tail_tick, tail_tick)))
    facts, versions = _dependency_values(snapshot, dependencies)
    last_abandon, committed = (intervals[0][:2] if intervals else (0, 0))
    return PreparedBranchProof(
        branch, first_tick, request.expected_inputs, states, tuple(safety_tails),
        committed, 0 if last_abandon is None else last_abandon, facts, versions,
        request.ruleset, counter.nodes - start_nodes, counter.physics_steps - start_steps,
        stopping_tail_steps=counter.tail_steps - start_tails)


def prepare_fixed_motion(request: PreparedMotionRequest, snapshot: SealedSnapshot) -> PreparedComputeResult:
    """Replay the parent's exact inputs; never choose or repair a candidate."""
    if type(request) is not PreparedMotionRequest or type(snapshot) is not SealedSnapshot:
        raise ContractViolation("fixed proof requires a typed request and sealed snapshot")
    request.__post_init__()
    validate_sealed_snapshot(snapshot)
    if request.snapshot_identity != snapshot.identity:
        raise ContractViolation("fixed proof received a different parent snapshot")
    if canonical_motion_digest(request.expected_inputs) != request.expected_input_digest:
        raise ContractViolation("fixed proof received a different parent input digest")
    counter = _RequestCounter(request.scan_budget)
    try:
        if request.ruleset != JAVA_1_21_RULESET:
            raise _ScanFailure(PreparedComputeStatus.UNSUPPORTED)
        if len(request.expected_inputs) > MAXIMUM_TRAJECTORY_TICKS:
            raise _ScanFailure(PreparedComputeStatus.BUDGET_EXHAUSTED)
        snapshot_versions = dict(snapshot.dependency_versions)
        missing_inherited = tuple(position for position, _ in request.inherited_dependencies
                                  if position not in snapshot_versions
                                  or snapshot.cell(position).knowledge is CellKnowledge.UNKNOWN)
        if missing_inherited:
            raise _ScanFailure(PreparedComputeStatus.MISSING_INFORMATION, missing_inherited)
        for position, expected in request.inherited_dependencies:
            if snapshot_versions[position] != expected:
                raise _ScanFailure(PreparedComputeStatus.BLOCKED)
        world = PhysicsWorldView.from_snapshot(snapshot)
        starts = ((StreamStartBranch.ON_TIME, StreamStartBranch.LATE_ONE)
                  if request.requested_window.latest_start_tick > request.requested_window.earliest_start_tick
                  else (StreamStartBranch.ON_TIME,))
        branches = tuple(_scan_branch(request, snapshot, world, counter, branch) for branch in starts)
        counter.complete_candidate()
        if branches[0].trajectory[-1] != request.exit_state:
            raise _ScanFailure(PreparedComputeStatus.BLOCKED)
        # A fixed candidate is counted once, not once per timing hypothesis.
        branches = branches[:-1] + (replace(branches[-1], completed_candidates=1),)
        dependencies = set(position for branch in branches for position, _ in branch.dependency_facts)
        dependencies.update(position for position, _ in request.inherited_dependencies)
        facts, versions = _dependency_values(snapshot, dependencies)
        proof = PreparedMotionProof(request.stream, branches, request.ruleset, facts, versions,
                                    counter.nodes, counter.physics_steps, counter.candidates,
                                    counter.tail_steps)
        digest = canonical_motion_digest(proof)
        bundle = PreparedMotionBundle(request, request.expected_inputs, request.expected_input_digest,
                                      proof, digest, snapshot.identity, request.ruleset,
                                      _bundle_bytes(request, request.expected_inputs,
                                                    request.expected_input_digest, proof, digest))
        return PreparedComputeResult(PreparedComputeStatus.PREPARED, request.job_token,
                                     counter.counts, bundle)
    except _ScanFailure as failure:
        return PreparedComputeResult(failure.status, request.job_token, counter.counts,
                                     missing_cells=failure.missing_cells)
    except Exception:
        # Internal calculation failures preserve consumed work and never carry
        # a candidate. Bad parent contracts above are rejected before scanning.
        return PreparedComputeResult(PreparedComputeStatus.WORKER_FAILED, request.job_token, counter.counts)
