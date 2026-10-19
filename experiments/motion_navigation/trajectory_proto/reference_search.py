"""Deterministic D096 primitive search; no wall clock or production integration."""
from dataclasses import dataclass, replace
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import query_support, required_cells_for_sweep
from mc2p.motion_nav.movement_transition import GoalSupport, MovementMode, ResourceState
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, PhysicsState, TickInput
from mc2p.motion_nav.world_model import BlockPos, CellFact, WorldView
from .commitment import (
    BoundaryInputs, CommitmentProof, ScanStatus, scan_commitment, support_is_safe,
)
from .contracts import (
    ApplicationEvidence, KnownInputApplication, SearchReason, SearchStatus,
    TimingBranch, TrajectorySearchRequest,
)
from .physics import CountedPhysics, CountLimit, ScanCounts, trajectory_digest
from .primitive_search import primitive_prefixes


COVERAGE = (
    "Deterministic cumulative input tiers and stable G/J/A/B action primitives; "
    "shared input-prefix tree retains every timing branch without cross-history merging; "
    "horizontal collision and unsupported/invalid steps discard only that prefix; "
    "fixed count and candidate horizon caps; no global reachability claim."
)


@dataclass(frozen=True, slots=True)
class BranchGoalCheck:
    timing_branch: TimingBranch
    state: PhysicsState
    accepted: bool
    support_fraction: float
    dependency_facts: tuple[tuple[BlockPos, CellFact], ...]


@dataclass(frozen=True, slots=True)
class ReferenceSearchOutcome:
    request_id: str
    status: SearchStatus
    reason: SearchReason
    inputs: tuple[TickInput, ...]
    proof: CommitmentProof | None
    goal_checks: tuple[BranchGoalCheck, ...]
    counts: ScanCounts
    expanded_nodes: int
    candidate_ticks: int
    branch_total_ticks: tuple[int, ...]
    input_order: tuple[TickInput, ...]
    coverage: str
    result_hash: str
    missing_cells: tuple[BlockPos, ...] = ()
    winning_tier: str | None = None
    completed_candidates: int = 0
    commitment_scans: int = 0

    def __post_init__(self):
        if self.status is not self.reason.status:
            raise ContractViolation("search reason must match its status")
        if (type(self.completed_candidates) is not int or self.completed_candidates < 0
                or type(self.commitment_scans) is not int or self.commitment_scans < 0):
            raise ContractViolation("search attempt and scan counts must be nonnegative integers")
        if self.status is SearchStatus.FOUND:
            if (type(self.proof) is not CommitmentProof
                    or self.inputs != self.proof.inputs
                    or len(self.goal_checks) != len(self.proof.branches)
                    or not all(check.accepted for check in self.goal_checks)
                    or type(self.winning_tier) is not str
                    or not self.winning_tier
                    or self.commitment_scans == 0):
                raise ContractViolation("FOUND requires actual proof and every branch goal check")
        elif self.proof is not None or self.winning_tier is not None:
            raise ContractViolation("only FOUND may retain a proof or winning input tier")


def _distance(state, goal):
    region = goal.region
    return math.sqrt(sum(max(low - value, 0., value - high) ** 2
                         for value, low, high in zip(state.position,
                             (region.min_x, region.min_y, region.min_z),
                             (region.max_x, region.max_y, region.max_z))))


