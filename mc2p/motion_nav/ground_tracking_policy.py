"""Pure ordinary-ground candidate generation shared by planning and execution."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import TYPE_CHECKING

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.block_motion_traits import unsupported_motion_cells
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, PlanarBodyState, predict_ground,
)
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.physics_types import PhysicsState
from mc2p.motion_nav.world_model import Aabb, BlockPos, WorldQueryCache, WorldView

if TYPE_CHECKING:
    from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteConfig
    from mc2p.motion_nav.ground_candidate_verifier import (
        GroundCandidateVerificationReport,
    )
    from mc2p.motion_nav.safe_ground_control import VerifiedGroundRouteCandidate


GROUND_TRACKING_POLICY_VERSION = "ground-tracking-policy-v1"
_EPSILON = 1.0e-9
_MOVEMENTS = tuple(
    MovementV1(forward=forward, strafe=strafe)
    for forward in (-1, 0, 1)
    for strafe in (-1, 0, 1)
)


@dataclass(frozen=True, slots=True)
class GroundTrackingRoute:
    points: tuple[tuple[float, float, float], ...]
    completion_bounds: Aabb | None = None

    def __post_init__(self) -> None:
        if (type(self.points) is not tuple or not self.points
                or any(type(point) is not tuple or len(point) != 3
                       or any(type(value) not in (int, float)
                              or not math.isfinite(float(value))
                              for value in point)
                       for point in self.points)):
            raise ContractViolation("ground tracking route requires finite points")
        if self.completion_bounds is not None and type(self.completion_bounds) is not Aabb:
            raise ContractViolation("ground tracking completion bounds must be typed")

    @classmethod
    def from_fixed_route(cls, route: FixedRoute) -> GroundTrackingRoute:
        points = tuple((point.x, point.y, point.z) for point in route.points)
        contract = route.execution_contract
        completion = None if contract is None else contract.completion_region
        return cls(points, None if completion is None else completion.bounds)


@dataclass(frozen=True, slots=True)
class GroundTrackingLimits:
    endpoint_tolerance_blocks: float
    stopped_speed_blocks_per_second: float
    minimum_support_fraction: float
    preferred_support_fraction: float
    wall_soft_margin_blocks: float
    motion_prediction_margin_blocks: float
    maximum_cross_track_blocks: float
    lookahead_min_blocks: float
    lookahead_max_blocks: float
    input_lease_ticks: int
    maximum_recovery_ticks: int

    @classmethod
    def from_fixed_route_config(cls, config: FixedRouteConfig) -> GroundTrackingLimits:
        return cls(
            config.endpoint_tolerance_blocks,
            config.stopped_speed_blocks_per_second,
            config.minimum_support_fraction,
            config.preferred_support_fraction,
            config.wall_soft_margin_blocks,
            config.motion_prediction_margin_blocks,
            config.maximum_cross_track_blocks,
            config.lookahead_min_blocks,
            config.lookahead_max_blocks,
            config.input_lease_ticks,
            config.maximum_recovery_ticks,
        )


@dataclass(frozen=True, slots=True)
class GroundTrackingContext:
    body: PlanarBodyState
    body_box: Aabb
    feet_y: float
    route: GroundTrackingRoute
    progress: float
    segment_index: int
    previous_movement: MovementV1
    target: tuple[float, float]
    braking: bool
    profile: GroundMotionProfile
    limits: GroundTrackingLimits
    world: WorldView
    query_cache: WorldQueryCache

    def __post_init__(self) -> None:
        if (type(self.body) is not PlanarBodyState
                or type(self.body_box) is not Aabb
                or type(self.route) is not GroundTrackingRoute
                or type(self.previous_movement) is not MovementV1
                or type(self.profile) is not GroundMotionProfile
                or type(self.limits) is not GroundTrackingLimits
                or type(self.world) is not WorldView
                or type(self.query_cache) is not WorldQueryCache
                or type(self.segment_index) is not int
                or type(self.braking) is not bool
                or type(self.target) is not tuple or len(self.target) != 2):
            raise ContractViolation("ground tracking context requires typed inputs")
        self.query_cache.validate_for(self.world)


@dataclass(frozen=True, slots=True)
class GroundTrackingCandidate:
    movement: MovementV1
    score: float
    missing_cells: tuple[BlockPos, ...]
    dependencies: tuple[BlockPos, ...]
    blocked: bool
    unsupported: bool
    progress_gain: float
    predicted_states: tuple[PlanarBodyState, ...]
    tracking_end: PlanarBodyState
    full_replay_eligible: bool = False
    support_boundary: bool = False
    control_ticks: int = 0
    verified_prefix_ticks: tuple[int, ...] = ()
    completion_distance_blocks: float = math.inf
    target_distance_blocks: float = math.inf
    control_prefix_inside: bool = False
    neutral_tail_inside: bool = False
    maximum_control_prefix_deviation_blocks: float = math.inf
    maximum_neutral_tail_deviation_blocks: float = math.inf
    verification_result: VerifiedGroundRouteCandidate | None = None


@dataclass(frozen=True, slots=True)
class GroundCompletionEvaluation:
    satisfied: bool
    current_inside: bool
    speed_satisfied: bool
    neutral_stop_inside: bool
    predicted_stop_inside: bool
    neutral_tail_safe: bool
    stopping_distance_blocks: float
    current_distance_to_completion_blocks: float
    predicted_stop_distance_to_completion_blocks: float
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...]


@dataclass(frozen=True, slots=True)
class _Projection:
    progress: float
    distance: float
    segment_index: int


class _Geometry:
    def __init__(self, route: GroundTrackingRoute) -> None:
        points: list[tuple[float, float, float]] = []
        for point in route.points:
            if not points or math.dist(point, points[-1]) > _EPSILON:
                points.append(point)
        self.points = tuple(points)
        self.lengths = tuple(
            math.hypot(b[0] - a[0], b[2] - a[2])
            for a, b in zip(self.points, self.points[1:])
        )
        cumulative = [0.0]
        for length in self.lengths:
            cumulative.append(cumulative[-1] + length)
        self.cumulative = tuple(cumulative)
        self.total_length = cumulative[-1]

    def point_at(self, progress: float) -> tuple[float, float]:
        if len(self.points) == 1 or self.total_length <= _EPSILON:
            point = self.points[-1]
            return point[0], point[2]
        value = min(self.total_length, max(0.0, progress))
        for index, length in enumerate(self.lengths):
            if value <= self.cumulative[index+1]+_EPSILON and length > _EPSILON:
                ratio = (value-self.cumulative[index])/length
                a, b = self.points[index], self.points[index+1]
                return a[0]+(b[0]-a[0])*ratio, a[2]+(b[2]-a[2])*ratio
        point = self.points[-1]
        return point[0], point[2]

    def project(self, x: float, z: float, previous_progress: float,
                segment_index: int, advance_radius: float) -> _Projection:
        if len(self.points) == 1 or self.total_length <= _EPSILON:
            point = self.points[-1]
            return _Projection(0.0, math.hypot(x-point[0], z-point[2]), 0)
        if not 0 <= segment_index < len(self.lengths):
            raise ContractViolation("ground tracking segment index is invalid")
        end_point = self.points[segment_index + 1]
        indices = [segment_index]
        if (math.hypot(x-end_point[0], z-end_point[2]) <= advance_radius
                and segment_index + 1 < len(self.lengths)):
            indices.append(segment_index + 1)
        best = None
        for index in indices:
            a, b = self.points[index], self.points[index+1]
            dx, dz = b[0]-a[0], b[2]-a[2]
            length2 = dx*dx + dz*dz
            if length2 <= _EPSILON:
                continue
            ratio = min(1.0, max(0.0, ((x-a[0])*dx + (z-a[2])*dz)/length2))
            px, pz = a[0]+dx*ratio, a[2]+dz*ratio
            candidate = _Projection(
                self.cumulative[index] + self.lengths[index]*ratio,
                math.hypot(x-px, z-pz), index,
            )
            if candidate.progress + .20 < previous_progress:
                continue
            if best is None or (candidate.distance, -candidate.progress) < (
                    best.distance, -best.progress):
                best = candidate
        if best is None:
            return _Projection(previous_progress, math.inf, segment_index)
        return best


@dataclass(frozen=True, slots=True)
class _Rollout:
    movement: MovementV1
    states: tuple[PlanarBodyState, ...]
    tracking_end: PlanarBodyState
    base_score: float
    progress_gain: float
    raw_progress_gain: float
    cross_track_blocked: bool
    unsupported: bool
    control_ticks: int


def _prediction_box(body: Aabb, delta: tuple[float, float, float], margin: float) -> Aabb:
    dx, _, dz = delta
    return Aabb(
        body.min_x if dx > _EPSILON else body.min_x-margin,
        body.min_y,
        body.min_z if dz > _EPSILON else body.min_z-margin,
        body.max_x if dx < -_EPSILON else body.max_x+margin,
        body.max_y,
        body.max_z if dz < -_EPSILON else body.max_z+margin,
    )


def _wall_penalty(body: Aabb, context: GroundTrackingContext) -> float:
    margin = context.limits.wall_soft_margin_blocks
    if margin <= 0:
        return 0.0
    penalty = 0.0
    cache = context.query_cache
    for x in range(math.floor(body.min_x-margin), math.floor(body.max_x+margin)+1):
        for y in range(math.floor(body.min_y+_EPSILON), math.floor(body.max_y-_EPSILON)+1):
            for z in range(math.floor(body.min_z-margin), math.floor(body.max_z+margin)+1):
                position = (x, y, z)
                fact = cache.cell(position)
                if fact.block is None or fact.block.fluid or fact.block.collision_kind == "unsupported":
                    continue
                for obstacle in cache.collision_boxes(position):
                    if body.max_y <= obstacle.min_y+_EPSILON or body.min_y >= obstacle.max_y-_EPSILON:
                        continue
                    gap_x = max(obstacle.min_x-body.max_x, body.min_x-obstacle.max_x, 0.0)
                    gap_z = max(obstacle.min_z-body.max_z, body.min_z-obstacle.max_z, 0.0)
                    distance = math.hypot(gap_x, gap_z)
                    if distance < margin:
                        penalty += ((margin-distance)/margin)**2
    return penalty


class GroundTrackingPolicy:
    """Stateless candidate scorer for recoverable standing WALK movement."""

    @staticmethod
    def physics_trajectory_fits_route(
        context: GroundTrackingContext,
        trajectory: tuple[PhysicsState, ...],
    ) -> bool:
        """Check the admitted route corridor without scoring the candidate."""
        if type(context) is not GroundTrackingContext or type(trajectory) is not tuple:
            raise ContractViolation("ground trajectory corridor check requires typed inputs")
        return GroundTrackingPolicy.physics_trajectories_fit_route(
            context, (trajectory,),
        )[0]

    @staticmethod
    def physics_trajectory_route_deviation(
        context: GroundTrackingContext,
        trajectory: tuple[PhysicsState, ...],
    ) -> float:
        """Return the largest admitted-route deviation without judging safety."""
        if type(context) is not GroundTrackingContext or type(trajectory) is not tuple:
            raise ContractViolation("ground trajectory corridor check requires typed inputs")
        geometry = _Geometry(context.route)
        return max((
            geometry.project(
                state.position[0], state.position[2], context.progress,
                context.segment_index, context.limits.maximum_cross_track_blocks,
            ).distance
            for state in trajectory
        ), default=math.inf)

    @staticmethod
    def physics_trajectories_fit_route(
        context: GroundTrackingContext,
        trajectories: tuple[tuple[PhysicsState, ...], ...],
    ) -> tuple[bool, ...]:
        """Batch corridor checks used by the isolated F2-CV oracle."""
        if (type(context) is not GroundTrackingContext
                or type(trajectories) is not tuple
                or any(type(value) is not tuple for value in trajectories)):
            raise ContractViolation("ground trajectory corridor batch requires typed inputs")
        return tuple(
            GroundTrackingPolicy.physics_trajectory_route_deviation(
                context, trajectory,
            ) <= context.limits.maximum_cross_track_blocks
            for trajectory in trajectories
        )

    @staticmethod
    def _planar_physics_state(state: PhysicsState) -> PlanarBodyState:
        return PlanarBodyState(
            state.position[0], state.position[2],
            state.velocity_blocks_per_tick[0] * 20.0,
            state.velocity_blocks_per_tick[2] * 20.0,
            state.yaw_radians,
        )

    @classmethod
    def verified_candidates(
        cls,
        context: GroundTrackingContext,
        report: GroundCandidateVerificationReport,
    ) -> tuple[GroundTrackingCandidate, ...]:
        """Score one complete formal family without physics or world reads."""
        from mc2p.motion_nav.ground_candidate_verifier import (
            GroundCandidateFamilyStatus,
            GroundCandidateSafety,
            GroundCandidateVerificationReport,
        )

        if (type(context) is not GroundTrackingContext
                or type(report) is not GroundCandidateVerificationReport):
            raise ContractViolation("verified ground quality requires typed inputs")
        if report.status is not GroundCandidateFamilyStatus.COMPLETE:
            return ()
        if len(report.candidates) != 18:
            raise ContractViolation("verified ground family must contain eighteen candidates")

        geometry = _Geometry(context.route)
        bounds = (
            context.route.completion_bounds
            if context.segment_index >= max(0, len(context.route.points) - 2)
            else None
        )
        evaluated = []
        for proof in report.candidates:
            result = proof.result
            tracking = proof.tracking_end
            stopped = proof.stopped_end
            if result is None or tracking is None or stopped is None:
                evaluated.append(GroundTrackingCandidate(
                    proof.movement, math.inf, proof.missing_cells,
                    proof.dependencies,
                    proof.status is GroundCandidateSafety.UNSAFE,
                    proof.status is GroundCandidateSafety.UNSUPPORTED,
                    -math.inf, (), cls._planar_physics_state(
                        stopped or tracking or context.body  # type: ignore[arg-type]
                    ) if type(stopped or tracking) is PhysicsState else context.body,
                    control_ticks=proof.control_ticks,
                ))
                continue

            prefix_deviation = cls.physics_trajectory_route_deviation(
                context, proof.verified_prefix,
            )
            tail_deviation = cls.physics_trajectory_route_deviation(
                context, proof.verified_neutral_tail,
            )
            prefix_inside = (
                prefix_deviation <= context.limits.maximum_cross_track_blocks
            )
            tail_inside = (
                tail_deviation <= context.limits.maximum_cross_track_blocks
            )
            tracking_planar = cls._planar_physics_state(tracking)
            stopped_planar = cls._planar_physics_state(stopped)
            quality_end = stopped_planar if context.braking else tracking_planar
            trajectory_projections = tuple(
                geometry.project(
                    state.position[0], state.position[2],
                    context.progress, context.segment_index,
                    context.limits.maximum_cross_track_blocks,
                )
                for state in result.trajectory
            )
            projection = geometry.project(
                quality_end.x, quality_end.z,
                context.progress, context.segment_index,
                context.limits.maximum_cross_track_blocks,
            )
            raw_gain = projection.progress - context.progress
            gain = raw_gain if context.braking else max(-.25, raw_gain)
            completion_distance = math.inf
            if bounds is not None:
                dx = max(bounds.min_x-stopped_planar.x, 0.0,
                         stopped_planar.x-bounds.max_x)
                dz = max(bounds.min_z-stopped_planar.z, 0.0,
                         stopped_planar.z-bounds.max_z)
                completion_distance = math.hypot(dx, dz)
            target_distance = math.hypot(
                stopped_planar.x-context.target[0],
                stopped_planar.z-context.target[1],
            )
            score_distance = (
                completion_distance
                if context.braking and bounds is not None
                else math.hypot(
                    quality_end.x-context.target[0],
                    quality_end.z-context.target[1],
                )
            )
            switched = (
                proof.movement.forward != context.previous_movement.forward
                or proof.movement.strafe != context.previous_movement.strafe
            )
            speed = math.hypot(quality_end.velocity_x, quality_end.velocity_z)
            score = (
                speed*8.0 + score_distance*12.0
                if context.braking else
                score_distance*2.0 + projection.distance*5.0 - gain*7.0
                + (.04 if switched else 0.0)
                + (.30 if proof.movement == MovementV1() else 0.0)
            )
            support_shortfall = max(
                0.0,
                context.limits.preferred_support_fraction-result.minimum_support,
            )
            score += support_shortfall*support_shortfall*(
                8.0 if context.braking else 10.0
            )
            blocked = (
                proof.status is GroundCandidateSafety.UNSAFE
                or not prefix_inside
            )
            unsupported = proof.status is GroundCandidateSafety.UNSUPPORTED
            missing = proof.missing_cells
            if proof.status is GroundCandidateSafety.NEEDS_INFORMATION:
                missing = proof.missing_cells
            if (not context.braking and not blocked and not unsupported
                    and not missing):
                contact_stalled = any(
                    state.horizontal_collision
                    and after.progress <= before.progress + _EPSILON
                    and not (
                        bounds is not None
                        and bounds.min_x-_EPSILON <= state.position[0] <= bounds.max_x+_EPSILON
                        and bounds.min_z-_EPSILON <= state.position[2] <= bounds.max_z+_EPSILON
                    )
                    for state, before, after in zip(
                        result.trajectory[1:proof.control_ticks+1],
                        trajectory_projections,
                        trajectory_projections[1:],
                    )
                )
                current_completion_distance = math.inf
                tracking_completion_distance = math.inf
                region_gain = 0.0
                enters_completion = False
                if bounds is not None:
                    current_completion_distance = math.hypot(
                        max(bounds.min_x-context.body.x,
                            context.body.x-bounds.max_x, 0.0),
                        max(bounds.min_z-context.body.z,
                            context.body.z-bounds.max_z, 0.0),
                    )
                    tracking_completion_distance = math.hypot(
                        max(bounds.min_x-tracking.position[0],
                            tracking.position[0]-bounds.max_x, 0.0),
                        max(bounds.min_z-tracking.position[2],
                            tracking.position[2]-bounds.max_z, 0.0),
                    )
                    enters_completion = tracking_completion_distance <= _EPSILON
                    region_gain = current_completion_distance-tracking_completion_distance
                if contact_stalled or (
                    gain <= .005 and region_gain <= .005 and not enters_completion
                ):
                    blocked = True
                    gain = -math.inf
                    score = math.inf
                else:
                    gain = max(gain, region_gain, .006 if enters_completion else 0.0)
            if blocked or unsupported or missing:
                score = math.inf
                if blocked or unsupported:
                    gain = -math.inf
            evaluated.append(GroundTrackingCandidate(
                proof.movement, score, missing, proof.dependencies,
                blocked, unsupported, gain,
                tuple(cls._planar_physics_state(value)
                      for value in result.trajectory),
                tracking_planar,
                False, result.support_boundary_rejected,
                proof.control_ticks, tuple(range(proof.control_ticks + 1)),
                completion_distance, target_distance,
                prefix_inside, tail_inside,
                prefix_deviation, tail_deviation, result,
            ))
        return tuple(evaluated)

    @staticmethod
    def _rollout(
        context: GroundTrackingContext,
        movement: MovementV1,
        control_ticks: int | None = None,
    ) -> _Rollout:
        body, limits = context.body, context.limits
        if control_ticks is None:
            control_ticks = limits.input_lease_ticks
        if type(control_ticks) is not int or not 0 <= control_ticks <= 2:
            raise ContractViolation("ground tracking control ticks must be zero, one, or two")
        control = GroundControl(movement.forward, -movement.strafe, body.yaw_radians)
        neutral = GroundControl(0, 0, body.yaw_radians)
        lease = predict_ground(body, (control,)*control_ticks, context.profile)
        tracking_end = lease[-1]
        released = tracking_end
        # ``predict_ground`` includes the observed state at index zero.
        states = list(lease)
        for _ in range(limits.maximum_recovery_ticks):
            if math.hypot(released.velocity_x, released.velocity_z) <= limits.stopped_speed_blocks_per_second:
                break
            released = predict_ground(released, (neutral,), context.profile)[-1]
            states.append(released)
        released_speed = math.hypot(released.velocity_x, released.velocity_z)
        if released_speed > limits.stopped_speed_blocks_per_second:
            return _Rollout(movement, tuple(states), tracking_end, math.inf,
                            -math.inf, -math.inf, False, True, control_ticks)
        retention = context.profile.velocity_retention_per_tick
        if released_speed > _EPSILON:
            if retention >= 1.0:
                return _Rollout(movement, tuple(states), tracking_end, math.inf,
                                -math.inf, -math.inf, False, True, control_ticks)
            scale = context.profile.tick_seconds/(1.0-retention)
            states.append(PlanarBodyState(
                released.x+released.velocity_x*scale,
                released.z+released.velocity_z*scale,
                0.0, 0.0, released.yaw_radians,
            ))
        end = states[-1] if context.braking else tracking_end
        speed = math.hypot(end.velocity_x, end.velocity_z)
        distance = math.hypot(end.x-context.target[0], end.z-context.target[1])
        bounds = (
            context.route.completion_bounds
            if context.segment_index >= max(0, len(context.route.points)-2)
            else None
        )
        if context.braking and bounds is not None:
            dx = max(bounds.min_x-end.x, 0.0, end.x-bounds.max_x)
            dz = max(bounds.min_z-end.z, 0.0, end.z-bounds.max_z)
            distance = math.hypot(dx, dz)
        geometry = _Geometry(context.route)
        projection = geometry.project(
            end.x, end.z, context.progress, context.segment_index,
            limits.maximum_cross_track_blocks,
        )
        raw_gain = projection.progress-context.progress
        gain = raw_gain if context.braking else max(-.25, raw_gain)
        switched = (movement.forward != context.previous_movement.forward
                    or movement.strafe != context.previous_movement.strafe)
        score = (speed*8.0 + distance*12.0 if context.braking else
                 distance*2.0 + projection.distance*5.0 - gain*7.0
                 + (.04 if switched else 0.0)
                 + (.30 if movement == MovementV1() else 0.0))
        return _Rollout(
            movement, tuple(states), tracking_end, score, gain, raw_gain,
            not context.braking and projection.distance > limits.maximum_cross_track_blocks,
            False, control_ticks,
        )

    @staticmethod
    def _evaluate(context: GroundTrackingContext, rollout: _Rollout) -> GroundTrackingCandidate:
        stopped = rollout.states[-1]
        bounds = (
            context.route.completion_bounds
            if context.segment_index >= max(0, len(context.route.points)-2)
            else None
        )
        completion_distance = math.inf
        if bounds is not None:
            dx = max(bounds.min_x-stopped.x, 0.0, stopped.x-bounds.max_x)
            dz = max(bounds.min_z-stopped.z, 0.0, stopped.z-bounds.max_z)
            completion_distance = math.hypot(dx, dz)
        target_distance = math.hypot(
            stopped.x-context.target[0], stopped.z-context.target[1],
        )
        if rollout.unsupported:
            return GroundTrackingCandidate(
                rollout.movement, math.inf, (), (), False, True, -math.inf,
                rollout.states, rollout.tracking_end,
                control_ticks=rollout.control_ticks,
                completion_distance_blocks=completion_distance,
                target_distance_blocks=target_distance,
            )
        missing: set[BlockPos] = set()
        dependencies: set[BlockPos] = set()
        support_penalty = wall_penalty = 0.0
        support_box = context.body_box
        previous = context.body
        blocked = unsupported = full = support_boundary = False
        margin = context.limits.motion_prediction_margin_blocks
        geometry = _Geometry(context.route)
        for state in rollout.states[1:]:
            delta = (state.x-previous.x, 0.0, state.z-previous.z)
            collision_box = _prediction_box(support_box, delta, margin)
            collision = sweep(collision_box, delta, context.world,
                              query_cache=context.query_cache)
            dependencies.update(collision.dependencies)
            if (context.profile.motion_catalog is not None
                    and context.profile.ground_model_id is not None
                    and unsupported_motion_cells(
                        context.profile.motion_catalog, context.world,
                        collision.dependencies, context.profile.ground_model_id,
                        query_cache=context.query_cache)):
                unsupported = True
                break
            if collision.status is QueryStatus.UNSUPPORTED:
                unsupported = True
                break
            if collision.status is QueryStatus.BLOCKED:
                blocked = True
                full = not collision.missing_cells
                break
            if collision.status is QueryStatus.NEEDS_INFORMATION:
                missing.update(collision.missing_cells)
            next_collision = collision_box.moved(*delta)
            next_support = support_box.moved(*delta)
            support = query_support(next_support, context.world,
                                    query_cache=context.query_cache)
            evidence = [support]
            actual = (math.floor(next_support.min_x+_EPSILON),
                      math.floor(next_support.max_x-_EPSILON),
                      math.floor(next_support.min_z+_EPSILON),
                      math.floor(next_support.max_z-_EPSILON))
            uncertain = (math.floor(next_collision.min_x+_EPSILON),
                         math.floor(next_collision.max_x-_EPSILON),
                         math.floor(next_collision.min_z+_EPSILON),
                         math.floor(next_collision.max_z-_EPSILON))
            if uncertain != actual:
                evidence.append(query_support(next_collision, context.world,
                                              query_cache=context.query_cache))
            for item in evidence:
                dependencies.update(item.dependencies)
                if (context.profile.motion_catalog is not None
                        and context.profile.ground_model_id is not None
                        and unsupported_motion_cells(
                            context.profile.motion_catalog, context.world,
                            item.dependencies, context.profile.ground_model_id,
                            query_cache=context.query_cache)):
                    unsupported = True
                    break
                if item.status is QueryStatus.UNSUPPORTED:
                    unsupported = True
                    break
                if item.status is QueryStatus.BLOCKED:
                    blocked, full, support_boundary = True, not item.missing_cells, True
                    break
                if item.status is QueryStatus.NEEDS_INFORMATION:
                    missing.update(item.missing_cells)
                if (item.status is QueryStatus.FEASIBLE
                        and context.profile.support_materials
                        and not set(item.support_materials).issubset(context.profile.support_materials)):
                    unsupported = True
                    break
            if blocked or unsupported:
                break
            route_position = geometry.project(
                state.x, state.z, context.progress, context.segment_index,
                context.limits.maximum_cross_track_blocks,
            )
            if route_position.distance > context.limits.maximum_cross_track_blocks:
                blocked = True
                break
            width = next_support.max_x-next_support.min_x
            depth = next_support.max_z-next_support.min_z
            sx, sz = min(margin, width), min(margin, depth)
            lost = width*depth-(width-sx)*(depth-sz)
            worst = max(0.0, support.support_fraction-lost/(width*depth))
            if worst < context.limits.minimum_support_fraction:
                blocked, full, support_boundary = True, not missing, True
                break
            shortfall = max(0.0, context.limits.preferred_support_fraction-worst)
            support_penalty += shortfall*shortfall
            wall_penalty += _wall_penalty(next_collision, context)
            support_box = next_support
            previous = state
        if blocked or unsupported:
            return GroundTrackingCandidate(
                rollout.movement, math.inf, tuple(sorted(missing)),
                tuple(sorted(dependencies)), blocked, unsupported, -math.inf,
                rollout.states, rollout.tracking_end, full, support_boundary,
                rollout.control_ticks, (), completion_distance, target_distance,
            )
        if rollout.cross_track_blocked:
            return GroundTrackingCandidate(
                rollout.movement, math.inf, tuple(sorted(missing)),
                tuple(sorted(dependencies)), True, False, rollout.raw_progress_gain,
                rollout.states, rollout.tracking_end,
                control_ticks=rollout.control_ticks,
                completion_distance_blocks=completion_distance,
                target_distance_blocks=target_distance,
            )
        score = rollout.base_score + support_penalty*(8.0 if context.braking else 10.0) + wall_penalty*2.0
        return GroundTrackingCandidate(
            rollout.movement, score, tuple(sorted(missing)),
            tuple(sorted(dependencies)), False, False, rollout.progress_gain,
            rollout.states, rollout.tracking_end,
            control_ticks=rollout.control_ticks,
            completion_distance_blocks=completion_distance,
            target_distance_blocks=target_distance,
        )

    @classmethod
    def safe_tail(
        cls,
        context: GroundTrackingContext,
        movement: MovementV1,
        control_ticks: int,
    ) -> GroundTrackingCandidate:
        """Check every possible applied prefix and its complete neutral tail."""
        if type(movement) is not MovementV1:
            raise ContractViolation("ground safe tail movement must be typed")
        if type(control_ticks) is not int or control_ticks not in (1, 2):
            raise ContractViolation("ground safe tail supports one or two ticks")
        checked = tuple(range(control_ticks + 1))
        results = tuple(
            cls._evaluate(context, cls._rollout(context, movement, prefix))
            for prefix in checked
        )
        selected = results[-1]
        missing = tuple(sorted({cell for item in results for cell in item.missing_cells}))
        dependencies = tuple(sorted({cell for item in results for cell in item.dependencies}))
        blocked = any(item.blocked for item in results)
        unsupported = any(item.unsupported for item in results)
        return GroundTrackingCandidate(
            movement=movement,
            score=selected.score if not blocked and not unsupported and not missing else math.inf,
            missing_cells=missing,
            dependencies=dependencies,
            blocked=blocked,
            unsupported=unsupported,
            progress_gain=(selected.progress_gain
                           if not blocked and not unsupported else -math.inf),
            predicted_states=selected.predicted_states,
            tracking_end=selected.tracking_end,
            # A known simplified-model rejection may be rechecked by the full
            # 1.21 calculator.  That replay still has to cover every prefix.
            full_replay_eligible=any(item.full_replay_eligible for item in results),
            support_boundary=any(item.support_boundary for item in results),
            control_ticks=control_ticks,
            verified_prefix_ticks=checked,
            completion_distance_blocks=selected.completion_distance_blocks,
            target_distance_blocks=selected.target_distance_blocks,
        )

    @classmethod
    def closed_candidates(
        cls, context: GroundTrackingContext,
    ) -> tuple[GroundTrackingCandidate, ...]:
        """Return every declared terminal movement/duration before ranking."""
        maximum_ticks = min(2, context.limits.input_lease_ticks)
        return tuple(
            cls.safe_tail(context, movement, ticks)
            for movement in _MOVEMENTS
            for ticks in range(1, maximum_ticks + 1)
        )

    @classmethod
    def completion(
        cls,
        context: GroundTrackingContext,
        *,
        maximum_speed_blocks_per_second: float,
    ) -> GroundCompletionEvaluation:
        """Evaluate current completion without authorizing a future command."""
        if (type(maximum_speed_blocks_per_second) not in (int, float)
                or not math.isfinite(float(maximum_speed_blocks_per_second))
                or maximum_speed_blocks_per_second < 0):
            raise ContractViolation("ground completion speed must be finite and nonnegative")
        bounds = context.route.completion_bounds
        if bounds is None:
            raise ContractViolation("ground completion requires completion bounds")
        neutral = cls._evaluate(
            context, cls._rollout(context, MovementV1(), 0),
        )

        def inside(state: PlanarBodyState) -> bool:
            return (bounds.min_x-_EPSILON <= state.x <= bounds.max_x+_EPSILON
                    and bounds.min_z-_EPSILON <= state.z <= bounds.max_z+_EPSILON
                    and bounds.min_y-_EPSILON <= context.feet_y <= bounds.max_y+_EPSILON)

        def distance_to_bounds(state: PlanarBodyState) -> float:
            dx = max(bounds.min_x-state.x, 0.0, state.x-bounds.max_x)
            dz = max(bounds.min_z-state.z, 0.0, state.z-bounds.max_z)
            return math.hypot(dx, dz)

        current_inside = inside(context.body)
        speed_satisfied = math.hypot(
            context.body.velocity_x, context.body.velocity_z,
        ) <= maximum_speed_blocks_per_second + _EPSILON
        neutral_tail_safe = (
            not neutral.blocked and not neutral.unsupported and not neutral.missing_cells
        )
        neutral_stop_inside = neutral_tail_safe and all(
            inside(state) for state in neutral.predicted_states
        )
        stopped = neutral.predicted_states[-1]
        predicted_stop_inside = inside(stopped)
        stopping_distance = math.hypot(
            stopped.x-context.body.x, stopped.z-context.body.z,
        )
        return GroundCompletionEvaluation(
            current_inside and speed_satisfied and neutral_stop_inside,
            current_inside,
            speed_satisfied,
            neutral_stop_inside,
            predicted_stop_inside,
            neutral_tail_safe,
            stopping_distance,
            distance_to_bounds(context.body),
            distance_to_bounds(stopped),
            neutral.dependencies,
            neutral.missing_cells,
        )

    @classmethod
    def completion_from_report(
        cls,
        context: GroundTrackingContext,
        report: GroundCandidateVerificationReport,
        *,
        maximum_speed_blocks_per_second: float,
    ) -> GroundCompletionEvaluation:
        """Evaluate completion from the verifier's already-proved neutral tail."""
        from mc2p.motion_nav.ground_candidate_verifier import (
            GroundCandidateVerificationReport,
        )

        if (type(context) is not GroundTrackingContext
                or type(report) is not GroundCandidateVerificationReport
                or type(maximum_speed_blocks_per_second) not in (int, float)
                or not math.isfinite(float(maximum_speed_blocks_per_second))
                or maximum_speed_blocks_per_second < 0):
            raise ContractViolation("verified ground completion requires typed inputs")
        bounds = context.route.completion_bounds
        if bounds is None:
            raise ContractViolation("verified ground completion requires completion bounds")
        neutral = report.neutral_result

        def inside_position(position: tuple[float, float, float]) -> bool:
            return (bounds.min_x-_EPSILON <= position[0] <= bounds.max_x+_EPSILON
                    and bounds.min_z-_EPSILON <= position[2] <= bounds.max_z+_EPSILON
                    and bounds.min_y-_EPSILON <= context.feet_y <= bounds.max_y+_EPSILON)

        def distance_position(position: tuple[float, float, float]) -> float:
            dx = max(bounds.min_x-position[0], 0.0, position[0]-bounds.max_x)
            dz = max(bounds.min_z-position[2], 0.0, position[2]-bounds.max_z)
            return math.hypot(dx, dz)

        current_position = (context.body.x, context.feet_y, context.body.z)
        current_inside = inside_position(current_position)
        speed_satisfied = math.hypot(
            context.body.velocity_x, context.body.velocity_z,
        ) <= maximum_speed_blocks_per_second + _EPSILON
        neutral_tail_safe = (
            neutral is not None and neutral.status is QueryStatus.FEASIBLE
            and bool(neutral.trajectory)
        )
        trajectory = () if neutral is None else neutral.trajectory
        neutral_stop_inside = neutral_tail_safe and all(
            inside_position(state.position) for state in trajectory
        )
        stopped_position = (
            current_position if not trajectory else trajectory[-1].position
        )
        predicted_stop_inside = inside_position(stopped_position)
        stopping_distance = math.hypot(
            stopped_position[0]-context.body.x,
            stopped_position[2]-context.body.z,
        )
        return GroundCompletionEvaluation(
            current_inside and speed_satisfied and neutral_stop_inside,
            current_inside,
            speed_satisfied,
            neutral_stop_inside,
            predicted_stop_inside,
            neutral_tail_safe,
            stopping_distance,
            distance_position(current_position),
            distance_position(stopped_position),
            () if neutral is None else neutral.dependencies,
            () if neutral is None else neutral.missing_cells,
        )

    @staticmethod
    def _rollout_key(value: _Rollout) -> tuple[float, int, int]:
        return value.base_score, value.movement.forward, value.movement.strafe

    @staticmethod
    def _candidate_key(value: GroundTrackingCandidate) -> tuple:
        if math.isfinite(value.completion_distance_blocks):
            return (
                value.completion_distance_blocks,
                value.target_distance_blocks,
                value.score,
                value.control_ticks,
                value.movement.forward,
                value.movement.strafe,
            )
        return value.score, value.movement.forward, value.movement.strafe

    @classmethod
    def evaluate(
        cls, context: GroundTrackingContext, movement: MovementV1,
    ) -> GroundTrackingCandidate:
        if type(movement) is not MovementV1:
            raise ContractViolation("ground tracking movement must be typed")
        return cls._evaluate(context, cls._rollout(context, movement))

    @classmethod
    def candidates(cls, context: GroundTrackingContext) -> tuple[GroundTrackingCandidate, ...]:
        if (context.route.completion_bounds is not None
                and context.segment_index >= max(0, len(context.route.points)-2)):
            maximum_ticks = min(2, context.limits.input_lease_ticks)
            evaluated = []
            for movement in _MOVEMENTS:
                duration_candidates = tuple(
                    cls.safe_tail(context, movement, ticks)
                    for ticks in range(1, maximum_ticks+1)
                )
                feasible = tuple(
                    candidate for candidate in duration_candidates
                    if (not candidate.blocked and not candidate.unsupported
                        and not candidate.missing_cells)
                )
                evaluated.append(
                    min(feasible, key=cls._candidate_key)
                    if feasible else min(duration_candidates, key=cls._candidate_key)
                )
            return tuple(evaluated)
        rollouts = tuple(cls._rollout(context, movement) for movement in _MOVEMENTS)
        ordered = tuple(sorted(rollouts, key=cls._rollout_key))
        evaluated: dict[tuple[int, int], GroundTrackingCandidate] = {}

        def evaluate(rollout: _Rollout) -> GroundTrackingCandidate:
            key = rollout.movement.forward, rollout.movement.strafe
            if key not in evaluated:
                evaluated[key] = cls._evaluate(context, rollout)
            return evaluated[key]

        selected = None
        for rollout in ordered:
            if selected is not None and cls._rollout_key(rollout) >= cls._candidate_key(selected):
                break
            candidate = evaluate(rollout)
            if (not candidate.blocked and not candidate.unsupported
                    and not candidate.missing_cells
                    and (selected is None
                         or cls._candidate_key(candidate) < cls._candidate_key(selected))):
                selected = candidate
        if selected is None:
            for rollout in ordered:
                evaluate(rollout)
        elif not context.braking:
            if selected.progress_gain <= .005:
                for rollout in ordered:
                    if rollout.progress_gain > selected.progress_gain+.005:
                        evaluate(rollout)
            if (selected.movement == MovementV1()
                    and selected.progress_gain <= .005
                    and math.hypot(context.body.velocity_x, context.body.velocity_z)
                    <= context.limits.stopped_speed_blocks_per_second):
                for rollout in ordered:
                    evaluate(rollout)
        return tuple(
            evaluated[(movement.forward, movement.strafe)]
            for movement in _MOVEMENTS
            if (movement.forward, movement.strafe) in evaluated
        )


