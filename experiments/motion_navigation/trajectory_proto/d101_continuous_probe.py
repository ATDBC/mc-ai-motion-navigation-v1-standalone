"""D101 isolated single-jump receipt loop; no production owners or worker."""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import count
import time

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_types import CalculationStatus, PhysicsState, TickInput

from . import m0_probe
from .commitment import ScanStatus, TailStatus, _damage, scan_commitment
from .contracts import SearchBudget, TimingBranch
from .d101_input_stream import (
    FirstApplicationReceipt, InputStreamTracker, KeyNodeCheck, KeyNodeCheckKind,
    KeyNodeEnvelope, StreamGeneration, StreamStartBranch,
)
from .physics import CountedPhysics, trajectory_digest
from .reference_search import _boundary_evidence, _goal_check
from .scenarios import representative_fixture


FROZEN_INPUT_HASH = "8e1c73bf14b044c746c557b2e569198860ea9b25f00f646a17a370bc55af02ed"
_GENERATION_SEQUENCE = count(1)
_SEQUENCE_STRIDE = 128  # 23 candidate inputs + bounded tails; never reuse a range.
_INJECTIONS = ("none", "first_late_two", "middle_late_one", "duplicate_first", "out_of_order_first")


@dataclass(frozen=True, slots=True)
class SingleJumpProbeSpec:
    start_branch: StreamStartBranch
    rounds: int
    injection: str = "none"

    def __post_init__(self):
        if type(self.start_branch) is not StreamStartBranch:
            raise ContractViolation("probe requires a typed start branch")
        if type(self.rounds) is not int or self.rounds < 1:
            raise ContractViolation("probe rounds must be positive")
        if self.injection not in _INJECTIONS:
            raise ContractViolation("unknown D101 single-jump injection")


@dataclass(frozen=True, slots=True)
class ProbeDecision:
    decision_index: int
    owner_id: str
    generation_id: str
    requested_tick: int
    control_sequence: int | None
    submitted_command: TickInput | None
    submitted_effect_tick: int | None
    applied_command: TickInput | None
    applied_control_sequence: int | None
    actual_effect_tick: int | None
    advance_source: str | None
    evidence_hash: str
    state_before: PhysicsState
    observed_state: PhysicsState
    first_receipt: FirstApplicationReceipt | None
    surviving_branches: tuple[StreamStartBranch, ...]
    key_nodes: tuple[str, ...]
    key_checks: tuple[KeyNodeCheck, ...]
    envelopes: tuple[KeyNodeEnvelope, ...]
    full_proof_calls: int
    physics_steps: int
    terminal_status: str
    reason: str
    duration_ns: int


@dataclass(frozen=True, slots=True)
class SingleJumpProbeResult:
    start_branch: StreamStartBranch
    injection: str
    terminal_status: str
    reason: str
    generation: StreamGeneration
    inputs: tuple[TickInput, ...]
    input_hash: str
    proof_hash: str
    first_receipt: FirstApplicationReceipt | None
    surviving_branches: tuple[StreamStartBranch, ...]
    key_node_comparisons: int
    full_proof_calls: int
    key_node_full_proof_calls: int
    safety_events: tuple[str, ...]
    final_state: PhysicsState
    damage_points: float
    physics_steps: int
    decisions: tuple[ProbeDecision, ...]

    @property
    def decision_durations_ns(self):
        return tuple(row.duration_ns for row in self.decisions)


@lru_cache(maxsize=1)
def frozen_inputs() -> tuple[TickInput, ...]:
    """Only fixture/input preparation is outside decision timing.

    Existing run_case selects the historically frozen input, not a D101 search.
    Reject a changed producer instead of silently changing this experiment.
    """
    candidate = m0_probe.run_case("jump_gap_1", "A3", options=m0_probe.FORMAL_OPTIONS)
    if (candidate.status is not m0_probe.ProbeStatus.FOUND
            or trajectory_digest(candidate.inputs) != FROZEN_INPUT_HASH):
        raise ContractViolation("jump_gap_1:A3 no longer matches the frozen input")
    return candidate.inputs


def _envelope(generation, branch, state, boundary, commitment):
    # Singleton ranges come from the actual full scanner. No residual tolerance.
    return KeyNodeEnvelope(
        generation, branch, boundary - 1, state, state.position, state.position,
        state.velocity_blocks_per_tick, state.velocity_blocks_per_tick,
        (state.yaw_radians, state.yaw_radians), (state.pitch_radians, state.pitch_radians),
        commitment - 1,
    )


