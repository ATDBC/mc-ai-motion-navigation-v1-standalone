"""D105 T1-A sound sparse commitment proof for the isolated D101 path.

The scanner keeps the legacy boundary order.  Its only omitted work is a
hypothetical stopping tail at an explicitly airborne boundary while a risk is
already open.  Such an omission is recorded, but is never classified SAFE or
UNSAFE and never supplies recovery inputs or dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

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
    StopTail,
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


class BoundaryDecisionSource(StrEnum):
    """Typed source for one sequential boundary decision."""

    RISK_CLOSED_TAIL = "RISK_CLOSED_TAIL"
    AIRBORNE_TAIL_OMITTED = "AIRBORNE_TAIL_OMITTED"
    OPEN_RISK_TAIL_EVALUATED = "OPEN_RISK_TAIL_EVALUATED"


class _RiskCertainty(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    boundary: int
    source: BoundaryDecisionSource
    risk_open_before: bool
    risk_open_after: bool
    tail: StopTail | None
    support_queried: bool
    support_safe: bool | None
    dependencies: tuple[BlockPos, ...]

    @property
    def tail_status(self) -> TailStatus | None:
        return None if self.tail is None else self.tail.status


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
class SoundSparseBranch:
    timing_branch: TimingBranch
    states: tuple[PhysicsState, ...]
    boundary_decisions: tuple[BoundaryDecision, ...]
    safety_tails: tuple[CompactStopTail, ...]
    risk_intervals: tuple[RiskInterval, ...]
    damage_points: float
    prelude_states: tuple[PhysicsState, ...]
    prelude_dependencies: tuple[BlockPos, ...]
    effect_tick: int
    total_ticks: int

    def safety_tail(self, boundary: int) -> CompactStopTail | None:
        return next((tail for tail in self.safety_tails if tail.boundary == boundary), None)


@dataclass(frozen=True, slots=True)
class SoundSparseProof:
    request: TrajectorySearchRequest
    inputs: tuple[TickInput, ...]
    boundary_inputs: tuple[tuple[BoundaryInputs, ...], ...]
    branches: tuple[SoundSparseBranch, ...]
    dependency_facts: tuple[tuple[BlockPos, CellFact], ...]
    ruleset: object
    options: ScanOptions
    counts: ScanCounts
    trajectory_hash: str


@dataclass(frozen=True, slots=True)
class SoundSparseScanResult:
    status: ScanStatus
    reason: SearchReason | CandidateRejection | None
    counts: ScanCounts
    proof: SoundSparseProof | None = None
    missing_cells: tuple[BlockPos, ...] = ()


@dataclass(frozen=True, slots=True)
class _SupportProbe:
    safe: bool | None
    dependencies: tuple[BlockPos, ...]
    status: ScanStatus | None = None
    reason: SearchReason | CandidateRejection | None = None
    missing: tuple[BlockPos, ...] = ()


def _probe_safe_support(state, world) -> _SupportProbe:
    """Read immutable support early without committing query order or budget."""
    dependencies = set()
    try:
        safe = _safe_support(state, world, dependencies)
        return _SupportProbe(safe, tuple(sorted(dependencies)))
    except _Incomplete as incomplete:
        return _SupportProbe(
            None,
            tuple(sorted(dependencies)),
            incomplete.status,
            incomplete.reason,
            tuple(incomplete.missing),
        )


def _tail_digest(tail: StopTail) -> str:
    return trajectory_digest((
        tail.boundary,
        tail.inputs,
        tail.states[0],
        tail.states[-1],
        tail.dependencies,
        tail.damage_points,
        tail.absolute_tick,
    ))


def _compact(tail: StopTail) -> CompactStopTail:
    if tail.status is not TailStatus.SAFE_STOP:
        raise ContractViolation("only an executable safe tail can enter the compact proof")
    return CompactStopTail(
        boundary=tail.boundary,
        inputs=tail.inputs,
        start_state=tail.states[0],
        end_state=tail.states[-1],
        dependencies=tail.dependencies,
        damage_points=tail.damage_points,
        absolute_tick=tail.absolute_tick,
        maximum_ticks=len(tail.inputs),
        tail_digest=_tail_digest(tail),
    )


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

    evaluated_tails = {}
    omitted_boundaries = set()
    support_probes = {}
    # Tail evaluation remains the observable first phase.  Immutable support
    # reads are cached only to decide whether later airborne tails are truly
    # hypothetical.  Their dependencies, node charge and typed failure are
    # committed later in the same order as legacy _risks.
    certainty = _RiskCertainty.CLOSED
    for row in rows:
        boundary = row.boundary
        state = states[boundary]
        if certainty is _RiskCertainty.OPEN and not state.on_ground:
            omitted_boundaries.add(boundary)
            continue
        tail = _tail(
            counter,
            boundary,
            prelude_states[:-1] + states[:boundary + 1],
            row.irrevocable_inputs,
            request.stop_input,
            world,
            minimum_y,
            request.task_damage_budget.maximum_expected_damage_points,
            options,
        )
        evaluated_tails[boundary] = tail
        dependencies.update(tail.dependencies)
        if (request.branch_preludes[branch_index] and boundary == 0
                and tail.status is not TailStatus.SAFE_STOP):
            raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                              CandidateRejection.PRELUDE_UNSAFE)
        if tail.status is TailStatus.UNSAFE:
            certainty = _RiskCertainty.OPEN
        elif state.on_ground and certainty is not _RiskCertainty.CLOSED:
            probe = _probe_safe_support(tail.states[0], world)
            support_probes[boundary] = probe
            if probe.status is not None:
                certainty = _RiskCertainty.UNRESOLVED
            elif probe.safe:
                certainty = _RiskCertainty.CLOSED
            elif certainty is not _RiskCertainty.OPEN:
                certainty = _RiskCertainty.UNRESOLVED

    terminal_tail = evaluated_tails[len(inputs)]
    if terminal_tail.status is not TailStatus.SAFE_STOP:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.FINAL_STOP_UNSAFE)

    # Risk/support processing is a second ordered phase.  This preserves the
    # legacy failure priority: every required tail has completed before the
    # first recovery support query can return UNKNOWN.
    decisions = []
    intervals = []
    first = None
    for row in rows:
        boundary = row.boundary
        risk_open_before = first is not None
        if boundary in omitted_boundaries:
            if not risk_open_before or states[boundary].on_ground:
                raise ContractViolation("airborne omission lost its open-risk basis")
            decisions.append(BoundaryDecision(
                boundary=boundary,
                source=BoundaryDecisionSource.AIRBORNE_TAIL_OMITTED,
                risk_open_before=True,
                risk_open_after=True,
                tail=None,
                support_queried=False,
                support_safe=None,
                dependencies=(),
            ))
            continue

        tail = evaluated_tails[boundary]
        before_support_dependencies = set(dependencies)
        support_queried = False
        support_safe = None
        if first is None:
            if tail.status is TailStatus.UNSAFE:
                first = boundary
        elif tail.status is TailStatus.SAFE_STOP and states[boundary].on_ground:
            support_queried = True
            counter.node()
            probe = support_probes[boundary]
            dependencies.update(probe.dependencies)
            if probe.status is not None:
                raise _Incomplete(probe.status, probe.reason, probe.missing)
            support_safe = probe.safe
            if support_safe:
                last_abandon = first - 1 if first else None
                intervals.append(RiskInterval(
                    last_abandon,
                    first,
                    boundary,
                    None if last_abandon is None
                    else evaluated_tails[last_abandon].absolute_tick,
                    evaluated_tails[first].absolute_tick,
                    evaluated_tails[first].absolute_tick + 1,
                    tail.absolute_tick,
                ))
                first = None

        decisions.append(BoundaryDecision(
            boundary=boundary,
            source=(BoundaryDecisionSource.OPEN_RISK_TAIL_EVALUATED
                    if risk_open_before
                    else BoundaryDecisionSource.RISK_CLOSED_TAIL),
            risk_open_before=risk_open_before,
            risk_open_after=first is not None,
            tail=tail,
            support_queried=support_queried,
            support_safe=support_safe,
            dependencies=tuple(sorted(
                set(tail.dependencies) | (dependencies - before_support_dependencies))),
        ))

    if first is not None:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.UNRECOVERED_RISK)

    safety_boundaries = {len(inputs)}
    if evaluated_tails[0].status is TailStatus.SAFE_STOP:
        safety_boundaries.add(0)
    for interval in intervals:
        if interval.last_abandon_boundary is not None:
            safety_boundaries.add(interval.last_abandon_boundary)
        safety_boundaries.add(interval.recovered_boundary)
    try:
        safety_tails = tuple(_compact(evaluated_tails[boundary])
                             for boundary in sorted(safety_boundaries))
    except (KeyError, ContractViolation) as error:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED,
                          CandidateRejection.UNRECOVERED_RISK) from error

    return SoundSparseBranch(
        timing_branch=branch,
        states=states,
        boundary_decisions=tuple(decisions),
        safety_tails=safety_tails,
        risk_intervals=tuple(intervals),
        damage_points=normal_damage,
        prelude_states=prelude_states,
        prelude_dependencies=prelude_dependencies,
        effect_tick=request.allowed_effect_ticks[branch_index],
        total_ticks=states[-1].movement_tick_id - request.anchor_state.movement_tick_id,
    )


def scan_sound_sparse_commitment(request, inputs, world, boundary_inputs, *,
                                 options=ScanOptions(), counter=None):
    """Build a D105 proof with one main replay and sequential boundary decisions."""
    if type(request) is not TrajectorySearchRequest or type(options) is not ScanOptions:
        raise ContractViolation("sound sparse scan requires a typed request and options")
    _inputs(inputs)
    if type(world) is not PhysicsWorldView or not world.is_detached:
        raise ContractViolation("sound sparse scanner requires a detached physics world snapshot")
    if world.ruleset != JAVA_1_21_RULESET:
        raise ContractViolation("sound sparse scanner supports only the Java 1.21 ruleset")
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
            _derive_branch(
                request, inputs, rows, index, branch, world, counter,
                dependencies, options,
            )
            for index, (branch, rows) in enumerate(zip(
                request.timing_branches, boundary_inputs))
        )
        facts = tuple((position, world.cell(position))
                      for position in sorted(dependencies))
        proof = SoundSparseProof(
            request=request,
            inputs=inputs,
            boundary_inputs=boundary_inputs,
            branches=branches,
            dependency_facts=facts,
            ruleset=JAVA_1_21_RULESET,
            options=options,
            counts=counter.counts,
            trajectory_hash="",
        )
        proof = replace(proof, trajectory_hash=trajectory_digest(proof))
        _validate_sound_sparse_proof_structure(proof)
        return SoundSparseScanResult(
            ScanStatus.VERIFIED_CANDIDATE, None, counter.counts, proof)
    except CountLimit as limited:
        return SoundSparseScanResult(
            ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason, counter.counts)
    except _Incomplete as incomplete:
        return SoundSparseScanResult(
            incomplete.status, incomplete.reason, counter.counts,
            missing_cells=incomplete.missing)


def validate_sound_sparse_proof(proof: SoundSparseProof) -> None:
    """Validate an externally supplied proof's identity and compact structure."""
    if type(proof) is not SoundSparseProof:
        raise ContractViolation("sound sparse proof requires its exact type")
    if trajectory_digest(replace(proof, trajectory_hash="")) != proof.trajectory_hash:
        raise ContractViolation("sound sparse proof digest has changed")
    _validate_sound_sparse_proof_structure(proof)


