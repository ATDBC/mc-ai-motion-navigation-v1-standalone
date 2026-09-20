"""Backend-neutral B02 projection from formal V3 observations."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, FieldStatusV0
from mc2p.contracts.observation_request_v3 import (
    MAX_AIR_QUERY_POSITIONS, ObservationRequestV3,
)
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.motion_nav.pre_floating_adapter import apply_visible_blocks
from mc2p.motion_nav.world_model import (
    Aabb, BlockPos, CellKnowledge, ObservationStamp, WorldKnowledge, WorldSessionId, WorldView,
)


_PLAYER_WIDTH = 0.6
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


@dataclass(frozen=True, slots=True)
class NavigationFrame:
    session: WorldSessionId
    body: BodyState
    world: WorldView
    source_backend: str
    changed_cells: tuple[BlockPos, ...] = ()


def _session(snapshot: ObservationSnapshotV3) -> WorldSessionId:
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
        self._retired: set[WorldSessionId] = set()
        self._air_retry_after_ticks = air_retry_after_ticks
        self._air_last_attempt: dict[BlockPos, int] = {}

    def air_request(self, positions: tuple[BlockPos, ...], *, max_positions: int = 128,
                    field_profile: str = "navigation_v1") -> tuple[ObservationRequestV3, tuple[BlockPos, ...]]:
        if type(positions) is not tuple:
            raise ContractViolation("air request positions must be immutable")
        if type(max_positions) is not int or not 1 <= max_positions <= MAX_AIR_QUERY_POSITIONS:
            raise ContractViolation("invalid per-frame air query budget")
        if self._world is None or self._latest_order is None:
            raise ContractViolation("air request requires an ingested navigation frame")
        tick = self._latest_order[0]
        view = self._world.view()
        candidates = tuple(position for position in sorted(set(positions))
                           if view.cell(position).knowledge is CellKnowledge.UNKNOWN
                           and (position not in self._air_last_attempt
                                or tick - self._air_last_attempt[position]
                                   >= self._air_retry_after_ticks))
        requested, deferred = candidates[:max_positions], candidates[max_positions:]
        for position in requested:
            self._air_last_attempt[position] = tick
        return ObservationRequestV3(field_profile, requested), deferred

    def ingest(self, snapshot: ObservationSnapshotV3) -> NavigationFrame:
        if type(snapshot) is not ObservationSnapshotV3:
            raise ContractViolation("navigation adapter requires Observation V3")
        session = _session(snapshot)
        if session != self._session:
            if session in self._retired:
                raise ContractViolation("snapshot belongs to a retired world session")
            if self._session is not None:
                self._retired.add(self._session)
            self._session = session
            self._world = WorldKnowledge(session)
            self._latest_order = None
            self._air_last_attempt.clear()
        assert self._world is not None
        stamp = ObservationStamp(
            session=session,
            sequence_id=snapshot.sequence_id,
            world_tick=snapshot.world_time_ticks.value,
            controller_clock_id=snapshot.controller_clock_id,
            received_monotonic_ns=snapshot.received_at_monotonic_ns,
        )
        if self._latest_order is not None and stamp.world_order <= self._latest_order:
            raise ContractViolation("navigation observation order did not advance")
        changed_cells: tuple[BlockPos, ...] = ()
        if snapshot.perception.status is FieldStatusV0.VALID:
            assert snapshot.perception.value is not None
            blocks = snapshot.perception.value.blocks
            positions = tuple(sorted({block.position for block in blocks}))
            before_view = self._world.view()
            before = {position: before_view.cell(position) for position in positions}
            apply_visible_blocks(self._world, stamp, blocks)
            after_view = self._world.view()
            changed_cells = tuple(position for position in positions if
                                  (before[position].knowledge, before[position].block)
                                  != (after_view.cell(position).knowledge,
                                      after_view.cell(position).block))
            for block in blocks:
                self._air_last_attempt.pop(block.position, None)
        self._latest_order = stamp.world_order
        return NavigationFrame(session, _body(snapshot, session, stamp), self._world.view(),
                               snapshot.source_backend, changed_cells)
