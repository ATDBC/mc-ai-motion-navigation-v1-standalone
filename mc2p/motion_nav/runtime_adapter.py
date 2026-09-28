"""Backend-neutral B02 projection from formal V3 observations."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from mc2p.contracts.common import ContractViolation, FieldStatusV0
from mc2p.contracts.observation_request_v3 import (
    MAX_AIR_QUERY_POSITIONS, ObservationRequestV3,
)
from mc2p.contracts.observation_v3 import AirQueryResultV3, ObservationSnapshotV3
from mc2p.contracts.observation_v2 import StatusEffectV2
from mc2p.motion_nav.observed_block_adapter import apply_observed_blocks
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, BlockPos, CellKnowledge, ObservationStamp, WorldKnowledge,
    WorldSessionId, WorldView,
)


_PLAYER_WIDTH = 0.6
TEST_ORACLE = object()
_POSE_HEIGHTS = {
    "standing": 1.8,
    "crouching": 1.5,
    "swimming": 0.6,
    "fall_flying": 0.6,
    "spin_attack": 0.6,
    "sleeping": 0.2,
    "dying": 0.2,
}


@dataclass(frozen=True, slots=True)
class BodyState:
    session: WorldSessionId
    sequence_id: int
    stamp: ObservationStamp
    position: tuple[float, float, float]
    velocity_blocks_per_second: tuple[float, float, float]
    yaw_radians: float
    pitch_radians: float
    pose: str
    body_box: Aabb
    is_on_ground: bool
    horizontal_collision: bool
    vertical_collision: bool
    is_sprinting: bool = False
    is_sneaking: bool = False
    food_points: int = 20
    saturation_points: float = 5.0
    is_swimming: bool = False
    is_submerged_in_water: bool = False
    game_mode: str = "survival"
    fall_distance_blocks: float = 0.0
    status_effects: tuple[StatusEffectV2, ...] = ()
    is_climbing: bool = False
    is_fall_flying: bool = False
    is_flying: bool = False
    allow_flying: bool = False
    is_using_item: bool = False
    movement_tick_id: int | None = None
    health_points: float | None = None


@dataclass(frozen=True, slots=True)
class NavigationFrame:
    session: WorldSessionId
    body: BodyState
    world: WorldView
    source_backend: str
    changed_cells: tuple[BlockPos, ...] = ()
    air_query_results: tuple[AirQueryResultV3, ...] = ()


def world_session_from_observation(snapshot: ObservationSnapshotV3) -> WorldSessionId:
    if type(snapshot) is not ObservationSnapshotV3:
        raise ContractViolation("world session requires Observation V3")
    return WorldSessionId(
        f"{snapshot.source_backend}:{snapshot.episode_id}:{snapshot.client_sample.clock_id}"
    )


def _body(snapshot: ObservationSnapshotV3, session: WorldSessionId,
          stamp: ObservationStamp) -> BodyState:
    if snapshot.self_state.status is not FieldStatusV0.VALID or snapshot.self_state.value is None:
        raise ContractViolation("navigation body state is unavailable")
    own = snapshot.self_state.value
    height = _POSE_HEIGHTS.get(own.pose)
    if height is None:
        raise ContractViolation(f"unsupported player pose: {own.pose}")
    x, y, z = own.position.x, own.position.y, own.position.z
    half = _PLAYER_WIDTH / 2.0
    return BodyState(
        session=session,
        sequence_id=snapshot.sequence_id,
        stamp=stamp,
        position=(x, y, z),
        velocity_blocks_per_second=(
            own.velocity.x * 20.0,
            own.velocity.y * 20.0,
            own.velocity.z * 20.0,
        ),
        yaw_radians=math.radians(own.yaw_degrees),
        pitch_radians=math.radians(own.pitch_degrees),
        pose=own.pose,
        is_sprinting=own.is_sprinting,
        is_sneaking=own.is_sneaking,
        food_points=own.food_points,
        saturation_points=own.saturation_points,
        is_swimming=own.is_swimming,
        is_submerged_in_water=own.is_submerged_in_water,
        game_mode=own.game_mode,
        fall_distance_blocks=own.fall_distance_blocks,
        status_effects=own.status_effects,
        is_climbing=own.is_climbing,
        is_fall_flying=own.is_fall_flying,
        is_flying=own.is_flying,
        allow_flying=own.allow_flying,
        is_using_item=own.is_using_item,
        movement_tick_id=own.movement_tick_id,
        health_points=(float(snapshot.health_points.value)
                       if snapshot.health_points.status is FieldStatusV0.VALID
                       and snapshot.health_points.value is not None
                       else None),
        body_box=Aabb(x - half, y, z - half, x + half, y + height, z + half),
        is_on_ground=own.is_on_ground,
        horizontal_collision=own.horizontal_collision,
        vertical_collision=own.vertical_collision,
    )


class NavigationObservationAdapter:
    """Owns one live world session and projects either formal backend identically."""

    def __init__(self, *, air_retry_after_ticks: int = 5) -> None:
        if type(air_retry_after_ticks) is not int or air_retry_after_ticks < 1:
            raise ContractViolation("air retry interval must be a positive tick count")
        self._session: WorldSessionId | None = None
        self._world: WorldKnowledge | None = None
        self._latest_order: tuple[int, int] | None = None
        self._latest_frame: NavigationFrame | None = None
        self._retired: set[WorldSessionId] = set()
        self._air_retry_after_ticks = air_retry_after_ticks
        self._air_last_attempt: dict[BlockPos, int] = {}
        self._known_recheck_after_ticks = 20
        self._known_last_attempt: dict[BlockPos, int] = {}
        self._shared_geometries: dict[tuple[object, ...], BlockGeometry] = {}

    @property
    def has_frame(self) -> bool:
        """Whether this adapter has ingested the world view used by requests."""
        return self._world is not None and self._latest_order is not None

    @property
    def latest_frame(self) -> NavigationFrame | None:
        """Return the current immutable projection without ingesting again."""
        return self._latest_frame

    def seed_test_oracle_memory(
        self, authority: object, blocks: dict[BlockPos, BlockGeometry],
        air: tuple[BlockPos, ...],
    ) -> NavigationFrame:
        """Load historical map facts for an isolated simulation, never live sensing.

        The test must explicitly present TEST_ORACLE. This entry cannot create a
        frame and cannot be used before the Runtime has ingested an observation.
        """
        if authority is not TEST_ORACLE:
            raise ContractViolation("historical memory requires TEST_ORACLE")
        if self._world is None or self._latest_frame is None:
            raise ContractViolation("historical memory requires an observed frame")
        if type(blocks) is not dict or type(air) is not tuple:
            raise ContractViolation("historical memory must be frozen test facts")
        if set(blocks).intersection(air):
            raise ContractViolation("historical block and air facts conflict")
        stamp = self._latest_frame.body.stamp
        self._world.observe_blocks(stamp, blocks)
        self._world.confirm_air(stamp, air)
        self._latest_frame = replace(self._latest_frame, world=self._world.view())
        return self._latest_frame

    def air_request(self, positions: tuple[BlockPos, ...], *, max_positions: int = 128,
                    field_profile: str = "navigation_v1",
                    include_known: bool = False) -> tuple[ObservationRequestV3, tuple[BlockPos, ...]]:
        if type(positions) is not tuple:
            raise ContractViolation("air request positions must be immutable")
        if type(max_positions) is not int or not 1 <= max_positions <= MAX_AIR_QUERY_POSITIONS:
            raise ContractViolation("invalid per-frame air query budget")
        if self._world is None or self._latest_order is None:
            raise ContractViolation("air request requires an ingested navigation frame")
        tick = self._latest_order[0]
        view = self._world.view()
        attempts = self._known_last_attempt if include_known else self._air_last_attempt
        retry_ticks = (
            self._known_recheck_after_ticks if include_known
            else self._air_retry_after_ticks
        )
        candidates = tuple(position for position in sorted(set(positions))
                           if (include_known
                               or view.cell(position).knowledge is CellKnowledge.UNKNOWN)
                           and (position not in attempts
                                or tick - attempts[position] >= retry_ticks))
        requested, deferred = candidates[:max_positions], candidates[max_positions:]
        for position in requested:
            attempts[position] = tick
        return ObservationRequestV3(field_profile, requested), deferred

    def ingest(self, snapshot: ObservationSnapshotV3) -> NavigationFrame:
        if type(snapshot) is not ObservationSnapshotV3:
            raise ContractViolation("navigation adapter requires Observation V3")
        session = world_session_from_observation(snapshot)
        if session != self._session:
            if session in self._retired:
                raise ContractViolation("snapshot belongs to a retired world session")
            if self._session is not None:
                self._retired.add(self._session)
            self._session = session
            self._world = WorldKnowledge(session)
            self._latest_order = None
            self._latest_frame = None
            self._air_last_attempt.clear()
            self._known_last_attempt.clear()
            self._shared_geometries.clear()
        assert self._world is not None
        stamp = ObservationStamp(
            session=session,
            sequence_id=snapshot.sequence_id,
            world_tick=snapshot.world_time_ticks.value,
            controller_clock_id=snapshot.controller_clock_id,
            received_monotonic_ns=snapshot.received_at_monotonic_ns,
        )
        if self._latest_order is not None and stamp.causal_order == self._latest_order:
            if self._latest_frame is None:
                raise ContractViolation("navigation adapter lost its latest frame")
            return self._latest_frame
        if self._latest_order is not None and stamp.causal_order < self._latest_order:
            raise ContractViolation("navigation observation order moved backward")
        changed_cells: tuple[BlockPos, ...] = ()
        air_query_results: tuple[AirQueryResultV3, ...] = ()
        if snapshot.perception.status is FieldStatusV0.VALID:
            assert snapshot.perception.value is not None
            blocks = snapshot.perception.value.blocks
            air_query_results = snapshot.perception.value.air_query_results
            changed_cells = apply_observed_blocks(
                self._world, stamp, blocks, self._shared_geometries,
                air_query_results,
            )
            for block in blocks:
                self._air_last_attempt.pop(block.position, None)
        self._latest_order = stamp.causal_order
        body = _body(snapshot, session, stamp)
        self._world.set_protection_center(body.position)
        frame = NavigationFrame(
            session, body, self._world.view(), snapshot.source_backend,
            changed_cells, air_query_results,
        )
        self._latest_frame = frame
        return frame
