"""Verify one immutable candidate and retain each irreversible risk interval.

This is a safety scanner, not a searcher or a request-goal verifier. Its verified
candidate result cannot by itself issue the search-level FOUND/BLOCKED result.
"""
from dataclasses import dataclass, replace
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.motion_nav.geometry import QueryStatus, query_support, required_cells_for_sweep
from mc2p.motion_nav.motion_risk import conservative_plain_fall_damage_points
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsRuleset, PhysicsState, TickInput,
)
from mc2p.motion_nav.world_model import BlockPos, CellFact, WorldView
from .contracts import KnownInputApplication, SearchReason, TimingBranch, TrajectorySearchRequest
from .physics import CountedPhysics, CountLimit, ScanCounts, trajectory_digest


ZERO_SPEED_EPSILON = 1.e-9
# Source: action_route_executor._safe_ground_state default execution floor.
MINIMUM_SAFE_SUPPORT_FRACTION = .15


class ScanStatus(StrEnum):
    VERIFIED_CANDIDATE = "VERIFIED_CANDIDATE"
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    NO_TRAJECTORY_IN_BUDGET = "NO_TRAJECTORY_IN_BUDGET"
    STALE = "STALE"


class CandidateRejection(StrEnum):
    UNSUPPORTED_PHYSICS = "unsupported_physics"
    INVALID_PHYSICS = "invalid_physics"
    DAMAGE_ALLOWANCE = "damage_allowance"
    FINAL_EXIT_NOT_GROUNDED = "final_exit_not_grounded"
    FINAL_STOP_UNSAFE = "final_stop_unsafe"
    TAIL_NOT_SETTLED = "tail_not_settled"
    UNRECOVERED_RISK = "unrecovered_risk"
    PRELUDE_ENTRY_MISMATCH = "prelude_entry_mismatch"
    PRELUDE_UNSAFE = "prelude_outside_same_safe_support"


class TailStatus(StrEnum):
    SAFE_STOP = "SAFE_STOP"
    UNSAFE = "UNSAFE"


@dataclass(frozen=True, slots=True)
class ScanOptions:
    max_tail_ticks: int = 40

    def __post_init__(self):
        if type(self.max_tail_ticks) is not int or not 1 <= self.max_tail_ticks <= 80:
            raise ContractViolation("tail horizon must be 1..80 ticks")


@dataclass(frozen=True, slots=True)
class BoundaryInputs:
    """Known ordered irrevocable inputs; None explicitly means missing evidence."""
    boundary: int
    irrevocable_inputs: tuple[TickInput, ...] | None
    absolute_tick: int | None = None
    applications: tuple[KnownInputApplication, ...] | None = None

    def __post_init__(self):
        require_nonnegative_int(self.boundary, "boundary")
        if self.irrevocable_inputs is not None:
            _inputs(self.irrevocable_inputs)
        if self.absolute_tick is not None:
            require_nonnegative_int(self.absolute_tick, "absolute boundary tick")
        if (self.applications is not None and (type(self.applications) is not tuple
                or any(type(item) is not KnownInputApplication for item in self.applications))):
            raise ContractViolation("application facts require an immutable typed tuple")


@dataclass(frozen=True, slots=True)
class StopTail:
    boundary: int
    status: TailStatus
    inputs: tuple[TickInput, ...]
    states: tuple[PhysicsState, ...]
    dependencies: tuple[BlockPos, ...]
    damage_points: float
    absolute_tick: int


@dataclass(frozen=True, slots=True)
class RiskInterval:
    last_abandon_boundary: int | None
    first_committed_boundary: int
    recovered_boundary: int
    last_abandon_tick: int | None
    first_committed_tick: int
    commitment_effect_tick: int
    recovered_tick: int


