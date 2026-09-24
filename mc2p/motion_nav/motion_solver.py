"""B10-B bounded command search for the first one-cell gap capability."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from pathlib import Path

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, ProjectionStatus, StateAnchor,
    project_movement_command,
)
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsState, TickInput,
)
from mc2p.motion_nav.world_model import BlockPos


SOLVER_ID = "r4-moving-one-cell-gap-command-search-v1"
_PROJECTION_ID = "mc2p.input-projection.v1"
_CARDINAL_DIRECTIONS = frozenset({(0, 1), (1, 0), (0, -1), (-1, 0)})
_STABLE_SPEED_LIMIT_PER_TICK = 0.01
_HEADING_TOLERANCE_RADIANS = math.radians(1.0)


@dataclass(frozen=True, slots=True)
class GapCommandTemplate:
    pre_jump_forward_ticks: int
    post_jump_axis: int
    post_jump_ticks: int

    def __post_init__(self) -> None:
        if (type(self.pre_jump_forward_ticks) is not int
                or not 0 <= self.pre_jump_forward_ticks <= 4):
            raise ContractViolation("gap pre-jump ticks must be within 0..4")
        if type(self.post_jump_axis) is not int or self.post_jump_axis not in {-1, 0, 1}:
            raise ContractViolation("gap post-jump axis must be -1, 0 or 1")
        if (type(self.post_jump_ticks) is not int
                or not 0 <= self.post_jump_ticks <= 6
                or (self.post_jump_axis == 0) != (self.post_jump_ticks == 0)):
            raise ContractViolation("gap post-jump axis and duration disagree")


@dataclass(frozen=True, slots=True)
class GapSolverPolicy:
    solver_id: str
    maximum_entry_speed_blocks_per_second: float
    direction_check_minimum_speed_blocks_per_second: float
    maximum_velocity_heading_error_degrees: float
    templates: tuple[GapCommandTemplate, ...]

    def __post_init__(self) -> None:
        require_identifier(self.solver_id, "gap solver policy id")
        values = (
            self.maximum_entry_speed_blocks_per_second,
            self.direction_check_minimum_speed_blocks_per_second,
            self.maximum_velocity_heading_error_degrees,
        )
        if any(type(value) not in (int, float) or not math.isfinite(float(value))
               for value in values):
            raise ContractViolation("gap solver policy values must be finite")
        if (self.maximum_entry_speed_blocks_per_second <= 0
                or self.direction_check_minimum_speed_blocks_per_second < 0
                or self.direction_check_minimum_speed_blocks_per_second
                   > self.maximum_entry_speed_blocks_per_second
                or not 0 < self.maximum_velocity_heading_error_degrees <= 45):
            raise ContractViolation("gap solver policy speed or direction range is invalid")
        if (type(self.templates) is not tuple or not self.templates
                or len(self.templates) > 64
                or any(type(value) is not GapCommandTemplate for value in self.templates)
                or len(set(self.templates)) != len(self.templates)):
            raise ContractViolation("gap solver policy templates must be bounded and unique")


DEFAULT_GAP_SOLVER_POLICY = GapSolverPolicy(
    SOLVER_ID,
    3.0,
    0.5,
    5.0,
    (
        GapCommandTemplate(0, 1, 1),
        GapCommandTemplate(0, 0, 0),
        GapCommandTemplate(0, -1, 1),
        GapCommandTemplate(0, -1, 2),
        GapCommandTemplate(0, -1, 3),
        GapCommandTemplate(0, -1, 4),
        GapCommandTemplate(0, -1, 5),
        GapCommandTemplate(1, 0, 0),
        GapCommandTemplate(1, -1, 1),
        GapCommandTemplate(1, -1, 2),
        GapCommandTemplate(1, -1, 3),
        GapCommandTemplate(1, -1, 4),
    ),
)


def load_gap_solver_policy(path: Path) -> GapSolverPolicy:
    if not isinstance(path, Path):
        raise ContractViolation("gap solver policy path must be a Path")
    try:
        value = json.loads(path.read_text("utf-8"))["verified_gap_solver"]
        return GapSolverPolicy(
            value["solver_id"],
            value["maximum_entry_speed_blocks_per_second"],
            value["direction_check_minimum_speed_blocks_per_second"],
            value["maximum_velocity_heading_error_degrees"],
            tuple(GapCommandTemplate(
                item["pre_jump_forward_ticks"],
                item["post_jump_axis"],
                item["post_jump_ticks"],
            ) for item in value["templates"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, ContractViolation):
            raise
        raise ContractViolation("gap solver policy document is invalid") from error


class SolveStatus(StrEnum):
    SOLVED = "solved"
    HARD_CONFLICT = "hard_conflict"
    NEEDS_WORLD = "needs_world"
    NEEDS_STATE = "needs_state"
    UNSUPPORTED = "unsupported"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_SOLUTION_WITHIN_SEARCH = "no_solution_within_search"
    INVALID_INPUT = "invalid_input"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class LandingRegion:
    min_x: float
    max_x: float
    min_z: float
    max_z: float
    surface_y: float

    def __post_init__(self) -> None:
        values = (self.min_x, self.max_x, self.min_z, self.max_z, self.surface_y)
        if any(type(value) not in (int, float) or not math.isfinite(float(value))
               for value in values):
            raise ContractViolation("landing region must be finite")
        if self.max_x <= self.min_x or self.max_z <= self.min_z:
            raise ContractViolation("landing region must have positive area")

    def contains(self, state: PhysicsState, *, epsilon: float = 1.0e-7) -> bool:
        x, y, z = state.position
        return (self.min_x - epsilon <= x <= self.max_x + epsilon
                and self.min_z - epsilon <= z <= self.max_z + epsilon
                and math.isclose(y, self.surface_y, abs_tol=epsilon))


@dataclass(frozen=True, slots=True)
class GapSolveRequest:
    direction: tuple[int, int]
    landing: LandingRegion
    execution_window: CandidateExecutionWindow
    max_candidates: int = 12
    max_ticks: int = 20
    exit_direction: tuple[int, int] | None = None
    exit_motion_ticks: int = 0
    policy: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY

    def __post_init__(self) -> None:
        if self.direction not in _CARDINAL_DIRECTIONS:
            raise ContractViolation("B10 gap solver requires a cardinal direction")
        if type(self.landing) is not LandingRegion:
            raise ContractViolation("gap solver requires a landing region")
        if type(self.execution_window) is not CandidateExecutionWindow:
            raise ContractViolation("gap solver requires an execution window")
        if (self.execution_window.latest_start_tick
                - self.execution_window.earliest_start_tick > 1):
            raise ContractViolation(
                "gap solver start window must cover at most two movement ticks"
            )
        if type(self.policy) is not GapSolverPolicy:
            raise ContractViolation("gap solver requires a typed policy")
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= 64:
            raise ContractViolation("gap solver candidate budget must be within 1..64")
        if type(self.max_ticks) is not int or not 12 <= self.max_ticks <= 60:
            raise ContractViolation("gap solver tick budget must be within 12..60")
        if self.exit_direction is None:
            if self.exit_motion_ticks != 0:
                raise ContractViolation("stable gap exit cannot request exit motion")
        elif (self.exit_direction not in _CARDINAL_DIRECTIONS
              or type(self.exit_motion_ticks) is not int
              or not 1 <= self.exit_motion_ticks <= 4):
            raise ContractViolation(
                "moving gap exit requires a cardinal direction and 1..4 ticks"
            )


@dataclass(frozen=True, slots=True)
class MotionCommandTick:
    movement: MovementV1
    required_movement_yaw_radians: float

    def __post_init__(self) -> None:
        if type(self.movement) is not MovementV1:
            raise ContractViolation("motion command requires MovementV1")
        if (type(self.required_movement_yaw_radians) not in (int, float)
                or not math.isfinite(float(self.required_movement_yaw_radians))):
            raise ContractViolation("motion command yaw must be finite")


@dataclass(frozen=True, slots=True)
class VerifiedMotionStartVariant:
    """One fully checked command replay for a concrete first application tick."""

    start_tick: int
    entry_state: PhysicsState
    tick_inputs: tuple[TickInput, ...]
    trajectory: tuple[PhysicsState, ...]
    step_events: tuple[tuple[str, ...], ...]
    exit_state: PhysicsState
    release_safe_command_indices: tuple[int, ...]
    world_dependencies: tuple[BlockPos, ...]
    resource_incomplete_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonnegative_int(self.start_tick, "verified start tick")
        count = len(self.tick_inputs)
        if (count == 0 or len(self.step_events) != count
                or len(self.trajectory) != count + 1):
            raise ContractViolation("verified start variant records disagree")
        if (self.trajectory[0] != self.entry_state
                or self.trajectory[-1] != self.exit_state):
            raise ContractViolation("verified start variant is detached")
        if self.entry_state.movement_tick_id + 1 != self.start_tick:
            raise ContractViolation("verified start variant uses another tick")
        if (self.release_safe_command_indices
                != tuple(sorted(set(self.release_safe_command_indices)))
                or any(type(index) is not int or not 0 <= index < count
                       for index in self.release_safe_command_indices)):
            raise ContractViolation("verified start release indices are invalid")
        if self.world_dependencies != tuple(sorted(set(self.world_dependencies))):
            raise ContractViolation("verified start dependencies must be unique")
        if (self.resource_incomplete_reasons
                != tuple(sorted(set(self.resource_incomplete_reasons)))):
            raise ContractViolation("verified start resource reasons must be unique")


@dataclass(frozen=True, slots=True)
class TrajectoryValidation:
    accepted: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise ContractViolation("trajectory validation requires a boolean result")
        if self.accepted == bool(self.reasons):
            raise ContractViolation("trajectory validation result and reasons disagree")


@dataclass(frozen=True, slots=True)
class VerifiedMotionResult:
    solver_id: str
    solver_policy: GapSolverPolicy
    ruleset_id: str
    input_projection_version: str
    anchor_observation_sequence_id: int
    anchor_movement_tick_id: int
    entry_state: PhysicsState
    commands: tuple[MotionCommandTick, ...]
    tick_inputs: tuple[TickInput, ...]
    trajectory: tuple[PhysicsState, ...]
    step_events: tuple[tuple[str, ...], ...]
    exit_state: PhysicsState
    landing: LandingRegion
    release_safe_command_indices: tuple[int, ...]
    world_dependencies: tuple[BlockPos, ...]
    resource_incomplete_reasons: tuple[str, ...]
    execution_window: CandidateExecutionWindow
    direction: tuple[int, int]
    exit_direction: tuple[int, int] | None
    exit_motion_ticks: int
    delayed_start_variants: tuple[VerifiedMotionStartVariant, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.solver_id, "solver id")
        if (type(self.solver_policy) is not GapSolverPolicy
                or self.solver_policy.solver_id != self.solver_id):
            raise ContractViolation("verified motion solver policy is detached")
        require_identifier(self.ruleset_id, "ruleset id")
        require_identifier(self.input_projection_version, "input projection version")
        count = len(self.commands)
        if (count == 0 or len(self.tick_inputs) != count
                or len(self.step_events) != count
                or len(self.trajectory) != count + 1):
            raise ContractViolation("verified motion records have different horizons")
        if self.trajectory[0] != self.entry_state or self.trajectory[-1] != self.exit_state:
            raise ContractViolation("verified motion entry or exit is detached")
        if self.entry_state.session != self.exit_state.session:
            raise ContractViolation("verified motion crosses world sessions")
        if self.anchor_movement_tick_id != self.entry_state.movement_tick_id:
            raise ContractViolation("verified motion anchor tick does not match entry")
        if (self.release_safe_command_indices
                != tuple(sorted(set(self.release_safe_command_indices)))
                or any(type(index) is not int or not 0 <= index < count
                       for index in self.release_safe_command_indices)):
            raise ContractViolation("release-safe command indices are invalid")
        if self.world_dependencies != tuple(sorted(set(self.world_dependencies))):
            raise ContractViolation("world dependencies must be sorted and unique")
        expected_delayed_ticks = tuple(range(
            self.execution_window.earliest_start_tick + 1,
            self.execution_window.latest_start_tick + 1,
        ))
        actual_delayed_ticks = tuple(
            variant.start_tick for variant in self.delayed_start_variants
        )
        if actual_delayed_ticks != expected_delayed_ticks:
            raise ContractViolation("verified motion start window is incomplete")
        if any(
            len(variant.tick_inputs) != count
            or variant.entry_state.session != self.entry_state.session
            or not set(variant.world_dependencies).issubset(self.world_dependencies)
            or not set(variant.resource_incomplete_reasons).issubset(
                self.resource_incomplete_reasons
            )
            for variant in self.delayed_start_variants
        ):
            raise ContractViolation("verified delayed start is detached")
        if self.direction not in _CARDINAL_DIRECTIONS:
            raise ContractViolation("verified gap motion requires a cardinal direction")
        if self.exit_direction is None:
            if self.exit_motion_ticks != 0:
                raise ContractViolation("stable verified motion cannot carry exit ticks")
        elif (self.exit_direction not in _CARDINAL_DIRECTIONS
              or type(self.exit_motion_ticks) is not int
              or not 1 <= self.exit_motion_ticks <= 4):
            raise ContractViolation("verified moving exit is invalid")

    def start_variant(self, start_tick: int) -> VerifiedMotionStartVariant | None:
        require_nonnegative_int(start_tick, "verified start tick")
        if start_tick == self.execution_window.earliest_start_tick:
            return VerifiedMotionStartVariant(
                start_tick,
                self.entry_state,
                self.tick_inputs,
                self.trajectory,
                self.step_events,
                self.exit_state,
                self.release_safe_command_indices,
                self.world_dependencies,
                self.resource_incomplete_reasons,
            )
        return next((
            variant for variant in self.delayed_start_variants
            if variant.start_tick == start_tick
        ), None)


@dataclass(frozen=True, slots=True)
class SolveResult:
    status: SolveStatus
    proof: VerifiedMotionResult | None = None
    candidates_evaluated: int = 0
    missing_cells: tuple[BlockPos, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not SolveStatus:
            raise ContractViolation("invalid solve status")
        if (self.status is SolveStatus.SOLVED) != (type(self.proof) is VerifiedMotionResult):
            raise ContractViolation("only a solved result can carry a proof")
        if type(self.candidates_evaluated) is not int or self.candidates_evaluated < 0:
            raise ContractViolation("invalid evaluated candidate count")


def _angle_error(a: float, b: float) -> float:
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def _required_yaw(direction: tuple[int, int]) -> float:
    dx, dz = direction
    return math.atan2(-dx, dz)


def _entry_check(anchor: StateAnchor, world: PhysicsWorldView,
                 request: GapSolveRequest) -> SolveResult | None:
    if (type(anchor) is not StateAnchor or type(world) is not PhysicsWorldView
            or type(request) is not GapSolveRequest):
        raise ContractViolation("gap solving requires an anchor, world and request")
    state = anchor.physics_state
    if (world.session != anchor.session or state.session != anchor.session
            or anchor.ruleset_id != JAVA_1_21_RULESET.ruleset_id
            or world.ruleset != JAVA_1_21_RULESET):
        return SolveResult(SolveStatus.INVALID_INPUT,
                           reasons=("session_or_ruleset_mismatch",))
    if anchor.input_projection_version != _PROJECTION_ID:
        return SolveResult(SolveStatus.UNSUPPORTED,
                           reasons=("input_projection_version",))
    if state.is_using_item:
        return SolveResult(SolveStatus.UNSUPPORTED,
                           reasons=("item_slowdown_not_supported",))
    if (state.pose != "standing" or state.swimming or state.climbing
            or state.fall_flying or state.flying):
        return SolveResult(SolveStatus.UNSUPPORTED,
                           reasons=("entry_movement_mode",))
    if not state.on_ground or state.jumping_cooldown_ticks != 0:
        return SolveResult(SolveStatus.NEEDS_STATE,
                           reasons=("grounded_jump_ready",))
    if state.food_points < 7:
        return SolveResult(SolveStatus.NEEDS_STATE,
                           reasons=("sprint_eligibility",))
    horizontal_speed = math.hypot(
        state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2],
    )
    if (horizontal_speed
            > request.policy.maximum_entry_speed_blocks_per_second / 20.0
              + 1.0e-9):
        return SolveResult(SolveStatus.NEEDS_STATE,
                           reasons=("entry_speed_outside_trial",))
    if (horizontal_speed
            >= request.policy.direction_check_minimum_speed_blocks_per_second
               / 20.0 - 1.0e-9):
        velocity_yaw = math.atan2(
            -state.velocity_blocks_per_tick[0],
            state.velocity_blocks_per_tick[2],
        )
        if (_angle_error(velocity_yaw, _required_yaw(request.direction))
                > math.radians(
                    request.policy.maximum_velocity_heading_error_degrees,
                ) + 1.0e-9):
            return SolveResult(SolveStatus.NEEDS_STATE,
                               reasons=("entry_velocity_direction",))
    if _angle_error(state.yaw_radians, _required_yaw(request.direction)) \
            > _HEADING_TOLERANCE_RADIANS:
        return SolveResult(SolveStatus.NEEDS_STATE,
                           reasons=("heading_alignment",))
    if not math.isclose(state.position[1], request.landing.surface_y, abs_tol=1.0e-7):
        return SolveResult(SolveStatus.HARD_CONFLICT,
                           reasons=("landing_level",))
    return None


def validate_gap_trajectory(
        trajectory: tuple[PhysicsState, ...],
        step_events: tuple[tuple[str, ...], ...],
        request: GapSolveRequest) -> TrajectoryValidation:
    if (type(trajectory) is not tuple or not trajectory
            or any(type(state) is not PhysicsState for state in trajectory)
            or type(step_events) is not tuple
            or any(type(events) is not tuple for events in step_events)
            or len(step_events) != len(trajectory) - 1
            or type(request) is not GapSolveRequest):
        raise ContractViolation("invalid trajectory validation input")
    reasons: list[str] = []
    flattened = tuple(event for events in step_events for event in events)
    if "takeoff" not in flattened or "left_ground" not in flattened:
        reasons.append("no_observed_takeoff")
    if "landed" not in flattened:
        reasons.append("no_observed_landing")
    if any(state.horizontal_collision for state in trajectory[1:]):
        reasons.append("horizontal_collision")
    if any(
        "vertical_collision" in events
        and trajectory[index].velocity_blocks_per_tick[1] > 0.0
        for index, events in enumerate(step_events)
    ):
        reasons.append("ceiling_collision")
    exit_state = trajectory[-1]
    if not exit_state.on_ground:
        reasons.append("not_grounded_at_exit")
    if not request.landing.contains(exit_state):
        reasons.append("landing_recovery_margin")
    speed = math.hypot(
        exit_state.velocity_blocks_per_tick[0],
        exit_state.velocity_blocks_per_tick[2],
    )
    maximum_exit_speed = (
        _STABLE_SPEED_LIMIT_PER_TICK
        if request.exit_direction is None else .22
    )
    if speed > maximum_exit_speed + 1.0e-9:
        reasons.append("exit_speed")
    if (request.exit_direction is not None
            and _angle_error(exit_state.yaw_radians,
                             _required_yaw(request.exit_direction))
            > _HEADING_TOLERANCE_RADIANS):
        reasons.append("exit_heading")
    return TrajectoryValidation(not reasons, tuple(reasons))


def _release_recovery_evidence(
        trajectory: tuple[PhysicsState, ...], world: PhysicsWorldView,
        request: GapSolveRequest,
) -> tuple[
        tuple[int, ...], tuple[BlockPos, ...], tuple[str, ...],
        SolveStatus | None, tuple[BlockPos, ...], tuple[str, ...],
]:
    """Prove that releasing every remaining input still reaches safe support."""
    safe: list[int] = []
    dependencies: set[BlockPos] = set()
    resource_reasons: set[str] = set()
    for index, release_state in enumerate(trajectory[:-1]):
        release_speed = math.hypot(
            release_state.velocity_blocks_per_tick[0],
            release_state.velocity_blocks_per_tick[2],
        )
        if (release_state.on_ground
                and math.isclose(
                    release_state.position[1], request.landing.surface_y,
                    abs_tol=1.0e-7,
                )
                and release_speed <= _STABLE_SPEED_LIMIT_PER_TICK + 1.0e-9):
            safe.append(index)
            continue
        current = release_state
        recovered = False
        for _ in range(request.max_ticks):
            neutral = TickInput(
                0.0, 0.0, False, False, False, current.yaw_radians,
            )
            calculated = step(current, neutral, world, JAVA_1_21_RULESET)
            dependencies.update(calculated.dependencies)
            if calculated.status is CalculationStatus.NEEDS_WORLD:
                return (
                    tuple(safe), tuple(sorted(dependencies)),
                    tuple(sorted(resource_reasons)), SolveStatus.NEEDS_WORLD,
                    calculated.missing_cells,
                    ("release_recovery_world_incomplete",),
                )
            if calculated.status is CalculationStatus.UNSUPPORTED:
                return (
                    tuple(safe), tuple(sorted(dependencies)),
                    tuple(sorted(resource_reasons)), SolveStatus.UNSUPPORTED,
                    (), calculated.unsupported_reasons,
                )
            if (calculated.status is not CalculationStatus.OK
                    or calculated.next_state is None):
                return (
                    tuple(safe), tuple(sorted(dependencies)),
                    tuple(sorted(resource_reasons)), SolveStatus.INVALID_INPUT,
                    (), calculated.invalid_reasons
                    or ("release_recovery_physics_invalid",),
                )
            assert calculated.resource_update is not None
            resource_reasons.update(
                calculated.resource_update.incomplete_reasons
            )
            current = calculated.next_state
            if current.horizontal_collision:
                break
            current_speed = math.hypot(
                current.velocity_blocks_per_tick[0],
                current.velocity_blocks_per_tick[2],
            )
            if (current.on_ground
                    and current_speed <= _STABLE_SPEED_LIMIT_PER_TICK + 1.0e-9):
                # Interruption safety is weaker than successful completion:
                # the body may stop near the landing edge, but it must regain
                # known support at the trial's original level without impact.
                recovered = math.isclose(
                    current.position[1], request.landing.surface_y,
                    abs_tol=1.0e-7,
                )
                break
        if recovered:
            safe.append(index)
    return (
        tuple(safe), tuple(sorted(dependencies)),
        tuple(sorted(resource_reasons)), None, (), (),
    )


def _replay_verified_commands(
        entry_state: PhysicsState,
        commands: tuple[MotionCommandTick, ...],
        world: PhysicsWorldView,
        request: GapSolveRequest,
        *, start_tick: int,
        inherited_dependencies: tuple[BlockPos, ...] = (),
        inherited_resource_reasons: tuple[str, ...] = (),
) -> tuple[VerifiedMotionStartVariant | None, SolveResult | None]:
    """Replay one immutable command list from one concrete pre-start state."""
    current = entry_state
    states = [current]
    inputs: list[TickInput] = []
    events: list[tuple[str, ...]] = []
    dependencies = set(inherited_dependencies)
    resource_reasons = set(inherited_resource_reasons)
    for command in commands:
        projected = project_movement_command(
            current, command.movement,
            movement_yaw_radians=command.required_movement_yaw_radians,
        )
        if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
            return None, SolveResult(
                SolveStatus.UNSUPPORTED, reasons=projected.reasons,
            )
        calculated = step(current, projected.tick_input, world, JAVA_1_21_RULESET)
        dependencies.update(calculated.dependencies)
        if calculated.status is CalculationStatus.NEEDS_WORLD:
            return None, SolveResult(
                SolveStatus.NEEDS_WORLD,
                missing_cells=calculated.missing_cells,
                reasons=("trajectory_world_incomplete",),
            )
        if calculated.status is CalculationStatus.UNSUPPORTED:
            return None, SolveResult(
                SolveStatus.UNSUPPORTED,
                reasons=calculated.unsupported_reasons,
            )
        if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
            return None, SolveResult(
                SolveStatus.INVALID_INPUT,
                reasons=calculated.invalid_reasons or ("physics_step_invalid",),
            )
        assert calculated.resource_update is not None
        resource_reasons.update(calculated.resource_update.incomplete_reasons)
        inputs.append(projected.tick_input)
        events.append(calculated.events)
        current = calculated.next_state
        states.append(current)
    trajectory = tuple(states)
    step_events = tuple(events)
    validation = validate_gap_trajectory(trajectory, step_events, request)
    if not validation.accepted:
        return None, SolveResult(
            SolveStatus.NEEDS_STATE,
            reasons=("start_window_commands_failed", *validation.reasons),
        )
    (release_safe, release_dependencies, release_resource_reasons,
     release_status, release_missing, release_reasons) = \
        _release_recovery_evidence(trajectory, world, request)
    if release_status is not None:
        return None, SolveResult(
            release_status,
            missing_cells=release_missing,
            reasons=release_reasons,
        )
    if len(release_safe) != len(commands):
        return None, SolveResult(
            SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
            reasons=("start_window_release_recovery_not_safe",),
        )
    dependencies.update(release_dependencies)
    resource_reasons.update(release_resource_reasons)
    return VerifiedMotionStartVariant(
        start_tick,
        entry_state,
        tuple(inputs),
        trajectory,
        step_events,
        trajectory[-1],
        release_safe,
        tuple(sorted(dependencies)),
        tuple(sorted(resource_reasons)),
    ), None


def _prove_delayed_starts(
        anchor: StateAnchor,
        commands: tuple[MotionCommandTick, ...],
        world: PhysicsWorldView,
        request: GapSolveRequest,
) -> tuple[
        tuple[VerifiedMotionStartVariant, ...],
        tuple[BlockPos, ...], tuple[str, ...], SolveResult | None,
]:
    window = request.execution_window
    if window.earliest_start_tick != anchor.movement_tick_id + 1:
        return (), (), (), SolveResult(
            SolveStatus.INVALID_INPUT,
            reasons=("execution_window_detached_from_anchor",),
        )
    current = anchor.physics_state
    prelude_dependencies: set[BlockPos] = set()
    prelude_resource_reasons: set[str] = set()
    variants: list[VerifiedMotionStartVariant] = []
    for start_tick in range(
            window.earliest_start_tick + 1,
            window.latest_start_tick + 1):
        target_entry_tick = start_tick - 1
        while current.movement_tick_id < target_entry_tick:
            projected = project_movement_command(
                current, MovementV1(),
                movement_yaw_radians=current.yaw_radians,
            )
            if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
                return (), (), (), SolveResult(
                    SolveStatus.UNSUPPORTED, reasons=projected.reasons,
                )
            calculated = step(
                current, projected.tick_input, world, JAVA_1_21_RULESET,
            )
            prelude_dependencies.update(calculated.dependencies)
            if calculated.status is CalculationStatus.NEEDS_WORLD:
                return (), (), (), SolveResult(
                    SolveStatus.NEEDS_WORLD,
                    missing_cells=calculated.missing_cells,
                    reasons=("start_window_world_incomplete",),
                )
            if calculated.status is CalculationStatus.UNSUPPORTED:
                return (), (), (), SolveResult(
                    SolveStatus.UNSUPPORTED,
                    reasons=calculated.unsupported_reasons,
                )
            if (calculated.status is not CalculationStatus.OK
                    or calculated.next_state is None):
                return (), (), (), SolveResult(
                    SolveStatus.INVALID_INPUT,
                    reasons=calculated.invalid_reasons
                    or ("start_window_physics_invalid",),
                )
            assert calculated.resource_update is not None
            prelude_resource_reasons.update(
                calculated.resource_update.incomplete_reasons
            )
            current = calculated.next_state
            if (not current.on_ground or current.horizontal_collision
                    or not math.isclose(
                        current.position[1], request.landing.surface_y,
                        abs_tol=1.0e-7,
                    )):
                return (), (), (), SolveResult(
                    SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
                    reasons=("start_window_neutral_prelude_not_safe",),
                )
        variant, failure = _replay_verified_commands(
            current, commands, world, request,
            start_tick=start_tick,
            inherited_dependencies=tuple(sorted(prelude_dependencies)),
            inherited_resource_reasons=tuple(sorted(prelude_resource_reasons)),
        )
        if failure is not None:
            return (), (), (), failure
        assert variant is not None
        variants.append(variant)
    dependencies = tuple(sorted({
        dependency
        for variant in variants
        for dependency in variant.world_dependencies
    }))
    resource_reasons = tuple(sorted({
        reason
        for variant in variants
        for reason in variant.resource_incomplete_reasons
    }))
    return tuple(variants), dependencies, resource_reasons, None


def _candidate_templates(request: GapSolveRequest):
    yield from request.policy.templates


def revalidate_gap_motion(
        proof: VerifiedMotionResult, anchor: StateAnchor,
        world: PhysicsWorldView,
        execution_window: CandidateExecutionWindow) -> SolveResult:
    """Replay one old command sequence from a fresh anchor without searching.

    This is the bounded control-side check used when a background result arrives
    after its source observation.  It never changes the command sequence and it
    never explores alternative timings.
    """
    if (type(proof) is not VerifiedMotionResult
            or type(anchor) is not StateAnchor
            or type(world) is not PhysicsWorldView
            or type(execution_window) is not CandidateExecutionWindow):
        raise ContractViolation("gap revalidation requires proof, anchor, world and window")
    if (anchor.session != world.session
            or anchor.ruleset_id != proof.ruleset_id
            or anchor.state_schema != proof.entry_state.state_schema
            or anchor.input_projection_version != proof.input_projection_version):
        return SolveResult(
            SolveStatus.INVALID_INPUT,
            reasons=("revalidation_identity_changed",),
        )
    request = GapSolveRequest(
        proof.direction, proof.landing, execution_window,
        max_candidates=1, max_ticks=max(12, len(proof.commands)),
        exit_direction=proof.exit_direction,
        exit_motion_ticks=proof.exit_motion_ticks,
        policy=proof.solver_policy,
    )
    rejected = _entry_check(anchor, world, request)
    if rejected is not None:
        return rejected
    primary, failure = _replay_verified_commands(
        anchor.physics_state,
        proof.commands,
        world,
        request,
        start_tick=execution_window.earliest_start_tick,
    )
    if failure is not None:
        if failure.status in {
                SolveStatus.NEEDS_STATE,
                SolveStatus.NO_SOLUTION_WITHIN_SEARCH}:
            return SolveResult(
                SolveStatus.NEEDS_STATE,
                missing_cells=failure.missing_cells,
                reasons=(
                    "entry_state_outside_revalidation_envelope",
                    "revalidated_commands_failed",
                    *failure.reasons,
                ),
            )
        return failure
    assert primary is not None
    (delayed_variants, delayed_dependencies, delayed_resource_reasons,
     delayed_failure) = _prove_delayed_starts(
        anchor, proof.commands, world, request,
    )
    if delayed_failure is not None:
        return delayed_failure
    dependencies = set(primary.world_dependencies)
    dependencies.update(delayed_dependencies)
    resource_reasons = set(primary.resource_incomplete_reasons)
    resource_reasons.update(delayed_resource_reasons)
    refreshed = VerifiedMotionResult(
        proof.solver_id, proof.solver_policy,
        proof.ruleset_id, proof.input_projection_version,
        anchor.observation_sequence_id, anchor.movement_tick_id,
        anchor.physics_state, proof.commands, primary.tick_inputs,
        primary.trajectory, primary.step_events, primary.exit_state,
        proof.landing, primary.release_safe_command_indices,
        tuple(sorted(dependencies)), tuple(sorted(resource_reasons)),
        execution_window, proof.direction, proof.exit_direction,
        proof.exit_motion_ticks, delayed_variants,
    )
    return SolveResult(SolveStatus.SOLVED, refreshed, candidates_evaluated=0)


def solve_one_cell_gap(anchor: StateAnchor, world: PhysicsWorldView,
                       request: GapSolveRequest) -> SolveResult:
    rejected = _entry_check(anchor, world, request)
    if rejected is not None:
        return rejected
    evaluated = 0
    saw_validation_failure = False
    saw_release_recovery_failure = False
    for template in _candidate_templates(request):
        if evaluated >= request.max_candidates:
            return SolveResult(
                SolveStatus.BUDGET_EXHAUSTED,
                candidates_evaluated=evaluated,
                reasons=("candidate_budget",),
            )
        evaluated += 1
        current = anchor.physics_state
        commands: list[MotionCommandTick] = []
        inputs: list[TickInput] = []
        states = [current]
        events: list[tuple[str, ...]] = []
        dependencies: set[BlockPos] = set()
        resource_reasons: set[str] = set()
        incomplete: SolveResult | None = None
        observed_airborne = False
        observed_landing = False
        exit_motion_applied = 0
        for tick in range(request.max_ticks):
            if observed_landing and request.exit_direction is not None:
                command = MotionCommandTick(
                    MovementV1(forward=1),
                    _required_yaw(request.exit_direction),
                )
                exit_motion_applied += 1
            else:
                if tick < template.pre_jump_forward_ticks:
                    movement = MovementV1(forward=1, sprint=True)
                elif tick == template.pre_jump_forward_ticks:
                    movement = MovementV1(forward=1, jump=True, sprint=True)
                elif (tick <= template.pre_jump_forward_ticks
                              + template.post_jump_ticks):
                    movement = MovementV1(
                        forward=template.post_jump_axis,
                        sprint=template.post_jump_axis == 1,
                    )
                else:
                    movement = MovementV1()
                command = MotionCommandTick(
                    movement, _required_yaw(request.direction),
                )
            commands.append(command)
            projected = project_movement_command(
                current, command.movement,
                movement_yaw_radians=command.required_movement_yaw_radians,
            )
            if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
                incomplete = SolveResult(
                    SolveStatus.UNSUPPORTED, candidates_evaluated=evaluated,
                    reasons=projected.reasons,
                )
                break
            calculated = step(current, projected.tick_input, world, JAVA_1_21_RULESET)
            dependencies.update(calculated.dependencies)
            if calculated.status is CalculationStatus.NEEDS_WORLD:
                incomplete = SolveResult(
                    SolveStatus.NEEDS_WORLD, candidates_evaluated=evaluated,
                    missing_cells=calculated.missing_cells,
                    reasons=("trajectory_world_incomplete",),
                )
                break
            if calculated.status is CalculationStatus.UNSUPPORTED:
                incomplete = SolveResult(
                    SolveStatus.UNSUPPORTED, candidates_evaluated=evaluated,
                    reasons=calculated.unsupported_reasons,
                )
                break
            if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
                incomplete = SolveResult(
                    SolveStatus.INVALID_INPUT, candidates_evaluated=evaluated,
                    reasons=calculated.invalid_reasons or ("physics_step_invalid",),
                )
                break
            inputs.append(projected.tick_input)
            events.append(calculated.events)
            assert calculated.resource_update is not None
            resource_reasons.update(calculated.resource_update.incomplete_reasons)
            current = calculated.next_state
            states.append(current)
            if not current.on_ground:
                observed_airborne = True
            if observed_airborne and current.on_ground:
                observed_landing = True
            if (observed_landing and request.exit_direction is not None
                    and exit_motion_applied >= request.exit_motion_ticks):
                break
        if incomplete is not None:
            return incomplete
        trajectory = tuple(states)
        step_events = tuple(events)
        validation = validate_gap_trajectory(trajectory, step_events, request)
        if not validation.accepted:
            saw_validation_failure = True
            continue
        (release_safe, release_dependencies, release_resource_reasons,
         release_status, release_missing, release_reasons) = \
            _release_recovery_evidence(trajectory, world, request)
        if release_status is not None:
            return SolveResult(
                release_status, candidates_evaluated=evaluated,
                missing_cells=release_missing, reasons=release_reasons,
            )
        if len(release_safe) != len(commands):
            saw_release_recovery_failure = True
            continue
        dependencies.update(release_dependencies)
        resource_reasons.update(release_resource_reasons)
        (delayed_variants, delayed_dependencies, delayed_resource_reasons,
         delayed_failure) = _prove_delayed_starts(
            anchor, tuple(commands), world, request,
        )
        if delayed_failure is not None:
            if delayed_failure.status in {
                    SolveStatus.NEEDS_STATE,
                    SolveStatus.NO_SOLUTION_WITHIN_SEARCH}:
                saw_validation_failure = True
                continue
            return SolveResult(
                delayed_failure.status,
                candidates_evaluated=evaluated,
                missing_cells=delayed_failure.missing_cells,
                reasons=delayed_failure.reasons,
            )
        dependencies.update(delayed_dependencies)
        resource_reasons.update(delayed_resource_reasons)
        proof = VerifiedMotionResult(
            request.policy.solver_id, request.policy,
            JAVA_1_21_RULESET.ruleset_id, _PROJECTION_ID,
            anchor.observation_sequence_id, anchor.movement_tick_id,
            anchor.physics_state, tuple(commands), tuple(inputs), trajectory,
            step_events, trajectory[-1], request.landing,
            release_safe,
            tuple(sorted(dependencies)), tuple(sorted(resource_reasons)),
            request.execution_window, request.direction,
            request.exit_direction, request.exit_motion_ticks,
            delayed_variants,
        )
        return SolveResult(
            SolveStatus.SOLVED, proof=proof, candidates_evaluated=evaluated,
        )
    return SolveResult(
        SolveStatus.NO_SOLUTION_WITHIN_SEARCH,
        candidates_evaluated=evaluated,
        reasons=(
            ("release_recovery_not_safe",)
            if saw_release_recovery_failure else
            ("validated_candidates_failed",)
            if saw_validation_failure else ("empty_candidate_space",)
        ),
    )