class GroundTrackingRolloutStatus(StrEnum):
    COMPLETE = "complete"
    NEEDS_INFORMATION = "needs_information"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    STALLED = "stalled"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class GroundTrackingRolloutResult:
    status: GroundTrackingRolloutStatus
    estimated_ticks: int
    commands: tuple[MovementV1, ...]
    final_position: tuple[float, float, float]
    final_velocity_blocks_per_second: tuple[float, float, float]
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...]
    policy_version: str = GROUND_TRACKING_POLICY_VERSION


def candidate_ground_tracking_routes(
    start: tuple[float, float, float],
    target: tuple[float, float, float],
    route_id_prefix: str,
) -> tuple[tuple[str, GroundTrackingRoute], ...]:
    """Build the frozen straight/XZ/ZX candidates and remove degenerates."""
    del route_id_prefix  # Identity is owned by the caller; geometry is pure.
    sx, sy, sz = start
    tx, ty, tz = target
    raw = (
        ("straight", ((sx, sy, sz), (tx, ty, tz))),
        ("x_then_z", ((sx, sy, sz), (tx, ty, sz), (tx, ty, tz))),
        ("z_then_x", ((sx, sy, sz), (sx, ty, tz), (tx, ty, tz))),
    )
    found = []
    identities = set()
    for kind, values in raw:
        points = []
        for value in values:
            if not points or math.dist(points[-1], value) > _EPSILON:
                points.append(value)
        identity = tuple(points)
        if len(points) < 2 or identity in identities:
            continue
        identities.add(identity)
        found.append((kind, GroundTrackingRoute(identity)))
    return tuple(found)