@dataclass(frozen=True, slots=True)
class BranchScan:
    timing_branch: TimingBranch
    states: tuple[PhysicsState, ...]
    tails: tuple[StopTail, ...]
    risk_intervals: tuple[RiskInterval, ...]
    damage_points: float
    prelude_states: tuple[PhysicsState, ...]
    prelude_dependencies: tuple[BlockPos, ...]
    effect_tick: int
    total_ticks: int


@dataclass(frozen=True, slots=True)
class CommitmentProof:
    request: TrajectorySearchRequest
    inputs: tuple[TickInput, ...]
    boundary_inputs: tuple[tuple[BoundaryInputs, ...], ...]
    branches: tuple[BranchScan, ...]
    dependency_facts: tuple[tuple[BlockPos, CellFact], ...]
    ruleset: PhysicsRuleset
    options: ScanOptions
    counts: ScanCounts
    trajectory_hash: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    status: ScanStatus
    reason: SearchReason | CandidateRejection | None
    counts: ScanCounts
    proof: CommitmentProof | None = None
    missing_cells: tuple[BlockPos, ...] = ()


class _Incomplete(Exception):
    def __init__(self, status, reason, missing=()):
        self.status, self.reason, self.missing = status, reason, missing


def _inputs(inputs):
    if type(inputs) is not tuple or any(type(command) is not TickInput for command in inputs):
        raise ContractViolation("candidate and in-flight inputs must be immutable TickInput tuples")


def _check_evidence(request, inputs, boundary_inputs):
    if (type(boundary_inputs) is not tuple or len(boundary_inputs) != len(request.entry_states)
            or any(type(rows) is not tuple for rows in boundary_inputs)):
        raise ContractViolation("each entry branch requires immutable boundary input evidence")
    for branch_index, rows in enumerate(boundary_inputs):
        if (len(rows) != len(inputs) + 1 or any(type(row) is not BoundaryInputs for row in rows)
                or tuple(row.boundary for row in rows) != tuple(range(len(inputs) + 1))):
            raise ContractViolation("declare input application evidence at every candidate boundary")
        for row in rows:
            commands = row.irrevocable_inputs
            if commands is None or row.absolute_tick is None or row.applications is None:
                raise _Incomplete(ScanStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION)
            if commands != inputs[row.boundary:row.boundary + len(commands)]:
                raise ContractViolation("irrevocable inputs must agree with the shared candidate continuation")
            if (row.absolute_tick != request.entry_states[branch_index].movement_tick_id + row.boundary
                    or len(row.applications) != len(commands)
                    or any((fact.session, fact.control_sequence, fact.effect_tick, fact.tick_input)
                           != (request.world_session,
                               request.first_candidate_control_sequence + row.boundary + index,
                               row.absolute_tick + index + 1, command)
                           for index, (fact, command) in enumerate(zip(row.applications, commands)))):
                raise _Incomplete(ScanStatus.STALE, SearchReason.INPUT_LEDGER)


def _calculated(counter, current, command, world, dependencies, *, tail=False):
    calculated = counter.step(current, command, world, tail=tail)
    dependencies.update(calculated.dependencies)
    if calculated.status is CalculationStatus.NEEDS_WORLD:
        raise _Incomplete(ScanStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD,
                          calculated.missing_cells)
    if calculated.status is not CalculationStatus.OK:
        reason = (CandidateRejection.UNSUPPORTED_PHYSICS
                  if calculated.status is CalculationStatus.UNSUPPORTED
                  else CandidateRejection.INVALID_PHYSICS)
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, reason)
    return calculated.next_state


def _damage(states):
    """Count each completed fall, plus any unfinished fall, conservatively.

    The formal solver uses the maximum fall for one air action. An explicit
    multi-risk candidate can contain several landings, so it must sum them.
    The request's task_damage_budget is the remaining task allowance at anchor.
    """
    total = peak = 0.
    for state in states:
        peak = max(peak, state.fall_distance_blocks)
        if state.on_ground:
            total += conservative_plain_fall_damage_points(peak)
            peak = 0.
    return total + conservative_plain_fall_damage_points(peak)


