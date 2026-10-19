"""Small immutable contracts shared by motion proof and later handoff stages.

This module declares which bounded control pattern a caller permits as a
fallback.  It does not choose a successor, own a worker, or grant execution
authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import hashlib
import json
import math
from typing import TYPE_CHECKING
from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.contracts.motion_activation import validate_frozen_value
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.physics_types import PhysicsState, TickInput
from mc2p.motion_nav.world_model import BlockPos

if TYPE_CHECKING:
    from mc2p.motion_nav.motion_solver import LandingRegion, MotionCommandTick, MotionSolveKind
    from mc2p.motion_nav.prepared_motion import PreparedScanBudget
    from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
    from mc2p.motion_nav.physics_types import PhysicsRuleset
else:
    # Solver/prepared import this module. Constructors below enforce the exact
    # native types lazily, while frozen graph validation can resolve hints
    # during either module's import order.
    MotionCommandTick = MotionSolveKind = LandingRegion = PreparedScanBudget = object
    PhysicsWorldBounds = PhysicsRuleset = object


@dataclass(frozen=True, slots=True)
class FixedSuccessorSpec:
    """A bounded caller choice; no candidate search or execution authority."""

    commands: tuple[MotionCommandTick, ...]
    kind: MotionSolveKind
    direction: tuple[int, int]
    landing: LandingRegion
    fallback_candidates: tuple[FallbackCandidate, ...]
    maximum_replay_steps: int
    scan_budget: PreparedScanBudget

    def __post_init__(self) -> None:
        from mc2p.motion_nav.motion_solver import LandingRegion, MotionCommandTick, MotionSolveKind
        from mc2p.motion_nav.prepared_motion import PreparedScanBudget
        validate_frozen_value(self)
        if (type(self.commands) is not tuple or not 1 <= len(self.commands) <= 40
                or any(type(command) is not MotionCommandTick for command in self.commands)
                or type(self.kind) is not MotionSolveKind or type(self.landing) is not LandingRegion
                or self.direction not in ((0, 1), (1, 0), (0, -1), (-1, 0))
                or type(self.scan_budget) is not PreparedScanBudget
                or type(self.maximum_replay_steps) is not int
                or not 1 <= self.maximum_replay_steps <= 40):
            raise ContractViolation("fixed successor requires bounded native inputs and budgets")
        validate_fallback_candidates(self.fallback_candidates)


@dataclass(frozen=True, slots=True)
class SuccessorFeasibilityBinding:
    """Fixed-input feasibility only; all lifecycle identities stay outside."""

    spec: FixedSuccessorSpec
    parent_request_digest: str
    parent_main_digest: str
    exit_fallback_digest: str
    first_start_tick: int
    exit_boundary_index: int
    entry_state: PhysicsState
    expected_inputs: tuple[TickInput, ...]
    input_digest: str
    exit_state: PhysicsState
    requested_window: CandidateExecutionWindow
    ruleset: PhysicsRuleset
    input_projection_version: str
    dependency_digest: str
    dependency_versions: tuple[tuple[BlockPos, int], ...]
    parent_dependency_versions: tuple[tuple[BlockPos, int], ...]
    resource_incomplete_reasons: tuple[str, ...]
    expected_damage_points: float
    damage_remaining_points: float
    physics_steps: int
    parent_verification_steps: int
    preparation_bounds: PhysicsWorldBounds

    def __post_init__(self) -> None:
        from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
        from mc2p.motion_nav.physics_types import PhysicsRuleset
        validate_frozen_value(self)
        if (type(self.spec) is not FixedSuccessorSpec
                or type(self.entry_state) is not PhysicsState
                or type(self.exit_state) is not PhysicsState
                or type(self.ruleset) is not PhysicsRuleset
                or type(self.preparation_bounds) is not PhysicsWorldBounds
                or type(self.requested_window) is not CandidateExecutionWindow
                or type(self.expected_inputs) is not tuple
                or len(self.expected_inputs) != len(self.spec.commands)
                or any(type(value) is not TickInput for value in self.expected_inputs)):
            raise ContractViolation("successor feasibility requires exact native replay values")
        for name in ("parent_request_digest", "parent_main_digest", "exit_fallback_digest",
                     "input_digest", "dependency_digest"):
            value = getattr(self, name)
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ContractViolation("successor feasibility requires sealed SHA-256 digests")
        for name in ("first_start_tick", "exit_boundary_index", "physics_steps", "parent_verification_steps"):
            require_nonnegative_int(getattr(self, name), name)
        if (self.physics_steps != len(self.spec.commands)
                or self.physics_steps > self.spec.maximum_replay_steps
                or self.requested_window != CandidateExecutionWindow(
                    self.entry_state.movement_tick_id + 1, self.entry_state.movement_tick_id + 1)
                or self.exit_state.movement_tick_id != self.entry_state.movement_tick_id + self.physics_steps
                or self.exit_state.session != self.entry_state.session
                or self.ruleset.ruleset_id != self.entry_state.ruleset_id
                or self.ruleset.state_schema != self.entry_state.state_schema):
            raise ContractViolation("successor feasibility timing, rules or replay budget differs")
        require_identifier(self.input_projection_version, "successor input projection")
        for versions in (self.dependency_versions, self.parent_dependency_versions):
            positions = tuple(pos for pos, _ in versions)
            if positions != tuple(sorted(set(positions))):
                raise ContractViolation("successor dependencies must be ordered and unique")
            for _, version in versions:
                require_nonnegative_int(version, "successor semantic dependency version")
        for amount in (self.expected_damage_points, self.damage_remaining_points):
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                raise ContractViolation("successor damage must be finite and nonnegative")
        if self.expected_damage_points > self.damage_remaining_points:
            raise ContractViolation("successor exceeds its remaining damage balance")
        from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest
        from mc2p.motion_nav.online_motion import project_movement_command, ProjectionStatus
        if (self.input_digest != canonical_motion_digest(self.expected_inputs)
                or self.dependency_digest != canonical_motion_digest(self.dependency_versions)
                or not self.exit_state.on_ground or not self.spec.landing.contains(self.exit_state)):
            raise ContractViolation("successor inputs, dependencies or terminal state differ from feasibility")
        for command, expected in zip(self.spec.commands, self.expected_inputs):
            projected = project_movement_command(self.entry_state, command.movement,
                movement_yaw_radians=command.required_movement_yaw_radians)
            if projected.status is not ProjectionStatus.READY or projected.tick_input != expected:
                raise ContractViolation("successor commands differ from the bound fixed inputs")

    @property
    def suffix_identity(self) -> tuple[object, ...]:
        """Only bounded digests cross into the existing prepared stream."""
        from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest
        return (SuccessorPreparationReference(self.parent_request_digest,
            self.parent_main_digest, self.exit_fallback_digest, self.input_digest,
            canonical_motion_digest(self.spec.commands),
            canonical_motion_digest(self.spec.fallback_candidates), self.entry_state,
            self.exit_state, self.requested_window, self.ruleset, self.input_projection_version,
            self.dependency_versions,
            self.parent_dependency_versions,
            self.damage_remaining_points, self.spec.scan_budget),)


@dataclass(frozen=True, slots=True)
class SuccessorPreparationReference:
    """Bounded reference in the existing stream; no complete proof graph."""

    parent_request_digest: str
    parent_main_digest: str
    exit_fallback_digest: str
    input_digest: str
    commands_digest: str
    fallbacks_digest: str
    entry_state: PhysicsState
    exit_state: PhysicsState
    requested_window: CandidateExecutionWindow
    ruleset: PhysicsRuleset
    input_projection_version: str
    dependency_versions: tuple[tuple[BlockPos, int], ...]
    parent_dependency_versions: tuple[tuple[BlockPos, int], ...]
    damage_remaining_points: float
    scan_budget: PreparedScanBudget

    def __post_init__(self) -> None:
        from mc2p.motion_nav.physics_types import PhysicsRuleset
        from mc2p.motion_nav.prepared_motion import PreparedScanBudget
        validate_frozen_value(self)
        if (type(self.entry_state) is not PhysicsState or type(self.exit_state) is not PhysicsState
                or type(self.requested_window) is not CandidateExecutionWindow
                or type(self.ruleset) is not PhysicsRuleset or type(self.scan_budget) is not PreparedScanBudget):
            raise ContractViolation("successor reference requires exact native contracts")
        require_identifier(self.input_projection_version, "successor reference input projection")
        for name in ("parent_request_digest", "parent_main_digest", "exit_fallback_digest",
                     "input_digest", "commands_digest", "fallbacks_digest"):
            value = getattr(self, name)
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ContractViolation("successor preparation requires sealed digests")
        if self.requested_window != CandidateExecutionWindow(
                self.entry_state.movement_tick_id + 1, self.entry_state.movement_tick_id + 1):
            raise ContractViolation("fixed successor reference requires its unique next tick")


class FallbackTerminalMode(StrEnum):
    STANDARD_SUPPORT = "standard_support"
    SNEAK_EDGE_STABLE = "sneak_edge_stable"


class FallbackYawMode(StrEnum):
    EXACT = "exact"
    PRESERVE_CURRENT = "preserve_current"


@dataclass(frozen=True, slots=True)
class FallbackCandidate:
    candidate_id: str
    tick_input: TickInput
    maximum_ticks: int = 40
    terminal_mode: FallbackTerminalMode = FallbackTerminalMode.STANDARD_SUPPORT
    yaw_mode: FallbackYawMode = FallbackYawMode.EXACT

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        require_identifier(self.candidate_id, "fallback candidate id")
        if type(self.tick_input) is not TickInput:
            raise ContractViolation("fallback candidate requires an exact tick input")
        if type(self.terminal_mode) is not FallbackTerminalMode:
            raise ContractViolation("fallback candidate requires a typed terminal mode")
        if type(self.yaw_mode) is not FallbackYawMode:
            raise ContractViolation("fallback candidate requires a typed yaw mode")
        if (self.yaw_mode is FallbackYawMode.PRESERVE_CURRENT
                and (self.tick_input.movement_yaw_radians != 0.0
                     or math.copysign(
                         1.0, self.tick_input.movement_yaw_radians) < 0.0)):
            raise ContractViolation(
                "preserve-current fallback requires the canonical zero yaw placeholder")
        if (self.terminal_mode is FallbackTerminalMode.SNEAK_EDGE_STABLE
                and not self.tick_input.sneak):
            raise ContractViolation("sneak-edge fallback must keep sneak pressed")
        require_nonnegative_int(self.maximum_ticks, "fallback maximum ticks")
        if not 1 <= self.maximum_ticks <= 40:
            raise ContractViolation("fallback maximum ticks must be within 1..40")

    def materialize(self, state: PhysicsState) -> TickInput:
        if type(state) is not PhysicsState:
            raise ContractViolation("fallback materialization requires a physics state")
        return (self.tick_input if self.yaw_mode is FallbackYawMode.EXACT else
                replace(self.tick_input,
                        movement_yaw_radians=state.yaw_radians))

    def matches_materialized(self, value: TickInput, state: PhysicsState) -> bool:
        return type(value) is TickInput and value == self.materialize(state)


def neutral_fallback(yaw_radians: float) -> FallbackCandidate:
    return FallbackCandidate(
        "neutral",
        TickInput(0.0, 0.0, False, False, False, float(yaw_radians)),
    )


def preserve_current_neutral_fallback() -> FallbackCandidate:
    return FallbackCandidate(
        "neutral",
        TickInput(0.0, 0.0, False, False, False, 0.0),
        yaw_mode=FallbackYawMode.PRESERVE_CURRENT,
    )


def validate_fallback_candidates(candidates: tuple[FallbackCandidate, ...]) -> None:
    if (type(candidates) is not tuple or not 1 <= len(candidates) <= 3
            or any(type(candidate) is not FallbackCandidate for candidate in candidates)):
        raise ContractViolation("fallback candidates must be a bounded typed tuple")
    identities = tuple(candidate.candidate_id for candidate in candidates)
    controls = tuple((candidate.tick_input, candidate.yaw_mode) for candidate in candidates)
    if len(set(identities)) != len(identities) or len(set(controls)) != len(controls):
        raise ContractViolation("fallback candidates must have unique identities and inputs")


def fallback_requires_continuous_support(candidate: FallbackCandidate) -> bool:
    if type(candidate) is not FallbackCandidate:
        raise ContractViolation("fallback support rule requires a typed candidate")
    return candidate.terminal_mode is FallbackTerminalMode.SNEAK_EDGE_STABLE


def fallback_terminal_body_ready(candidate: FallbackCandidate,
                                 state: PhysicsState) -> bool:
    if type(candidate) is not FallbackCandidate or type(state) is not PhysicsState:
        raise ContractViolation("fallback terminal rule requires typed values")
    speed = math.hypot(state.velocity_blocks_per_tick[0],
                       state.velocity_blocks_per_tick[2])
    return (state.on_ground and speed <= 1.0e-9
            and (candidate.terminal_mode is FallbackTerminalMode.STANDARD_SUPPORT
                 or state.sneaking))


def fallback_release_witness_body_ready(
        candidate: FallbackCandidate, terminal: PhysicsState,
        witness_input: TickInput, witness: PhysicsState) -> bool:
    if (type(candidate) is not FallbackCandidate
            or type(terminal) is not PhysicsState
            or type(witness_input) is not TickInput
            or type(witness) is not PhysicsState):
        raise ContractViolation("fallback witness rule requires typed values")
    if candidate.terminal_mode is not FallbackTerminalMode.SNEAK_EDGE_STABLE:
        return False
    return (not any((witness_input.forward, witness_input.strafe))
            and not witness_input.jump and not witness_input.sneak
            and not witness_input.sprint and witness.on_ground
            and witness.movement_tick_id == terminal.movement_tick_id + 1
            and not witness.sneaking
            and math.hypot(witness.velocity_blocks_per_tick[0],
                           witness.velocity_blocks_per_tick[2]) <= 1.0e-9
            and all(abs(actual - expected) <= 1.0e-9
                    for actual, expected in zip(witness.position,
                                                terminal.position)))


@dataclass(frozen=True, slots=True)
class FallbackTailProof:
    """A parent boundary's main-suffix reference and exact fallback segment.

    Only the fallback states are stored here. A committed boundary continues
    the parent's inputs up to recovery_boundary_index before using this segment.
    """

    boundary_index: int
    recovery_boundary_index: int
    candidate: FallbackCandidate
    inputs: tuple[TickInput, ...]
    trajectory: tuple[PhysicsState, ...]
    dependencies: tuple[BlockPos, ...]
    resource_incomplete_reasons: tuple[str, ...]
    expected_damage_points: float
    release_witness_input: TickInput | None = None
    release_witness_state: PhysicsState | None = None
    main_suffix_digest: str | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        require_nonnegative_int(self.boundary_index, "fallback boundary index")
        require_nonnegative_int(self.recovery_boundary_index, "fallback recovery boundary index")
        deferred = self.recovery_boundary_index > self.boundary_index
        if self.recovery_boundary_index < self.boundary_index:
            raise ContractViolation("fallback recovery cannot precede its boundary")
        if deferred:
            if (type(self.main_suffix_digest) is not str
                    or len(self.main_suffix_digest) != 64
                    or any(value not in "0123456789abcdef" for value in self.main_suffix_digest)):
                raise ContractViolation("committed fallback requires its exact main suffix digest")
        elif self.main_suffix_digest is not None:
            raise ContractViolation("immediate fallback cannot carry a main suffix digest")
        if (type(self.candidate) is not FallbackCandidate
                or type(self.inputs) is not tuple
                or not 1 <= len(self.inputs) <= self.candidate.maximum_ticks
                or any(type(value) is not TickInput for value in self.inputs)
                or type(self.trajectory) is not tuple
                or len(self.trajectory) != len(self.inputs) + 1
                or any(type(value) is not PhysicsState for value in self.trajectory)):
            raise ContractViolation("fallback tail requires a bounded exact replay")
        if any(not self.candidate.matches_materialized(value, self.trajectory[index])
               for index, value in enumerate(self.inputs)):
            raise ContractViolation("fallback tail inputs differ from the selected candidate")
        first = self.trajectory[0]
        if any(
                state.session != first.session
                or state.movement_tick_id != first.movement_tick_id + index
                for index, state in enumerate(self.trajectory)):
            raise ContractViolation("fallback tail trajectory is not contiguous")
        if not fallback_terminal_body_ready(self.candidate, self.trajectory[-1]):
            raise ContractViolation("fallback tail terminal body is not safely stopped")
        if self.dependencies != tuple(sorted(set(self.dependencies))):
            raise ContractViolation("fallback dependencies must be ordered and unique")
        if self.resource_incomplete_reasons != tuple(sorted(set(
                self.resource_incomplete_reasons))):
            raise ContractViolation("fallback resource reasons must be ordered and unique")
        if (type(self.expected_damage_points) not in (int, float)
                or not math.isfinite(float(self.expected_damage_points))
                or self.expected_damage_points < 0.0):
            raise ContractViolation("fallback expected damage must be finite and nonnegative")
        witnessed = self.release_witness_input is not None
        if witnessed != (self.release_witness_state is not None):
            raise ContractViolation("fallback release witness fields must be present together")
        if self.candidate.terminal_mode is FallbackTerminalMode.SNEAK_EDGE_STABLE:
            if not witnessed:
                raise ContractViolation("sneak-edge fallback requires a neutral release witness")
            assert self.release_witness_input is not None
            terminal = self.trajectory[-1]
            witness = self.release_witness_state
            assert witness is not None
            if (not fallback_terminal_body_ready(self.candidate, terminal)
                    or not fallback_release_witness_body_ready(
                        self.candidate, terminal,
                        self.release_witness_input, witness)):
                raise ContractViolation("sneak-edge fallback witness is detached")
        elif witnessed:
            raise ContractViolation("standard fallback cannot carry a release witness")


def main_suffix_digest(inputs: tuple[TickInput, ...],
                       trajectory: tuple[PhysicsState, ...]) -> str:
    """Bind a compact reference to exact parent values during proof creation."""
    value = (tuple(asdict(item) for item in inputs),
             tuple(asdict(state) for state in trajectory))
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         allow_nan=False, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProvenFallbackSelection:
    candidate: FallbackCandidate
    generation_id: str
    owner_id: str
    route_id: str
    action_index: int
    input_start_index: int | None
    first_effect_tick: int | None
    permitted_window: CandidateExecutionWindow | None
    release_witness_input: TickInput | None = None
    release_witness_state: PhysicsState | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        if type(self.candidate) is not FallbackCandidate:
            raise ContractViolation("proved fallback selection requires its declared candidate")
        require_identifier(self.generation_id, "fallback generation id")
        require_identifier(self.owner_id, "fallback owner id")
        require_identifier(self.route_id, "fallback route id")
        require_nonnegative_int(self.action_index, "fallback action index")
        applied = self.input_start_index is not None
        if applied != (self.first_effect_tick is not None) or applied != (self.permitted_window is not None):
            raise ContractViolation("fallback effect index, tick and window must be present together")
        if applied:
            require_nonnegative_int(self.input_start_index, "fallback input start index")
            require_nonnegative_int(self.first_effect_tick, "fallback first effect tick")
            if type(self.permitted_window) is not CandidateExecutionWindow:
                raise ContractViolation("proved fallback selection requires a typed window")
            if (self.permitted_window.earliest_start_tick != self.first_effect_tick
                    or self.permitted_window.latest_start_tick != self.first_effect_tick):
                raise ContractViolation("proved fallback window must name the exact first effect tick")
        witnessed = self.release_witness_input is not None
        if witnessed != (self.release_witness_state is not None):
            raise ContractViolation("fallback release witness input and state must be present together")
        if self.candidate.terminal_mode is FallbackTerminalMode.SNEAK_EDGE_STABLE:
            if not witnessed:
                raise ContractViolation("sneak-edge fallback requires a neutral release witness")
            if (type(self.release_witness_input) is not TickInput
                    or type(self.release_witness_state) is not PhysicsState
                    or any((self.release_witness_input.forward,
                            self.release_witness_input.strafe))
                    or self.release_witness_input.jump
                    or self.release_witness_input.sneak
                    or self.release_witness_input.sprint):
                raise ContractViolation("sneak-edge release witness must use neutral input")
        elif witnessed:
            raise ContractViolation("standard fallback cannot carry an edge release witness")

    @property
    def applied(self) -> bool:
        return self.input_start_index is not None