def _body_box(state: PhysicsState) -> Aabb:
    x, y, z = state.position
    radius = state.body_width*.5
    return Aabb(x-radius, y, z-radius, x+radius, y+state.body_height, z+radius)


def _release_distance(body: PlanarBodyState, target_speed: float,
                      profile: GroundMotionProfile,
                      limits: GroundTrackingLimits) -> float:
    state = body
    distance = 0.0
    neutral = GroundControl(0, 0, body.yaw_radians)
    for _ in range(limits.maximum_recovery_ticks):
        if math.hypot(state.velocity_x, state.velocity_z) <= target_speed:
            break
        following = predict_ground(state, (neutral,), profile)[-1]
        distance += math.hypot(following.x-state.x, following.z-state.z)
        state = following
    return distance


def rollout_ground_tracking_route(
    *,
    entry_state: PhysicsState,
    route: GroundTrackingRoute,
    completion_region: GroundCompletionRegion,
    world: WorldView,
    profile: GroundMotionProfile,
    limits: GroundTrackingLimits,
    maximum_ticks: int,
) -> GroundTrackingRolloutResult:
    """Estimate one recoverable standing-WALK route with the execution policy."""
    if (type(entry_state) is not PhysicsState
            or type(route) is not GroundTrackingRoute
            or type(completion_region) is not GroundCompletionRegion
            or type(world) is not WorldView
            or type(profile) is not GroundMotionProfile
            or type(limits) is not GroundTrackingLimits
            or type(maximum_ticks) is not int or maximum_ticks < 1):
        raise ContractViolation("ground tracking rollout requires typed bounded inputs")
    if (entry_state.pose != "standing" or entry_state.sprinting
            or entry_state.sneaking or entry_state.swimming
            or entry_state.climbing or entry_state.fall_flying
            or entry_state.flying or not entry_state.on_ground):
        return GroundTrackingRolloutResult(
            GroundTrackingRolloutStatus.UNSUPPORTED, 0, (), entry_state.position,
            tuple(value*20.0 for value in entry_state.velocity_blocks_per_tick),
            completion_region.dependencies, (),
        )
    geometry = _Geometry(route)
    body = PlanarBodyState(
        entry_state.position[0], entry_state.position[2],
        entry_state.velocity_blocks_per_tick[0]*20.0,
        entry_state.velocity_blocks_per_tick[2]*20.0,
        entry_state.yaw_radians,
    )
    box = _body_box(entry_state)
    progress = 0.0
    segment = 0
    previous = MovementV1()
    commands = []
    dependencies = set(completion_region.dependencies)
    missing: set[BlockPos] = set()
    no_progress = 0
    best_completion_distance = math.inf
    best_stop_distance = math.inf
    best_speed = math.inf
    cache = WorldQueryCache(world)
    policy_route = GroundTrackingRoute(route.points, completion_region.bounds)
    for _ in range(maximum_ticks):
        projection = geometry.project(
            body.x, body.z, progress, segment,
            limits.maximum_cross_track_blocks,
        )
        old_progress = progress
        progress = max(progress, projection.progress)
        segment = max(segment, projection.segment_index)
        speed = math.hypot(body.velocity_x, body.velocity_z)
        neutral_context = GroundTrackingContext(
            body, box, entry_state.position[1], policy_route, progress, segment,
            previous, (route.points[-1][0], route.points[-1][2]), True,
            profile, limits, world, cache,
        )
        completion = GroundTrackingPolicy.completion(
            neutral_context,
            maximum_speed_blocks_per_second=limits.stopped_speed_blocks_per_second,
        )
        dependencies.update(completion.dependencies)
        missing.update(completion.missing_cells)
        terminal_improved = (
            completion.current_distance_to_completion_blocks + .005
            < best_completion_distance
            or completion.predicted_stop_distance_to_completion_blocks + .005
            < best_stop_distance
            or (speed + .05 < best_speed
                and completion.current_distance_to_completion_blocks
                <= best_completion_distance + .005)
        )
        if progress >= old_progress+.005 or terminal_improved:
            no_progress = 0
            best_completion_distance = min(
                best_completion_distance,
                completion.current_distance_to_completion_blocks,
            )
            best_stop_distance = min(
                best_stop_distance,
                completion.predicted_stop_distance_to_completion_blocks,
            )
            best_speed = min(best_speed, speed)
        elif previous != MovementV1():
            no_progress += 1
        if no_progress >= 20:
            return GroundTrackingRolloutResult(
                GroundTrackingRolloutStatus.STALLED, len(commands), tuple(commands),
                (body.x, entry_state.position[1], body.z),
                (body.velocity_x, 0.0, body.velocity_z),
                tuple(sorted(dependencies)), tuple(sorted(missing)),
            )
        if completion.satisfied:
            return GroundTrackingRolloutResult(
                GroundTrackingRolloutStatus.COMPLETE, len(commands), tuple(commands),
                (body.x, entry_state.position[1], body.z),
                (body.velocity_x, 0.0, body.velocity_z),
                tuple(sorted(dependencies)), (),
            )
        inside = completion.current_inside
        remaining = max(0.0, geometry.total_length-progress)
        braking = (inside or remaining <= _release_distance(
            body, limits.stopped_speed_blocks_per_second, profile, limits,
        ) + limits.endpoint_tolerance_blocks*.65)
        lookahead = min(
            limits.lookahead_max_blocks,
            max(limits.lookahead_min_blocks,
                limits.lookahead_min_blocks+speed*.18),
        )
        target = (route.points[-1][0], route.points[-1][2]) if braking else geometry.point_at(progress+lookahead)
        context = GroundTrackingContext(
            body, box, entry_state.position[1], policy_route, progress, segment,
            previous, target, braking, profile, limits, world, cache,
        )
        candidates = GroundTrackingPolicy.candidates(context)
        dependencies.update(cell for item in candidates for cell in item.dependencies)
        missing.update(cell for item in candidates for cell in item.missing_cells)
        feasible = [item for item in candidates if not item.blocked
                    and not item.unsupported and not item.missing_cells]
        if not feasible:
            status = (GroundTrackingRolloutStatus.NEEDS_INFORMATION if missing else
                      GroundTrackingRolloutStatus.UNSUPPORTED
                      if any(item.unsupported for item in candidates) else
                      GroundTrackingRolloutStatus.BLOCKED)
            return GroundTrackingRolloutResult(
                status, len(commands), tuple(commands),
                (body.x, entry_state.position[1], body.z),
                (body.velocity_x, 0.0, body.velocity_z),
                tuple(sorted(dependencies)), tuple(sorted(missing)),
            )
        selected = min(feasible, key=GroundTrackingPolicy._candidate_key)
        movement = selected.movement
        control = GroundControl(movement.forward, -movement.strafe, body.yaw_radians)
        following = predict_ground(body, (control,), profile)[-1]
        box = box.moved(following.x-body.x, 0.0, following.z-body.z)
        body = following
        previous = movement
        commands.append(movement)
    return GroundTrackingRolloutResult(
        GroundTrackingRolloutStatus.BUDGET_EXHAUSTED, len(commands), tuple(commands),
        (body.x, entry_state.position[1], body.z),
        (body.velocity_x, 0.0, body.velocity_z),
        tuple(sorted(dependencies)), tuple(sorted(missing)),
    )


__all__ = [
    "GROUND_TRACKING_POLICY_VERSION", "GroundCompletionEvaluation",
    "GroundTrackingCandidate",
    "GroundTrackingContext", "GroundTrackingLimits", "GroundTrackingPolicy",
    "GroundTrackingRolloutResult", "GroundTrackingRolloutStatus",
    "GroundTrackingRoute", "candidate_ground_tracking_routes",
    "rollout_ground_tracking_route",
]