def support_is_safe(state, support):
    return (state.on_ground and support.status is QueryStatus.FEASIBLE
            and support.support_fraction >= MINIMUM_SAFE_SUPPORT_FRACTION - 1.e-9)


def _safe_support(state, world, dependencies):
    """Use the public support query, because prior vertical contact can lag motion."""
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    facts = {position: world.cell(position) for position in cells}
    dependencies.update(cells)
    support_world = WorldView.detached(world.session, world.geometry_revision, 0, facts)
    support = query_support(state.body_box, support_world)
    dependencies.update(support.dependencies)
    if support.missing_cells:
        raise _Incomplete(ScanStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD, support.missing_cells)
    return support_is_safe(state, support)


def _safe_stop(state, world, dependencies, *, minimum_y, damage, maximum_damage):
    speed = math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
    return (speed <= ZERO_SPEED_EPSILON
            and state.position[1] >= minimum_y - 1.e-7
            and damage <= maximum_damage + 1.e-9
            and _safe_support(state, world, dependencies))


def _prove_entry(counter, request, branch_index, world):
    prelude = request.branch_preludes[branch_index]
    if prelude is None:
        raise _Incomplete(ScanStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION)
    anchor = request.anchor_state
    states, dependencies = [anchor], set()
    for fact in prelude:
        current = _calculated(counter, states[-1], fact.tick_input, world, dependencies)
        states.append(current)
        if (not anchor.on_ground or not current.on_ground or current.horizontal_collision
                or not math.isclose(current.position[1], anchor.position[1], abs_tol=1.e-7)
                or not _safe_support(anchor, world, dependencies)
                or not _safe_support(current, world, dependencies)):
            raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.PRELUDE_UNSAFE)
    if (states[-1] != request.entry_states[branch_index]
            or states[-1].movement_tick_id + 1 != request.allowed_effect_ticks[branch_index]):
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.PRELUDE_ENTRY_MISMATCH)
    return tuple(states), tuple(sorted(dependencies))


def _tail(counter, boundary, prefix, commands, stop_input, world, minimum_y,
          maximum_damage, options):
    counter.node()
    current = prefix[-1]
    states, inputs, dependencies = [current], [], set()
    for tick in range(options.max_tail_ticks):
        command = commands[tick] if tick < len(commands) else stop_input
        current = _calculated(counter, current, command, world, dependencies, tail=True)
        states.append(current)
        inputs.append(command)
        damage = _damage(prefix[:-1] + tuple(states))
        if current.position[1] < minimum_y - 1.e-7 or damage > maximum_damage + 1.e-9:
            return StopTail(boundary, TailStatus.UNSAFE, tuple(inputs), tuple(states),
                            tuple(sorted(dependencies)), damage, prefix[-1].movement_tick_id)
        # Even a fabricated grounded entry is stepped; successful downward
        # collision now establishes known support, not the entry flag alone.
        if (tick + 1 >= len(commands)
                and _safe_stop(current, world, dependencies, minimum_y=minimum_y,
                               damage=damage, maximum_damage=maximum_damage)):
            return StopTail(boundary, TailStatus.SAFE_STOP, tuple(inputs), tuple(states),
                            tuple(sorted(dependencies)), damage, prefix[-1].movement_tick_id)
    raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.TAIL_NOT_SETTLED)


def _risks(tails, counter, world, dependencies):
    intervals = []
    first = None
    for tail in tails:
        if tail.status is TailStatus.UNSAFE and first is None:
            first = tail.boundary
        elif (tail.status is TailStatus.SAFE_STOP and first is not None
              and tail.states[0].on_ground):
            # Java's previous vertical contact can leave on_ground true after
            # this tick's horizontal movement has already left every support.
            # An eventual safe tail (including an irrevocable jump) cannot make
            # that unsupported current boundary a recovered landing.
            counter.node()
            if not _safe_support(tail.states[0], world, dependencies):
                continue
            intervals.append(RiskInterval(first - 1 if first else None, first, tail.boundary,
                             tails[first - 1].absolute_tick if first else None,
                             tails[first].absolute_tick, tails[first].absolute_tick + 1,
                             tail.absolute_tick))
            first = None
    if first is not None:
        raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.UNRECOVERED_RISK)
    return tuple(intervals)


