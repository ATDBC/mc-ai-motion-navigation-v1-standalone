"""Owned observation action for direct-drop landing evidence."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge


DIRECT_DROP_EVIDENCE_DISTANCE_BLOCKS = 4.5
DIRECT_DROP_EVIDENCE_MAX_AGE_TICKS = 5
DIRECT_DROP_EDGE_PROBE_CORNER_OFFSET_BLOCKS = .65
DIRECT_DROP_EDGE_PROBE_HOLD_RADIUS_BLOCKS = .18
DIRECT_DROP_EDGE_PROBE_ENTRY_RADIUS_BLOCKS = .08
DIRECT_DROP_EDGE_PROBE_ENTRY_SPEED_BLOCKS_PER_SECOND = .10
DIRECT_DROP_EDGE_PROBE_EVIDENCE_RADIUS_BLOCKS = .30
DIRECT_DROP_EDGE_PROBE_MAX_FRAMES = 40
DIRECT_DROP_EDGE_PROBE_LOOK_TOLERANCE_DEGREES = 1.0
DIRECT_DROP_EDGE_PROBE_MAX_LOOK_DELTA_DEGREES = 36.0


class LandingEdgeProbeState(StrEnum):
    APPROACHING = "approaching"
    HOLDING_EDGE = "holding_edge"
    POSITIONING_ENTRY = "positioning_entry"
    RELEASING = "releasing"
    READY = "ready"
    TIMED_OUT = "timed_out"
    ENDED = "ended"


@dataclass(slots=True)
class LandingEdgeProbe:
    """Own a bounded sneak-to-edge observation for one goal and landing cell."""

    goal_id: str
    goal_revision: int
    landing_cell: BlockPos
    started_sequence_id: int
    state: LandingEdgeProbeState = LandingEdgeProbeState.APPROACHING
    edge_sequence_id: int | None = None
    ended_reason: str | None = None
    home_yaw_radians: float | None = None
    home_pitch_radians: float | None = None
    vantage_position: tuple[float, float] | None = None
    entry_position: tuple[float, float] | None = None
    evidence_sequence_id: int | None = None

    def __post_init__(self) -> None:
        require_identifier(self.goal_id, "landing edge probe goal id")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("landing edge probe goal revision is invalid")
        if (type(self.landing_cell) is not tuple
                or len(self.landing_cell) != 3
                or any(type(value) is not int for value in self.landing_cell)):
            raise ContractViolation("landing edge probe cell must be an integer position")
        if type(self.started_sequence_id) is not int or self.started_sequence_id < 0:
            raise ContractViolation("landing edge probe start sequence is invalid")
        if type(self.state) is not LandingEdgeProbeState:
            raise ContractViolation("landing edge probe state must be typed")

    @property
    def active(self) -> bool:
        return self.state in {
            LandingEdgeProbeState.APPROACHING,
            LandingEdgeProbeState.HOLDING_EDGE,
        }

    @property
    def owned(self) -> bool:
        return self.state not in {
            LandingEdgeProbeState.TIMED_OUT,
            LandingEdgeProbeState.ENDED,
        }

    def belongs_to(self, goal_id: str, goal_revision: int) -> bool:
        return (
            self.owned
            and self.goal_id == goal_id
            and self.goal_revision == goal_revision
        )

    def end(self, reason: str) -> None:
        if type(reason) is not str or not reason.strip():
            raise ContractViolation("landing edge probe end reason is required")
        self.state = LandingEdgeProbeState.ENDED
        self.ended_reason = reason.strip()

    @property
    def releasing(self) -> bool:
        return self.state is LandingEdgeProbeState.RELEASING

    @property
    def positioning_entry(self) -> bool:
        return self.state is LandingEdgeProbeState.POSITIONING_ENTRY

    @property
    def ready(self) -> bool:
        return self.state is LandingEdgeProbeState.READY

    def begin_handoff(self, reason: str) -> None:
        """Release sneak before the admitted route takes body ownership."""
        if type(reason) is not str or not reason.strip():
            raise ContractViolation("landing edge probe handoff reason is required")
        if not self.owned or self.ready:
            return
        self.state = LandingEdgeProbeState.RELEASING
        self.ended_reason = reason.strip()

    def begin_entry_alignment(self, frame: NavigationFrame) -> None:
        """Seal lower evidence and move sideways to a stable drop entry."""
        if not self.active or self.state is not LandingEdgeProbeState.HOLDING_EDGE:
            raise ContractViolation("landing edge probe is not holding its vantage")
        if not self.has_observed_evidence(frame, self.landing_cell):
            raise ContractViolation("landing edge probe has no admissible evidence")
        evidence = frame.world.cell(self.landing_cell).visual_air_evidence
        assert evidence is not None
        self.evidence_sequence_id = evidence.stamp.sequence_id
        self.state = LandingEdgeProbeState.POSITIONING_ENTRY

    def finish_release(self, frame: NavigationFrame) -> bool:
        if not self.handoff_ready(frame):
            return False
        self.state = LandingEdgeProbeState.READY
        return True

    def handoff_ready(self, frame: NavigationFrame) -> bool:
        speed = math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        ) if type(frame) is NavigationFrame else math.inf
        return (
            type(frame) is NavigationFrame
            and self.releasing
            and frame.body.is_on_ground
            and not frame.body.is_sneaking
            and frame.body.pose == "standing"
            and self._at_entry_position(frame)
            and speed
                <= DIRECT_DROP_EDGE_PROBE_ENTRY_SPEED_BLOCKS_PER_SECOND
                   + 1.0e-9
            and self._look_restored(frame)
        )

    def release_look(self, frame: NavigationFrame) -> LookV1 | None:
        """Restore the view owned by the route before the probe hands off."""
        if (type(frame) is not NavigationFrame
                or not self.releasing
                or self.home_yaw_radians is None
                or self.home_pitch_radians is None
                or self._look_restored(frame)):
            return None
        yaw_delta = math.degrees(_angle_delta(
            self.home_yaw_radians, frame.body.yaw_radians,
        ))
        pitch_delta = math.degrees(
            self.home_pitch_radians - frame.body.pitch_radians
        )
        maximum = DIRECT_DROP_EDGE_PROBE_MAX_LOOK_DELTA_DEGREES
        return LookV1(
            yaw_delta_degrees=max(-maximum, min(maximum, yaw_delta)),
            pitch_delta_degrees=max(-maximum, min(maximum, pitch_delta)),
        )

    def _look_restored(self, frame: NavigationFrame) -> bool:
        if self.home_yaw_radians is None or self.home_pitch_radians is None:
            return True
        tolerance = math.radians(
            DIRECT_DROP_EDGE_PROBE_LOOK_TOLERANCE_DEGREES
        )
        return (
            abs(_angle_delta(
                self.home_yaw_radians, frame.body.yaw_radians,
            )) <= tolerance
            and abs(
                self.home_pitch_radians - frame.body.pitch_radians
            ) <= tolerance
        )

    def movement(self, frame: NavigationFrame) -> MovementV1:
        """Sneak to one side of the ledge, then hold for lower-cell evidence.

        Looking straight over an edge is insufficient: before the camera
        crosses the boundary, the platform hides the lower side face; after it
        crosses, that face is behind the camera.  A diagonal corner stance
        leaves a small part of the current support under the body while moving
        the eye outside one side of the landing cell.
        """
        if type(frame) is not NavigationFrame or not self.owned:
            return MovementV1()
        if (not self.ready
                and frame.body.sequence_id - self.started_sequence_id
                    >= DIRECT_DROP_EDGE_PROBE_MAX_FRAMES):
            self.state = LandingEdgeProbeState.TIMED_OUT
            self.ended_reason = "landing_edge_probe_timed_out"
            return MovementV1()
        if self.ready or self.releasing:
            return MovementV1()
        if (not frame.body.is_on_ground
                or frame.body.pose not in {"standing", "crouching"}
                or not _has_known_support(frame)):
            return MovementV1()
        if self.positioning_entry:
            if self.entry_position is None:
                raise ContractViolation("landing edge probe has no entry position")
            dx = self.entry_position[0] - frame.body.position[0]
            dz = self.entry_position[1] - frame.body.position[2]
            speed = math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            )
            if (math.hypot(dx, dz)
                    <= DIRECT_DROP_EDGE_PROBE_ENTRY_RADIUS_BLOCKS
                    and speed
                    <= DIRECT_DROP_EDGE_PROBE_ENTRY_SPEED_BLOCKS_PER_SECOND):
                self.state = LandingEdgeProbeState.RELEASING
                return MovementV1()
            if (math.hypot(dx, dz)
                    <= DIRECT_DROP_EDGE_PROBE_ENTRY_RADIUS_BLOCKS):
                return MovementV1(sneak=True)
            return _movement_toward(dx, dz, frame.body.yaw_radians)
        if self.home_yaw_radians is None:
            self.home_yaw_radians = frame.body.yaw_radians
            self.home_pitch_radians = frame.body.pitch_radians
        if self.vantage_position is None:
            self.vantage_position = _corner_vantage(
                frame.body.position, self.landing_cell,
            )
        if self.entry_position is None:
            self.entry_position = _edge_entry_stance(
                frame.body.position, self.landing_cell,
            )
        dx = self.vantage_position[0] - frame.body.position[0]
        dz = self.vantage_position[1] - frame.body.position[2]
        if math.hypot(dx, dz) <= DIRECT_DROP_EDGE_PROBE_HOLD_RADIUS_BLOCKS:
            if self.edge_sequence_id is None:
                self.edge_sequence_id = frame.body.sequence_id
            self.state = LandingEdgeProbeState.HOLDING_EDGE
            return MovementV1(sneak=True)
        return _movement_toward(dx, dz, frame.body.yaw_radians)

    def allows_evidence(
        self,
        frame: NavigationFrame,
        landing_cell: BlockPos,
    ) -> bool:
        """Accept only lower evidence acquired after this probe reached its edge."""
        if (type(frame) is not NavigationFrame
                or not self.owned
                or landing_cell != self.landing_cell
                or self.edge_sequence_id is None
                or frame.body.sequence_id < self.edge_sequence_id):
            return False
        fact = frame.world.cell(landing_cell)
        evidence = fact.visual_air_evidence
        if fact.knowledge is not CellKnowledge.AIR:
            return False
        if (not self.ready
                or self.evidence_sequence_id is None
                or not frame.body.is_on_ground
                or frame.body.is_sneaking
                or frame.body.pose != "standing"
                or not self._at_entry_position(frame)):
            return False
        age = frame.body.sequence_id - self.evidence_sequence_id
        return 0 <= age <= DIRECT_DROP_EDGE_PROBE_MAX_FRAMES * 2

    def has_observed_evidence(
        self,
        frame: NavigationFrame,
        landing_cell: BlockPos,
    ) -> bool:
        """Check lower evidence while the probe still owns the view corner."""
        if (type(frame) is not NavigationFrame
                or not self.owned
                or landing_cell != self.landing_cell
                or self.edge_sequence_id is None
                or frame.body.sequence_id < self.edge_sequence_id):
            return False
        fact = frame.world.cell(landing_cell)
        evidence = fact.visual_air_evidence
        if (fact.knowledge is not CellKnowledge.AIR
                or self.state is not LandingEdgeProbeState.HOLDING_EDGE
                or self.vantage_position is None
                or evidence is None
                or not evidence.lower_region_visible
                or evidence.stamp.sequence_id < self.edge_sequence_id
                or not frame.body.is_on_ground
                or not frame.body.is_sneaking):
            return False
        age = frame.body.sequence_id - evidence.stamp.sequence_id
        if age < 0 or age > DIRECT_DROP_EVIDENCE_MAX_AGE_TICKS:
            return False
        stance_error = math.hypot(
            self.vantage_position[0] - frame.body.position[0],
            self.vantage_position[1] - frame.body.position[2],
        )
        return (
            stance_error
            <= DIRECT_DROP_EDGE_PROBE_EVIDENCE_RADIUS_BLOCKS + 1.0e-9
        )

    def _at_entry_position(self, frame: NavigationFrame) -> bool:
        if self.entry_position is None:
            return False
        return math.hypot(
            self.entry_position[0] - frame.body.position[0],
            self.entry_position[1] - frame.body.position[2],
        ) <= DIRECT_DROP_EDGE_PROBE_ENTRY_RADIUS_BLOCKS + 1.0e-9


def _corner_vantage(
    body_position: tuple[float, float, float],
    landing_cell: BlockPos,
) -> tuple[float, float]:
    landing_x = landing_cell[0] + .5
    landing_z = landing_cell[2] + .5
    dx = landing_x - body_position[0]
    dz = landing_z - body_position[2]
    length = math.hypot(dx, dz)
    if length <= 1.0e-9:
        return body_position[0], body_position[2]
    forward_x, forward_z = dx / length, dz / length
    # Deterministically use the right-hand corner.  A blocked corner ends in a
    # bounded failure; choosing among observation positions belongs to the
    # later exploration coordinator.
    lateral_x, lateral_z = forward_z, -forward_x
    offset = DIRECT_DROP_EDGE_PROBE_CORNER_OFFSET_BLOCKS
    return (
        landing_x - forward_x * offset + lateral_x * offset,
        landing_z - forward_z * offset + lateral_z * offset,
    )


def _edge_entry_stance(
    body_position: tuple[float, float, float],
    landing_cell: BlockPos,
) -> tuple[float, float]:
    landing_x = landing_cell[0] + .5
    landing_z = landing_cell[2] + .5
    dx = landing_x - body_position[0]
    dz = landing_z - body_position[2]
    length = math.hypot(dx, dz)
    if length <= 1.0e-9:
        return body_position[0], body_position[2]
    forward_x, forward_z = dx / length, dz / length
    offset = DIRECT_DROP_EDGE_PROBE_CORNER_OFFSET_BLOCKS
    return (
        landing_x - forward_x * offset,
        landing_z - forward_z * offset,
    )


def _angle_delta(target: float, current: float) -> float:
    return (target - current + math.pi) % (2.0 * math.pi) - math.pi


def _movement_toward(
    direction_x: float,
    direction_z: float,
    yaw_radians: float,
) -> MovementV1:
    length = math.hypot(direction_x, direction_z)
    if length <= 1.0e-9:
        return MovementV1(sneak=True)
    direction_x /= length
    direction_z /= length
    sin_yaw, cos_yaw = math.sin(yaw_radians), math.cos(yaw_radians)
    forward = -sin_yaw * direction_x + cos_yaw * direction_z
    controller_strafe = -cos_yaw * direction_x - sin_yaw * direction_z

    def axis(value: float) -> int:
        if value > .2:
            return 1
        if value < -.2:
            return -1
        return 0

    return MovementV1(
        forward=axis(forward),
        strafe=axis(-controller_strafe),
        sneak=True,
    )


def _has_known_support(frame: NavigationFrame) -> bool:
    body = frame.body
    min_x = math.floor(body.body_box.min_x + 1.0e-6)
    max_x = math.floor(body.body_box.max_x - 1.0e-6)
    min_z = math.floor(body.body_box.min_z + 1.0e-6)
    max_z = math.floor(body.body_box.max_z - 1.0e-6)
    feet_y = body.position[1]
    for y in range(math.floor(feet_y) - 1, math.floor(feet_y) + 1):
        for x in range(min_x, max_x + 1):
            for z in range(min_z, max_z + 1):
                fact = frame.world.cell((x, y, z))
                if fact.knowledge is not CellKnowledge.BLOCK:
                    continue
                assert fact.block is not None
                for box in fact.block.world_boxes((x, y, z)):
                    if (box.max_x > body.body_box.min_x + 1.0e-6
                            and box.min_x < body.body_box.max_x - 1.0e-6
                            and box.max_z > body.body_box.min_z + 1.0e-6
                            and box.min_z < body.body_box.max_z - 1.0e-6
                            and 0.0 <= feet_y - box.max_y <= .125):
                        return True
    return False


def information_probe_movement(
    frame: NavigationFrame,
    lower_region_positions: frozenset[BlockPos],
) -> MovementV1:
    """Compatibility helper for the first frame of an owned edge probe."""
    if (type(frame) is not NavigationFrame
            or type(lower_region_positions) is not frozenset
            or len(lower_region_positions) != 1
            or not frame.body.is_on_ground
            or frame.body.pose not in {"standing", "crouching"}):
        return MovementV1()
    target = next(iter(lower_region_positions))
    body = frame.body
    dx = target[0] + .5 - body.position[0]
    dz = target[2] + .5 - body.position[2]
    horizontal = math.hypot(dx, dz)
    if (horizontal < .2 or horizontal > 1.75
            or target[1] + 1 > body.position[1] - .5
            or not _has_known_support(frame)):
        return MovementV1()
    desired_yaw = math.atan2(-dx, dz)
    yaw_error = math.atan2(
        math.sin(desired_yaw - body.yaw_radians),
        math.cos(desired_yaw - body.yaw_radians),
    )
    if abs(math.degrees(yaw_error)) > 5.0:
        return MovementV1()
    vantage = _corner_vantage(body.position, target)
    return _movement_toward(
        vantage[0] - body.position[0],
        vantage[1] - body.position[2],
        body.yaw_radians,
    )
