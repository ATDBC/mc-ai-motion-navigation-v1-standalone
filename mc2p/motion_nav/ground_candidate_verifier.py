"""Bounded formal verification for the closed ordinary-ground control family."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math
import time
from typing import Callable

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.block_motion_traits import unsupported_motion_cells
from mc2p.motion_nav.geometry import (
    QueryStatus, query_support, required_cells_for_sweep, sweep,
)
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_tracking_policy import GroundTrackingContext
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.safe_ground_control import (
    CachedGroundPhysicsWorld,
    VerifiedGroundRouteCandidate,
    ground_route_state_matches,
)
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge, WorldQueryCache


CLOSED_GROUND_MOVEMENTS = tuple(
    MovementV1(forward=forward, strafe=strafe)
    for forward in (-1, 0, 1)
    for strafe in (-1, 0, 1)
)
_CLOSED_KEYS = frozenset(
    (movement.forward, movement.strafe) for movement in CLOSED_GROUND_MOVEMENTS
)
_ORDINARY_HORIZONTAL_RETENTION = 0.6 * 0.91
_VERTICAL_RETENTION = 0.9800000190734863


class GroundCandidateSafety(StrEnum):
    SAFE = "safe"
    UNSAFE = "unsafe"
    NEEDS_INFORMATION = "needs_information"
    UNSUPPORTED = "unsupported"
    BUDGET_EXHAUSTED = "budget_exhausted"


class GroundCandidateFamilyStatus(StrEnum):
    COMPLETE = "complete"
    BUDGET_EXHAUSTED = "budget_exhausted"


class GroundCandidateVerificationPath(StrEnum):
    CALCULATOR = "calculator"
    KERNEL = "kernel"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class GroundRouteFit:
    """Route membership for one proved prefix and its passive stop tail."""

    control_prefix_inside: bool
    neutral_tail_inside: bool
    maximum_control_prefix_deviation_blocks: float
    maximum_neutral_tail_deviation_blocks: float

    def __post_init__(self) -> None:
        if (type(self.control_prefix_inside) is not bool
                or type(self.neutral_tail_inside) is not bool
                or any(type(value) not in (int, float) or value < 0
                       for value in (
                           self.maximum_control_prefix_deviation_blocks,
                           self.maximum_neutral_tail_deviation_blocks,
                       ))):
            raise ContractViolation("ground route fit requires nonnegative typed facts")


_UNKNOWN_ROUTE_FIT = GroundRouteFit(False, False, math.inf, math.inf)


@dataclass(frozen=True, slots=True)
class GroundCandidateVerification:
    movement: MovementV1
    control_ticks: int
    status: GroundCandidateSafety
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...]
    result: VerifiedGroundRouteCandidate | None
    prefix_statuses: tuple[GroundCandidateSafety, ...] = ()
    verification_path: GroundCandidateVerificationPath = (
        GroundCandidateVerificationPath.CALCULATOR
    )
    route_fit: GroundRouteFit = _UNKNOWN_ROUTE_FIT
    tracking_end: PhysicsState | None = None
    stopped_end: PhysicsState | None = None
    verified_prefix: tuple[PhysicsState, ...] = ()
    verified_neutral_tail: tuple[PhysicsState, ...] = ()

    def __post_init__(self) -> None:
        """Store the two endpoints and both trajectory parts in the report.

        Callers must not reconstruct either endpoint from a second rollout.
        The verifier still accepts typed rejected candidates, whose physical
        result may end before a tracking or stopped state exists.
        """
        if self.result is None or not self.result.trajectory:
            return
        prefix_end = min(self.control_ticks, len(self.result.trajectory) - 1)
        prefix = self.result.trajectory[:prefix_end + 1]
        tail = self.result.trajectory[prefix_end:]
        object.__setattr__(self, "tracking_end", self.result.tracking_end)
        object.__setattr__(self, "stopped_end", self.result.trajectory[-1])
        object.__setattr__(self, "verified_prefix", prefix)
        object.__setattr__(self, "verified_neutral_tail", tail)

    @property
    def eligible(self) -> bool:
        return (
            self.status is GroundCandidateSafety.SAFE
            and self.route_fit.control_prefix_inside
            and self.result is not None
        )


@dataclass(frozen=True, slots=True)
class GroundCandidateVerificationStats:
    prefix0_computations: int
    first_tick_computations: int
    second_tick_computations: int
    physics_steps: int
    neutral_tail_steps: int
    cache_hits: int
    world_queries: int
    kernel_tails: int
    batch_tails: int
    calculator_tails: int


@dataclass(frozen=True, slots=True)
class GroundCandidateVerificationReport:
    candidates: tuple[GroundCandidateVerification, ...]
    status: GroundCandidateFamilyStatus
    neutral_safe: bool
    neutral_result: VerifiedGroundRouteCandidate | None
    stats: GroundCandidateVerificationStats

    @property
    def complete(self) -> bool:
        return self.status is GroundCandidateFamilyStatus.COMPLETE

    @property
    def safe_candidates(self) -> tuple[GroundCandidateVerification, ...]:
        if not self.complete:
            return ()
        return tuple(
            item for item in self.candidates
            if item.status is GroundCandidateSafety.SAFE
        )

    @property
    def eligible_candidates(self) -> tuple[GroundCandidateVerification, ...]:
        if not self.complete:
            return ()
        return tuple(item for item in self.candidates if item.eligible)


@dataclass(frozen=True, slots=True)
class GroundSequenceReplay:
    """One formal varying-command replay plus its passive stop tail.

    This is the public reuse boundary for bounded ground sequence solvers.  It
    intentionally shares the exact replay engine used by the closed one/two
    tick candidate family, so callers cannot grow a second ground model.
    """

    status: GroundCandidateSafety
    commands: tuple[MovementV1, ...]
    trajectory: tuple[PhysicsState, ...]
    tracking_end: PhysicsState | None
    stopped_end: PhysicsState | None
    control_trajectory: tuple[PhysicsState, ...]
    neutral_tail: tuple[PhysicsState, ...]
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...]
    minimum_support: float
    physics_steps: int
    verification_path: GroundCandidateVerificationPath
    reason: str

    @property
    def safe(self) -> bool:
        return self.status is GroundCandidateSafety.SAFE


@dataclass(frozen=True, slots=True)
class GroundIncrementalStep:
    """One already-verified ordinary-ground branch advanced by one tick.

    This contract is intentionally smaller than ``GroundSequenceReplay``.
    Search may carry it between beam layers, but it cannot be published as an
    executable proof.  The winning command sequence still has to pass the
    complete replay and every-prefix stop-tail proof.
    """

    status: GroundCandidateSafety
    state: PhysicsState | None
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...]
    minimum_support: float
    reason: str
    physics_steps: int

    @property
    def safe(self) -> bool:
        return self.status is GroundCandidateSafety.SAFE and self.state is not None


class _BudgetExhausted(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _PathResult:
    result: VerifiedGroundRouteCandidate
    final_state: PhysicsState | None
    verification_path: GroundCandidateVerificationPath


class _ReplayEngine:
    def __init__(
        self,
        frame: NavigationFrame,
        state: PhysicsState | None,
        *,
        tail_ticks: int,
        minimum_support: float,
        profile: GroundMotionProfile,
        query_cache: WorldQueryCache,
        budget_check: Callable[[], bool],
        verified_entry: bool = False,
    ) -> None:
        self.frame = frame
        self.state = state
        self.tail_ticks = tail_ticks
        self.minimum_support = minimum_support
        self.profile = profile
        self.query_cache = query_cache
        self.world = CachedGroundPhysicsWorld(frame, query_cache)
        self._budget_check = budget_check
        self.physics_steps = 0
        self.neutral_tail_steps = 0
        self.kernel_tails = 0
        self.batch_tails = 0
        self.calculator_tails = 0
        self.initial_dependencies: set[BlockPos] = set()
        self._material_dependencies_checked: set[BlockPos] = set()
        self.initial_status = QueryStatus.FEASIBLE
        self.initial_missing: tuple[BlockPos, ...] = ()
        self.initial_reason = "verified"
        if verified_entry:
            if (type(state) is not PhysicsState
                    or state.session != frame.session
                    or state.ruleset_id != JAVA_1_21_RULESET.ruleset_id
                    or state.state_schema != JAVA_1_21_RULESET.state_schema
                    or state.pose != "standing" or not state.on_ground
                    or state.sprinting or state.sneaking or state.swimming
                    or state.submerged_in_water or state.climbing
                    or state.fall_flying or state.flying
                    or state.is_using_item):
                self.initial_status = QueryStatus.UNSUPPORTED
                self.initial_reason = "incremental_entry_not_ordinary_ground"
            else:
                # The preceding beam layer already proved clearance, support,
                # material and the exact state.  Rechecking them here would
                # turn one-tick expansion back into prefix replay.
                self.initial_status = QueryStatus.FEASIBLE
                self._material_dependencies_checked.update(
                    self.initial_dependencies
                )
                return
        elif not ground_route_state_matches(frame, state):
            self.initial_status = QueryStatus.UNSUPPORTED
            self.initial_reason = "state_unavailable_or_mismatched"
        elif profile.motion_catalog is None or profile.ground_model_id is None:
            self.initial_status = QueryStatus.UNSUPPORTED
            self.initial_reason = "ordinary_model_required"
        else:
            assert state is not None
            clearance = sweep(
                state.body_box, (0.0, 0.0, 0.0), frame.world,
                query_cache=query_cache,
            )
            self.initial_dependencies.update(clearance.dependencies)
            if clearance.status is not QueryStatus.FEASIBLE:
                self.initial_status = clearance.status
                self.initial_missing = clearance.missing_cells
                self.initial_reason = "initial_clearance_rejected"
        if self.initial_status is QueryStatus.FEASIBLE:
            assert state is not None
            support = query_support(
                state.body_box, frame.world, query_cache=query_cache,
            )
            self.initial_dependencies.update(support.dependencies)
            if support.status is not QueryStatus.FEASIBLE:
                self.initial_status = support.status
                self.initial_missing = support.missing_cells
                self.initial_reason = "initial_support_rejected"
            elif (support.support_fraction < minimum_support
                    or not set(support.support_materials).issubset(profile.support_materials)):
                self.initial_status = QueryStatus.BLOCKED
                self.initial_reason = "initial_support_or_material_rejected"
            elif unsupported_motion_cells(
                    profile.motion_catalog, frame.world,
                    tuple(sorted(self.initial_dependencies)), profile.ground_model_id,
                    query_cache=query_cache):
                self.initial_status = QueryStatus.UNSUPPORTED
                self.initial_reason = "initial_material_outside_active_model"
            else:
                self._material_dependencies_checked.update(self.initial_dependencies)

    def initial_failure(self) -> VerifiedGroundRouteCandidate | None:
        if self.initial_status is QueryStatus.FEASIBLE:
            return None
        return VerifiedGroundRouteCandidate(
            self.initial_status, (), None, 1.0,
            tuple(sorted(self.initial_dependencies)), self.initial_missing,
            0, self.initial_reason,
        )

    def _advance(
        self,
        current: PhysicsState,
        movement: MovementV1,
        dependencies: set[BlockPos],
        lowest_support: float,
    ) -> tuple[QueryStatus, PhysicsState | None, float, tuple[BlockPos, ...], str, bool]:
        if self._budget_check():
            raise _BudgetExhausted
        projected = project_movement_command(current, movement)
        if projected.status is not ProjectionStatus.READY:
            return QueryStatus.UNSUPPORTED, None, lowest_support, (), "command_projection_failed", False
        self.physics_steps += 1
        calculated = physics_step(
            current, projected.tick_input, self.world, JAVA_1_21_RULESET,
        )
        dependencies.update(calculated.dependencies)
        if calculated.status is CalculationStatus.NEEDS_WORLD:
            return (QueryStatus.NEEDS_INFORMATION, None, lowest_support,
                    calculated.missing_cells, "missing_world", False)
        if calculated.status is not CalculationStatus.OK:
            return QueryStatus.UNSUPPORTED, None, lowest_support, (), "calculator_rejected", False
        assert calculated.next_state is not None
        following = calculated.next_state
        if (not following.on_ground
                or abs(following.position[1] - self.state.position[1]) > 1.0e-9  # type: ignore[union-attr]
                or following.pose != "standing"
                or following.fall_distance_blocks > 0):
            return QueryStatus.BLOCKED, following, lowest_support, (), "vertical_motion_or_lost_ground", True
        support = query_support(
            following.body_box, self.frame.world,
            query_cache=self.query_cache,
        )
        dependencies.update(support.dependencies)
        unchecked = dependencies - self._material_dependencies_checked
        if unchecked and unsupported_motion_cells(
            self.profile.motion_catalog, self.frame.world,
            tuple(sorted(unchecked)), self.profile.ground_model_id,
            query_cache=self.query_cache,
        ):
            return QueryStatus.UNSUPPORTED, following, lowest_support, (), "material_outside_active_model", False
        self._material_dependencies_checked.update(unchecked)
        if support.status is not QueryStatus.FEASIBLE:
            return (support.status, following, lowest_support, support.missing_cells,
                    "support_rejected", support.status is QueryStatus.BLOCKED)
        lowest_support = min(lowest_support, support.support_fraction)
        if (not set(support.support_materials).issubset(self.profile.support_materials)
                or support.support_fraction < self.minimum_support):
            return QueryStatus.BLOCKED, following, lowest_support, (), "insufficient_support", True
        return QueryStatus.FEASIBLE, following, lowest_support, (), "verified", False

    def path(
        self,
        movement: MovementV1,
        control_ticks: int,
        *,
        prefix_states: tuple[PhysicsState, ...] | None = None,
        prefix_dependencies: tuple[BlockPos, ...] = (),
        prefix_minimum_support: float = 1.0,
    ) -> _PathResult:
        failure = self.initial_failure()
        if failure is not None:
            return _PathResult(failure, None, GroundCandidateVerificationPath.CALCULATOR)
        assert self.state is not None
        trajectory = list(prefix_states or (self.state,))
        current = trajectory[-1]
        dependencies = set(self.initial_dependencies)
        dependencies.update(prefix_dependencies)
        lowest_support = prefix_minimum_support
        tracking_end = current if control_ticks == 0 else None
        remaining_controls = control_ticks - (len(trajectory) - 1)
        for _ in range(max(0, remaining_controls)):
            status, following, lowest_support, missing, reason, support_boundary = self._advance(
                current, movement, dependencies, lowest_support,
            )
            if following is not None:
                trajectory.append(following)
            if status is not QueryStatus.FEASIBLE:
                result = VerifiedGroundRouteCandidate(
                    status, tuple(trajectory), None, lowest_support,
                    tuple(sorted(dependencies)), tuple(sorted(set(missing))),
                    0, reason, support_boundary_rejected=support_boundary,
                )
                return _PathResult(result, following, GroundCandidateVerificationPath.CALCULATOR)
            assert following is not None
            current = following
        tracking_end = current
        kernel = self._neutral_kernel(
            current, trajectory, tracking_end, dependencies, lowest_support,
            maximum_ticks=self.tail_ticks,
        )
        if kernel is not None:
            if kernel.verification_path is GroundCandidateVerificationPath.KERNEL:
                self.kernel_tails += 1
            else:
                self.batch_tails += 1
            return kernel
        batch = self._neutral_batch_tail(
            current, trajectory, tracking_end, dependencies, lowest_support,
            maximum_ticks=self.tail_ticks,
        )
        if batch is not None:
            self.batch_tails += 1
            return batch
        self.calculator_tails += 1
        for index in range(self.tail_ticks):
            if self._budget_check():
                raise _BudgetExhausted
            if math.hypot(
                current.velocity_blocks_per_tick[0],
                current.velocity_blocks_per_tick[2],
            ) <= 1.0e-9:
                result = VerifiedGroundRouteCandidate(
                    QueryStatus.FEASIBLE, tuple(trajectory), tracking_end,
                    lowest_support, tuple(sorted(dependencies)), (), 0, "verified",
                )
                return _PathResult(result, current, GroundCandidateVerificationPath.CALCULATOR)
            self.neutral_tail_steps += 1
            status, following, lowest_support, missing, reason, support_boundary = self._advance(
                current, MovementV1(), dependencies, lowest_support,
            )
            if following is not None:
                trajectory.append(following)
            if status is not QueryStatus.FEASIBLE:
                result = VerifiedGroundRouteCandidate(
                    status, tuple(trajectory), tracking_end, lowest_support,
                    tuple(sorted(dependencies)), tuple(sorted(set(missing))),
                    0, reason, support_boundary_rejected=support_boundary,
                )
                return _PathResult(result, following, GroundCandidateVerificationPath.CALCULATOR)
            assert following is not None
            current = following
            remaining = self.tail_ticks - index - 1
            if remaining:
                suffix = self._neutral_kernel(
                    current, trajectory, tracking_end, dependencies,
                    lowest_support, maximum_ticks=remaining,
                )
                if suffix is not None:
                    self.kernel_tails += 1
                    return _PathResult(
                        suffix.result, suffix.final_state,
                        GroundCandidateVerificationPath.MIXED,
                    )
        status = (QueryStatus.FEASIBLE if math.hypot(
            current.velocity_blocks_per_tick[0], current.velocity_blocks_per_tick[2],
        ) <= 1.0e-9 else QueryStatus.BLOCKED)
        reason = "verified" if status is QueryStatus.FEASIBLE else "stop_tail_did_not_settle"
        return _PathResult(VerifiedGroundRouteCandidate(
            status, tuple(trajectory), tracking_end, lowest_support,
            tuple(sorted(dependencies)), (), 0, reason,
        ), current, GroundCandidateVerificationPath.CALCULATOR)

    def _neutral_kernel(
        self,
        current: PhysicsState,
        trajectory: list[PhysicsState],
        tracking_end: PhysicsState,
        dependencies: set[BlockPos],
        lowest_support: float,
        *,
        maximum_ticks: int,
    ) -> _PathResult | None:
        """Exact ordinary-flat neutral tail; return None at any complex edge."""
        if (current.pose != "standing" or not current.on_ground
                or current.sprinting or current.sneaking or current.swimming
                or current.submerged_in_water or current.climbing
                or current.fall_flying or current.flying or current.is_using_item):
            return None
        states = list(trajectory)
        state = current
        for _ in range(maximum_ticks):
            if self._budget_check():
                raise _BudgetExhausted
            vx, vy, vz = state.velocity_blocks_per_tick
            if math.hypot(vx, vz) <= 1.0e-9:
                break
            vx = 0.0 if abs(vx) < 0.003 else vx
            vy = 0.0 if abs(vy) < 0.003 else vy
            vz = 0.0 if abs(vz) < 0.003 else vz
            if vy >= -1.0e-9:
                return None
            following = replace(
                state,
                movement_tick_id=state.movement_tick_id + 1,
                position=(state.position[0] + vx, state.position[1], state.position[2] + vz),
                velocity_blocks_per_tick=(
                    vx * _ORDINARY_HORIZONTAL_RETENTION,
                    -state.gravity_attribute * _VERTICAL_RETENTION,
                    vz * _ORDINARY_HORIZONTAL_RETENTION,
                ),
                on_ground=True,
                horizontal_collision=False,
                vertical_collision=True,
                sprinting=False,
                sneaking=False,
                jumping_cooldown_ticks=0,
                fall_distance_blocks=0.0,
            )
            states.append(following)
            state = following
        end = state.body_box
        start = current.body_box
        horizontal = sweep(
            start,
            (state.position[0] - current.position[0], 0.0,
             state.position[2] - current.position[2]),
            self.frame.world, query_cache=self.query_cache,
        )
        dependencies.update(horizontal.dependencies)
        if horizontal.status is not QueryStatus.FEASIBLE:
            return None
        from mc2p.motion_nav.world_model import Aabb
        support_envelope = Aabb(
            min(start.min_x, end.min_x), start.min_y,
            min(start.min_z, end.min_z), max(start.max_x, end.max_x),
            start.max_y, max(start.max_z, end.max_z),
        )
        support = query_support(
            support_envelope, self.frame.world, query_cache=self.query_cache,
        )
        dependencies.update(support.dependencies)
        if (support.status is not QueryStatus.FEASIBLE
                or support.support_fraction < 1.0 - 1.0e-9
                or not support.support_materials
                or not set(support.support_materials).issubset(
                    self.profile.support_materials)):
            return None
        lowest_support = min(lowest_support, support.support_fraction)
        support_materials = support.support_materials
        for value in states[len(trajectory)-1:]:
            surface_position = (
                math.floor(value.position[0]),
                math.floor(value.position[1] - 0.500001),
                math.floor(value.position[2]),
            )
            surface = self.query_cache.cell(surface_position)
            dependencies.add(surface_position)
            if (surface.knowledge is not CellKnowledge.BLOCK
                    or surface.block is None
                    or surface.block.material_key not in support_materials
                    or surface.block.fluid
                    or surface.block.collision_kind != "full_cube"):
                return None
        unchecked = dependencies - self._material_dependencies_checked
        if unchecked and unsupported_motion_cells(
            self.profile.motion_catalog, self.frame.world,
            tuple(sorted(unchecked)), self.profile.ground_model_id,
            query_cache=self.query_cache,
        ):
            return None
        self._material_dependencies_checked.update(unchecked)
        path = (GroundCandidateVerificationPath.KERNEL
                if len(support_materials) == 1
                else GroundCandidateVerificationPath.MIXED)
        return _PathResult(VerifiedGroundRouteCandidate(
            QueryStatus.FEASIBLE, tuple(states), tracking_end,
            lowest_support, tuple(sorted(dependencies)), (), 0,
            ("verified_flat_kernel" if path is GroundCandidateVerificationPath.KERNEL
             else "verified_tick_batch"),
        ), state, path)

    def _neutral_batch_tail(
        self,
        current: PhysicsState,
        trajectory: list[PhysicsState],
        tracking_end: PhysicsState,
        dependencies: set[BlockPos],
        lowest_support: float,
        *,
        maximum_ticks: int,
    ) -> _PathResult | None:
        """Exact per-tick ordinary fallback with one shared collision sweep."""
        if (current.pose != "standing" or not current.on_ground
                or current.sprinting or current.sneaking or current.swimming
                or current.submerged_in_water or current.climbing
                or current.fall_flying or current.flying or current.is_using_item):
            return None
        states = list(trajectory)
        state = current
        for _ in range(maximum_ticks):
            if self._budget_check():
                raise _BudgetExhausted
            vx, vy, vz = state.velocity_blocks_per_tick
            if math.hypot(vx, vz) <= 1.0e-9:
                break
            vx = 0.0 if abs(vx) < 0.003 else vx
            vy = 0.0 if abs(vy) < 0.003 else vy
            vz = 0.0 if abs(vz) < 0.003 else vz
            if vy >= -1.0e-9:
                return None
            state = replace(
                state,
                movement_tick_id=state.movement_tick_id + 1,
                position=(state.position[0] + vx, state.position[1], state.position[2] + vz),
                velocity_blocks_per_tick=(
                    vx * _ORDINARY_HORIZONTAL_RETENTION,
                    -state.gravity_attribute * _VERTICAL_RETENTION,
                    vz * _ORDINARY_HORIZONTAL_RETENTION,
                ),
                on_ground=True, horizontal_collision=False,
                vertical_collision=True, sprinting=False, sneaking=False,
                jumping_cooldown_ticks=0, fall_distance_blocks=0.0,
            )
            states.append(state)
        horizontal = sweep(
            current.body_box,
            (state.position[0] - current.position[0], 0.0,
             state.position[2] - current.position[2]),
            self.frame.world, query_cache=self.query_cache,
        )
        dependencies.update(horizontal.dependencies)
        if horizontal.status is not QueryStatus.FEASIBLE:
            return None
        missing: set[BlockPos] = set()
        for value in states[len(trajectory)-1:]:
            support = query_support(
                value.body_box, self.frame.world, query_cache=self.query_cache,
            )
            dependencies.update(support.dependencies)
            missing.update(support.missing_cells)
            if support.status is QueryStatus.NEEDS_INFORMATION:
                return _PathResult(VerifiedGroundRouteCandidate(
                    QueryStatus.NEEDS_INFORMATION, tuple(states), tracking_end,
                    lowest_support, tuple(sorted(dependencies)), tuple(sorted(missing)),
                    0, "missing_world",
                ), value, GroundCandidateVerificationPath.MIXED)
            if support.status is QueryStatus.UNSUPPORTED:
                return _PathResult(VerifiedGroundRouteCandidate(
                    QueryStatus.UNSUPPORTED, tuple(states), tracking_end,
                    lowest_support, tuple(sorted(dependencies)), tuple(sorted(missing)),
                    0, "support_rejected",
                ), value, GroundCandidateVerificationPath.MIXED)
            if (support.status is QueryStatus.BLOCKED
                    or support.support_fraction < self.minimum_support):
                return _PathResult(VerifiedGroundRouteCandidate(
                    QueryStatus.BLOCKED, tuple(states), tracking_end,
                    lowest_support, tuple(sorted(dependencies)), tuple(sorted(missing)),
                    0, "insufficient_support", support_boundary_rejected=True,
                ), value, GroundCandidateVerificationPath.MIXED)
            if not set(support.support_materials).issubset(self.profile.support_materials):
                return _PathResult(VerifiedGroundRouteCandidate(
                    QueryStatus.BLOCKED, tuple(states), tracking_end,
                    lowest_support, tuple(sorted(dependencies)), tuple(sorted(missing)),
                    0, "material_outside_active_model",
                ), value, GroundCandidateVerificationPath.MIXED)
            lowest_support = min(lowest_support, support.support_fraction)
        unchecked = dependencies - self._material_dependencies_checked
        if unchecked and unsupported_motion_cells(
            self.profile.motion_catalog, self.frame.world,
            tuple(sorted(unchecked)), self.profile.ground_model_id,
            query_cache=self.query_cache,
        ):
            return _PathResult(VerifiedGroundRouteCandidate(
                QueryStatus.UNSUPPORTED, tuple(states), tracking_end,
                lowest_support, tuple(sorted(dependencies)), tuple(sorted(missing)),
                0, "material_outside_active_model",
            ), state, GroundCandidateVerificationPath.MIXED)
        self._material_dependencies_checked.update(unchecked)
        return _PathResult(VerifiedGroundRouteCandidate(
            QueryStatus.FEASIBLE, tuple(states), tracking_end, lowest_support,
            tuple(sorted(dependencies)), (), 0, "verified_tick_batch",
        ), state, GroundCandidateVerificationPath.MIXED)


def _aggregate_status(results: tuple[VerifiedGroundRouteCandidate, ...]) -> GroundCandidateSafety:
    if any(result.status is QueryStatus.BLOCKED for result in results):
        return GroundCandidateSafety.UNSAFE
    if any(result.status is QueryStatus.UNSUPPORTED for result in results):
        return GroundCandidateSafety.UNSUPPORTED
    if any(result.status is QueryStatus.NEEDS_INFORMATION for result in results):
        return GroundCandidateSafety.NEEDS_INFORMATION
    if all(result.status is QueryStatus.FEASIBLE for result in results):
        return GroundCandidateSafety.SAFE
    return GroundCandidateSafety.UNSAFE


def _aggregate_result(
    selected: VerifiedGroundRouteCandidate,
    required: tuple[VerifiedGroundRouteCandidate, ...],
) -> VerifiedGroundRouteCandidate:
    status = _aggregate_status(required)
    query_status = {
        GroundCandidateSafety.SAFE: QueryStatus.FEASIBLE,
        GroundCandidateSafety.UNSAFE: QueryStatus.BLOCKED,
        GroundCandidateSafety.NEEDS_INFORMATION: QueryStatus.NEEDS_INFORMATION,
        GroundCandidateSafety.UNSUPPORTED: QueryStatus.UNSUPPORTED,
    }[status]
    dependencies = tuple(sorted({cell for result in required for cell in result.dependencies}))
    missing = tuple(sorted({cell for result in required for cell in result.missing_cells}))
    return VerifiedGroundRouteCandidate(
        query_status, selected.trajectory, selected.tracking_end,
        min(result.minimum_support for result in required), dependencies, missing,
        0, "closed_candidate_safe" if status is GroundCandidateSafety.SAFE else
        "closed_candidate_" + status.value,
        selected.sneak_edge_clipped,
        any(result.support_boundary_rejected for result in required),
    )


class GroundCandidateVerifier:
    """Classify every declared one/two-tick WALK candidate before ranking."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        self._clock_ns = clock_ns

    def verify(
        self,
        frame: NavigationFrame,
        state: PhysicsState | None,
        tracking_context: GroundTrackingContext,
        *,
        tail_ticks: int,
        minimum_support: float,
        profile: GroundMotionProfile,
        query_cache: WorldQueryCache,
        movements: tuple[MovementV1, ...] = CLOSED_GROUND_MOVEMENTS,
        deadline_ns: int | None = None,
    ) -> GroundCandidateVerificationReport:
        if (type(movements) is not tuple or len(movements) != 9
                or any(type(item) is not MovementV1 for item in movements)
                or frozenset((item.forward, item.strafe) for item in movements) != _CLOSED_KEYS
                or any(item.jump or item.sprint or item.sneak for item in movements)):
            raise ContractViolation("ground candidate verifier requires the closed nine-key family")
        if type(tail_ticks) is not int or tail_ticks < 0:
            raise ContractViolation("ground candidate tail ticks must be nonnegative")
        if type(tracking_context) is not GroundTrackingContext:
            raise ContractViolation("ground candidate verifier requires tracking context")
        if (tracking_context.world is not frame.world
                or tracking_context.query_cache is not query_cache):
            raise ContractViolation("ground candidate tracking context differs from query scope")
        query_cache.validate_for(frame.world)
        def exhausted() -> bool:
            return deadline_ns is not None and self._clock_ns() >= deadline_ns

        engine = _ReplayEngine(
            frame, state, tail_ticks=tail_ticks, minimum_support=minimum_support,
            profile=profile, query_cache=query_cache, budget_check=exhausted,
        )
        candidates: dict[tuple[int, int, int], GroundCandidateVerification] = {}
        first_count = second_count = 0
        try:
            neutral_path = engine.path(MovementV1(), 0)
            neutral = neutral_path.result
        except _BudgetExhausted:
            neutral = None

        # The caller may permute the declaration only to prove order
        # independence. Scheduling remains canonical under a deadline.
        del movements
        for movement in CLOSED_GROUND_MOVEMENTS:
            if exhausted():
                for control_ticks in (1, 2):
                    candidates[(movement.forward, movement.strafe, control_ticks)] = (
                        GroundCandidateVerification(
                            movement, control_ticks, GroundCandidateSafety.BUDGET_EXHAUSTED,
                            (), (), None, (), GroundCandidateVerificationPath.CALCULATOR,
                        )
                    )
                continue
            try:
                first_path = engine.path(movement, 1)
                first = first_path.result
                first_count += 1
            except _BudgetExhausted:
                for control_ticks in (1, 2):
                    candidates[(movement.forward, movement.strafe, control_ticks)] = (
                        GroundCandidateVerification(
                            movement, control_ticks,
                            GroundCandidateSafety.BUDGET_EXHAUSTED,
                            (), (), None, (), GroundCandidateVerificationPath.CALCULATOR,
                        )
                    )
                continue
            assert neutral is not None
            first_aggregate = _aggregate_result(first, (neutral, first))
            first_status = _aggregate_status((neutral, first))
            candidates[(movement.forward, movement.strafe, 1)] = GroundCandidateVerification(
                movement, 1, first_status,
                first_aggregate.dependencies, first_aggregate.missing_cells, first_aggregate,
                (_aggregate_status((neutral,)), _aggregate_status((first,))),
                (first_path.verification_path
                 if first_path.verification_path is neutral_path.verification_path
                 else GroundCandidateVerificationPath.MIXED),
            )
            if exhausted():
                candidates[(movement.forward, movement.strafe, 2)] = GroundCandidateVerification(
                    movement, 2, GroundCandidateSafety.BUDGET_EXHAUSTED, (), (), None,
                    (), GroundCandidateVerificationPath.CALCULATOR,
                )
                continue
            # The shared first tick is preserved at the front of the two-tick path.
            if len(first.trajectory) >= 2 and first.status is QueryStatus.FEASIBLE:
                prefix = first.trajectory[:2]
                try:
                    second_path = engine.path(
                        movement, 2, prefix_states=prefix,
                        prefix_dependencies=first.dependencies,
                        prefix_minimum_support=first.minimum_support,
                    )
                    second = second_path.result
                except _BudgetExhausted:
                    candidates[(movement.forward, movement.strafe, 2)] = (
                        GroundCandidateVerification(
                            movement, 2, GroundCandidateSafety.BUDGET_EXHAUSTED,
                            (), (), None, (), GroundCandidateVerificationPath.CALCULATOR,
                        )
                    )
                    continue
            else:
                second = first
                second_path = first_path
            second_count += 1
            second_aggregate = _aggregate_result(second, (neutral, first, second))
            second_status = _aggregate_status((neutral, first, second))
            candidates[(movement.forward, movement.strafe, 2)] = GroundCandidateVerification(
                movement, 2, second_status,
                second_aggregate.dependencies, second_aggregate.missing_cells,
                second_aggregate,
                tuple(_aggregate_status((value,)) for value in (neutral, first, second)),
                (second_path.verification_path
                 if (second_path.verification_path is first_path.verification_path
                     and first_path.verification_path is neutral_path.verification_path)
                 else GroundCandidateVerificationPath.MIXED),
            )

        ordered = tuple(
            candidates[(movement.forward, movement.strafe, control_ticks)]
            for movement in CLOSED_GROUND_MOVEMENTS for control_ticks in (1, 2)
        )
        complete = neutral is not None and all(
            item.status is not GroundCandidateSafety.BUDGET_EXHAUSTED for item in ordered
        )
        # WorldQueryCache intentionally owns the hit counters; older cache
        # implementations expose neither field, so typed zero remains honest.
        stats = GroundCandidateVerificationStats(
            1, first_count, second_count, engine.physics_steps,
            engine.neutral_tail_steps,
            int(getattr(query_cache, "hits", 0)),
            int(getattr(query_cache, "misses", 0)),
            engine.kernel_tails,
            engine.batch_tails,
            engine.calculator_tails,
        )
        return GroundCandidateVerificationReport(
            ordered, GroundCandidateFamilyStatus.COMPLETE if complete
            else GroundCandidateFamilyStatus.BUDGET_EXHAUSTED,
            neutral is not None and neutral.status is QueryStatus.FEASIBLE,
            neutral, stats,
        )