def _potential_goal(request, states):
    minimum = request.minimum_terminal_speed_blocks_per_second
    maximum = request.goal.maximum_terminal_speed_blocks_per_second
    for state in states:
        speed = 20. * math.hypot(
            state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
        if (not state.on_ground or _distance(state, request.goal) != 0.
                or speed + 1.e-12 < minimum or speed > maximum + 1.e-12):
            return False
    return True


def _goal_check(request, branch, state, world, counter):
    # One charged node covers this bounded public geometry read and GoalState call.
    counter.node()
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    facts = {position: world.cell(position) for position in cells}
    detached = WorldView.detached(world.session, world.geometry_revision, 0, facts)
    support = query_support(state.body_box, detached)
    if support.missing_cells:
        return None, support.missing_cells
    mode = (MovementMode.CRAWL if state.pose == "swimming" else
            MovementMode.CROUCH if state.sneaking else
            MovementMode.SPRINT if state.sprinting else MovementMode.WALK)
    solid = support_is_safe(state, support)
    accepted = solid and request.goal.accepts(
        position=state.position, support=GoalSupport.SOLID, mode=mode, pose=state.pose,
        speed_blocks_per_second=20. * math.hypot(
            state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]),
        resources=ResourceState((("food_points", float(state.food_points)),)),
        yaw_radians=state.yaw_radians)
    speed = 20. * math.hypot(
        state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
    accepted = accepted and speed + 1.e-12 >= request.minimum_terminal_speed_blocks_per_second
    dependencies = tuple((position, world.cell(position))
                         for position in sorted(set(cells) | set(support.dependencies)))
    return BranchGoalCheck(branch, state, bool(accepted), support.support_fraction, dependencies), ()


def _boundary_evidence(request, inputs):
    # The reference experiment assumes the next candidate tick is irrevocable.
    # These are explicit conditional applications, never fabricated actual receipts.
    rows = []
    for entry in request.entry_states:
        branch_rows = []
        for boundary in range(len(inputs) + 1):
            commands = inputs[boundary:boundary + 1]
            absolute = entry.movement_tick_id + boundary
            applications = tuple(KnownInputApplication(
                request.world_session, request.first_candidate_control_sequence + boundary,
                absolute + 1, command, f"candidate-{boundary}", ApplicationEvidence.PREDICTED)
                for command in commands)
            branch_rows.append(BoundaryInputs(boundary, commands, absolute, applications))
        rows.append(tuple(branch_rows))
    return tuple(rows)


def reference_search(request, world):
    """Enumerate cumulative action primitives and verify all timing branches.

    Prefix simulation, every layer and every scan share one request counter.
    Earlier layers finish first and a FOUND result returns before later layers.
    """
    if (type(request) is not TrajectorySearchRequest or type(world) is not PhysicsWorldView
            or not world.is_detached or world.ruleset != JAVA_1_21_RULESET):
        raise ContractViolation("reference search requires a typed request and detached Java 1.21 world")
    counter = CountedPhysics(request.budget)
    expanded = 0
    completed_candidates = 0
    commitment_scans = 0

    def outcome(reason, inputs=(), proof=None, checks=(), missing=(), winning_tier=None,
                input_order=None):
        result = ReferenceSearchOutcome(request.request_id, reason.status, reason, inputs,
            proof, checks, counter.counts, expanded, len(inputs),
            tuple(branch.total_ticks for branch in proof.branches) if proof else (),
            input_order or request.supported_inputs, COVERAGE, "", tuple(sorted(missing)),
            winning_tier, completed_candidates, commitment_scans)
        return replace(result, result_hash=trajectory_digest(result))

    if world.session != request.world_session or world.geometry_revision != request.geometry_revision:
        return outcome(SearchReason.WORLD_DEPENDENCY)
    if any(prelude is None for prelude in request.branch_preludes):
        return outcome(SearchReason.MISSING_INPUT_APPLICATION)
    if len(request.entry_states) > request.budget.max_timing_branches:
        return outcome(SearchReason.TIMING_BRANCH_BUDGET)
    if request.goal.minimum_resources.values:
        return outcome(SearchReason.UNPROVEN_RESOURCES)
    try:
        state_cache = {(): request.entry_states}

        def execute(inputs):
            if inputs in state_cache:
                return state_cache[inputs], ()
            parent = inputs[:-1]
            states, missing = execute(parent)
            if states is None:
                return None, missing
            next_states = []
            for state in states:
                result = counter.step(state, inputs[-1], world)
                if result.status is CalculationStatus.NEEDS_WORLD:
                    return None, result.missing_cells
                if (result.status is not CalculationStatus.OK
                        or result.next_state.horizontal_collision):
                    return None, ()
                next_states.append(result.next_state)
            state_cache[inputs] = tuple(next_states)
            return state_cache[inputs], ()

        prefix_states, missing = execute(request.input_prefix)
        if missing:
            return outcome(SearchReason.UNKNOWN_WORLD, missing=missing)
        if prefix_states is None:
            return outcome(SearchReason.SEARCH_EXHAUSTED)

        seen_sequences = set()

        def consider(inputs, states, tier):
            nonlocal completed_candidates, commitment_scans
            completed_candidates += 1
            if not _potential_goal(request, states):
                return None
            checks = []
            for branch, state in zip(request.timing_branches, states):
                check, missing_cells = _goal_check(request, branch, state, world, counter)
                if missing_cells:
                    return outcome(SearchReason.UNKNOWN_WORLD, missing=missing_cells)
                checks.append(check)
            if not all(check.accepted for check in checks):
                return None
            scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs),
                                   counter=counter)
            if scan.status is not ScanStatus.NO_TRAJECTORY_IN_BUDGET:
                commitment_scans += 1
            if scan.status in (ScanStatus.NEEDS_INFORMATION, ScanStatus.STALE,
                               ScanStatus.NO_TRAJECTORY_IN_BUDGET):
                return outcome(scan.reason, missing=scan.missing_cells)
            if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
                return None
            facts = dict(scan.proof.dependency_facts)
            for check in checks:
                facts.update(check.dependency_facts)
            proof = replace(scan.proof, counts=counter.counts,
                            dependency_facts=tuple(sorted(facts.items())), trajectory_hash="")
            proof = replace(proof, trajectory_hash=trajectory_digest(proof))
            return outcome(SearchReason.VERIFIED_TRAJECTORY, inputs, proof, tuple(checks),
                           winning_tier=tier.tier_id, input_order=tier.inputs)

        for tier in request.input_tiers:
            for primitive in primitive_prefixes(tier):
                inputs = request.input_prefix + primitive.inputs
                if inputs in seen_sequences or len(inputs) > request.budget.max_trajectory_ticks:
                    continue
                seen_sequences.add(inputs)
                counter.node()
                expanded += 1
                states, missing = execute(inputs)
                if missing:
                    return outcome(SearchReason.UNKNOWN_WORLD, missing=missing)
                if states is None:
                    continue
                result = consider(inputs, states, tier)
                if result is not None:
                    return result
                for brake_ticks in range(1, request.budget.max_trajectory_ticks - len(inputs) + 1):
                    braked = primitive.with_brake(request.stop_input, brake_ticks)
                    brake_inputs = request.input_prefix + braked.inputs
                    brake_states, missing = execute(brake_inputs)
                    if missing:
                        return outcome(SearchReason.UNKNOWN_WORLD, missing=missing)
                    if brake_states is None:
                        break
                    result = consider(brake_inputs, brake_states, tier)
                    if result is not None:
                        return result
        reason = (SearchReason.TRAJECTORY_TICK_BUDGET
                  if request.budget.max_trajectory_ticks < 40
                  else SearchReason.SEARCH_EXHAUSTED)
        return outcome(reason)
    except CountLimit as limited:
        return outcome(limited.reason)
