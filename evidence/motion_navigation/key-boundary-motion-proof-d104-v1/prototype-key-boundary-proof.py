"""D104 compact commitment proof for the isolated D101 path.

The old scanner remains the K0 oracle.  This module replays the main trajectory
once, finds risk transitions from observed support and input-transition events,
and retains stopping tails only at safety-relevant boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState, TickInput
from mc2p.motion_nav.world_model import BlockPos, CellFact

from .commitment import (
    BoundaryInputs,
    CandidateRejection,
    RiskInterval,
    ScanOptions,
    ScanStatus,
    TailStatus,
    _Incomplete,
    _calculated,
    _check_evidence,
    _damage,
    _inputs,
    _prove_entry,
    _safe_support,
    _tail,
)
from .contracts import SearchReason, TimingBranch, TrajectorySearchRequest
from .physics import CountedPhysics, CountLimit, ScanCounts, trajectory_digest


@dataclass(frozen=True, slots=True)
class CompactStopTail:
    boundary: int
    inputs: tuple[TickInput, ...]
    start_state: PhysicsState
    end_state: PhysicsState
    dependencies: tuple[BlockPos, ...]
    damage_points: float
    absolute_tick: int
    maximum_ticks: int
    tail_digest: str


@dataclass(frozen=True, slots=True)
class KeyBoundaryBranch:
    timing_branch: TimingBranch
    states: tuple[PhysicsState, ...]
    safety_tails: tuple[CompactStopTail, ...]
    risk_intervals: tuple[RiskInterval, ...]
    comparison_boundaries: tuple[int, ...]
    damage_points: float
    prelude_states: tuple[PhysicsState, ...]
    prelude_dependencies: tuple[BlockPos, ...]
    effect_tick: int
    total_ticks: int

    def safety_tail(self, boundary: int) -> CompactStopTail | None:
        return next((tail for tail in self.safety_tails if tail.boundary == boundary), None)


@dataclass(frozen=True, slots=True)
class KeyBoundaryProof:
    request: TrajectorySearchRequest
    inputs: tuple[TickInput, ...]
    boundary_inputs: tuple[tuple[BoundaryInputs, ...], ...]
    branches: tuple[KeyBoundaryBranch, ...]
    dependency_facts: tuple[tuple[BlockPos, CellFact], ...]
    ruleset: object
    options: ScanOptions
    counts: ScanCounts
    trajectory_hash: str


@dataclass(frozen=True, slots=True)
class KeyBoundaryScanResult:
    status: ScanStatus
    reason: SearchReason | CandidateRejection | None
    counts: ScanCounts
    proof: KeyBoundaryProof | None = None
    missing_cells: tuple[BlockPos, ...] = ()


def _tail_digest(boundary, inputs, start, end, dependencies, damage, absolute_tick):
    return trajectory_digest((boundary, inputs, start, end, dependencies, damage, absolute_tick))


def _compact(tail) -> CompactStopTail:
    if tail.status is not TailStatus.SAFE_STOP:
        raise ContractViolation("only a safe key tail can enter a compact proof")
    inputs = tail.inputs
    dependencies = tail.dependencies
    start, end = tail.states[0], tail.states[-1]
    return CompactStopTail(
        tail.boundary, inputs, start, end, dependencies, tail.damage_points,
        tail.absolute_tick, len(inputs),
        _tail_digest(tail.boundary, inputs, start, end, dependencies,
                     tail.damage_points, tail.absolute_tick),
    )


def _event_boundaries(inputs, rows, support_safe):
    """Return dynamic places where stopping safety can change.

    A risk transition can start when the main input changes, when already
    sampled input changes, or when support changes.  Adjacent boundaries are
    included because a stopping tail starts before the command takes effect.
    No action name or jump tick participates in this calculation.
    """
    end = len(inputs)
    events = {0, end}

    def around(boundary):
        events.update(index for index in (boundary - 1, boundary, boundary + 1)
                      if 0 <= index <= end)

    for boundary in range(1, end):
        if inputs[boundary] != inputs[boundary - 1]:
            around(boundary)
    for boundary in range(1, end + 1):
        if rows[boundary].irrevocable_inputs != rows[boundary - 1].irrevocable_inputs:
            around(boundary)
        if support_safe[boundary] != support_safe[boundary - 1]:
            around(boundary)
    return tuple(sorted(events))


def _derive_branch(request, inputs, rows, branch_index, branch, world, counter,
                   dependencies, options):
    counter.node()
    prelude_states, prelude_dependencies = _prove_entry(
        counter, request, branch_index, world)
    dependencies.update(prelude_dependencies)
    entry = prelude_states[-1]
    states = [entry]
    for command in inputs:
        states.append(_calculated(counter, states[-1], command, world, dependencies))
    states = tuple(states)
    normal_damage = _damage(prelude_states[:-1] + states)
    if normal_damage > request.task_damage_budget.maximum_expected_damage_points:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.DAMAGE_ALLOWANCE)
    if not states[-1].on_ground:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.FINAL_EXIT_NOT_GROUNDED)
    minimum_y = min(entry.position[1], states[-1].position[1])

    support_safe = []
    for state in states:
        support_safe.append(_safe_support(state, world, dependencies))

    tails = {}

    def tail_at(boundary):
        if boundary not in tails:
            row = rows[boundary]
            tails[boundary] = _tail(
                counter, boundary,
                prelude_states[:-1] + states[:boundary + 1],
                row.irrevocable_inputs, request.stop_input, world, minimum_y,
                request.task_damage_budget.maximum_expected_damage_points, options)
            dependencies.update(tails[boundary].dependencies)
        return tails[boundary]

    seeds = _event_boundaries(inputs, rows, support_safe)
    for boundary in seeds:
        tail_at(boundary)
    if request.branch_preludes[branch_index] and tail_at(0).status is not TailStatus.SAFE_STOP:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.PRELUDE_UNSAFE)
    if tail_at(len(inputs)).status is not TailStatus.SAFE_STOP:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.FINAL_STOP_UNSAFE)

    intervals = []
    covered_until = -1
    for seed in seeds:
        if seed < covered_until or tail_at(seed).status is TailStatus.SAFE_STOP:
            continue
        first = seed
        while first > 0 and tail_at(first - 1).status is TailStatus.UNSAFE:
            first -= 1
        last_abandon = first - 1 if first else None
        if last_abandon is not None and tail_at(last_abandon).status is not TailStatus.SAFE_STOP:
            raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                              CandidateRejection.UNRECOVERED_RISK)
        recovered = None
        for boundary in range(first + 1, len(inputs) + 1):
            if support_safe[boundary] and tail_at(boundary).status is TailStatus.SAFE_STOP:
                recovered = boundary
                break
        if recovered is None:
            raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                              CandidateRejection.UNRECOVERED_RISK)
        intervals.append(RiskInterval(
            last_abandon, first, recovered,
            None if last_abandon is None else states[last_abandon].movement_tick_id,
            states[first].movement_tick_id, states[first].movement_tick_id + 1,
            states[recovered].movement_tick_id,
        ))
        covered_until = recovered
    if len(intervals) > 1:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.UNRECOVERED_RISK)

    safety_boundaries = {0, len(inputs)}
    comparison_boundaries = {0, len(inputs)}
    for interval in intervals:
        if interval.last_abandon_boundary is not None:
            safety_boundaries.add(interval.last_abandon_boundary)
            comparison_boundaries.add(interval.last_abandon_boundary)
        comparison_boundaries.add(interval.first_committed_boundary)
        safety_boundaries.add(interval.recovered_boundary)
        comparison_boundaries.add(interval.recovered_boundary)
    compact_tails = tuple(_compact(tail_at(boundary))
                          for boundary in sorted(safety_boundaries))
    return KeyBoundaryBranch(
        branch, states, compact_tails, tuple(intervals),
        tuple(sorted(comparison_boundaries)), normal_damage,
        prelude_states, prelude_dependencies,
        request.allowed_effect_ticks[branch_index],
        states[-1].movement_tick_id - request.anchor_state.movement_tick_id,
    )


def compact_scan_commitment(request, inputs, world, boundary_inputs, *,
                            options=ScanOptions(), counter=None):
    """Create a compact proof without invoking the old full scanner."""
    if type(request) is not TrajectorySearchRequest or type(options) is not ScanOptions:
        raise ContractViolation("compact scan requires a typed request and options")
    _inputs(inputs)
    if type(world) is not PhysicsWorldView or not world.is_detached:
        raise ContractViolation("compact scanner requires a detached physics world snapshot")
    if world.ruleset != JAVA_1_21_RULESET:
        raise ContractViolation("compact scanner supports only the Java 1.21 ruleset")
    if (inputs[:len(request.input_prefix)] != request.input_prefix
            or any(command not in request.supported_inputs for command in inputs)):
        raise ContractViolation("candidate must extend the shared prefix with supported inputs")
    if counter is None:
        counter = CountedPhysics(request.budget)
    elif type(counter) is not CountedPhysics or counter.budget != request.budget:
        raise ContractViolation("shared counter must own this request's unchanged total budget")
    try:
        if world.session != request.world_session or world.geometry_revision != request.geometry_revision:
            raise _Incomplete(ScanStatus.STALE, SearchReason.WORLD_DEPENDENCY)
        if any(prelude is None for prelude in request.branch_preludes):
            raise _Incomplete(ScanStatus.NEEDS_INFORMATION,
                              SearchReason.MISSING_INPUT_APPLICATION)
        _check_evidence(request, inputs, boundary_inputs)
        if len(request.entry_states) > request.budget.max_timing_branches:
            raise CountLimit(SearchReason.TIMING_BRANCH_BUDGET)
        if len(inputs) > request.budget.max_trajectory_ticks:
            raise CountLimit(SearchReason.TRAJECTORY_TICK_BUDGET)
        dependencies = set()
        branches = tuple(
            _derive_branch(request, inputs, rows, index, branch, world, counter,
                           dependencies, options)
            for index, (branch, rows) in enumerate(zip(request.timing_branches,
                                                        boundary_inputs))
        )
        facts = tuple((position, world.cell(position)) for position in sorted(dependencies))
        proof = KeyBoundaryProof(
            request, inputs, boundary_inputs, branches, facts, JAVA_1_21_RULESET,
            options, counter.counts, "")
        proof = replace(proof, trajectory_hash=trajectory_digest(proof))
        validate_compact_proof(proof)
        return KeyBoundaryScanResult(ScanStatus.VERIFIED_CANDIDATE, None,
                                     counter.counts, proof)
    except CountLimit as limited:
        return KeyBoundaryScanResult(ScanStatus.NO_TRAJECTORY_IN_BUDGET,
                                     limited.reason, counter.counts)
    except _Incomplete as incomplete:
        return KeyBoundaryScanResult(incomplete.status, incomplete.reason,
                                     counter.counts,
                                     missing_cells=incomplete.missing)


def validate_compact_proof(proof: KeyBoundaryProof) -> None:
    if type(proof) is not KeyBoundaryProof:
        raise ContractViolation("compact proof requires its exact type")
    if trajectory_digest(replace(proof, trajectory_hash="")) != proof.trajectory_hash:
        raise ContractViolation("compact proof digest has changed")
    end = len(proof.inputs)
    for branch in proof.branches:
        if len(branch.risk_intervals) > 1:
            raise ContractViolation("compact proof supports one risk interval")
        required = {0, end}
        comparisons = {0, end}
        for interval in branch.risk_intervals:
            if interval.last_abandon_boundary is not None:
                required.add(interval.last_abandon_boundary)
                comparisons.add(interval.last_abandon_boundary)
            required.add(interval.recovered_boundary)
            comparisons.update((interval.first_committed_boundary,
                                interval.recovered_boundary))
        if tuple(tail.boundary for tail in branch.safety_tails) != tuple(sorted(required)):
            raise ContractViolation("compact proof is missing a key safety tail")
        if branch.comparison_boundaries != tuple(sorted(comparisons)):
            raise ContractViolation("compact proof is missing a comparison boundary")
        for tail in branch.safety_tails:
            if (tail.start_state != branch.states[tail.boundary]
                    or tail.maximum_ticks != len(tail.inputs)
                    or not 1 <= tail.maximum_ticks <= proof.options.max_tail_ticks
                    or tail.end_state.movement_tick_id
                    != tail.start_state.movement_tick_id + len(tail.inputs)):
                raise ContractViolation("compact key safety tail contract changed")
            expected = _tail_digest(
                tail.boundary, tail.inputs, tail.start_state, tail.end_state,
                tail.dependencies, tail.damage_points, tail.absolute_tick)
            if expected != tail.tail_digest:
                raise ContractViolation("compact key safety tail digest has changed")


def prove_deviation_tail(proof: KeyBoundaryProof, branch_index: int, boundary: int,
                         world: PhysicsWorldView, counter: CountedPhysics) -> CompactStopTail:
    """Prove one event-triggered pre-commit stop from the observed boundary.

    Compact proofs intentionally do not carry a tail for every ordinary tick.
    When a command actually goes missing before commitment, the owner may spend
    bounded work once before sending a stopping input.
    """
    validate_compact_proof(proof)
    if (type(branch_index) is not int or not 0 <= branch_index < len(proof.branches)
            or type(boundary) is not int or not 0 <= boundary <= len(proof.inputs)):
        raise ContractViolation("deviation tail requires an existing proof boundary")
    if type(counter) is not CountedPhysics or counter.budget != proof.request.budget:
        raise ContractViolation("deviation tail must share the request physics budget")
    branch = proof.branches[branch_index]
    if any(world.cell(position) != fact for position, fact in proof.dependency_facts):
        raise ContractViolation("deviation tail dependencies have changed")
    interval = branch.risk_intervals[0] if branch.risk_intervals else None
    if interval is not None and boundary >= interval.first_committed_boundary:
        raise ContractViolation("committed deviation must follow the proved recovery segment")
    row = proof.boundary_inputs[branch_index][boundary]
    minimum_y = min(branch.states[0].position[1], branch.states[-1].position[1])
    tail = _tail(
        counter, boundary,
        branch.prelude_states[:-1] + branch.states[:boundary + 1],
        row.irrevocable_inputs, proof.request.stop_input, world, minimum_y,
        proof.request.task_damage_budget.maximum_expected_damage_points, proof.options)
    if tail.status is not TailStatus.SAFE_STOP:
        raise ContractViolation("pre-commit deviation has no safe bounded tail")
    return _compact(tail)
