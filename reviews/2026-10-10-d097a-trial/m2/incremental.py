"""D097-A M2: incremental ("lazy") commitment proof, simulated offline as rolling submission.

`lazy_prove` replays the project's `scan_commitment` boundary by boundary.  At each boundary k it
computes the stop tail of every timing branch (the next command is already in flight), and applies
the scanner's `_risks` rule one tail at a time:

* no branch has an open risk interval after tail k -> "tick" decision: command k may be sent;
* a tail opens an interval at k -> "commit" decision: the tails k+1.. are computed (strict mode:
  every boundary, exactly like the scanner) until every open interval closes, at the recovery
  boundary r.  Commands k..r-1 are a locked suffix (cannot be abandoned); command r is an ordinary
  permitted command whose tail was proved inside the same decision.
* the last boundary (no command left) closes the candidate: "final" (or the end of a commit).

Physics, support and tail logic are the scanner's own private helpers (`commitment._tail`,
`_prove_entry`, `_calculated`, `_damage`, `_safe_support`); only the interval bookkeeping of the
batch-only `_risks` is re-stated incrementally (`_risk_step`) because `_risks` cannot be driven one
tail at a time.  Equivalence of that restatement is what run_m2.py / test_m2.py check.

Verdict mirroring.  The scanner processes branch 0 completely, then branch 1; within a branch it
reports plan errors, then tail errors (boundary order), then FINAL_STOP_UNSAFE, then `_risks`
errors, then UNRECOVERED_RISK.  The online phase halts at the first error in time (a real system
stops there).  `lazy_prove` then finishes the earlier-ordered branches offline (not counted in any
decision) so that the returned status/reason is the one the scanner would return.

Mode "strict_runahead" (addendum 2, fix F1): identical semantics to "strict", but an ordinary tick
decision keeps computing the tails of LATER boundaries (boundary order, all branches, risk bookkeeping
applied as each tail becomes available) until the decision has used at least `runahead_steps` physics
steps or the last boundary is reached; a commit decision only computes the tails not yet available.
All work is charged to the decision that does it, so the per-candidate step total equals strict mode.
An error found in a look-ahead tail halts the online loop earlier; the final status/reason is the same
as strict (and as the scanner) because the offline `resolve()` phase is unchanged.
Online premise (not exercised here): the look-ahead uses the plan's predicted rollout states; if the
real anchor deviates from the prediction the precomputed tails must be discarded (M3 question).

Fault flags exist only for mutation testing and are off by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
import time

from experiments.motion_navigation.trajectory_proto import commitment as C
from experiments.motion_navigation.trajectory_proto.commitment import (
    BoundaryInputs, CandidateRejection, RiskInterval, ScanOptions, ScanStatus, StopTail, TailStatus,
)
from experiments.motion_navigation.trajectory_proto.contracts import (
    SearchReason, TrajectorySearchRequest,
)
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics, CountLimit, ScanCounts
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import BlockPos, CellFact, CellKnowledge

MODES = ("strict", "minimal", "strict_runahead")
FAULTS = frozenset({
    "no_landing_proof", "unknown_as_free", "one_branch", "ignore_inflight",
    "accept_stale", "ignore_damage", "swap_locked_command",
})


def check_faults(faults) -> frozenset:
    faults = frozenset(faults)
    unknown = faults - FAULTS
    if unknown:
        raise ValueError(f"unknown fault flags: {sorted(unknown)}")
    return faults


# ---------------------------------------------------------------- permits ---------------------------

class PermitStatus(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"


class ExecutionCheck(StrEnum):
    ALLOWED = "ALLOWED"
    COMMAND_MISMATCH = "COMMAND_MISMATCH"
    OUT_OF_RANGE = "OUT_OF_RANGE"


@dataclass(frozen=True, slots=True)
class Permit:
    """Authority to send `commands` (starting at candidate boundary `boundary`).

    The first `locked_count` commands are a locked suffix: once the first is sent the executor must
    send all of them unchanged, because the body is committed until the recovery boundary.
    """
    kind: str                                   # "tick" | "commit"
    boundary: int
    commands: tuple[TickInput, ...]
    locked_count: int
    request_id: str
    anchor_id: str
    entry_states: tuple
    goal: object
    goal_revision: int
    input_ledger_id: str
    first_candidate_control_sequence: int
    branch_preludes: tuple
    world_session: object
    dependency_facts: tuple[tuple[BlockPos, CellFact], ...]


@dataclass(frozen=True, slots=True)
class PermitCheck:
    status: PermitStatus
    reason: SearchReason | None = None
    missing_cells: tuple = ()


def validate_permit(permit, request, world, *, faults=frozenset()) -> PermitCheck:
    """STALE when any identity or dependency fact changed; NEEDS_INFORMATION when a dependency is
    now unknown (explicit staleness has priority, as in `validate_commitment`)."""
    faults = check_faults(faults)
    if type(permit) is not Permit or type(request) is not TrajectorySearchRequest \
            or type(world) is not PhysicsWorldView:
        raise ContractViolation("permit validation requires typed permit, request and world")
    if "accept_stale" in faults:
        return PermitCheck(PermitStatus.VALID)
    reason = None
    if request.request_id != permit.request_id:
        reason = SearchReason.REQUEST_IDENTITY
    elif world.session != permit.world_session or request.world_session != permit.world_session:
        reason = SearchReason.WORLD_DEPENDENCY
    elif request.anchor_id != permit.anchor_id or request.entry_states != permit.entry_states:
        reason = SearchReason.ANCHOR
    elif request.goal_revision != permit.goal_revision or request.goal != permit.goal:
        reason = SearchReason.GOAL_REVISION
    elif (request.input_ledger_id != permit.input_ledger_id
          or request.first_candidate_control_sequence != permit.first_candidate_control_sequence
          or any(new is not None and new != old
                 for new, old in zip(request.branch_preludes, permit.branch_preludes))):
        reason = SearchReason.INPUT_LEDGER
    elif world.ruleset != JAVA_1_21_RULESET:
        reason = SearchReason.REQUEST_IDENTITY
    if reason is not None:
        return PermitCheck(PermitStatus.STALE, reason)
    unknown = []
    for position, fact in permit.dependency_facts:
        now = world.cell(position)
        if now.knowledge is CellKnowledge.UNKNOWN and fact.knowledge is not CellKnowledge.UNKNOWN:
            unknown.append(position)
        elif now != fact:
            return PermitCheck(PermitStatus.STALE, SearchReason.WORLD_DEPENDENCY)
    if any(prelude is None for prelude in request.branch_preludes):
        return PermitCheck(PermitStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION)
    if unknown and "unknown_as_free" not in faults:
        return PermitCheck(PermitStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD,
                           tuple(sorted(unknown)))
    return PermitCheck(PermitStatus.VALID)


def check_execution(permit, command, boundary=None, *, faults=frozenset()) -> ExecutionCheck:
    """Executor gate: only the permitted (or locked) command at its boundary may be sent."""
    faults = check_faults(faults)
    if type(permit) is not Permit:
        raise ContractViolation("execution check requires a permit")
    if "swap_locked_command" in faults:
        return ExecutionCheck.ALLOWED
    index = (permit.boundary if boundary is None else boundary) - permit.boundary
    if not 0 <= index < len(permit.commands):
        return ExecutionCheck.OUT_OF_RANGE
    return ExecutionCheck.ALLOWED if command == permit.commands[index] else ExecutionCheck.COMMAND_MISMATCH


# ---------------------------------------------------------------- results ---------------------------

@dataclass(frozen=True, slots=True)
class Decision:
    kind: str                 # "plan" | "tick" | "commit" | "final"
    boundary: int             # first boundary handled by this decision (-1 for plan)
    end_boundary: int         # last boundary whose tail was computed in this decision
    physics_steps: int
    nodes: int
    wall_ns: int
    outcome: str              # "permitted" | "verified" | "rejected"
    permit: Permit | None = None
    computed_to: int = -1     # highest boundary whose tails exist for every branch after this decision


@dataclass(frozen=True, slots=True)
class LazyBranch:
    timing_branch: object
    states: tuple
    tails: dict
    risk_intervals: tuple[RiskInterval, ...]
    damage_points: float


@dataclass(frozen=True, slots=True)
class LazyResult:
    status: ScanStatus
    reason: object
    branches: tuple[LazyBranch, ...]
    decisions: tuple[Decision, ...]
    counts: ScanCounts
    dependencies: frozenset
    missing_cells: tuple
    online_halt: tuple | None        # (boundary, status, reason) of the first error in time
    resolution_steps: int            # physics steps spent only to mirror the scanner's reason
    mode: str
    faults: frozenset

    @property
    def permits(self):
        return tuple(d.permit for d in self.decisions if d.permit is not None)


def _unknown_as_air(world, bounds=((-9, 9), (-50, 7), (-14, 16))):
    """Fault helper ("unknown_as_free"): a detached copy in which every unknown cell inside the trial
    box reads as known air, i.e. missing information silently becomes free space."""
    from mc2p.motion_nav.world_model import ObservationStamp, WorldView
    stamp = ObservationStamp(world.session, 0, 0, "fault-unknown-as-air", 0)
    air = CellFact(CellKnowledge.AIR, stamp, None)
    facts = {}
    for x in range(bounds[0][0], bounds[0][1] + 1):
        for y in range(bounds[1][0], bounds[1][1] + 1):
            for z in range(bounds[2][0], bounds[2][1] + 1):
                fact = world.cell((x, y, z))
                facts[(x, y, z)] = air if fact.knowledge is CellKnowledge.UNKNOWN else fact
    return PhysicsWorldView(WorldView.detached(world.session, world.geometry_revision, 0, facts),
                            world.ruleset)


# ---------------------------------------------------------------- engine ----------------------------

_REJECT = ScanStatus.CANDIDATE_REJECTED


class _Err:
    __slots__ = ("status", "reason", "missing", "boundary")

    def __init__(self, status, reason, missing=(), boundary=None):
        self.status, self.reason, self.missing, self.boundary = status, reason, missing, boundary


class _Branch:
    def __init__(self, index, timing_branch):
        self.index, self.timing_branch = index, timing_branch
        self.prelude_states = self.states = None
        self.minimum_y = 0.
        self.normal_damage = 0.
        self.tails = {}
        self.intervals = []
        self.open_first = None
        self.next_boundary = 0
        self.open_after = {}               # boundary -> open_first right after that boundary's tail
        self.plan_error = self.tail_error = self.risk_error = None


class _Run:
    def __init__(self, request, inputs, world, rows, options, counter, mode, faults, clock):
        self.request, self.inputs, self.world, self.rows = request, inputs, world, rows
        self.options, self.counter, self.mode, self.faults, self.clock = options, counter, mode, faults, clock
        self.n = len(inputs)
        self.branches = []
        self.plan_deps = set()
        self.dep_by_boundary = {}          # boundary -> cells read while computing that boundary
        self.cur_deps = self.plan_deps     # sink for the work currently being done
        self.frontier = -1                 # tails exist for every branch up to this boundary
        self.last_attempt = -1
        self.runahead_steps = 0
        self.plan_facts = ()
        self.decisions = []
        self.maximum_damage = request.task_damage_budget.maximum_expected_damage_points
        self.pre_t = self.pre_s = self.pre_n = 0

    # ---- decision bookkeeping
    def _begin(self):
        self.pre_n, self.pre_s, self.pre_t = self.counter.nodes, self.counter.physics_steps, self.clock()

    def _end(self, kind, boundary, end, outcome, permit=None):
        wall = self.clock() - self.pre_t
        self.decisions.append(Decision(kind, boundary, end, self.counter.physics_steps - self.pre_s,
                                       self.counter.nodes - self.pre_n, wall, outcome, permit,
                                       self.frontier))

    def all_dependencies(self):
        out = set(self.plan_deps)
        for cells in self.dep_by_boundary.values():
            out |= cells
        return frozenset(out)

    def _permit(self, kind, k, j):
        n = self.n
        last = min(j, n - 1)
        cells = set()
        for boundary in range(k, j + 1):
            cells |= self.dep_by_boundary.get(boundary, ())
        facts = self.plan_facts + tuple((p, self.world.cell(p)) for p in sorted(cells))
        locked = 0 if kind == "tick" else j - k if j < n else n - k
        request = self.request
        return Permit(kind, k, self.inputs[k:last + 1], locked, request.request_id, request.anchor_id,
                      request.entry_states, request.goal, request.goal_revision,
                      request.input_ledger_id, request.first_candidate_control_sequence,
                      request.branch_preludes, request.world_session, facts)

    # ---- plan stage: same checks as the scanner, per branch
    def plan(self):
        request, faults = self.request, self.faults
        self._begin()
        self.cur_deps = self.plan_deps
        timing = request.timing_branches[:1] if "one_branch" in faults else request.timing_branches
        for index, timing_branch in enumerate(timing):
            branch = _Branch(index, timing_branch)
            self.branches.append(branch)
            try:
                self.counter.node()
                branch.prelude_states, deps = C._prove_entry(self.counter, request, index, self.world)
                self.cur_deps.update(deps)
                entry = branch.prelude_states[-1]
                states = [entry]
                for command in self.inputs:
                    states.append(self._rollout_step(states[-1], command))
                branch.states = tuple(states)
                branch.normal_damage = C._damage(branch.prelude_states[:-1] + branch.states)
                if ("ignore_damage" not in faults
                        and branch.normal_damage > self.maximum_damage):
                    raise C._Incomplete(_REJECT, CandidateRejection.DAMAGE_ALLOWANCE)
                if not branch.states[-1].on_ground:
                    raise C._Incomplete(_REJECT, CandidateRejection.FINAL_EXIT_NOT_GROUNDED)
                branch.minimum_y = min(entry.position[1], branch.states[-1].position[1])
            except C._Incomplete as incomplete:
                branch.plan_error = _Err(incomplete.status, incomplete.reason, incomplete.missing)
            except CountLimit as limited:
                branch.plan_error = _Err(ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason)
            if branch.plan_error is not None:
                break          # the scanner never plans a later branch after an earlier plan error
        self.plan_facts = tuple((p, self.world.cell(p)) for p in sorted(self.plan_deps))
        self._end("plan", -1, -1, "rejected" if self.failed() else "permitted")

    def _rollout_step(self, state, command):
        try:
            return C._calculated(self.counter, state, command, self.world, self.cur_deps)
        except C._Incomplete as incomplete:
            if "unknown_as_free" in self.faults and incomplete.status is ScanStatus.NEEDS_INFORMATION:
                return state
            raise

    # ---- tails and the incremental `_risks`
    def _tail(self, branch, row):
        prefix = branch.prelude_states[:-1] + branch.states[:row.boundary + 1]
        commands = () if "ignore_inflight" in self.faults else row.irrevocable_inputs
        maximum = math.inf if "ignore_damage" in self.faults else self.maximum_damage
        try:
            return C._tail(self.counter, row.boundary, prefix, commands, self.request.stop_input,
                           self.world, branch.minimum_y, maximum, self.options)
        except C._Incomplete as incomplete:
            if "unknown_as_free" in self.faults and incomplete.status is ScanStatus.NEEDS_INFORMATION:
                return StopTail(row.boundary, TailStatus.SAFE_STOP, (), (prefix[-1],), (), 0.,
                                prefix[-1].movement_tick_id)
            raise

    def _support(self, state):
        try:
            return C._safe_support(state, self.world, self.cur_deps)
        except C._Incomplete as incomplete:
            if "unknown_as_free" in self.faults and incomplete.status is ScanStatus.NEEDS_INFORMATION:
                return True
            raise

    def _risk_step(self, branch, tail, support_known):
        if tail.status is TailStatus.UNSAFE and branch.open_first is None:
            branch.open_first = tail.boundary
            return
        if branch.open_first is None:
            return
        if "no_landing_proof" in self.faults:
            if not (tail.boundary > branch.open_first and tail.states[0].on_ground):
                return
        elif not (tail.status is TailStatus.SAFE_STOP and tail.states[0].on_ground):
            return
        elif not support_known:
            self.counter.node()
            if not self._support(tail.states[0]):
                return
        first = branch.open_first
        branch.intervals.append(RiskInterval(
            first - 1 if first else None, first, tail.boundary,
            branch.tails[first - 1].absolute_tick if first else None,
            branch.tails[first].absolute_tick, branch.tails[first].absolute_tick + 1,
            tail.absolute_tick))
        branch.open_first = None

    def advance_branch(self, branch, j, support_known=False):
        self.cur_deps = self.dep_by_boundary.setdefault(j, set())
        row = self.rows[branch.index][j]
        try:
            tail = self._tail(branch, row)
            if (self.request.branch_preludes[branch.index] and row.boundary == 0
                    and tail.status is not TailStatus.SAFE_STOP):
                raise C._Incomplete(_REJECT, CandidateRejection.PRELUDE_UNSAFE)
        except C._Incomplete as incomplete:
            branch.tail_error = _Err(incomplete.status, incomplete.reason, incomplete.missing, j)
            branch.next_boundary = j + 1
            return
        except CountLimit as limited:
            branch.tail_error = _Err(ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason, (), j)
            branch.next_boundary = j + 1
            return
        branch.tails[j] = tail
        self.cur_deps.update(tail.dependencies)
        branch.next_boundary = j + 1
        if branch.risk_error is None:
            try:
                self._risk_step(branch, tail, support_known)
            except C._Incomplete as incomplete:
                branch.risk_error = _Err(incomplete.status, incomplete.reason, incomplete.missing, j)
            except CountLimit as limited:
                branch.risk_error = _Err(ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason, (), j)
        branch.open_after[j] = branch.open_first

    def _advance_open_minimal(self, branch, j):
        """Minimal mode: an open branch only needs its first grounded, supported boundary."""
        self.cur_deps = self.dep_by_boundary.setdefault(j, set())
        state = branch.states[j]
        branch.next_boundary = j + 1
        if not state.on_ground:
            return
        try:
            self.counter.node()
            supported = self._support(state)
        except C._Incomplete as incomplete:
            branch.risk_error = _Err(incomplete.status, incomplete.reason, incomplete.missing, j)
            return
        except CountLimit as limited:
            branch.risk_error = _Err(ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason, (), j)
            return
        if supported:
            self.advance_branch(branch, j, support_known=True)

    def advance(self, j, committing=False):
        """Compute the tail at boundary j for every branch; the first error stops the boundary."""
        self.last_attempt = j
        for branch in self.branches:
            if self.error(branch) is not None:
                return
            if (committing and self.mode == "minimal" and branch.open_first is not None
                    and j < self.n):
                self._advance_open_minimal(branch, j)
            else:
                self.advance_branch(branch, j)
            if self.error(branch) is not None:
                return
        self.frontier = j

    # ---- error view in the scanner's per-branch order
    def error(self, branch):
        if branch.plan_error is not None:
            return branch.plan_error
        if branch.tail_error is not None:
            return branch.tail_error
        if branch.next_boundary == self.n + 1 and self.n in branch.tails:
            if branch.tails[self.n].status is not TailStatus.SAFE_STOP:
                return _Err(_REJECT, CandidateRejection.FINAL_STOP_UNSAFE, (), self.n)
            if branch.risk_error is not None:
                return branch.risk_error
            if branch.open_first is not None:
                return _Err(_REJECT, CandidateRejection.UNRECOVERED_RISK, (), self.n)
            return None
        return branch.risk_error

    def failed(self):
        return any(self.error(branch) is not None for branch in self.branches)

    def any_open(self):
        return any(branch.open_first is not None for branch in self.branches)

    def open_at(self, j):
        """Was any branch inside a risk interval right after boundary j's tail?"""
        return any(branch.open_after.get(j) is not None for branch in self.branches)

    # ---- phase A: the online decision loop
    def online(self):
        n = self.n
        k = 0
        while True:
            self._begin()
            self.advance(k)
            j = k
            if k == n:
                kind = "final"
            elif not self.failed() and self.any_open():
                kind = "commit"
                while self.any_open() and not self.failed() and j < n:
                    j += 1
                    self.advance(j, committing=True)
            else:
                kind = "tick"
            if self.failed():
                self._end(kind, k, j, "rejected")
                return False
            if j == n:
                self._end(kind, k, j, "verified", self._permit("commit", k, j) if kind == "commit" else None)
                return True
            self._end(kind, k, j, "permitted", self._permit(kind, k, j))
            k = j + 1

    def online_runahead(self):
        """Same decisions as `online`, with the look-ahead of fix F1 (see module docstring)."""
        n = self.n
        k = 0
        while True:
            self._begin()
            if k > self.frontier:
                self.advance(k)
            j = k
            if self.failed():
                kind = "final" if k == n else "tick"
            elif k == n:
                kind = "final"
            elif self.open_at(k):
                kind = "commit"
                while self.open_at(j) and j < n:
                    j += 1
                    if j > self.frontier:
                        self.advance(j)
                        if self.failed():
                            break
            else:
                kind = "tick"
                while (self.frontier < n and not self.failed()
                       and self.counter.physics_steps - self.pre_s < self.runahead_steps):
                    self.advance(self.frontier + 1)
            if self.failed():
                self._end(kind, k, self.last_attempt, "rejected")
                return False
            if j == n:
                self._end(kind, k, j, "verified", self._permit("commit", k, j) if kind == "commit" else None)
                return True
            self._end(kind, k, j, "permitted", self._permit(kind, k, j))
            k = j + 1

    # ---- phase B: mirror the scanner's reason (offline, not part of any decision)
    def resolve(self):
        first = self.counter.physics_steps
        for branch in self.branches:
            if branch.plan_error is not None:
                break
            while branch.tail_error is None and branch.next_boundary <= self.n:
                self.advance_branch(branch, branch.next_boundary)
            if self.error(branch) is not None:
                break
        return self.counter.physics_steps - first

    def first_error(self):
        for branch in self.branches:
            error = self.error(branch)
            if error is not None:
                return error
        return None