def scan_commitment(request, inputs, world, boundary_inputs, *, options=ScanOptions(), counter=None):
    """Both declared entry hypotheses replay this one shared candidate sequence.

    Scanner-owned preludes replay known waiting input from the common anchor.
    Each candidate entry must exactly match that replay and its effect tick.
    max_trajectory_ticks bounds candidate length; total_ticks includes waiting.
    Nodes count branch replays, boundary-tail attempts and recovery support
    queries. A supplied request counter also includes preceding search work.
    Tail ticks and all step attempts share max_physics_steps, with a per-tail cap.
    """
    if type(request) is not TrajectorySearchRequest or type(options) is not ScanOptions:
        raise ContractViolation("scan requires a typed request and options")
    _inputs(inputs)
    if type(world) is not PhysicsWorldView or not world.is_detached:
        raise ContractViolation("scanner requires a detached physics world snapshot")
    if world.ruleset != JAVA_1_21_RULESET:
        raise ContractViolation("scanner supports only the Java 1.21 ruleset")
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
            raise _Incomplete(ScanStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION)
        _check_evidence(request, inputs, boundary_inputs)
        if len(request.entry_states) > request.budget.max_timing_branches:
            raise CountLimit(SearchReason.TIMING_BRANCH_BUDGET)
        if len(inputs) > request.budget.max_trajectory_ticks:
            raise CountLimit(SearchReason.TRAJECTORY_TICK_BUDGET)
        branches, dependencies = [], set()
        for branch_index, (branch, rows) in enumerate(zip(request.timing_branches, boundary_inputs)):
            counter.node()
            prelude_states, prelude_dependencies = _prove_entry(counter, request, branch_index, world)
            dependencies.update(prelude_dependencies)
            entry = prelude_states[-1]
            states = [entry]
            for command in inputs:
                states.append(_calculated(counter, states[-1], command, world, dependencies))
            states = tuple(states)
            normal_damage = _damage(prelude_states[:-1] + states)
            if normal_damage > request.task_damage_budget.maximum_expected_damage_points:
                raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.DAMAGE_ALLOWANCE)
            if not states[-1].on_ground:
                raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.FINAL_EXIT_NOT_GROUNDED)
            minimum_y = min(entry.position[1], states[-1].position[1])
            tails = []
            for row in rows:
                tail = _tail(counter, row.boundary, prelude_states[:-1] + states[:row.boundary + 1],
                             row.irrevocable_inputs, request.stop_input, world, minimum_y,
                             request.task_damage_budget.maximum_expected_damage_points, options)
                dependencies.update(tail.dependencies)
                if (request.branch_preludes[branch_index] and row.boundary == 0
                        and tail.status is not TailStatus.SAFE_STOP):
                    raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.PRELUDE_UNSAFE)
                tails.append(tail)
            if tails[-1].status is not TailStatus.SAFE_STOP:
                raise _Incomplete(ScanStatus.CANDIDATE_REJECTED, CandidateRejection.FINAL_STOP_UNSAFE)
            branches.append(BranchScan(branch, states, tuple(tails),
                            _risks(tails, counter, world, dependencies), normal_damage,
                            prelude_states, prelude_dependencies,
                            request.allowed_effect_ticks[branch_index],
                            states[-1].movement_tick_id - request.anchor_state.movement_tick_id))
        facts = tuple((position, world.cell(position)) for position in sorted(dependencies))
        proof = CommitmentProof(request, inputs, boundary_inputs, tuple(branches), facts,
                                JAVA_1_21_RULESET, options, counter.counts, "")
        proof = replace(proof, trajectory_hash=trajectory_digest(proof))
        return ScanResult(ScanStatus.VERIFIED_CANDIDATE, None, counter.counts, proof)
    except CountLimit as limited:
        return ScanResult(ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason, counter.counts)
    except _Incomplete as incomplete:
        return ScanResult(incomplete.status, incomplete.reason, counter.counts,
                          missing_cells=incomplete.missing)