def _run_one(spec, fixture, inputs):
    serial = next(_GENERATION_SEQUENCE)
    start_ns = time.perf_counter_ns()
    first_sequence = serial * _SEQUENCE_STRIDE
    # A separate received prelude has a unique sequence too. It belongs to the
    # same owner but does not pretend to be the stream's first application.
    preludes = tuple(tuple(replace(fact, control_sequence=first_sequence - 1,
                                  evidence_id=f"d101-wait-{serial}") for fact in prelude)
                     for prelude in fixture.request.branch_preludes)
    request = replace(fixture.request, request_id=f"d101-request-{serial}",
                      input_ledger_id=f"d101-ledger-{serial}", branch_preludes=preludes,
                      first_candidate_control_sequence=first_sequence,
                      budget=SearchBudget(10000, 100000, 40, 2))
    counter = CountedPhysics(request.budget)
    # This probe sends one tick at a time: there are no already-sampled future
    # commands at a boundary. Empty in-flight declarations are actual protocol
    # premises, allowing the scanner to prove a stop if the next input is late.
    evidence = tuple(tuple(replace(row, irrevocable_inputs=(), applications=()) for row in rows)
                     for rows in _boundary_evidence(request, inputs))
    scanned = scan_commitment(request, inputs, fixture.world, evidence, counter=counter)
    if scanned.status is not ScanStatus.VERIFIED_CANDIDATE:
        raise ContractViolation(f"frozen D101 candidate lacks proof: {scanned.status}:{scanned.reason}")
    proof = scanned.proof
    identity = (("world", request.world_session.value, request.geometry_revision),
                ("request", trajectory_digest(request)), ("proof", proof.trajectory_hash))
    generation = StreamGeneration(
        f"d101-generation-{serial}", f"d101-owner-{serial}", request.request_id, 0,
        request.allowed_effect_ticks[0], inputs, identity, first_sequence,
    )
    tracker = InputStreamTracker.start(generation)
    surviving = tuple(StreamStartBranch)
    actual_index = int(spec.start_branch is StreamStartBranch.LATE_ONE)
    branch = proof.branches[actual_index]
    risk = branch.risk_intervals[0]
    nodes = {risk.first_committed_boundary: "commitment", risk.recovered_boundary: "landing"}
    envelopes = {index: {boundary: _envelope(generation, start_branch, b.states[boundary],
                                             boundary, b.risk_intervals[0].first_committed_boundary)
                         for boundary in nodes}
                 for index, (start_branch, b) in enumerate(zip(StreamStartBranch, proof.branches))}
    state = request.anchor_state
    states = [state]
    rows = []
    safety = []
    first_receipt = None
    status, reason = "ACTIVE", "candidate_running"
    cursor = 0
    tail = None
    tail_cursor = 0
    total_full = 1
    first_iteration = True

    while status == "ACTIVE":
        if not first_iteration:
            start_ns = time.perf_counter_ns()
        before_steps = 0 if first_iteration else counter.physics_steps
        before = state
        receipt = None
        key_names, checks, node_envelopes = (), (), ()
        command = submitted = None
        seq = effect = actual_tick = applied_seq = None
        source = None
        if any(fixture.world.cell(pos) != fact for pos, fact in proof.dependency_facts):
            status, reason = "REJECTED", "world_dependency_changed"
        elif spec.injection in ("first_late_two", "duplicate_first", "out_of_order_first"):
            # Malformed received protocol facts are rejected without authorizing
            # any simulator movement, including any claimed late-two movement.
            submitted = inputs[0]
            seq = first_sequence
            effect = generation.requested_start_tick + (2 if spec.injection == "first_late_two" else 0)
            try:
                if spec.injection == "duplicate_first":
                    receipt = FirstApplicationReceipt(seq, effect, submitted)
                    tracker.record_first_application(receipt)
                    surviving = (StreamStartBranch.ON_TIME,)
                    tracker.record_first_application(receipt)
                elif spec.injection == "out_of_order_first":
                    tracker.record_first_application(FirstApplicationReceipt(seq + 1, effect, submitted))
                else:
                    tracker.record_first_application(FirstApplicationReceipt(seq, effect, submitted))
            except ContractViolation:
                status, reason = "CONTRACT_REJECTED", spec.injection
            else:
                status, reason = "UNEXPECTED_ACCEPTANCE", spec.injection
                safety.append("invalid_receipt_accepted")
        else:
            prelude = actual_index == 1 and first_receipt is None and cursor == 0 and state == request.anchor_state
            if prelude:
                command = request.branch_preludes[1][0].tick_input
                seq = applied_seq = first_sequence - 1
                effect = request.allowed_effect_ticks[0]
                source = "proved_start_prelude"
                expected = branch.prelude_states[1]
            elif tail is not None:
                command = tail.inputs[tail_cursor]
                # Tail sequence occupies the reserved range after all candidate
                # sequence numbers; it cannot be confused with a delayed input.
                seq = applied_seq = first_sequence + len(inputs) + tail_cursor
                effect = state.movement_tick_id + 1
                source = "proved_stop_tail"
                expected = tail.states[tail_cursor + 1]
            else:
                submitted = inputs[cursor]
                seq = first_sequence + cursor
                effect = (generation.requested_start_tick + actual_index if cursor == 0
                          else tracker.expected_effect_tick(cursor))
                expected = branch.states[cursor + 1]
                if state != branch.states[cursor]:
                    status, reason = "REJECTED", "body_outside_proof"
                elif spec.injection == "middle_late_one" and cursor == 1:
                    # Input is offered one tick past its bound deadline. The
                    # backend does not apply it. Its due tick uses the already
                    # proved empty-in-flight stop tail, not an invented neutral.
                    effect += 1
                    tail = branch.tails[cursor]
                    if tail.status is not TailStatus.SAFE_STOP:
                        raise ContractViolation("negative injection lacks a proved bounded exit")
                    command = tail.inputs[0]
                    applied_seq = first_sequence + len(inputs)
                    source = "proved_stop_tail"
                    expected = tail.states[1]
                    reason = "middle_command_late_one"
                else:
                    command = submitted
                    applied_seq = seq
                    source = "candidate"
            if status == "ACTIVE":
                # One gate owns all simulated advancement. No UNKNOWN result or
                # unmatched state is converted into an applied observation.
                calculated = counter.step(state, command, fixture.world)
                if calculated.status is not CalculationStatus.OK:
                    status, reason = "REJECTED", f"physics:{calculated.status.value}"
                    command = None
                else:
                    state = calculated.next_state
                    actual_tick = state.movement_tick_id
                    if state != expected:
                        status, reason = "SAFETY_FAILURE", "physics_outside_proof"
                        safety.append(reason)
                    states.append(state)
                    if _damage(tuple(states)) > request.task_damage_budget.maximum_expected_damage_points:
                        safety.append("damage_allowance_exceeded")
                        status = "SAFETY_FAILURE"
                    if state.position[1] < request.anchor_state.position[1] - 1.e-7:
                        safety.append("fall_below_safe_floor")
                        status = "SAFETY_FAILURE"
                    if source == "candidate":
                        if cursor == 0:
                            receipt = FirstApplicationReceipt(seq, actual_tick, command)
                            surviving = (tracker.record_first_application(receipt),)
                            first_receipt = receipt
                        elif actual_tick != tracker.expected_effect_tick(cursor):
                            status, reason = "SAFETY_FAILURE", "middle_application_wrong_tick"
                            safety.append(reason)
                        cursor += 1
                        if cursor in nodes:
                            key_names = (nodes[cursor],)
                            node_envelopes = (envelopes[actual_index][cursor],)
                            check = tracker.compare_key_node(
                                state, node_envelopes[0], generation_id=generation.generation_id,
                                dependency_identity=identity)
                            checks = (check,)
                            if check.kind is not KeyNodeCheckKind.COMPARE_ONLY:
                                status, reason = "REJECTED", check.reason
                        if cursor == len(inputs) and status == "ACTIVE":
                            goal, missing = _goal_check(request, TimingBranch(request.timing_branches[actual_index]),
                                                        state, fixture.world, counter)
                            status = "COMPLETED" if not missing and goal.accepted else "REJECTED"
                            reason = "safe_landing_and_goal" if status == "COMPLETED" else "terminal_goal_not_met"
                    elif source == "proved_stop_tail":
                        tail_cursor += 1
                        if tail_cursor == len(tail.inputs) and status == "ACTIVE":
                            # The scanner proved support, damage and a settled
                            # stop for this exact state, not only on_ground.
                            status, reason = "BOUNDED_DEVIATION", "middle_command_late_one"
        row = ProbeDecision(
            len(rows), generation.owner_id, generation.generation_id,
            before.movement_tick_id, seq, submitted, effect, command, applied_seq, actual_tick,
            source, proof.trajectory_hash, before, state, receipt, surviving,
            key_names, checks, node_envelopes, int(first_iteration),
            counter.physics_steps - before_steps, status, reason, 0,
        )
        rows.append(row)
        # Construct the terminal result inside the final decision's clock.
        result = None
        if status != "ACTIVE":
            result = SingleJumpProbeResult(
                spec.start_branch, spec.injection, status, reason, generation, inputs,
                FROZEN_INPUT_HASH, proof.trajectory_hash, first_receipt, surviving,
                sum(len(item.key_checks) for item in rows), total_full,
                sum(check.full_proof_calls for item in rows for check in item.key_checks),
                tuple(safety), state, _damage(tuple(states)), counter.physics_steps, tuple(rows),
            )
        elapsed = time.perf_counter_ns() - start_ns
        rows[-1] = replace(row, duration_ns=elapsed)
        if result is not None:
            return replace(result, decisions=tuple(rows))  # Attach the clock measurement only.
        first_iteration = False
        if len(rows) > 80:
            raise ContractViolation("D101 single-jump count bound exhausted")


def run_single_jump_probe(spec: SingleJumpProbeSpec) -> tuple[SingleJumpProbeResult, ...]:
    if type(spec) is not SingleJumpProbeSpec:
        raise ContractViolation("single-jump probe requires a typed spec")
    fixture = representative_fixture("jump_gap_1")
    inputs = frozen_inputs()
    return tuple(_run_one(spec, fixture, inputs) for _ in range(spec.rounds))
