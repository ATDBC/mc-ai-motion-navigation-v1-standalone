"""Calculator-driven stand-in for the Fabric backend (step-0 prototype).

The formal chain is used unchanged:
    PlayerRuntimeV1 -> RuntimeNavigationDriver -> NavigationSession
    -> planner / admission / executors / motion coordinator.
Only the game is replaced: every control frame advances one movement tick with
the repository's 1.21 motion calculator (physics_1_21.step) on a fully known
"truth" world, and the observation returned to the Runtime is built from that
state with the V3 payload fixtures.

Sensor model (deliberately simple, documented limits):
  * blocks: every solid truth block within BLOCK_RADIUS of the body is reported
    each frame (surface_depth source);
  * requested air cells: out_of_range beyond 16 blocks; outside_view when no
    sample point is inside the 120 x 120 degree view; occluded when every
    in-view sample point is hidden by a truth block; otherwise visible_air with
    the nearest-point distance and lower_region_visible = any visible sample in
    the bottom quarter of the cell.  Occlusion is tested by stepping along the
    eye-to-point segment every 0.05 blocks against truth collision boxes.
  * no entities, no fluids, no partial-transparency rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.reset import ResetResultV0
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from mc2p.runtime.backend_v1 import BackendStepResultV1
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.test_action_receipt import receipt_value

BLOCK_RADIUS = 6
MAX_DISTANCE = 16.0
HALF_FOV_RADIANS = math.radians(60.0)
SLAB_ID = "minecraft:smooth_stone_slab"


@dataclass(frozen=True, slots=True)
class Scene:
    """Solid blocks of a small test world; everything else in `volume` is air."""

    solids: dict[tuple[int, int, int], str]
    volume: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]   # (x0,x1),(y0,y1),(z0,z1) inclusive

    def with_floor(self) -> "Scene":
        """Close the bottom of the volume so that any fall ends on a block."""
        (x0, x1), (y0, _), (z0, z1) = self.volume
        floor = {(x, y, z): "minecraft:stone" for x in range(x0 - 1, x1 + 2)
                 for y in range(y0 - 2, y0 + 1) for z in range(z0 - 1, z1 + 2)}
        return Scene({**floor, **self.solids}, self.volume)

    def geometry(self, block_id: str) -> BlockGeometry:
        if block_id == SLAB_ID:
            return BlockGeometry(block_id, "boxes", (Aabb(0, 0, 0, 1, .5, 1),))
        return BlockGeometry.full_cube(block_id)

    def air_cells(self) -> tuple[tuple[int, int, int], ...]:
        (x0, x1), (y0, y1), (z0, z1) = self.volume
        return tuple(
            (x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
            for z in range(z0, z1 + 1) if (x, y, z) not in self.solids
        )


@dataclass(slots=True)
class Perturbations:
    """Things the game does that the controller did not ask for."""

    late_ticks: frozenset[int] = frozenset()                  # movement ticks that apply the previous input
    impulses: dict[int, tuple[float, float, float]] = field(default_factory=dict)   # tick -> added velocity (b/tick)
    world_edits: dict[int, dict[tuple[int, int, int], str | None]] = field(default_factory=dict)


class CalculatorBackend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(self, clock: list[int], scene: Scene, start: tuple[float, float, float],
                 yaw_degrees: float = 0.0, *, perturbations: Perturbations | None = None,
                 episode: str = "episode-sim") -> None:
        self.clock = clock
        self.scene = scene
        self.episode = episode
        self.perturbations = perturbations or Perturbations()
        self.sequence = 0
        self.movement_tick = 1
        self.health = 20.0
        self.damage_taken = 0.0
        self.actions: list[ActionSnapshotV1] = []
        self.applied: list[MovementV1] = []
        self._previous_movement = MovementV1()
        self._truth_session = WorldSessionId("sim-truth")
        self._build_truth()
        self.state = self._initial_state(start, yaw_degrees)

    # ----- truth world -------------------------------------------------------
    def _build_truth(self) -> None:
        self.truth = WorldKnowledge(self._truth_session)
        stamp = ObservationStamp(self._truth_session, self.sequence, self.sequence, "sim", self.sequence)
        self.truth.observe_blocks(stamp, {p: self.scene.geometry(b) for p, b in self.scene.solids.items()})
        self.truth.confirm_air(stamp, self.scene.air_cells())
        self.world = PhysicsWorldView(self.truth.view(), JAVA_1_21_RULESET)

    def _initial_state(self, position, yaw_degrees) -> PhysicsState:
        x, y, z = position
        stamp = ObservationStamp(self._truth_session, 1, 1, "sim", 0)
        body = BodyState(self._truth_session, 1, stamp, position, (0.0, -1.568, 0.0),
                         math.radians(yaw_degrees), 0.0, "standing",
                         Aabb(x - .3, y, z - .3, x + .3, y + 1.8, z + .3), True, False, True)
        frame = NavigationFrame(self._truth_session, body, self.truth.view(), "fabric")
        return build_physics_state(frame, JAVA_1_21_RULESET, dict(
            jumping_cooldown_ticks=0, movement_speed_attribute=0.1, step_height_blocks=0.6,
            gravity_attribute=0.08, jump_strength_attribute=0.42)).require_state()

    def solid_at(self, cell) -> str | None:
        return self.scene.solids.get(cell)

    # ----- observation -------------------------------------------------------
    def _eye(self) -> tuple[float, float, float]:
        x, y, z = self.state.position
        return x, y + (1.27 if self.state.pose == "crouching" else 1.62), z

    def _nearby_blocks(self):
        bx, by, bz = (math.floor(v) for v in self.state.position)
        blocks = []
        for (x, y, z), block_id in self.scene.solids.items():
            if max(abs(x - bx), abs(y - by), abs(z - bz)) <= BLOCK_RADIUS:
                if block_id == SLAB_ID:
                    blocks.append(observed_block((x, y, z), block_id, kind="boxes", boxes=((0, 0, 0, 1, .5, 1),)))
                else:
                    blocks.append(observed_block((x, y, z), block_id))
        return blocks

    def _blocked(self, point) -> bool:
        x, y, z = point
        cell = (math.floor(x), math.floor(y), math.floor(z))
        block_id = self.scene.solids.get(cell)
        if block_id is None:
            return False
        top = .5 if block_id == SLAB_ID else 1.0
        return y - cell[1] <= top

    def _visible(self, eye, point, target) -> bool:
        dx, dy, dz = (point[i] - eye[i] for i in range(3))
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        steps = max(1, int(length / .05))
        for i in range(1, steps):
            t = i / steps
            sample = (eye[0] + dx * t, eye[1] + dy * t, eye[2] + dz * t)
            if tuple(math.floor(v) for v in sample) == target:
                continue
            if self._blocked(sample):
                return False
        return True

    def _in_view(self, eye, point) -> bool:
        yaw, pitch = self.state.yaw_radians, self.state.pitch_radians
        forward = (-math.sin(yaw) * math.cos(pitch), -math.sin(pitch), math.cos(yaw) * math.cos(pitch))
        right = (-math.cos(yaw), 0.0, -math.sin(yaw))
        up = (right[1] * forward[2] - right[2] * forward[1],
              right[2] * forward[0] - right[0] * forward[2],
              right[0] * forward[1] - right[1] * forward[0])
        d = tuple(point[i] - eye[i] for i in range(3))
        f = sum(d[i] * forward[i] for i in range(3))
        if f <= 0:
            return False
        r = sum(d[i] * right[i] for i in range(3))
        u = sum(d[i] * up[i] for i in range(3))
        return abs(math.atan2(r, f)) <= HALF_FOV_RADIANS and abs(math.atan2(u, f)) <= HALF_FOV_RADIANS

    def air_query(self, cell) -> dict | None:
        if cell in self.scene.solids:
            return None                          # reported as a block instead
        eye = self._eye()
        nearest = [max(cell[i] - eye[i], eye[i] - cell[i] - 1, 0.0) for i in range(3)]
        distance = math.sqrt(sum(v * v for v in nearest))
        if distance > MAX_DISTANCE:
            return {"position": list(cell), "status": "out_of_range",
                    "observer_distance_blocks": None, "lower_region_visible": None}
        grid = (.1, .3, .5, .7, .9)
        points = [(cell[0] + a, cell[1] + h, cell[2] + b)
                  for a in grid for b in grid for h in (.05, .2, .5, .95)]
        in_view = [p for p in points if self._in_view(eye, p)]
        if not in_view:
            return {"position": list(cell), "status": "outside_view",
                    "observer_distance_blocks": None, "lower_region_visible": None}
        visible = [p for p in in_view if self._visible(eye, p, cell)]
        if not visible:
            return {"position": list(cell), "status": "occluded",
                    "observer_distance_blocks": None, "lower_region_visible": None}
        return {"position": list(cell), "status": "visible_air",
                "observer_distance_blocks": distance,
                "lower_region_visible": any(p[1] - cell[1] <= .25 for p in visible)}

    def observation(self, *, request_sequence_id=None, air_positions=()):
        s = self.state
        blocks = self._nearby_blocks()
        air_results = []
        for cell in sorted(set(tuple(p) for p in air_positions)):
            if cell in self.scene.solids:
                block_id = self.scene.solids[cell]
                if not any(b.position == cell for b in blocks):
                    blocks.append(observed_block(cell, block_id, kind="boxes", boxes=((0, 0, 0, 1, .5, 1),))
                                  if block_id == SLAB_ID else observed_block(cell, block_id))
                continue
            result = self.air_query(cell)
            air_results.append(result)
            if result["status"] == "visible_air":
                # The V3 contract pairs every visual-air success with a resolved air cell.
                blocks.append(observed_block(cell, "minecraft:air", kind="empty", sources=("air_query",)))
        snapshot = follow_snapshot(
            sequence=self.sequence, received=self.clock[0], position=s.position,
            blocks=tuple(blocks), episode=self.episode,
            yaw=math.degrees(s.yaw_radians), pitch=math.degrees(s.pitch_radians),
            self_changes={
                "movement_tick_id": self.movement_tick,
                "is_on_ground": s.on_ground,
                "velocity": dict(zip("xyz", s.velocity_blocks_per_tick)),
                "pose": s.pose,
                "is_sneaking": s.sneaking,
                "is_sprinting": s.sprinting,
                "eye_height_blocks": 1.27 if s.pose == "crouching" else 1.62,
                "fall_distance_blocks": s.fall_distance_blocks,
                "horizontal_collision": s.horizontal_collision,
                "vertical_collision": s.vertical_collision,
                "health_points": self.health,
            },
        )
        if air_results:
            perception = replace(snapshot.perception.value, air_query_results=tuple(
                _air_result(value) for value in air_results))
            snapshot = replace(snapshot, perception=replace(snapshot.perception, value=perception))
        return replace(snapshot, request_sequence_id=request_sequence_id)

    # ----- Runtime backend protocol -----------------------------------------
    def reset(self, request):
        return ResetResultV0(request.request_id, request.episode_id, True,
                             replace(self.observation(), episode_id=request.episode_id))

    def advance(self, movement: MovementV1, look_yaw: float = 0.0, look_pitch: float = 0.0) -> None:
        """One game tick with the given (already arbitrated) input."""
        tick = self.movement_tick + 1
        if tick in self.perturbations.late_ticks:
            movement = self._previous_movement
        self._previous_movement = movement
        for cell, block_id in self.perturbations.world_edits.get(tick, {}).items():
            if block_id is None:
                self.scene.solids.pop(cell, None)
            else:
                self.scene.solids[cell] = block_id
        if tick in self.perturbations.world_edits:
            self._build_truth()
            self.state = replace(self.state, session=self._truth_session)
        state = replace(
            self.state,
            yaw_radians=math.atan2(math.sin(self.state.yaw_radians + math.radians(look_yaw)),
                                   math.cos(self.state.yaw_radians + math.radians(look_yaw))),
            pitch_radians=max(-math.pi / 2, min(math.pi / 2,
                              self.state.pitch_radians + math.radians(look_pitch))),
        )
        projected = project_movement_command(state, movement)
        if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
            projected = project_movement_command(state, MovementV1())
        was_on_ground = state.on_ground
        falling_from = state.fall_distance_blocks
        result = physics_step(state, projected.tick_input, self.world, JAVA_1_21_RULESET)
        if result.next_state is None:
            raise RuntimeError(f"calculator stopped: {result.status} {getattr(result, 'reasons', ())}")
        next_state = result.next_state
        if tick in self.perturbations.impulses:
            vx, vy, vz = next_state.velocity_blocks_per_tick
            ix, iy, iz = self.perturbations.impulses[tick]
            next_state = replace(next_state, velocity_blocks_per_tick=(vx + ix, vy + iy, vz + iz))
        if next_state.on_ground and not was_on_ground:
            damage = max(0.0, math.ceil(max(falling_from, next_state.fall_distance_blocks) - 3.0))
            self.health -= damage
            self.damage_taken += damage
        self.state = next_state
        self.movement_tick = tick
        self.applied.append(movement)

    def step(self, action, deadline, *, observation_request=None):
        if type(action) is not ActionSnapshotV1:
            raise AssertionError("wrong action type")
        self.actions.append(action)
        self.sequence += 1
        self.clock[0] += 50_000_000
        self.advance(action.movement, action.look.yaw_delta_degrees, action.look.pitch_delta_degrees)
        applied = self.applied[-1]
        observation = replace(
            self.observation(request_sequence_id=action.request_sequence_id,
                             air_positions=() if observation_request is None
                             else observation_request.air_positions),
            episode_id=action.episode_id,
        )
        leased = applied != MovementV1()
        sample = {
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": self.movement_tick,
            "episode_id": action.episode_id,
            "request_sequence_id": action.request_sequence_id,
            "sampled_at_jvm_ns": self.movement_tick,
            "state": "leased" if leased else "neutral",
            "forward": float(applied.forward), "strafe": float(applied.strafe),
            "jump": applied.jump, "sneak": applied.sneak, "sprint": applied.sprint,
        }
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id, generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observation.world_time_ticks.value,
                input_samples=self.movement_tick, leased_input_samples=1 if leased else 0,
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": 0,
            "oldest_retained_input_tick": self.movement_tick,
            "input_applications": [sample],
        })
        return BackendStepResultV1(observation, 0, False, False, receipt)

    def close(self):
        pass


def _air_result(value):
    from mc2p.contracts.observation_v3 import AirQueryResultV3
    return AirQueryResultV3(tuple(value["position"]), value["status"],
                            value["observer_distance_blocks"], value["lower_region_visible"])
