"""Deterministic bounded tick-input search; no action types or wall clock.

Best-first ordering is a heuristic, not an optimality or completeness proof.
Only exact complete PhysicsState equality merges nodes. Collision and unsupported
step attempts discard a candidate, never prove request-level BLOCKED.
"""
from dataclasses import dataclass, replace
import heapq
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import QueryStatus, query_support, required_cells_for_sweep
from mc2p.motion_nav.movement_transition import GoalSupport, MovementMode, ResourceState
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, PhysicsState, TickInput
from mc2p.motion_nav.world_model import BlockPos, CellFact, WorldView
from .commitment import BoundaryInputs, CommitmentProof, ScanStatus, ZERO_SPEED_EPSILON, scan_commitment
from .contracts import (
    ApplicationEvidence, KnownInputApplication, SearchReason, SearchStatus,
    TimingBranch, TrajectorySearchRequest,
)
from .physics import CountedPhysics, CountLimit, ScanCounts, trajectory_digest


COVERAGE = (
    "Deterministic greedy best-first over the declared ordered TickInput alphabet; "
    "one tick per expansion; exact complete PhysicsState equality only; "
    "horizontal collision and unsupported/invalid steps discarded; "
    "fixed count and candidate horizon caps; no global completeness or optimality claim."
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

    def __post_init__(self):
        if self.status is not self.reason.status:
            raise ContractViolation("search reason must match its status")
        if self.status is SearchStatus.FOUND:
            if (type(self.proof) is not CommitmentProof
                    or self.inputs != self.proof.inputs
                    or len(self.goal_checks) != len(self.proof.branches)
                    or not all(check.accepted for check in self.goal_checks)):
                raise ContractViolation("FOUND requires actual proof and every branch goal check")
        elif self.proof is not None:
            raise ContractViolation("only FOUND may retain a proof")


def _distance(state, goal):
    region = goal.region
    return math.sqrt(sum(max(low - value, 0., value - high) ** 2
                         for value, low, high in zip(state.position,
                             (region.min_x, region.min_y, region.min_z),
                             (region.max_x, region.max_y, region.max_z))))


def _priority(state, goal):
    # This estimates ranking only; it neither predicts physics nor prunes nodes.
    return _distance(state, goal) + .25 * math.hypot(
        state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])


def _potential_goal(state, goal):
    return (state.on_ground and _distance(state, goal) == 0.
            and math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
                <= min(ZERO_SPEED_EPSILON, goal.maximum_terminal_speed_blocks_per_second / 20. + 1.e-12))


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
    solid = state.on_ground and support.status is QueryStatus.FEASIBLE
    stopped = math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) <= ZERO_SPEED_EPSILON
    accepted = solid and stopped and request.goal.accepts(
        position=state.position, support=GoalSupport.SOLID, mode=mode, pose=state.pose,
        speed_blocks_per_second=20. * math.hypot(
            state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]),
        resources=ResourceState((("food_points", float(state.food_points)),)),
        yaw_radians=state.yaw_radians)
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
    """Expand nominal states, then verify all real timing branches together.

    All frontier work, scanner replays/tails and goal geometry checks use this
    single request counter. No candidate can acquire a fresh scan allowance.
    """
    if (type(request) is not TrajectorySearchRequest or type(world) is not PhysicsWorldView
            or not world.is_detached or world.ruleset != JAVA_1_21_RULESET):
        raise ContractViolation("reference search requires a typed request and detached Java 1.21 world")
    counter = CountedPhysics(request.budget)
    expanded = 0

    def outcome(reason, inputs=(), proof=None, checks=(), missing=()):
        result = ReferenceSearchOutcome(request.request_id, reason.status, reason, inputs,
            proof, checks, counter.counts, expanded, len(inputs),
            tuple(branch.total_ticks for branch in proof.branches) if proof else (),
            request.supported_inputs, COVERAGE, "", tuple(sorted(missing)))
        return replace(result, result_hash=trajectory_digest(result))

    if world.session != request.world_session or world.geometry_revision != request.geometry_revision:
        return outcome(SearchReason.WORLD_DEPENDENCY)
    if any(prelude is None for prelude in request.branch_preludes):
        return outcome(SearchReason.MISSING_INPUT_APPLICATION)
    if len(request.entry_states) > request.budget.max_timing_branches:
        return outcome(SearchReason.TIMING_BRANCH_BUDGET)
    try:
        current = request.anchor_state
        for command in request.input_prefix:
            result = counter.step(current, command, world)
            if result.status is CalculationStatus.NEEDS_WORLD:
                return outcome(SearchReason.UNKNOWN_WORLD, missing=result.missing_cells)
            if result.status is not CalculationStatus.OK or result.next_state.horizontal_collision:
                return outcome(SearchReason.SEARCH_EXHAUSTED)
            current = result.next_state
        serial = 0
        frontier = [(_priority(current, request.goal), serial, current, request.input_prefix)]
        seen = {current}
        horizon_cut = False
        while frontier:
            _, _, current, inputs = heapq.heappop(frontier)
            counter.node()
            expanded += 1
            if _potential_goal(current, request.goal):
                scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs),
                                       counter=counter)
                if scan.status in (ScanStatus.NEEDS_INFORMATION, ScanStatus.STALE,
                                   ScanStatus.NO_TRAJECTORY_IN_BUDGET):
                    return outcome(scan.reason, missing=scan.missing_cells)
                if scan.status is ScanStatus.VERIFIED_CANDIDATE:
                    checks = []
                    for branch in scan.proof.branches:
                        check, missing = _goal_check(request, branch.timing_branch,
                                                    branch.states[-1], world, counter)
                        if missing:
                            return outcome(SearchReason.UNKNOWN_WORLD, missing=missing)
                        checks.append(check)
                    if all(check.accepted for check in checks):
                        facts = dict(scan.proof.dependency_facts)
                        for check in checks:
                            facts.update(check.dependency_facts)
                        proof = replace(scan.proof, counts=counter.counts,
                                        dependency_facts=tuple(sorted(facts.items())), trajectory_hash="")
                        proof = replace(proof, trajectory_hash=trajectory_digest(proof))
                        return outcome(SearchReason.VERIFIED_TRAJECTORY, inputs, proof, tuple(checks))
            if len(inputs) >= request.budget.max_trajectory_ticks:
                horizon_cut = True
                continue
            for command in request.supported_inputs:
                result = counter.step(current, command, world)
                if result.status is CalculationStatus.NEEDS_WORLD:
                    return outcome(SearchReason.UNKNOWN_WORLD, missing=result.missing_cells)
                if result.status is not CalculationStatus.OK or result.next_state.horizontal_collision:
                    continue
                successor = result.next_state
                if successor in seen:
                    continue
                seen.add(successor)
                serial += 1
                heapq.heappush(frontier, (_priority(successor, request.goal), serial,
                                          successor, inputs + (command,)))
        return outcome(SearchReason.TRAJECTORY_TICK_BUDGET if horizon_cut else SearchReason.SEARCH_EXHAUSTED)
    except CountLimit as limited:
        return outcome(limited.reason)