def verify_ground_command_sequence(
    frame: NavigationFrame,
    state: PhysicsState | None,
    commands: tuple[MovementV1, ...],
    *,
    tail_ticks: int,
    minimum_support: float,
    profile: GroundMotionProfile,
    query_cache: WorldQueryCache,
    deadline_ns: int | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> GroundSequenceReplay:
    """Replay a bounded varying-command WALK sequence with the formal engine."""
    if (type(frame) is not NavigationFrame
            or type(commands) is not tuple
            or any(type(command) is not MovementV1 for command in commands)
            or any(command.jump or command.sprint or command.sneak
                   for command in commands)
            or type(tail_ticks) is not int or tail_ticks < 0
            or type(minimum_support) not in (int, float)
            or not 0 < float(minimum_support) <= 1
            or type(profile) is not GroundMotionProfile
            or type(query_cache) is not WorldQueryCache
            or (deadline_ns is not None and type(deadline_ns) is not int)):
        raise ContractViolation("ground sequence replay requires typed bounded WALK inputs")
    query_cache.validate_for(frame.world)

    def exhausted() -> bool:
        return deadline_ns is not None and clock_ns() >= deadline_ns

    engine = _ReplayEngine(
        frame, state, tail_ticks=tail_ticks,
        minimum_support=float(minimum_support), profile=profile,
        query_cache=query_cache, budget_check=exhausted,
    )
    if engine.initial_status is not QueryStatus.FEASIBLE:
        failure = engine.initial_failure()
        assert failure is not None
        return GroundSequenceReplay(
            _aggregate_status((failure,)), commands, failure.trajectory,
            None, None, failure.trajectory, (), failure.dependencies,
            failure.missing_cells, failure.minimum_support,
            engine.physics_steps, GroundCandidateVerificationPath.CALCULATOR,
            failure.reason,
        )
    assert state is not None
    trajectory = [state]
    dependencies = set(engine.initial_dependencies)
    current = state
    lowest_support = 1.0
    try:
        for command in commands:
            status, following, lowest_support, missing, reason, support_boundary = (
                engine._advance(current, command, dependencies, lowest_support)
            )
            if following is not None:
                trajectory.append(following)
            if status is not QueryStatus.FEASIBLE:
                failure = VerifiedGroundRouteCandidate(
                    status, tuple(trajectory), None, lowest_support,
                    tuple(sorted(dependencies)), tuple(sorted(set(missing))),
                    engine.physics_steps, reason,
                    support_boundary_rejected=support_boundary,
                )
                return GroundSequenceReplay(
                    _aggregate_status((failure,)), commands, failure.trajectory,
                    None, None, failure.trajectory, (), failure.dependencies,
                    failure.missing_cells, failure.minimum_support,
                    engine.physics_steps, GroundCandidateVerificationPath.CALCULATOR,
                    failure.reason,
                )
            assert following is not None
            current = following
        replay = engine.path(
            MovementV1(), 0,
            prefix_states=tuple(trajectory),
            prefix_dependencies=tuple(sorted(dependencies)),
            prefix_minimum_support=lowest_support,
        )
    except _BudgetExhausted:
        return GroundSequenceReplay(
            GroundCandidateSafety.BUDGET_EXHAUSTED, commands,
            tuple(trajectory), None, None, tuple(trajectory), (),
            tuple(sorted(dependencies)), (), lowest_support,
            engine.physics_steps, GroundCandidateVerificationPath.CALCULATOR,
            "ground_sequence_budget_exhausted",
        )
    result = replay.result
    split = min(len(commands), max(0, len(result.trajectory) - 1))
    control = result.trajectory[:split + 1]
    tail = result.trajectory[split:]
    return GroundSequenceReplay(
        _aggregate_status((result,)), commands, result.trajectory,
        result.tracking_end, result.trajectory[-1] if result.trajectory else None,
        control, tail, result.dependencies, result.missing_cells,
        result.minimum_support, engine.physics_steps,
        replay.verification_path, result.reason,
    )


def advance_verified_ground_command(
    frame: NavigationFrame,
    state: PhysicsState,
    command: MovementV1,
    *,
    minimum_support: float,
    previous_minimum_support: float,
    previous_dependencies: tuple[BlockPos, ...],
    profile: GroundMotionProfile,
    query_cache: WorldQueryCache,
    deadline_ns: int | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> GroundIncrementalStep:
    """Advance one safe beam branch by exactly one player movement tick.

    ``state`` must be the output of a prior complete replay or this function.
    The function proves this one new tick only.  It deliberately cannot create
    a ``GroundTerminalSequence`` or replace the final canonical replay.
    """
    if (type(frame) is not NavigationFrame or type(state) is not PhysicsState
            or type(command) is not MovementV1
            or command.jump or command.sprint or command.sneak
            or type(minimum_support) not in (int, float)
            or not 0 < float(minimum_support) <= 1
            or type(previous_minimum_support) not in (int, float)
            or not 0 < float(previous_minimum_support) <= 1
            or type(previous_dependencies) is not tuple
            or previous_dependencies != tuple(sorted(set(previous_dependencies)))
            or type(profile) is not GroundMotionProfile
            or type(query_cache) is not WorldQueryCache
            or (deadline_ns is not None and type(deadline_ns) is not int)):
        raise ContractViolation(
            "incremental ground step requires a verified typed branch"
        )
    query_cache.validate_for(frame.world)

    def exhausted() -> bool:
        return deadline_ns is not None and clock_ns() >= deadline_ns

    engine = _ReplayEngine(
        frame, state, tail_ticks=0,
        minimum_support=float(minimum_support), profile=profile,
        query_cache=query_cache, budget_check=exhausted,
        verified_entry=True,
    )
    if engine.initial_status is not QueryStatus.FEASIBLE:
        failure = engine.initial_failure()
        assert failure is not None
        return GroundIncrementalStep(
            _aggregate_status((failure,)), None, failure.dependencies,
            failure.missing_cells, failure.minimum_support,
            failure.reason, engine.physics_steps,
        )
    dependencies = set(previous_dependencies)
    try:
        status, following, support, missing, reason, _ = engine._advance(
            state, command, dependencies, float(previous_minimum_support),
        )
    except _BudgetExhausted:
        return GroundIncrementalStep(
            GroundCandidateSafety.BUDGET_EXHAUSTED, None,
            tuple(sorted(dependencies)), (), float(previous_minimum_support),
            "ground_incremental_step_budget_exhausted", engine.physics_steps,
        )
    if status is QueryStatus.FEASIBLE:
        candidate = GroundCandidateSafety.SAFE
    elif status is QueryStatus.NEEDS_INFORMATION:
        candidate = GroundCandidateSafety.NEEDS_INFORMATION
    elif status is QueryStatus.UNSUPPORTED:
        candidate = GroundCandidateSafety.UNSUPPORTED
    else:
        candidate = GroundCandidateSafety.UNSAFE
    return GroundIncrementalStep(
        candidate, following if candidate is GroundCandidateSafety.SAFE else None,
        tuple(sorted(dependencies)), tuple(sorted(set(missing))), support,
        reason, engine.physics_steps,
    )


__all__ = [
    "CLOSED_GROUND_MOVEMENTS", "GroundCandidateFamilyStatus", "GroundCandidateSafety",
    "GroundIncrementalStep", "advance_verified_ground_command",
    "GroundRouteFit",
    "GroundCandidateVerification", "GroundCandidateVerificationReport",
    "GroundCandidateVerificationStats", "GroundCandidateVerifier",
    "GroundSequenceReplay", "verify_ground_command_sequence",
]
