"""Shared rollout and safety primitives for the isolated D098 experiment.

This module deliberately does not import private functions from the batch
commitment scanner.  The scanner remains an independent reference oracle.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import hmac
import math
import secrets

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import QueryStatus, query_support, required_cells_for_sweep
from mc2p.motion_nav.motion_risk import conservative_plain_fall_damage_points
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import CellKnowledge, WorldView

from .commitment import (
    BoundaryInputs,
    CandidateRejection,
    RiskInterval,
    ScanOptions,
    ScanStatus,
    StopTail,
    TailStatus,
)
from .contracts import SearchReason, TrajectorySearchRequest
from .physics import CountedPhysics, trajectory_digest


ZERO_SPEED_EPSILON = 1.e-9
MINIMUM_SAFE_SUPPORT_FRACTION = .15
_ISSUER_TOKEN = object()
_TRACE_KEY = secrets.token_bytes(32)


class RolloutFailure(Exception):
    def __init__(self, status, reason, missing=()):
        self.status, self.reason, self.missing = status, reason, tuple(missing)


def validate_inputs(inputs: tuple[TickInput, ...]) -> None:
    if type(inputs) is not tuple or any(type(command) is not TickInput for command in inputs):
        raise ContractViolation("candidate and in-flight inputs must be immutable TickInput tuples")


def validate_boundary_evidence(request, inputs, boundary_inputs) -> None:
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
                raise RolloutFailure(ScanStatus.NEEDS_INFORMATION,
                                     SearchReason.MISSING_INPUT_APPLICATION)
            if commands != inputs[row.boundary:row.boundary + len(commands)]:
                raise ContractViolation("irrevocable inputs disagree with candidate continuation")
            if (row.absolute_tick != request.entry_states[branch_index].movement_tick_id + row.boundary
                    or len(row.applications) != len(commands)
                    or any((fact.session, fact.control_sequence, fact.effect_tick, fact.tick_input)
                           != (request.world_session,
                               request.first_candidate_control_sequence + row.boundary + index,
                               row.absolute_tick + index + 1, command)
                           for index, (fact, command) in enumerate(zip(row.applications, commands)))):
                raise RolloutFailure(ScanStatus.STALE, SearchReason.INPUT_LEDGER)


def calculated_step(counter: CountedPhysics, current, command, world, dependencies,
                    *, tail: bool = False):
    calculated = counter.step(current, command, world, tail=tail)
    dependencies.update(calculated.dependencies)
    if calculated.status is CalculationStatus.NEEDS_WORLD:
        raise RolloutFailure(ScanStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD,
                             calculated.missing_cells)
    if calculated.status is not CalculationStatus.OK:
        reason = (CandidateRejection.UNSUPPORTED_PHYSICS
                  if calculated.status is CalculationStatus.UNSUPPORTED
                  else CandidateRejection.INVALID_PHYSICS)
        raise RolloutFailure(ScanStatus.CANDIDATE_REJECTED, reason)
    return calculated.next_state


def damage_points(states) -> float:
    """Sum completed falls and conservatively include an unfinished fall."""
    total = peak = 0.
    for state in states:
        peak = max(peak, state.fall_distance_blocks)
        if state.on_ground:
            total += conservative_plain_fall_damage_points(peak)
            peak = 0.
    return total + conservative_plain_fall_damage_points(peak)


def support_is_safe(state, support) -> bool:
    return (state.on_ground and support.status is QueryStatus.FEASIBLE
            and support.support_fraction >= MINIMUM_SAFE_SUPPORT_FRACTION - 1.e-9)


def safe_support(state, world, dependencies) -> bool:
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    facts = {position: world.cell(position) for position in cells}
    dependencies.update(cells)
    support_world = WorldView.detached(world.session, world.geometry_revision, 0, facts)
    support = query_support(state.body_box, support_world)
    dependencies.update(support.dependencies)
    if support.missing_cells:
        raise RolloutFailure(ScanStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD,
                             support.missing_cells)
    return support_is_safe(state, support)


def safe_stop(state, world, dependencies, *, minimum_y, damage, maximum_damage) -> bool:
    speed = math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
    return (speed <= ZERO_SPEED_EPSILON
            and state.position[1] >= minimum_y - 1.e-7
            and damage <= maximum_damage + 1.e-9
            and safe_support(state, world, dependencies))


def prove_entry(counter, request, branch_index, world):
    prelude = request.branch_preludes[branch_index]
    if prelude is None:
        raise RolloutFailure(ScanStatus.NEEDS_INFORMATION,
                             SearchReason.MISSING_INPUT_APPLICATION)
    anchor = request.anchor_state
    states, dependencies = [anchor], set()
    for fact in prelude:
        current = calculated_step(counter, states[-1], fact.tick_input, world, dependencies)
        states.append(current)
        if (not anchor.on_ground or not current.on_ground or current.horizontal_collision
                or not math.isclose(current.position[1], anchor.position[1], abs_tol=1.e-7)
                or not safe_support(anchor, world, dependencies)
                or not safe_support(current, world, dependencies)):
            raise RolloutFailure(ScanStatus.CANDIDATE_REJECTED,
                                 CandidateRejection.PRELUDE_UNSAFE)
    if (states[-1] != request.entry_states[branch_index]
            or states[-1].movement_tick_id + 1 != request.allowed_effect_ticks[branch_index]):
        raise RolloutFailure(ScanStatus.CANDIDATE_REJECTED,
                             CandidateRejection.PRELUDE_ENTRY_MISMATCH)
    return tuple(states), tuple(sorted(dependencies))


def stopping_tail(counter, boundary, boundary_state, damage_prefix, commands, stop_input,
                  world, minimum_y, maximum_damage, options: ScanOptions, *,
                  damage_work=None, legacy_prefix_state_count=1):
    counter.node()
    current = boundary_state
    states, inputs, dependencies = [current], [], set()
    for tick in range(options.max_tail_ticks):
        command = commands[tick] if tick < len(commands) else stop_input
        current = calculated_step(counter, current, command, world, dependencies, tail=True)
        states.append(current)
        inputs.append(command)
        damage_prefix = advance_damage(damage_prefix, current)
        if damage_work is not None:
            damage_work.state_updates += 1
            damage_work.legacy_state_visits += legacy_prefix_state_count + tick + 1
        damage = damage_prefix.points
        if current.position[1] < minimum_y - 1.e-7 or damage > maximum_damage + 1.e-9:
            return StopTail(boundary, TailStatus.UNSAFE, tuple(inputs), tuple(states),
                            tuple(sorted(dependencies)), damage, boundary_state.movement_tick_id)
        if (tick + 1 >= len(commands)
                and safe_stop(current, world, dependencies, minimum_y=minimum_y,
                              damage=damage, maximum_damage=maximum_damage)):
            return StopTail(boundary, TailStatus.SAFE_STOP, tuple(inputs), tuple(states),
                            tuple(sorted(dependencies)), damage, boundary_state.movement_tick_id)
    raise RolloutFailure(ScanStatus.CANDIDATE_REJECTED,
                         CandidateRejection.TAIL_NOT_SETTLED)


def advance_risk(open_first, tail, tails, counter, support_check, *,
                 support_known: bool = False, require_safe_tail: bool = True):
    """Advance one risk interval.  Batch and rolling callers share this rule."""
    if tail.status is TailStatus.UNSAFE and open_first is None:
        return tail.boundary, None
    if open_first is None:
        return None, None
    if require_safe_tail:
        if not (tail.status is TailStatus.SAFE_STOP and tail.states[0].on_ground):
            return open_first, None
    elif not (tail.boundary > open_first and tail.states[0].on_ground):
        return open_first, None
    if require_safe_tail and not support_known:
        counter.node()
        if not support_check(tail.states[0]):
            return open_first, None
    first = open_first
    interval = RiskInterval(
        first - 1 if first else None, first, tail.boundary,
        tails[first - 1].absolute_tick if first else None,
        tails[first].absolute_tick, tails[first].absolute_tick + 1,
        tail.absolute_tick,
    )
    return None, interval


def risk_intervals(tails, counter, world, dependencies):
    intervals, first = [], None
    for tail in tails:
        first, interval = advance_risk(
            first, tail, tails, counter,
            lambda state: safe_support(state, world, dependencies),
        )
        if interval is not None:
            intervals.append(interval)
    if first is not None:
        raise RolloutFailure(ScanStatus.CANDIDATE_REJECTED,
                             CandidateRejection.UNRECOVERED_RISK)
    return tuple(intervals)


@dataclass(frozen=True, slots=True)
class DamagePrefix:
    settled_points: float
    open_fall_distance: float

    @property
    def points(self) -> float:
        return (self.settled_points
                + conservative_plain_fall_damage_points(self.open_fall_distance))


@dataclass(slots=True)
class DamageWork:
    state_updates: int = 0
    legacy_state_visits: int = 0


def advance_damage(prefix: DamagePrefix, state) -> DamagePrefix:
    peak = max(prefix.open_fall_distance, state.fall_distance_blocks)
    if state.on_ground:
        return DamagePrefix(
            prefix.settled_points + conservative_plain_fall_damage_points(peak),
            0.,
        )
    return DamagePrefix(prefix.settled_points, peak)


def damage_prefixes(states) -> tuple[DamagePrefix, ...]:
    prefix = DamagePrefix(0., 0.)
    rows = []
    for state in states:
        prefix = advance_damage(prefix, state)
        rows.append(prefix)
    return tuple(rows)


class TraceReuseStatus(StrEnum):
    VALID = "VALID"
    REJECTED = "REJECTED"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"


@dataclass(frozen=True, slots=True)
class TraceReuseCheck:
    status: TraceReuseStatus
    reason: SearchReason | None = None
    missing_cells: tuple = ()


@dataclass(frozen=True, slots=True)
class ControllerRolloutTrace:
    request: TrajectorySearchRequest
    request_id: str
    goal: object
    goal_revision: int
    anchor_id: str
    entry_states: tuple
    input_ledger_id: str
    first_candidate_control_sequence: int
    branch_preludes: tuple
    world_session: object
    geometry_revision: int
    ruleset: object
    ruleset_id: str
    state_schema: str
    inputs: tuple
    prelude_states_by_branch: tuple
    branch_states: tuple
    timing_branches: tuple
    dependencies: tuple
    task_damage_budget: object
    allowed_effect_ticks: tuple
    stop_input: object
    budget: object
    input_prefix: tuple
    supported_inputs: tuple
    input_tiers: tuple
    route_guidance: tuple
    minimum_terminal_speed_blocks_per_second: float
    damage_prefixes_by_branch: tuple
    damage_state_updates: int
    payload_hash: str
    issuer_seal: bytes


def _trace_payload(trace: ControllerRolloutTrace | dict) -> dict:
    if isinstance(trace, dict):
        return trace
    return {name: getattr(trace, name) for name in (
        "request", "request_id", "goal", "goal_revision", "anchor_id", "entry_states",
        "input_ledger_id", "first_candidate_control_sequence", "branch_preludes",
        "world_session", "geometry_revision", "ruleset",
        "ruleset_id", "state_schema", "inputs", "prelude_states_by_branch",
        "branch_states", "timing_branches",
        "dependencies", "task_damage_budget", "allowed_effect_ticks", "stop_input",
        "budget", "input_prefix", "supported_inputs", "input_tiers", "route_guidance",
        "minimum_terminal_speed_blocks_per_second",
        "damage_prefixes_by_branch", "damage_state_updates",
    )}


def _seal(payload_hash: str) -> bytes:
    return hmac.digest(_TRACE_KEY, payload_hash.encode("ascii"), "sha256")


def _payload_digest(payload: dict) -> str:
    # trajectory_digest canonicalises dataclasses and tuples; keeping the
    # sorted mapping as an immutable tuple also makes added/removed fields part
    # of the authenticated value.
    return trajectory_digest(tuple(sorted(payload.items())))


def trace_payload_digest_for_test(trace: ControllerRolloutTrace) -> str:
    """Return the public payload digest without exposing the issuer secret.

    Tests use this to model an attacker who can recompute an ordinary hash but
    cannot mint the HMAC issuer seal.
    """
    if type(trace) is not ControllerRolloutTrace:
        raise ContractViolation("trace digest helper requires a typed trace")
    return _payload_digest(_trace_payload(trace))


def validate_trace_candidate(request: TrajectorySearchRequest,
                             inputs: tuple[TickInput, ...],
                             world: PhysicsWorldView) -> None:
    """Validate every request/candidate premise before a trace is issued."""
    if type(request) is not TrajectorySearchRequest:
        raise ContractViolation("trace requires a typed request")
    validate_inputs(inputs)
    if type(world) is not PhysicsWorldView or not world.is_detached:
        raise ContractViolation("trace requires a detached physics world snapshot")
    if world.ruleset != JAVA_1_21_RULESET:
        raise ContractViolation("trace supports only the Java 1.21 ruleset")
    if (world.session != request.world_session
            or world.geometry_revision != request.geometry_revision):
        raise ContractViolation("trace world identity must match the request")
    if (inputs[:len(request.input_prefix)] != request.input_prefix
            or any(command not in request.supported_inputs for command in inputs)):
        raise ContractViolation("candidate must extend the shared prefix with supported inputs")
    if len(request.entry_states) > request.budget.max_timing_branches:
        raise ContractViolation("candidate exceeds timing branch budget")
    if len(inputs) > request.budget.max_trajectory_ticks:
        raise ContractViolation("candidate exceeds trajectory tick budget")
    if any(prelude is None for prelude in request.branch_preludes):
        raise ContractViolation("trace cannot issue without branch prelude evidence")
    if any((state.ruleset_id, state.state_schema)
           != (request.entry_states[0].ruleset_id, request.entry_states[0].state_schema)
           for state in request.entry_states):
        raise ContractViolation("entry hypotheses must share physics rules")
    first = request.entry_states[0]
    if ((first.ruleset_id, first.state_schema)
            != (world.ruleset.ruleset_id, world.ruleset.state_schema)):
        raise ContractViolation("request entry state rules must match the world ruleset")


def _issue_trace(payload: dict, token) -> ControllerRolloutTrace:
    if token is not _ISSUER_TOKEN:
        raise ContractViolation("controller traces are issued only by the shared rollout kernel")
    payload_hash = _payload_digest(payload)
    return ControllerRolloutTrace(**payload, payload_hash=payload_hash,
                                  issuer_seal=_seal(payload_hash))


def build_controller_trace(request: TrajectorySearchRequest, inputs: tuple[TickInput, ...],
                           world: PhysicsWorldView, *, counter: CountedPhysics | None = None
                           ) -> ControllerRolloutTrace:
    """Replay the two controller branches and issue an authenticated immutable trace."""
    validate_trace_candidate(request, inputs, world)
    if counter is not None and counter.budget != request.budget:
        raise ContractViolation("trace counter must own the unchanged request budget")
    counter = counter or CountedPhysics(request.budget)
    branches, preludes, damage_rows, dependencies = [], [], [], set()
    for branch_index in range(len(request.entry_states)):
        prelude, deps = prove_entry(counter, request, branch_index, world)
        preludes.append(prelude)
        dependencies.update(deps)
        states = [prelude[-1]]
        for command in inputs:
            states.append(calculated_step(counter, states[-1], command, world, dependencies))
        branches.append(tuple(states))
        combined = prelude[:-1] + tuple(states)
        damage_rows.append(damage_prefixes(combined)[len(prelude) - 1:])
    facts = tuple((position, world.cell(position)) for position in sorted(dependencies))
    first = request.entry_states[0]
    payload = {
        "request": request,
        "request_id": request.request_id,
        "goal": request.goal,
        "goal_revision": request.goal_revision,
        "anchor_id": request.anchor_id,
        "entry_states": request.entry_states,
        "input_ledger_id": request.input_ledger_id,
        "first_candidate_control_sequence": request.first_candidate_control_sequence,
        "branch_preludes": request.branch_preludes,
        "world_session": request.world_session,
        "geometry_revision": request.geometry_revision,
        "ruleset": world.ruleset,
        "ruleset_id": first.ruleset_id,
        "state_schema": first.state_schema,
        "inputs": inputs,
        "prelude_states_by_branch": tuple(preludes),
        "branch_states": tuple(branches),
        "timing_branches": request.timing_branches,
        "dependencies": facts,
        "task_damage_budget": request.task_damage_budget,
        "allowed_effect_ticks": request.allowed_effect_ticks,
        "stop_input": request.stop_input,
        "budget": request.budget,
        "input_prefix": request.input_prefix,
        "supported_inputs": request.supported_inputs,
        "input_tiers": request.input_tiers,
        "route_guidance": request.route_guidance,
        "minimum_terminal_speed_blocks_per_second":
            request.minimum_terminal_speed_blocks_per_second,
        "damage_prefixes_by_branch": tuple(damage_rows),
        "damage_state_updates": sum(len(prelude) - 1 + len(states)
                                    for prelude, states in zip(preludes, branches)),
    }
    return _issue_trace(payload, _ISSUER_TOKEN)


def validate_trace_for_reuse(trace, request, inputs, world) -> TraceReuseCheck:
    if type(trace) is not ControllerRolloutTrace:
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.REQUEST_IDENTITY)
    if (type(request) is not TrajectorySearchRequest or type(world) is not PhysicsWorldView
            or not world.is_detached):
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.REQUEST_IDENTITY)
    payload = _trace_payload(trace)
    payload_hash = _payload_digest(payload)
    if (payload_hash != trace.payload_hash
            or not hmac.compare_digest(trace.issuer_seal, _seal(payload_hash))):
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.REQUEST_IDENTITY)
    if (trace.request != request
            or trace.request_id != request.request_id
            or trace.world_session != request.world_session
            or trace.ruleset_id != request.entry_states[0].ruleset_id
            or trace.state_schema != request.entry_states[0].state_schema):
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.REQUEST_IDENTITY)
    if (world.session != request.world_session or world.session != trace.world_session
            or world.geometry_revision != request.geometry_revision
            or world.geometry_revision != trace.geometry_revision
            or world.ruleset != trace.ruleset):
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.WORLD_DEPENDENCY)
    if (trace.anchor_id != request.anchor_id or trace.entry_states != request.entry_states
            or tuple(states[0] for states in trace.branch_states) != request.entry_states):
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.ANCHOR)
    if trace.goal_revision != request.goal_revision or trace.goal != request.goal:
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.GOAL_REVISION)
    if (trace.input_ledger_id != request.input_ledger_id
            or trace.first_candidate_control_sequence != request.first_candidate_control_sequence
            or trace.allowed_effect_ticks != request.allowed_effect_ticks
            or trace.branch_preludes != request.branch_preludes):
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.INPUT_LEDGER)
    if (trace.inputs != inputs or trace.timing_branches != request.timing_branches
            or trace.task_damage_budget != request.task_damage_budget
            or trace.stop_input != request.stop_input
            or trace.budget != request.budget
            or trace.input_prefix != request.input_prefix
            or trace.supported_inputs != request.supported_inputs
            or trace.input_tiers != request.input_tiers
            or trace.route_guidance != request.route_guidance
            or trace.minimum_terminal_speed_blocks_per_second
               != request.minimum_terminal_speed_blocks_per_second):
        return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.REQUEST_IDENTITY)
    unknown = []
    for position, fact in trace.dependencies:
        now = world.cell(position)
        if now.knowledge is CellKnowledge.UNKNOWN and fact.knowledge is not CellKnowledge.UNKNOWN:
            unknown.append(position)
        elif now != fact:
            return TraceReuseCheck(TraceReuseStatus.REJECTED, SearchReason.WORLD_DEPENDENCY)
    if unknown:
        return TraceReuseCheck(TraceReuseStatus.NEEDS_INFORMATION,
                               SearchReason.UNKNOWN_WORLD, tuple(sorted(unknown)))
    return TraceReuseCheck(TraceReuseStatus.VALID)
