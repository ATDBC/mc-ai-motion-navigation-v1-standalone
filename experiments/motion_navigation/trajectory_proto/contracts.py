"""Immutable P0 request contracts; no search or production integration."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.movement_transition import GoalState
from mc2p.motion_nav.physics_types import PhysicsState, TickInput
from mc2p.motion_nav.world_model import WorldSessionId


class SearchStatus(StrEnum):
    FOUND = "FOUND"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    BLOCKED = "BLOCKED"
    NO_TRAJECTORY_IN_BUDGET = "NO_TRAJECTORY_IN_BUDGET"
    STALE = "STALE"


class SearchReason(StrEnum):
    VERIFIED_TRAJECTORY = "verified_trajectory"
    UNKNOWN_WORLD = "unknown_world"
    MISSING_INPUT_APPLICATION = "missing_input_application"
    UNPROVEN_RESOURCES = "unproven_resources"
    KNOWN_NECESSARY_CONDITION = "known_necessary_condition"
    NODE_BUDGET = "node_budget"
    PHYSICS_STEP_BUDGET = "physics_step_budget"
    TRAJECTORY_TICK_BUDGET = "trajectory_tick_budget"
    TIMING_BRANCH_BUDGET = "timing_branch_budget"
    SEARCH_EXHAUSTED = "search_exhausted"
    REQUEST_IDENTITY = "request_identity"
    WORLD_DEPENDENCY = "world_dependency"
    GOAL_REVISION = "goal_revision"
    ANCHOR = "anchor"
    INPUT_LEDGER = "input_ledger"

    @property
    def status(self) -> SearchStatus:
        if self is SearchReason.VERIFIED_TRAJECTORY:
            return SearchStatus.FOUND
        if self in (SearchReason.UNKNOWN_WORLD, SearchReason.MISSING_INPUT_APPLICATION,
                    SearchReason.UNPROVEN_RESOURCES):
            return SearchStatus.NEEDS_INFORMATION
        if self is SearchReason.KNOWN_NECESSARY_CONDITION:
            return SearchStatus.BLOCKED
        if self in (SearchReason.NODE_BUDGET, SearchReason.PHYSICS_STEP_BUDGET,
                    SearchReason.TRAJECTORY_TICK_BUDGET, SearchReason.TIMING_BRANCH_BUDGET,
                    SearchReason.SEARCH_EXHAUSTED):
            return SearchStatus.NO_TRAJECTORY_IN_BUDGET
        return SearchStatus.STALE


class TimingBranch(StrEnum):
    ON_TIME = "ON_TIME"
    LATE_ONE_TICK = "LATE_ONE_TICK"


class ApplicationEvidence(StrEnum):
    PREDICTED = "conditional_prediction"
    APPLIED = "observed_application"


@dataclass(frozen=True, slots=True)
class KnownInputApplication:
    """One known conditional application or received actual application fact."""
    session: WorldSessionId
    control_sequence: int
    effect_tick: int
    tick_input: TickInput
    evidence_id: str
    evidence: ApplicationEvidence
    actual_movement_tick: int | None = None

    def __post_init__(self):
        if type(self.session) is not WorldSessionId or type(self.tick_input) is not TickInput:
            raise ContractViolation("application requires a typed session and input")
        require_nonnegative_int(self.control_sequence, "control sequence")
        require_nonnegative_int(self.effect_tick, "effect tick")
        require_identifier(self.evidence_id, "application evidence id")
        if type(self.evidence) is not ApplicationEvidence:
            raise ContractViolation("application evidence must be typed")
        if self.evidence is ApplicationEvidence.APPLIED:
            require_nonnegative_int(self.actual_movement_tick, "actual movement tick")
            if self.actual_movement_tick != self.effect_tick:
                raise ContractViolation("actual application must match its bound effect tick")
        elif self.actual_movement_tick is not None:
            raise ContractViolation("conditional prediction cannot claim a received actual tick")


@dataclass(frozen=True, slots=True)
class SearchBudget:
    max_nodes: int
    max_physics_steps: int
    max_trajectory_ticks: int
    max_timing_branches: int

    def __post_init__(self) -> None:
        for name in ("max_nodes", "max_physics_steps", "max_trajectory_ticks", "max_timing_branches"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ContractViolation(f"{name} must be a positive integer")
        if self.max_trajectory_ticks > 40:
            raise ContractViolation("P0 trajectory limit is 40 ticks")
        if self.max_timing_branches > 2:
            raise ContractViolation("P0 timing branch limit is two")


def _typed_tuple(value: tuple, item_type: type, name: str, *, nonempty: bool = False) -> None:
    if (type(value) is not tuple or (nonempty and not value)
            or any(type(item) is not item_type for item in value)):
        raise ContractViolation(f"{name} requires an immutable tuple of {item_type.__name__}")


def validate_timing_branches(value: tuple[TimingBranch, ...]) -> None:
    _typed_tuple(value, TimingBranch, "timing branches", nonempty=True)
    if value not in ((TimingBranch.ON_TIME,), tuple(TimingBranch)):
        raise ContractViolation("timing branches must be ON_TIME then optional LATE_ONE_TICK")


@dataclass(frozen=True, slots=True)
class InputTier:
    """One cumulative ordered input alphabet used by the primitive search."""

    tier_id: str
    inputs: tuple[TickInput, ...]

    def __post_init__(self) -> None:
        require_identifier(self.tier_id, "input tier id")
        _typed_tuple(self.inputs, TickInput, "input tier inputs", nonempty=True)


@dataclass(frozen=True, slots=True)
class TrajectorySearchRequest:
    """First entry is the common anchor; late entry follows its known prelude.

    Entry states and effect ticks correspond to timing_branches in order. Both
    hypotheses are retained when branch budget is insufficient: the search must
    return TIMING_BRANCH_BUDGET rather than choosing the favorable hypothesis.
    World payload/proof data belongs to the P0 physics adapter, not this identity.
    """

    request_id: str
    entry_states: tuple[PhysicsState, ...]
    world_session: WorldSessionId
    geometry_revision: int
    goal: GoalState
    goal_revision: int
    task_damage_budget: TaskDamageBudget
    anchor_id: str
    input_ledger_id: str
    budget: SearchBudget
    timing_branches: tuple[TimingBranch, ...]
    allowed_effect_ticks: tuple[int, ...]
    input_prefix: tuple[TickInput, ...]
    supported_inputs: tuple[TickInput, ...]
    input_tiers: tuple[InputTier, ...]
    branch_preludes: tuple[tuple[KnownInputApplication, ...] | None, ...]
    first_candidate_control_sequence: int
    route_guidance: tuple[tuple[float, float, float], ...] = ()
    minimum_terminal_speed_blocks_per_second: float = 0.
    stop_input: TickInput = field(kw_only=True)

    def __post_init__(self) -> None:
        for name in ("request_id", "anchor_id", "input_ledger_id"):
            require_identifier(getattr(self, name), name)
        for name in ("geometry_revision", "goal_revision"):
            require_nonnegative_int(getattr(self, name), name)
        for name, kind in (("world_session", WorldSessionId), ("goal", GoalState),
                           ("task_damage_budget", TaskDamageBudget), ("budget", SearchBudget)):
            if type(getattr(self, name)) is not kind:
                raise ContractViolation(f"{name} requires {kind.__name__}")
        validate_timing_branches(self.timing_branches)
        _typed_tuple(self.entry_states, PhysicsState, "entry states", nonempty=True)
        if len(self.entry_states) != len(self.timing_branches):
            raise ContractViolation("each timing branch requires exactly one entry state")
        if any(state.session != self.world_session for state in self.entry_states):
            raise ContractViolation("entry states must share the request world session")
        first = self.entry_states[0]
        if any((state.ruleset_id, state.state_schema)
               != (first.ruleset_id, first.state_schema)
               for state in self.entry_states):
            raise ContractViolation("entry hypotheses must share physics rules")
        _typed_tuple(self.allowed_effect_ticks, int, "allowed effect ticks", nonempty=True)
        expected = tuple(first.movement_tick_id + 1 + offset
                         for offset in range(len(self.timing_branches)))
        if self.allowed_effect_ticks != expected:
            raise ContractViolation("effect ticks must be the next tick and optional one-tick delay")
        if tuple(state.movement_tick_id + 1 for state in self.entry_states) != expected:
            raise ContractViolation("each candidate entry tick must precede its first effect tick")
        require_nonnegative_int(self.first_candidate_control_sequence, "first candidate control sequence")
        if type(self.branch_preludes) is not tuple or len(self.branch_preludes) != len(self.entry_states):
            raise ContractViolation("each timing branch requires an immutable prelude declaration")
        if self.branch_preludes[0] != ():
            raise ContractViolation("ON_TIME must have an empty known prelude")
        for index, prelude in enumerate(self.branch_preludes):
            if prelude is None:
                continue
            _typed_tuple(prelude, KnownInputApplication, "branch prelude")
            if len(prelude) != index:
                raise ContractViolation("LATE_ONE_TICK requires exactly one known waiting input")
            for application in prelude:
                if (application.session != self.world_session
                        or application.effect_tick != first.movement_tick_id + 1
                        or application.control_sequence >= self.first_candidate_control_sequence):
                    raise ContractViolation("prelude application must precede the common candidate schedule")
        _typed_tuple(self.input_prefix, TickInput, "shared input prefix")
        _typed_tuple(self.supported_inputs, TickInput, "supported inputs", nonempty=True)
        _typed_tuple(self.input_tiers, InputTier, "input tiers", nonempty=True)
        if len({tier.tier_id for tier in self.input_tiers}) != len(self.input_tiers):
            raise ContractViolation("input tier ids must be unique")
        for previous, current in zip(self.input_tiers, self.input_tiers[1:]):
            if (len(current.inputs) <= len(previous.inputs)
                    or current.inputs[:len(previous.inputs)] != previous.inputs):
                raise ContractViolation("each input tier must strictly extend the previous prefix")
        if self.input_tiers[-1].inputs != self.supported_inputs:
            raise ContractViolation("final input tier must equal supported inputs")
        if (type(self.stop_input) is not TickInput
                or self.stop_input.forward != 0. or self.stop_input.strafe != 0.
                or self.stop_input.jump or self.stop_input.sneak or self.stop_input.sprint):
            raise ContractViolation("stop input must be zero movement without action modifiers")
        if any(self.stop_input not in tier.inputs for tier in self.input_tiers):
            raise ContractViolation("stop input must belong to every declared input layer")
        minimum_speed = self.minimum_terminal_speed_blocks_per_second
        if (type(minimum_speed) not in (int, float) or not math.isfinite(minimum_speed)
                or minimum_speed < 0.
                or minimum_speed > self.goal.maximum_terminal_speed_blocks_per_second):
            raise ContractViolation("minimum terminal speed must be finite, nonnegative and within goal maximum")
        if len(self.input_prefix) > self.budget.max_trajectory_ticks:
            raise ContractViolation("shared input prefix exceeds trajectory budget")
        if any(command not in self.supported_inputs for command in self.input_prefix):
            raise ContractViolation("shared prefix must use the declared input set")
        if type(self.route_guidance) is not tuple:
            raise ContractViolation("route guidance must be immutable")
        for point in self.route_guidance:
            if (type(point) is not tuple or len(point) != 3
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
                raise ContractViolation("route guidance requires finite coordinate triples")

    @property
    def anchor_state(self) -> PhysicsState:
        return self.entry_states[0]


@dataclass(frozen=True, slots=True)
class TrajectorySearchResult:
    """Classification only; VERIFIED_TRAJECTORY is issued by the verifier.

    A candidate collision is not a request-level necessary-condition proof.
    This contract does not itself search, verify or supply a trajectory.
    """

    request_id: str
    status: SearchStatus
    reason: SearchReason

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request id")
        if type(self.status) is not SearchStatus or type(self.reason) is not SearchReason:
            raise ContractViolation("search result requires typed status and reason")
        if self.status is not self.reason.status:
            raise ContractViolation("search status contradicts its evidence classification")