def lazy_prove(request, inputs, world, boundary_inputs, *, mode="strict", faults=frozenset(),
               counter=None, options=ScanOptions(), clock=time.perf_counter_ns,
               runahead_steps=60) -> LazyResult:
    """Offline simulation of rolling submission; see the module docstring."""
    faults = check_faults(faults)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if type(runahead_steps) is not int or runahead_steps < 1:
        raise ValueError("runahead_steps must be a positive integer")
    if type(request) is not TrajectorySearchRequest or type(options) is not ScanOptions:
        raise ContractViolation("proof requires a typed request and options")
    C._inputs(inputs)
    if type(world) is not PhysicsWorldView or not world.is_detached:
        raise ContractViolation("proof requires a detached physics world snapshot")
    if world.ruleset != JAVA_1_21_RULESET:
        raise ContractViolation("proof supports only the Java 1.21 ruleset")
    if (inputs[:len(request.input_prefix)] != request.input_prefix
            or any(command not in request.supported_inputs for command in inputs)):
        raise ContractViolation("candidate must extend the shared prefix with supported inputs")
    if counter is None:
        counter = CountedPhysics(request.budget)
    elif type(counter) is not CountedPhysics or counter.budget != request.budget:
        raise ContractViolation("shared counter must own this request's unchanged total budget")

    def early(status, reason, missing=()):
        return LazyResult(status, reason, (), (), counter.counts, frozenset(), tuple(missing), None, 0,
                          mode, faults)

    try:
        if world.session != request.world_session or world.geometry_revision != request.geometry_revision:
            raise C._Incomplete(ScanStatus.STALE, SearchReason.WORLD_DEPENDENCY)
        if any(prelude is None for prelude in request.branch_preludes):
            raise C._Incomplete(ScanStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION)
        C._check_evidence(request, inputs, boundary_inputs)
        if len(request.entry_states) > request.budget.max_timing_branches:
            raise CountLimit(SearchReason.TIMING_BRANCH_BUDGET)
        if len(inputs) > request.budget.max_trajectory_ticks:
            raise CountLimit(SearchReason.TRAJECTORY_TICK_BUDGET)
    except CountLimit as limited:
        return early(ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason)
    except C._Incomplete as incomplete:
        return early(incomplete.status, incomplete.reason, incomplete.missing)

    run_world = _unknown_as_air(world) if "unknown_as_free" in faults else world
    run = _Run(request, inputs, run_world, boundary_inputs, options, counter, mode, faults, clock)
    run.runahead_steps = runahead_steps
    run.plan()
    verified = False
    halt = None
    if not run.failed():
        verified = run.online_runahead() if mode == "strict_runahead" else run.online()
    if not verified:
        error = run.first_error()
        halt = (run.decisions[-1].end_boundary, error.status, error.reason)
    resolution = 0
    if not verified and mode in ("strict", "strict_runahead"):
        resolution = run.resolve()
    error = None if verified else run.first_error()
    if error is None and not verified:       # minimal mode may halt on a not-yet-complete view
        error = run.first_error()
    branches = tuple(LazyBranch(b.timing_branch, b.states or (), b.tails, tuple(b.intervals),
                                b.normal_damage) for b in run.branches)
    if verified:
        return LazyResult(ScanStatus.VERIFIED_CANDIDATE, None, branches, tuple(run.decisions),
                          counter.counts, run.all_dependencies(), (), None, 0, mode, faults)
    return LazyResult(error.status, error.reason, branches, tuple(run.decisions), counter.counts,
                      run.all_dependencies(), tuple(error.missing), halt, resolution, mode, faults)