def _validate_sound_sparse_proof_structure(proof: SoundSparseProof) -> None:
    """Check the freshly built structure without recomputing its full digest."""
    end = len(proof.inputs)
    for branch in proof.branches:
        decisions = branch.boundary_decisions
        if tuple(decision.boundary for decision in decisions) != tuple(range(end + 1)):
            raise ContractViolation("sound sparse proof requires one decision per boundary")
        expected_intervals = []
        first = None
        for decision in decisions:
            state = branch.states[decision.boundary]
            risk_open_before = first is not None
            if decision.risk_open_before != risk_open_before:
                raise ContractViolation("boundary risk-open input does not follow prior decisions")
            if (risk_open_before and not state.on_ground
                    and (decision.source is not BoundaryDecisionSource.AIRBORNE_TAIL_OMITTED
                         or decision.tail is not None
                         or decision.tail_status is not None)):
                raise ContractViolation(
                    "open-risk airborne boundary must omit its hypothetical tail")
            if decision.source is BoundaryDecisionSource.AIRBORNE_TAIL_OMITTED:
                if (not decision.risk_open_before or not decision.risk_open_after
                        or state.on_ground or decision.tail is not None
                        or decision.tail_status is not None
                        or decision.support_queried or decision.support_safe is not None
                        or decision.dependencies):
                    raise ContractViolation("only an open-risk airborne tail may be omitted")
                continue

            expected_source = (BoundaryDecisionSource.OPEN_RISK_TAIL_EVALUATED
                               if risk_open_before
                               else BoundaryDecisionSource.RISK_CLOSED_TAIL)
            if (decision.source is not expected_source
                    or decision.tail is None
                    or decision.tail.boundary != decision.boundary
                    or decision.tail.states[0] != state):
                raise ContractViolation("evaluated boundary is missing its ordered typed tail")
            should_query_support = (risk_open_before
                                    and decision.tail_status is TailStatus.SAFE_STOP
                                    and state.on_ground)
            if (decision.support_queried != should_query_support
                    or should_query_support != (decision.support_safe is not None)):
                raise ContractViolation("grounded recovery support query order changed")

            if first is None and decision.tail_status is TailStatus.UNSAFE:
                first = decision.boundary
            elif (first is not None
                  and decision.tail_status is TailStatus.SAFE_STOP
                  and decision.support_safe):
                last_abandon = first - 1 if first else None
                opening = decisions[first].tail
                previous = None if last_abandon is None else decisions[last_abandon].tail
                if opening is None or (last_abandon is not None and previous is None):
                    raise ContractViolation("risk transition lacks its evaluated boundary tail")
                expected_intervals.append(RiskInterval(
                    last_abandon,
                    first,
                    decision.boundary,
                    None if previous is None else previous.absolute_tick,
                    opening.absolute_tick,
                    opening.absolute_tick + 1,
                    decision.tail.absolute_tick,
                ))
                first = None
            if decision.risk_open_after != (first is not None):
                raise ContractViolation("boundary risk-open output does not match its decision")

        if first is not None:
            raise ContractViolation("verified proof retains an unrecovered risk")
        if branch.risk_intervals != tuple(expected_intervals):
            raise ContractViolation("risk intervals do not match sequential boundary decisions")

        required = {end}
        if decisions[0].tail_status is TailStatus.SAFE_STOP:
            required.add(0)
        for interval in branch.risk_intervals:
            if not (0 <= interval.first_committed_boundary
                    < interval.recovered_boundary <= end):
                raise ContractViolation("risk interval boundaries are invalid")
            if interval.last_abandon_boundary is not None:
                required.add(interval.last_abandon_boundary)
            required.add(interval.recovered_boundary)
        if tuple(tail.boundary for tail in branch.safety_tails) != tuple(sorted(required)):
            raise ContractViolation("compact proof is missing an executable safety tail")
        for tail in branch.safety_tails:
            if (tail.start_state != branch.states[tail.boundary]
                    or tail.maximum_ticks != len(tail.inputs)
                    or not 1 <= tail.maximum_ticks <= proof.options.max_tail_ticks
                    or tail.end_state.movement_tick_id
                    != tail.start_state.movement_tick_id + len(tail.inputs)):
                raise ContractViolation("compact safety tail contract changed")
            expected = trajectory_digest((
                tail.boundary,
                tail.inputs,
                tail.start_state,
                tail.end_state,
                tail.dependencies,
                tail.damage_points,
                tail.absolute_tick,
            ))
            if expected != tail.tail_digest:
                raise ContractViolation("compact safety tail digest has changed")
