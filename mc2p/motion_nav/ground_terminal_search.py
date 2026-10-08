"""Pure bounded multi-tick search for ordinary-ground terminal completion."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import itertools
import json
import math
import time
from typing import Callable, Iterable

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.async_work import AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.ground_candidate_verifier import (
    CLOSED_GROUND_MOVEMENTS,
    GroundCandidateSafety,
    GroundIncrementalStep,
    GroundSequenceReplay,
    advance_verified_ground_command,
    verify_ground_command_sequence,
)
from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, control_world_direction,
)
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.online_motion import CandidateExecutionWindow, StateAnchor
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import Aabb, BlockPos, WorldQueryCache


BEAM_WIDTH = 512
MAX_PREPARATION_TICKS = 32
GROUND_TERMINAL_BEAM_WORST_CASE_NS = 500_000_000
GROUND_TERMINAL_TICK_NS = 50_000_000
GROUND_TERMINAL_DELIVERY_MARGIN_TICKS = 1
GROUND_TERMINAL_BEAM_LEAD_TICKS = (
    math.ceil(GROUND_TERMINAL_BEAM_WORST_CASE_NS / GROUND_TERMINAL_TICK_NS)
    + GROUND_TERMINAL_DELIVERY_MARGIN_TICKS
)
GROUND_TERMINAL_BEAM_PREPARATION_TICKS = GROUND_TERMINAL_BEAM_LEAD_TICKS - 1
GROUND_TERMINAL_SEARCH_VERSION = "ground-terminal-search-v1"
_EPSILON = 1.0e-9
_NEUTRAL = MovementV1()
_MOVEMENT_ORDER = tuple(sorted(
    CLOSED_GROUND_MOVEMENTS,
    key=lambda value: (value == _NEUTRAL, value.forward, value.strafe),
))


class _GroundSearchStopped(Exception):
    def __init__(self, status: "GroundTerminalSearchStatus") -> None:
        super().__init__(status.value)
        self.status = status


class GroundTerminalSearchStatus(StrEnum):
    SOLVED = "solved"
    ALREADY_SATISFIED = "already_satisfied"
    NEEDS_INFORMATION = "needs_information"
    UNSUPPORTED_ENTRY = "unsupported_entry"
    NO_PROVED_SEQUENCE = "no_proved_sequence"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INSUFFICIENT_LEAD = "insufficient_lead"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    STALE = "stale"
    INTERNAL_ERROR = "internal_error"


class GroundTerminalSearchLayer(StrEnum):
    PHASE = "phase"
    BEAM = "beam"


class GroundTerminalPrimitive(StrEnum):
    TARGET_X = "target_x"
    TARGET_Z = "target_z"
    BRAKE_X = "brake_x"
    BRAKE_Z = "brake_z"
    TARGET_XZ = "target_xz"
    BRAKE_XZ = "brake_xz"
    TARGET_X_BRAKE_Z = "target_x_brake_z"
    BRAKE_X_TARGET_Z = "brake_x_target_z"
    NEUTRAL = "neutral"


_PHASE_TEMPLATES: tuple[tuple[GroundTerminalPrimitive, ...], ...] = (
    (GroundTerminalPrimitive.TARGET_X,),
    (GroundTerminalPrimitive.BRAKE_X_TARGET_Z,),
    (GroundTerminalPrimitive.TARGET_X_BRAKE_Z,
     GroundTerminalPrimitive.BRAKE_X_TARGET_Z),
    (GroundTerminalPrimitive.TARGET_X, GroundTerminalPrimitive.TARGET_Z,
     GroundTerminalPrimitive.NEUTRAL),
)


@dataclass(frozen=True, slots=True)
class GroundTerminalSearchLimits:
    maximum_ticks: int = 8
    neutral_tail_ticks: int = 30
    phase_candidate_budget: int = 4096
    beam_node_budget: int = 32768
    maximum_final_speed_blocks_per_second: float = 0.1
    minimum_support_fraction: float = 0.15
    beam_width: int = BEAM_WIDTH
    deadline_ns: int | None = None

    def __post_init__(self) -> None:
        if type(self.maximum_ticks) is not int or not 1 <= self.maximum_ticks <= 64:
            raise ContractViolation("ground terminal maximum ticks must be within 1..64")
        if (type(self.neutral_tail_ticks) is not int or self.neutral_tail_ticks < 1
                or type(self.phase_candidate_budget) is not int
                or self.phase_candidate_budget < 1
                or type(self.beam_node_budget) is not int
                or self.beam_node_budget < 1):
            raise ContractViolation("ground terminal budgets must be positive")
        if self.beam_width != BEAM_WIDTH:
            raise ContractViolation("ground terminal beam width is fixed at 512")
        for value, name in (
            (self.maximum_final_speed_blocks_per_second, "final speed"),
            (self.minimum_support_fraction, "minimum support"),
        ):
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ContractViolation(f"ground terminal {name} must be finite")
        if (self.maximum_final_speed_blocks_per_second < 0
                or not 0 < self.minimum_support_fraction <= 1):
            raise ContractViolation("ground terminal physical limits are invalid")
        if self.deadline_ns is not None and (
                type(self.deadline_ns) is not int or self.deadline_ns < 0):
            raise ContractViolation("ground terminal deadline is invalid")


@dataclass(frozen=True, slots=True)
class GroundTerminalSolveRequest:
    anchor: StateAnchor
    work_identity: AsyncWorkIdentity
    goal_id: str
    goal_revision: int
    route_id: str
    route_revision: int
    action_index: int
    execution_window: CandidateExecutionWindow
    frame: NavigationFrame
    completion: GroundCompletionRegion
    profile: GroundMotionProfile
    limits: GroundTerminalSearchLimits
    preparation_inputs: tuple[MovementV1, ...] = ()
    route_corridor: tuple[Aabb, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.anchor) is not StateAnchor
                or type(self.work_identity) is not AsyncWorkIdentity
                or self.work_identity.work_kind is not AsyncWorkKind.MOTION_SOLVE
                or type(self.execution_window) is not CandidateExecutionWindow
                or type(self.frame) is not NavigationFrame
                or type(self.completion) is not GroundCompletionRegion
                or type(self.profile) is not GroundMotionProfile
                or type(self.limits) is not GroundTerminalSearchLimits):
            raise ContractViolation("ground terminal request requires typed inputs")
        require_identifier(self.goal_id, "ground terminal goal")
        require_identifier(self.route_id, "ground terminal route")
        if any(type(value) is not int or value < 0 for value in (
                self.goal_revision, self.route_revision, self.action_index)):
            raise ContractViolation("ground terminal revisions and action index must be nonnegative")
        if self.goal_revision < 1 or self.route_revision < 1:
            raise ContractViolation("ground terminal revisions must be positive")
        if (type(self.preparation_inputs) is not tuple
                or any(type(value) is not MovementV1
                       or value.jump or value.sprint or value.sneak
                       for value in self.preparation_inputs)
                or len(self.preparation_inputs) > MAX_PREPARATION_TICKS
                or type(self.route_corridor) is not tuple
                or any(type(value) is not Aabb for value in self.route_corridor)):
            raise ContractViolation("ground terminal preparation and corridor must be typed")
        state = self.anchor.physics_state
        expected_start = self.anchor.movement_tick_id + 1 + len(self.preparation_inputs)
        if (self.anchor.session != self.frame.session
                or state.session != self.frame.session
                or self.work_identity.world_session_id != self.frame.session.value
                or state.ruleset_id != JAVA_1_21_RULESET.ruleset_id
                or self.execution_window.earliest_start_tick != expected_start
                or self.execution_window.latest_start_tick != expected_start + 1):
            raise ContractViolation("ground terminal identities or execution window disagree")


@dataclass(frozen=True, slots=True)
class GroundTerminalPrefixTail:
    prefix_ticks: int
    safe: bool
    trajectory: tuple[PhysicsState, ...]
    stopped_end: PhysicsState
    dependencies: tuple[BlockPos, ...]
    minimum_support: float

    def __post_init__(self) -> None:
        if (type(self.prefix_ticks) is not int or self.prefix_ticks < 0
                or self.safe is not True
                or type(self.trajectory) is not tuple or not self.trajectory
                or any(type(state) is not PhysicsState for state in self.trajectory)
                or type(self.stopped_end) is not PhysicsState
                or self.trajectory[-1] != self.stopped_end
                or type(self.dependencies) is not tuple
                or self.dependencies != tuple(sorted(set(self.dependencies)))
                or type(self.minimum_support) not in (int, float)
                or not 0 < self.minimum_support <= 1):
            raise ContractViolation("ground terminal prefix tail proof is incomplete")


@dataclass(frozen=True, slots=True)
class GroundTerminalBranchProof:
    delay_ticks: int
    entry_state: PhysicsState
    command_trajectory: tuple[PhysicsState, ...]
    prefix_tails: tuple[GroundTerminalPrefixTail, ...]
    stopped_end: PhysicsState
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        if (self.delay_ticks not in (0, 1)
                or type(self.entry_state) is not PhysicsState
                or type(self.command_trajectory) is not tuple
                or not self.command_trajectory
                or self.command_trajectory[0] != self.entry_state
                or any(type(state) is not PhysicsState
                       for state in self.command_trajectory)
                or type(self.prefix_tails) is not tuple
                or tuple(value.prefix_ticks for value in self.prefix_tails)
                    != tuple(range(len(self.prefix_tails)))
                or not self.prefix_tails
                or type(self.stopped_end) is not PhysicsState
                or self.prefix_tails[-1].stopped_end != self.stopped_end
                or self.dependencies != tuple(sorted(set(self.dependencies)))):
            raise ContractViolation("ground terminal branch proof is incomplete")


def _movement_payload(value: MovementV1) -> tuple:
    return (value.forward, value.strafe, value.jump, value.sneak, value.sprint)


def _branch_payload(value: GroundTerminalBranchProof) -> dict:
    return {
        "delay": value.delay_ticks,
        "entry": value.entry_state.to_mapping(),
        "commands": [state.to_mapping() for state in value.command_trajectory],
        "tails": [{
            "prefix": tail.prefix_ticks,
            "trajectory": [state.to_mapping() for state in tail.trajectory],
            "stopped": tail.stopped_end.to_mapping(),
            "dependencies": tail.dependencies,
            "support": tail.minimum_support,
        } for tail in value.prefix_tails],
        "stopped": value.stopped_end.to_mapping(),
        "dependencies": value.dependencies,
    }


def _ground_terminal_sequence_id(
    *,
    layer: GroundTerminalSearchLayer,
    work_identity: AsyncWorkIdentity,
    anchor: StateAnchor,
    goal_id: str,
    goal_revision: int,
    route_id: str,
    route_revision: int,
    action_index: int,
    execution_window: CandidateExecutionWindow,
    preparation_inputs: tuple[MovementV1, ...],
    route_corridor: tuple[Aabb, ...],
    commands: tuple[MovementV1, ...],
    normal: GroundTerminalBranchProof,
    late1: GroundTerminalBranchProof,
    dependencies: tuple[BlockPos, ...],
    completion: GroundCompletionRegion,
    cost_ticks: int,
    primitive_ids: tuple[GroundTerminalPrimitive, ...],
    ruleset_id: str,
) -> str:
    payload = json.dumps({
        "version": GROUND_TERMINAL_SEARCH_VERSION,
        "layer": layer.value,
        "work": work_identity.key,
        "anchor": {
            "observation": anchor.observation_sequence_id,
            "tick": anchor.movement_tick_id,
            "phase": anchor.phase.value,
            "projection": anchor.input_projection_version,
            "ruleset": anchor.ruleset_id,
            "schema": anchor.state_schema,
            "state": anchor.physics_state.to_mapping(),
        },
        "goal": (goal_id, goal_revision),
        "route": (route_id, route_revision, action_index),
        "window": (execution_window.earliest_start_tick,
                   execution_window.latest_start_tick),
        "completion": {
            "bounds": completion.bounds.as_tuple(),
            "reference": completion.reference_point,
            "support_height": completion.support_height,
            "surface": completion.surface_identity,
            "dependencies": completion.dependencies,
        },
        "preparation": [_movement_payload(value) for value in preparation_inputs],
        "corridor": [value.as_tuple() for value in route_corridor],
        "commands": [_movement_payload(value) for value in commands],
        "primitives": [value.value for value in primitive_ids],
        "normal": _branch_payload(normal),
        "late1": _branch_payload(late1),
        "dependencies": dependencies,
        "cost": cost_ticks,
        "ruleset": ruleset_id,
    }, sort_keys=True, separators=(",", ":"))
    return "ground-terminal:" + sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GroundTerminalSequence:
    sequence_id: str
    layer: GroundTerminalSearchLayer
    work_identity: AsyncWorkIdentity
    anchor: StateAnchor
    goal_id: str
    goal_revision: int
    route_id: str
    route_revision: int
    action_index: int
    execution_window: CandidateExecutionWindow
    preparation_inputs: tuple[MovementV1, ...]
    route_corridor: tuple[Aabb, ...]
    commands: tuple[MovementV1, ...]
    normal: GroundTerminalBranchProof
    late1: GroundTerminalBranchProof
    dependencies: tuple[BlockPos, ...]
    completion: GroundCompletionRegion
    cost_ticks: int
    primitive_ids: tuple[GroundTerminalPrimitive, ...]
    ruleset_id: str = JAVA_1_21_RULESET.ruleset_id

    def __post_init__(self) -> None:
        require_identifier(self.goal_id, "ground terminal sequence goal")
        require_identifier(self.route_id, "ground terminal sequence route")
        if (not self.sequence_id.startswith("ground-terminal:")
                or type(self.layer) is not GroundTerminalSearchLayer
                or type(self.work_identity) is not AsyncWorkIdentity
                or type(self.anchor) is not StateAnchor
                or type(self.execution_window) is not CandidateExecutionWindow
                or type(self.preparation_inputs) is not tuple
                or any(type(value) is not MovementV1
                       for value in self.preparation_inputs)
                or type(self.route_corridor) is not tuple
                or any(type(value) is not Aabb for value in self.route_corridor)
                or type(self.commands) is not tuple
                or any(type(value) is not MovementV1 for value in self.commands)
                or not self.commands or self.cost_ticks != len(self.commands)
                or type(self.goal_revision) is not int or self.goal_revision < 1
                or type(self.route_revision) is not int or self.route_revision < 1
                or type(self.action_index) is not int or self.action_index < 0
                or self.normal.delay_ticks != 0 or self.late1.delay_ticks != 1
                or len(self.normal.prefix_tails) != len(self.commands) + 1
                or len(self.late1.prefix_tails) != len(self.commands) + 1
                or len(self.normal.command_trajectory) != len(self.commands) + 1
                or len(self.late1.command_trajectory) != len(self.commands) + 1
                or not self.dependencies
                or self.dependencies != tuple(sorted(set(self.dependencies)))
                or not set(self.completion.dependencies).issubset(self.dependencies)
                or type(self.primitive_ids) is not tuple
                or any(type(value) is not GroundTerminalPrimitive
                       for value in self.primitive_ids)
                or self.ruleset_id != JAVA_1_21_RULESET.ruleset_id):
            raise ContractViolation("ground terminal sequence proof is incomplete")
        expected = _ground_terminal_sequence_id(
            layer=self.layer, work_identity=self.work_identity,
            anchor=self.anchor, goal_id=self.goal_id,
            goal_revision=self.goal_revision, route_id=self.route_id,
            route_revision=self.route_revision, action_index=self.action_index,
            execution_window=self.execution_window,
            preparation_inputs=self.preparation_inputs,
            route_corridor=self.route_corridor, commands=self.commands,
            normal=self.normal, late1=self.late1,
            dependencies=self.dependencies, completion=self.completion,
            cost_ticks=self.cost_ticks, primitive_ids=self.primitive_ids,
            ruleset_id=self.ruleset_id,
        )
        if self.sequence_id != expected:
            raise ContractViolation("ground terminal sequence identity does not match proof")


@dataclass(frozen=True, slots=True)
class GroundTerminalSearchStats:
    phase_candidates: int = 0
    phase_physics_steps: int = 0
    beam_nodes: int = 0
    beam_physics_steps: int = 0
    beam_maximum_width: int = 0
    beam_generated: int = 0
    beam_deduplicated: int = 0
    beam_kept: int = 0
    beam_hard_safety_rejected: int = 0
    replay_candidates: int = 0
    replay_physics_steps: int = 0
    canonical_proofs: int = 0


@dataclass(frozen=True, slots=True)
class _BeamBranch:
    state: PhysicsState
    dependencies: tuple[BlockPos, ...]
    minimum_support: float


@dataclass(frozen=True, slots=True)
class _BeamNode:
    commands: tuple[MovementV1, ...]
    normal: _BeamBranch
    late1: _BeamBranch

    @property
    def branches(self) -> tuple[_BeamBranch, _BeamBranch]:
        return (self.normal, self.late1)


@dataclass(frozen=True, slots=True)
class GroundTerminalSearchResult:
    status: GroundTerminalSearchStatus
    sequence: GroundTerminalSequence | None = None
    dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()
    reasons: tuple[str, ...] = ()
    stats: GroundTerminalSearchStats = GroundTerminalSearchStats()

    def __post_init__(self) -> None:
        if ((self.status is GroundTerminalSearchStatus.SOLVED)
                != (type(self.sequence) is GroundTerminalSequence)):
            raise ContractViolation("only solved ground terminal search can carry a sequence")
        if (type(self.status) is not GroundTerminalSearchStatus
                or self.dependencies != tuple(sorted(set(self.dependencies)))
                or self.missing_cells != tuple(sorted(set(self.missing_cells)))
                or type(self.reasons) is not tuple
                or any(type(value) is not str for value in self.reasons)
                or type(self.stats) is not GroundTerminalSearchStats):
            raise ContractViolation("ground terminal search result is malformed")


def _inside_corridor(state: PhysicsState, corridor: tuple[Aabb, ...]) -> bool:
    x, y, z = state.position
    return not corridor or any(
        box.min_x <= x <= box.max_x
        and box.min_y <= y <= box.max_y
        and box.min_z <= z <= box.max_z
        for box in corridor
    )


def _speed(state: PhysicsState) -> float:
    return math.hypot(
        state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2],
    ) * 20.0


def _completion_margin(region: GroundCompletionRegion, state: PhysicsState) -> float:
    x, _, z = state.position
    bounds = region.bounds
    return min(x - bounds.min_x, bounds.max_x - x,
               z - bounds.min_z, bounds.max_z - z)


def _complete(request: GroundTerminalSolveRequest, state: PhysicsState) -> bool:
    return (request.completion.contains(state.position)
            and state.pose == "standing" and state.on_ground
            and _speed(state)
                <= request.limits.maximum_final_speed_blocks_per_second + _EPSILON)


def _world_movement(dx: float, dz: float, yaw: float) -> MovementV1:
    length = math.hypot(dx, dz)
    if length <= _EPSILON:
        return _NEUTRAL
    dx, dz = dx / length, dz / length
    best = None
    for movement in _MOVEMENT_ORDER:
        if movement == _NEUTRAL:
            continue
        wx, wz = control_world_direction(GroundControl(
            movement.forward, -movement.strafe, yaw,
        ))
        score = (wx * dx + wz * dz, -abs(movement.forward) - abs(movement.strafe),
                 -movement.forward, -movement.strafe)
        if best is None or score > best[0]:
            best = (score, movement)
    assert best is not None
    return best[1]


def _axis_target(value: float, low: float, high: float) -> float:
    if value < low:
        return low - value
    if value > high:
        return high - value
    return 0.0


def _primitive_movement(
    primitive: GroundTerminalPrimitive,
    state: PhysicsState,
    completion: GroundCompletionRegion,
) -> MovementV1:
    dx = _axis_target(state.position[0], completion.bounds.min_x,
                      completion.bounds.max_x)
    dz = _axis_target(state.position[2], completion.bounds.min_z,
                      completion.bounds.max_z)
    vx, _, vz = state.velocity_blocks_per_tick
    brake_x = 0.0 if abs(vx) <= _EPSILON else -math.copysign(1.0, vx)
    brake_z = 0.0 if abs(vz) <= _EPSILON else -math.copysign(1.0, vz)
    vector = {
        GroundTerminalPrimitive.TARGET_X: (dx, 0.0),
        GroundTerminalPrimitive.TARGET_Z: (0.0, dz),
        GroundTerminalPrimitive.BRAKE_X: (brake_x, 0.0),
        GroundTerminalPrimitive.BRAKE_Z: (0.0, brake_z),
        GroundTerminalPrimitive.TARGET_XZ: (dx, dz),
        GroundTerminalPrimitive.BRAKE_XZ: (brake_x, brake_z),
        GroundTerminalPrimitive.TARGET_X_BRAKE_Z: (dx, brake_z),
        GroundTerminalPrimitive.BRAKE_X_TARGET_Z: (brake_x, dz),
        GroundTerminalPrimitive.NEUTRAL: (0.0, 0.0),
    }[primitive]
    return _world_movement(*vector, state.yaw_radians)


def _duration_vectors_for_total(stage_count: int, total: int) -> Iterable[tuple[int, ...]]:
    for cuts in itertools.combinations(range(1, total), stage_count - 1):
        points = (0,) + cuts + (total,)
        yield tuple(b - a for a, b in zip(points, points[1:]))


def _physics_state_key(state: PhysicsState) -> tuple:
    return (
        tuple(float(value).hex() for value in state.position),
        tuple(float(value).hex() for value in state.velocity_blocks_per_tick),
        float(state.yaw_radians).hex(), state.pose, state.on_ground,
        state.horizontal_collision, state.vertical_collision,
        state.sprinting, state.sneaking, state.fall_distance_blocks,
    )


def _beam_state_key(node: _BeamNode) -> tuple:
    if type(node) is not _BeamNode:
        raise ContractViolation("beam deduplication requires a typed dual branch")
    return (
        tuple(_physics_state_key(branch.state) for branch in node.branches),
        len(node.commands),
        # Depth plus the two branch ticks is the input-projection phase.  The
        # ordinary WALK input itself has no hidden latch once PhysicsState has
        # been produced, so retaining the previous key would defeat valid
        # state deduplication.
        tuple(branch.state.movement_tick_id for branch in node.branches),
        JAVA_1_21_RULESET.ruleset_id,
    )


class _GroundTerminalSolver:
    def __init__(self, request: GroundTerminalSolveRequest,
                 clock_ns: Callable[[], int],
                 stop_check: Callable[[], GroundTerminalSearchStatus | None],
                 ) -> None:
        self.request = request
        self.clock_ns = clock_ns
        self.stop_check = stop_check
        self.phase_candidates = 0
        self.phase_steps = 0
        self.beam_nodes = 0
        self.beam_steps = 0
        self.beam_maximum_width = 0
        self.beam_deduplicated = 0
        self.beam_kept = 0
        self.beam_hard_safety_rejected = 0
        self.replay_candidates = 0
        self.replay_steps = 0
        self.canonical_proofs = 0
        self.dependencies: set[BlockPos] = set(request.completion.dependencies)
        self.missing: set[BlockPos] = set()
        self.query_cache = WorldQueryCache(request.frame.world)

    def _stats(self) -> GroundTerminalSearchStats:
        return GroundTerminalSearchStats(
            self.phase_candidates, self.phase_steps, self.beam_nodes,
            self.beam_steps, self.beam_maximum_width,
            self.beam_nodes, self.beam_deduplicated, self.beam_kept,
            self.beam_hard_safety_rejected, self.replay_candidates,
            self.replay_steps, self.canonical_proofs,
        )

    def _stop_status(self) -> GroundTerminalSearchStatus | None:
        external = self.stop_check()
        if external is not None:
            if external not in {
                    GroundTerminalSearchStatus.CANCELLED,
                    GroundTerminalSearchStatus.STALE,
                    GroundTerminalSearchStatus.TIMEOUT}:
                raise ContractViolation("ground terminal stop callback returned invalid status")
            return external
        deadline = self.request.limits.deadline_ns
        if deadline is not None and self.clock_ns() >= deadline:
            return GroundTerminalSearchStatus.TIMEOUT
        return None

    def _stopped_result(
        self, status: GroundTerminalSearchStatus,
    ) -> GroundTerminalSearchResult:
        return GroundTerminalSearchResult(
            status,
            dependencies=tuple(sorted(self.dependencies)),
            missing_cells=tuple(sorted(self.missing)),
            reasons=(f"ground_terminal_{status.value}",),
            stats=self._stats(),
        )

    def _deadline(self) -> bool:
        return self._stop_status() is not None

    def _checkpoint(self) -> None:
        status = self._stop_status()
        if status is not None:
            raise _GroundSearchStopped(status)

    def _replay_clock(self) -> int:
        self._checkpoint()
        return self.clock_ns()

    def _replay(self, commands: tuple[MovementV1, ...], *, tail_ticks: int,
                layer: GroundTerminalSearchLayer) -> GroundSequenceReplay:
        replay = verify_ground_command_sequence(
            self.request.frame, self.request.anchor.physics_state,
            commands, tail_ticks=tail_ticks,
            minimum_support=self.request.limits.minimum_support_fraction,
            profile=self.request.profile,
            query_cache=self.query_cache,
            deadline_ns=(self.request.limits.deadline_ns
                         if self.request.limits.deadline_ns is not None
                         else 2**63 - 1),
            clock_ns=self._replay_clock,
        )
        self.dependencies.update(replay.dependencies)
        self.missing.update(replay.missing_cells)
        if layer is GroundTerminalSearchLayer.PHASE:
            self.phase_steps += replay.physics_steps
        else:
            self.beam_steps += replay.physics_steps
        return replay

    def _quick_branch(self, commands: tuple[MovementV1, ...], delay: int,
                      layer: GroundTerminalSearchLayer) -> GroundSequenceReplay:
        prefix = self.request.preparation_inputs + ((_NEUTRAL,) if delay else ())
        return self._replay(prefix + commands,
                            tail_ticks=self.request.limits.neutral_tail_ticks,
                            layer=layer)

    def _quick_candidate(self, commands: tuple[MovementV1, ...],
                         layer: GroundTerminalSearchLayer,
                         ) -> tuple[bool, tuple, tuple[GroundSequenceReplay, ...]]:
        branches = tuple(self._quick_branch(commands, delay, layer)
                         for delay in (0, 1))
        if any(branch.status is GroundCandidateSafety.BUDGET_EXHAUSTED
               for branch in branches):
            return False, ("budget",), branches
        if any(branch.status is GroundCandidateSafety.NEEDS_INFORMATION
               for branch in branches):
            return False, ("missing",), branches
        if any(not branch.safe or branch.stopped_end is None
               for branch in branches):
            return False, ("unsafe",), branches
        prep = len(self.request.preparation_inputs)
        for delay, branch in enumerate(branches):
            assert branch.stopped_end is not None
            command_states = branch.control_trajectory[prep + delay:]
            if (not _complete(self.request, branch.stopped_end)
                    or any(not _inside_corridor(state, self.request.route_corridor)
                           for state in command_states)):
                return False, ("incomplete",), branches
        normal, late = branches
        assert normal.stopped_end is not None and late.stopped_end is not None
        margin = min(_completion_margin(self.request.completion, normal.stopped_end),
                     _completion_margin(self.request.completion, late.stopped_end))
        changes = sum(a != b for a, b in zip(commands, commands[1:]))
        key = (len(commands), changes, -margin,
               tuple((value.forward, value.strafe) for value in commands))
        return True, key, branches

    def _commands_for_template(
        self, template: tuple[GroundTerminalPrimitive, ...], durations: tuple[int, ...],
    ) -> tuple[MovementV1, ...] | None:
        commands: tuple[MovementV1, ...] = ()
        state = self.request.anchor.physics_state
        for primitive, duration in zip(template, durations):
            movement = _primitive_movement(primitive, state, self.request.completion)
            commands += (movement,) * duration
            prefix = self._replay(
                self.request.preparation_inputs + commands,
                tail_ticks=0, layer=GroundTerminalSearchLayer.PHASE,
            )
            if prefix.tracking_end is None:
                return None
            if prefix.status in {
                    GroundCandidateSafety.NEEDS_INFORMATION,
                    GroundCandidateSafety.UNSUPPORTED,
                    GroundCandidateSafety.BUDGET_EXHAUSTED}:
                return None
            state = prefix.tracking_end
        return commands

    def _prefix_tail(
        self, commands: tuple[MovementV1, ...], delay: int, prefix_ticks: int,
        layer: GroundTerminalSearchLayer,
    ) -> GroundTerminalPrefixTail | None:
        prefix = self.request.preparation_inputs + ((_NEUTRAL,) if delay else ())
        replay = self._replay(
            prefix + commands[:prefix_ticks],
            tail_ticks=self.request.limits.neutral_tail_ticks,
            layer=layer,
        )
        if not replay.safe or replay.stopped_end is None:
            return None
        return GroundTerminalPrefixTail(
            prefix_ticks, True, replay.neutral_tail, replay.stopped_end,
            replay.dependencies, replay.minimum_support,
        )

    def _branch_proof(
        self, commands: tuple[MovementV1, ...], delay: int,
        layer: GroundTerminalSearchLayer,
    ) -> GroundTerminalBranchProof | None:
        prefix = self.request.preparation_inputs + ((_NEUTRAL,) if delay else ())
        full = self._replay(
            prefix + commands,
            tail_ticks=self.request.limits.neutral_tail_ticks,
            layer=layer,
        )
        if not full.safe or full.stopped_end is None:
            return None
        entry_index = len(prefix)
        if len(full.control_trajectory) <= entry_index:
            return None
        tails = []
        dependencies = set(full.dependencies)
        for prefix_ticks in range(len(commands) + 1):
            tail = self._prefix_tail(commands, delay, prefix_ticks, layer)
            if tail is None:
                return None
            tails.append(tail)
            dependencies.update(tail.dependencies)
        return GroundTerminalBranchProof(
            delay, full.control_trajectory[entry_index],
            full.control_trajectory[entry_index:], tuple(tails),
            full.stopped_end, tuple(sorted(dependencies)),
        )

    def _sequence(
        self, commands: tuple[MovementV1, ...],
        primitives: tuple[GroundTerminalPrimitive, ...],
        layer: GroundTerminalSearchLayer,
    ) -> GroundTerminalSequence | None:
        normal = self._branch_proof(commands, 0, layer)
        late = self._branch_proof(commands, 1, layer)
        if normal is None or late is None:
            return None
        if not _complete(self.request, normal.stopped_end) or not _complete(
                self.request, late.stopped_end):
            return None
        dependencies = tuple(sorted(
            set(self.request.completion.dependencies)
            | set(normal.dependencies) | set(late.dependencies)
        ))
        identity = _ground_terminal_sequence_id(
            layer=layer, work_identity=self.request.work_identity,
            anchor=self.request.anchor, goal_id=self.request.goal_id,
            goal_revision=self.request.goal_revision,
            route_id=self.request.route_id,
            route_revision=self.request.route_revision,
            action_index=self.request.action_index,
            execution_window=self.request.execution_window,
            preparation_inputs=self.request.preparation_inputs,
            route_corridor=self.request.route_corridor, commands=commands,
            normal=normal, late1=late, dependencies=dependencies,
            completion=self.request.completion, cost_ticks=len(commands),
            primitive_ids=primitives, ruleset_id=JAVA_1_21_RULESET.ruleset_id,
        )
        return GroundTerminalSequence(
            identity, layer, self.request.work_identity, self.request.anchor,
            self.request.goal_id, self.request.goal_revision,
            self.request.route_id, self.request.route_revision,
            self.request.action_index, self.request.execution_window,
            self.request.preparation_inputs, self.request.route_corridor,
            commands, normal, late,
            dependencies, self.request.completion, len(commands), primitives,
        )

    def phase(self) -> GroundTerminalSearchResult | None:
        for stage_count in (1, 2, 3):
            templates = tuple(template for template in _PHASE_TEMPLATES
                              if len(template) == stage_count)
            # Cost ticks is the first stable ordering key.  Once one complete
            # total length has a solution, longer sequences cannot win and do
            # not need to be evaluated.
            for total in range(stage_count, self.request.limits.maximum_ticks + 1):
                solved = []
                for template in templates:
                    for durations in _duration_vectors_for_total(stage_count, total):
                        self._checkpoint()
                        if self.phase_candidates >= (
                                self.request.limits.phase_candidate_budget):
                            return GroundTerminalSearchResult(
                                GroundTerminalSearchStatus.BUDGET_EXHAUSTED,
                                dependencies=tuple(sorted(self.dependencies)),
                                missing_cells=tuple(sorted(self.missing)),
                                reasons=("phase_search_budget_exhausted",),
                                stats=self._stats(),
                            )
                        self.phase_candidates += 1
                        commands = self._commands_for_template(template, durations)
                        if commands is None:
                            continue
                        eligible, key, _ = self._quick_candidate(
                            commands, GroundTerminalSearchLayer.PHASE,
                        )
                        if eligible:
                            solved.append((key, template, commands))
                if not solved:
                    continue
                solved.sort(key=lambda value: (value[0],
                    tuple(item.value for item in value[1])))
                for _, template, commands in solved:
                    sequence = self._sequence(
                        commands, template, GroundTerminalSearchLayer.PHASE,
                    )
                    if sequence is not None:
                        return GroundTerminalSearchResult(
                            GroundTerminalSearchStatus.SOLVED, sequence,
                            sequence.dependencies, stats=self._stats(),
                        )
        return None

    def _initial_beam_node(self) -> _BeamNode | None:
        branches: list[_BeamBranch] = []
        for delay in (0, 1):
            self._checkpoint()
            prefix = self.request.preparation_inputs + (
                (_NEUTRAL,) if delay else ()
            )
            replay = self._replay(
                prefix, tail_ticks=0, layer=GroundTerminalSearchLayer.BEAM,
            )
            if replay.status is GroundCandidateSafety.NEEDS_INFORMATION:
                self.missing.update(replay.missing_cells)
                return None
            if (not replay.safe or replay.tracking_end is None
                    or not _inside_corridor(
                        replay.tracking_end, self.request.route_corridor)):
                self.beam_hard_safety_rejected += 1
                return None
            branches.append(_BeamBranch(
                replay.tracking_end, replay.dependencies,
                replay.minimum_support,
            ))
        return _BeamNode((), branches[0], branches[1])

    def _advance_beam_branch(
        self, branch: _BeamBranch, movement: MovementV1,
    ) -> _BeamBranch | None:
        self._checkpoint()
        advanced = advance_verified_ground_command(
            self.request.frame, branch.state, movement,
            minimum_support=self.request.limits.minimum_support_fraction,
            previous_minimum_support=branch.minimum_support,
            previous_dependencies=branch.dependencies,
            profile=self.request.profile,
            query_cache=self.query_cache,
            deadline_ns=(self.request.limits.deadline_ns
                         if self.request.limits.deadline_ns is not None
                         else 2**63 - 1),
            clock_ns=self._replay_clock,
        )
        self.beam_steps += advanced.physics_steps
        self.dependencies.update(advanced.dependencies)
        self.missing.update(advanced.missing_cells)
        if (not advanced.safe or advanced.state is None
                or not _inside_corridor(
                    advanced.state, self.request.route_corridor)):
            self.beam_hard_safety_rejected += 1
            return None
        return _BeamBranch(
            advanced.state, advanced.dependencies, advanced.minimum_support,
        )

    def _completion_nearby(self, node: _BeamNode) -> bool:
        bounds = self.request.completion.bounds
        # Ordinary ground retains at most 0.6*0.91 horizontal velocity each
        # tick.  This geometric series is a conservative upper bound on how
        # far a released key can carry the branch before it settles.
        retention = .6 * .91
        for branch in node.branches:
            state = branch.state
            distance = math.hypot(
                _axis_target(state.position[0], bounds.min_x, bounds.max_x),
                _axis_target(state.position[2], bounds.min_z, bounds.max_z),
            )
            per_tick = math.hypot(
                state.velocity_blocks_per_tick[0],
                state.velocity_blocks_per_tick[2],
            )
            if distance > per_tick / (1.0 - retention) + .05:
                return False
        return True

    def _node_score(self, node: _BeamNode) -> tuple:
        bounds = self.request.completion.bounds
        branch_scores = []
        for branch in node.branches:
            state = branch.state
            branch_scores.append((
                math.hypot(
                    _axis_target(state.position[0], bounds.min_x, bounds.max_x),
                    _axis_target(state.position[2], bounds.min_z, bounds.max_z),
                ),
                _speed(state),
            ))
        changes = sum(
            a != b for a, b in zip(node.commands, node.commands[1:])
        )
        return (
            max(value[0] for value in branch_scores),
            max(value[1] for value in branch_scores),
            changes,
            tuple((item.forward, item.strafe) for item in node.commands),
        )

    def beam(self) -> GroundTerminalSearchResult:
        lead_ticks = (self.request.execution_window.earliest_start_tick
                      - self.request.anchor.movement_tick_id)
        if lead_ticks < GROUND_TERMINAL_BEAM_LEAD_TICKS:
            return GroundTerminalSearchResult(
                GroundTerminalSearchStatus.INSUFFICIENT_LEAD,
                dependencies=tuple(sorted(self.dependencies)),
                missing_cells=tuple(sorted(self.missing)),
                reasons=("beam_requires_bounded_delivery_lead",), stats=self._stats(),
            )
        initial = self._initial_beam_node()
        if initial is None:
            status = (GroundTerminalSearchStatus.NEEDS_INFORMATION
                      if self.missing else
                      GroundTerminalSearchStatus.NO_PROVED_SEQUENCE)
            return GroundTerminalSearchResult(
                status, dependencies=tuple(sorted(self.dependencies)),
                missing_cells=tuple(sorted(self.missing)),
                reasons=("beam_initial_state_rejected",), stats=self._stats(),
            )
        frontier: tuple[_BeamNode, ...] = (initial,)
        for depth in range(1, self.request.limits.maximum_ticks + 1):
            scored_by_state: dict[tuple, tuple[tuple, _BeamNode]] = {}
            replay_nodes: dict[tuple, tuple[tuple, _BeamNode]] = {}
            complete_depth = True
            for prefix in frontier:
                for movement in _MOVEMENT_ORDER:
                    self._checkpoint()
                    if self.beam_nodes >= self.request.limits.beam_node_budget:
                        complete_depth = False
                        break
                    self.beam_nodes += 1
                    normal = self._advance_beam_branch(prefix.normal, movement)
                    if normal is None:
                        continue
                    late1 = self._advance_beam_branch(prefix.late1, movement)
                    if late1 is None:
                        continue
                    node = _BeamNode(
                        prefix.commands + (movement,), normal, late1,
                    )
                    state_key = _beam_state_key(node)
                    score = self._node_score(node)
                    candidate = (score, node)
                    previous = scored_by_state.get(state_key)
                    if previous is not None:
                        self.beam_deduplicated += 1
                    if previous is None or score < previous[0]:
                        scored_by_state[state_key] = candidate
                    if self._completion_nearby(node):
                        old = replay_nodes.get(state_key)
                        if old is None or score < old[0]:
                            replay_nodes[state_key] = candidate
                if not complete_depth:
                    break
            if not complete_depth:
                return GroundTerminalSearchResult(
                    GroundTerminalSearchStatus.BUDGET_EXHAUSTED,
                    dependencies=tuple(sorted(self.dependencies)),
                    missing_cells=tuple(sorted(self.missing)),
                    reasons=("beam_search_budget_exhausted",), stats=self._stats(),
                )
            if replay_nodes:
                for _, node in sorted(replay_nodes.values())[:BEAM_WIDTH]:
                    self._checkpoint()
                    self.replay_candidates += 1
                    before = self.beam_steps
                    eligible, _, _ = self._quick_candidate(
                        node.commands, GroundTerminalSearchLayer.BEAM,
                    )
                    self.replay_steps += self.beam_steps - before
                    if not eligible:
                        continue
                    self._checkpoint()
                    self.canonical_proofs += 1
                    sequence = self._sequence(
                        node.commands, (), GroundTerminalSearchLayer.BEAM,
                    )
                    if sequence is not None:
                        return GroundTerminalSearchResult(
                            GroundTerminalSearchStatus.SOLVED, sequence,
                            sequence.dependencies, stats=self._stats(),
                        )
            scored = sorted(scored_by_state.values())
            frontier = tuple(node for _, node in scored[:BEAM_WIDTH])
            self.beam_kept += len(frontier)
            self.beam_maximum_width = max(self.beam_maximum_width, len(frontier))
            if not frontier:
                break
        status = (GroundTerminalSearchStatus.NEEDS_INFORMATION
                  if self.missing else GroundTerminalSearchStatus.NO_PROVED_SEQUENCE)
        return GroundTerminalSearchResult(
            status, dependencies=tuple(sorted(self.dependencies)),
            missing_cells=tuple(sorted(self.missing)),
            reasons=("no_proved_ground_terminal_sequence",), stats=self._stats(),
        )


def solve_ground_terminal_sequence(
    request: GroundTerminalSolveRequest,
    *,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    stop_check: Callable[[], GroundTerminalSearchStatus | None] = lambda: None,
) -> GroundTerminalSearchResult:
    if type(request) is not GroundTerminalSolveRequest:
        raise ContractViolation("ground terminal solve requires a typed request")
    if not callable(stop_check):
        raise ContractViolation("ground terminal stop callback must be callable")
    solver = _GroundTerminalSolver(request, clock_ns, stop_check)
    try:
        solver._checkpoint()
        if _complete(request, request.anchor.physics_state):
            return GroundTerminalSearchResult(
                GroundTerminalSearchStatus.ALREADY_SATISFIED,
                dependencies=request.completion.dependencies,
                reasons=("entry_already_satisfies_completion",),
            )
        initial = solver._replay((), tail_ticks=request.limits.neutral_tail_ticks,
                                 layer=GroundTerminalSearchLayer.PHASE)
        if initial.status is GroundCandidateSafety.NEEDS_INFORMATION:
            return GroundTerminalSearchResult(
                GroundTerminalSearchStatus.NEEDS_INFORMATION,
                dependencies=initial.dependencies, missing_cells=initial.missing_cells,
                reasons=(initial.reason,), stats=solver._stats(),
            )
        if initial.status in {GroundCandidateSafety.UNSUPPORTED,
                              GroundCandidateSafety.UNSAFE}:
            return GroundTerminalSearchResult(
                GroundTerminalSearchStatus.UNSUPPORTED_ENTRY,
                dependencies=initial.dependencies, missing_cells=initial.missing_cells,
                reasons=(initial.reason,), stats=solver._stats(),
            )
        if initial.status is GroundCandidateSafety.BUDGET_EXHAUSTED:
            status = solver._stop_status()
            return (solver._stopped_result(status) if status is not None else
                    GroundTerminalSearchResult(
                        GroundTerminalSearchStatus.BUDGET_EXHAUSTED,
                        reasons=(initial.reason,), stats=solver._stats(),
                    ))
        phase = solver.phase()
        if phase is not None:
            return phase
        if solver.missing:
            return GroundTerminalSearchResult(
                GroundTerminalSearchStatus.NEEDS_INFORMATION,
                dependencies=tuple(sorted(solver.dependencies)),
                missing_cells=tuple(sorted(solver.missing)),
                reasons=("phase_search_needs_information",),
                stats=solver._stats(),
            )
        return solver.beam()
    except _GroundSearchStopped as stopped:
        return solver._stopped_result(stopped.status)


__all__ = [
    "BEAM_WIDTH", "MAX_PREPARATION_TICKS", "GROUND_TERMINAL_SEARCH_VERSION",
    "GROUND_TERMINAL_BEAM_WORST_CASE_NS", "GROUND_TERMINAL_BEAM_LEAD_TICKS",
    "GROUND_TERMINAL_BEAM_PREPARATION_TICKS",
    "GroundTerminalBranchProof", "GroundTerminalPrefixTail",
    "GroundTerminalPrimitive", "GroundTerminalSearchLayer",
    "GroundTerminalSearchLimits", "GroundTerminalSearchResult",
    "GroundTerminalSearchStats", "GroundTerminalSearchStatus",
    "GroundTerminalSequence", "GroundTerminalSolveRequest",
    "solve_ground_terminal_sequence",
]
