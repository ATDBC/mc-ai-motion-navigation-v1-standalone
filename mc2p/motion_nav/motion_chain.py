"""Small immutable contracts shared by motion proof and later handoff stages.

This module declares which bounded control pattern a caller permits as a
fallback.  It does not choose a successor, own a worker, or grant execution
authority.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math
from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.contracts.motion_activation import validate_frozen_value
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from mc2p.motion_nav.physics_types import PhysicsState, TickInput
from mc2p.motion_nav.world_model import BlockPos


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
    """One selected, completely replayed fallback from one command boundary."""

    boundary_index: int
    candidate: FallbackCandidate
    inputs: tuple[TickInput, ...]
    trajectory: tuple[PhysicsState, ...]
    dependencies: tuple[BlockPos, ...]
    resource_incomplete_reasons: tuple[str, ...]
    expected_damage_points: float
    release_witness_input: TickInput | None = None
    release_witness_state: PhysicsState | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        require_nonnegative_int(self.boundary_index, "fallback boundary index")
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