def validate_commitment(proof, request, world, boundary_inputs, *, boundary):
    """Check identities, dependencies and only this submission's branch facts.

    The scanner retains full boundary evidence in its proof. A caller submitting
    one prefix does not need application observations for past/future prefixes.
    Explicitly stale identity/dependencies take priority over missing receipts.
    """
    if (type(proof) is not CommitmentProof or type(request) is not TrajectorySearchRequest
            or type(world) is not PhysicsWorldView):
        raise ContractViolation("commitment validation requires typed proof, request and world")
    if type(boundary) is not int or not 0 <= boundary < len(proof.inputs):
        raise ContractViolation("submission boundary must identify a candidate command")
    old = proof.request
    reason = None
    if request.request_id != old.request_id:
        reason = SearchReason.REQUEST_IDENTITY
    elif world.session != old.world_session or request.world_session != old.world_session:
        reason = SearchReason.WORLD_DEPENDENCY
    elif request.anchor_id != old.anchor_id or request.entry_states != old.entry_states:
        reason = SearchReason.ANCHOR
    elif request.goal_revision != old.goal_revision or request.goal != old.goal:
        reason = SearchReason.GOAL_REVISION
    elif (request.input_ledger_id != old.input_ledger_id
          or request.first_candidate_control_sequence != old.first_candidate_control_sequence
          or any(new is not None and new != previous
                 for new, previous in zip(request.branch_preludes, old.branch_preludes))):
        reason = SearchReason.INPUT_LEDGER
    elif replace(request, geometry_revision=old.geometry_revision,
                 branch_preludes=old.branch_preludes) != old:
        reason = SearchReason.REQUEST_IDENTITY
    elif (world.ruleset != proof.ruleset or
          trajectory_digest(replace(proof, trajectory_hash="")) != proof.trajectory_hash):
        reason = SearchReason.REQUEST_IDENTITY
    elif any(world.cell(position) != fact for position, fact in proof.dependency_facts):
        reason = SearchReason.WORLD_DEPENDENCY
    if reason:
        return ScanResult(ScanStatus.STALE, reason, ScanCounts())
    if (type(boundary_inputs) is not tuple or len(boundary_inputs) != len(old.entry_states)
            or any(type(rows) is not tuple for rows in boundary_inputs)):
        raise ContractViolation("each entry branch requires immutable boundary input evidence")
    current = tuple(rows[boundary] if len(rows) > boundary else None for rows in boundary_inputs)
    if any(row is not None and type(row) is not BoundaryInputs for row in current):
        raise ContractViolation("current application facts require BoundaryInputs")
    if any(row is not None and any(
           getattr(row, field) is not None
           and getattr(row, field) != getattr(proof.boundary_inputs[index][boundary], field)
           for field in ("boundary", "absolute_tick", "irrevocable_inputs", "applications"))
           for index, row in enumerate(current)):
        return ScanResult(ScanStatus.STALE, SearchReason.INPUT_LEDGER, ScanCounts())
    if (any(prelude is None for prelude in request.branch_preludes)
            or any(row is None or row.irrevocable_inputs is None or row.absolute_tick is None
                   or row.applications is None for row in current)):
        return ScanResult(ScanStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION,
                          ScanCounts())
    return ScanResult(ScanStatus.VERIFIED_CANDIDATE, None, ScanCounts(), proof)
