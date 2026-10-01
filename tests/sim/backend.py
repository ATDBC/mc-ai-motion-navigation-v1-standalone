"""Calculator-driven game backend for the formal navigation simulation.

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
from typing import Callable

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.reset import ResetResultV0
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState, TickInput
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


def _block_state(block_id: str) -> tuple[str, dict[str, str]]:
    if "[" not in block_id:
        return block_id, {}
    material, raw = block_id.split("[", 1)
    if not raw.endswith("]"):
        raise ValueError(f"invalid simulated block state: {block_id}")
    values = {}
    for item in raw[:-1].split(","):
        key, value = item.split("=", 1)
        values[key] = value
    return material, values


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
        material, state = _block_state(block_id)
        if material == SLAB_ID:
            height = 1.0 if state.get("type") == "double" else .5
            return BlockGeometry(
                material, "boxes", (Aabb(0, 0, 0, 1, height, 1),),
            )
        if material == "minecraft:dirt_path":
            return BlockGeometry(
                material, "boxes", (Aabb(0, 0, 0, 1, 15 / 16, 1),),
            )
        if material == "minecraft:white_carpet":
            return BlockGeometry(
                material, "boxes", (Aabb(0, 0, 0, 1, 1 / 16, 1),),
            )
        if material == "minecraft:snow":
            layers = int(state.get("layers", "1"))
            if not 2 <= layers <= 8:
                raise ValueError(f"invalid simulated snow layers: {block_id}")
            return BlockGeometry(
                material, "boxes", (
                    Aabb(0, 0, 0, 1, (layers - 1) / 8, 1),
                ),
            )
        if material == "minecraft:oak_stairs":
            if state.get("half", "bottom") != "bottom" or state.get(
                    "shape", "straight") != "straight":
                raise ValueError(f"unsupported simulated stair state: {block_id}")
            facing = state.get("facing", "south")
            upper = {
                "north": Aabb(0, .5, 0, 1, 1, .5),
                "south": Aabb(0, .5, .5, 1, 1, 1),
                "west": Aabb(0, .5, 0, .5, 1, 1),
                "east": Aabb(.5, .5, 0, 1, 1, 1),
            }.get(facing)
            if upper is None:
                raise ValueError(f"invalid simulated stair facing: {block_id}")
            boxes = tuple(sorted(
                (Aabb(0, 0, 0, 1, .5, 1), upper),
                key=Aabb.as_tuple,
            ))
            return BlockGeometry(material, "boxes", boxes)
        return BlockGeometry.full_cube(material)

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
    omitted_receipt_ticks: frozenset[int] = frozenset()


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
        self._external_perturbation_guard: Callable[[], bool] | None = None
        self._external_impulse_guard: Callable[[], bool] | None = None
        self.sequence = 0
        self.movement_tick = 1
        self.health = 20.0
        self.damage_taken = 0.0
        self.actions: list[ActionSnapshotV1] = []
        self.applied: list[MovementV1] = []
        self.sampled_inputs: list[TickInput] = []
        self.applied_commands: list[tuple[int, int | None, MovementV1]] = []
        self.submitted_commands: list[tuple[int, MovementV1, int, int]] = []
        self._pending_actions: list[tuple[int, ActionSnapshotV1]] = []
        self._active_action: ActionSnapshotV1 | None = None
        self._active_samples_left = 0
        self.sample_states: list[str] = []
        self.command_events: list[dict] = []
        self._sample_records: list[dict] = []
        self._receipt_sample_cursor = 0
        self._omitted_sample_ticks: set[int] = set()
        self.dispatched_perturbations: list[tuple[str, int]] = []
        self._recorded_perturbation_dispatches: set[tuple[str, int]] = set()
        self.applied_perturbations: list[tuple[str, int]] = []
        self._recorded_perturbations: set[tuple[str, int]] = set()
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

    def set_external_perturbation_guard(
        self, guard: Callable[[], bool],
    ) -> None:
        if not callable(guard):
            raise TypeError("external perturbation guard must be callable")
        self._external_perturbation_guard = guard

    def set_external_impulse_guard(
        self, guard: Callable[[], bool],
    ) -> None:
        """Limit shove injection to a scope that includes its real recovery owner."""
        if not callable(guard):
            raise TypeError("external impulse guard must be callable")
        self._external_impulse_guard = guard

    def _external_perturbations_enabled(self) -> bool:
        return (
            self._external_perturbation_guard is None
            or bool(self._external_perturbation_guard())
        )

    def _external_impulses_enabled(self) -> bool:
        return (
            self._external_perturbations_enabled()
            and (
                self._external_impulse_guard is None
                or bool(self._external_impulse_guard())
            )
        )

    # ----- observation -------------------------------------------------------
    def _eye(self) -> tuple[float, float, float]:
        x, y, z = self.state.position
        return x, y + (1.27 if self.state.pose == "crouching" else 1.62), z

    def _nearby_blocks(self):
        bx, by, bz = (math.floor(v) for v in self.state.position)
        blocks = []
        for (x, y, z), block_id in self.scene.solids.items():
            if max(abs(x - bx), abs(y - by), abs(z - bz)) <= BLOCK_RADIUS:
                geometry = self.scene.geometry(block_id)
                blocks.append(observed_block(
                    (x, y, z), geometry.material_key,
                    kind=geometry.collision_kind,
                    boxes=tuple(box.as_tuple() for box in geometry.boxes),
                ))
        return blocks

    def _blocked(self, point) -> bool:
        x, y, z = point
        cell = (math.floor(x), math.floor(y), math.floor(z))
        block_id = self.scene.solids.get(cell)
        if block_id is None:
            return False
        return any(
            box.min_x <= x <= box.max_x
            and box.min_y <= y <= box.max_y
            and box.min_z <= z <= box.max_z
            for box in self.scene.geometry(block_id).world_boxes(cell)
        )

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
                    geometry = self.scene.geometry(block_id)
                    blocks.append(observed_block(
                        cell, geometry.material_key,
                        kind=geometry.collision_kind,
                        boxes=tuple(box.as_tuple() for box in geometry.boxes),
                    ))
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
        perturbations_enabled = self._external_perturbations_enabled()
        world_edits = (
            self.perturbations.world_edits.get(tick, {})
            if perturbations_enabled else {}
        )
        if world_edits:
            self._record_perturbation_dispatch(
                "remove_landing_support", tick,
            )
        world_changed = False
        for cell, block_id in world_edits.items():
            if block_id is None:
                if cell in self.scene.solids:
                    self.scene.solids.pop(cell)
                    world_changed = True
            else:
                if self.scene.solids.get(cell) != block_id:
                    self.scene.solids[cell] = block_id
                    world_changed = True
        if world_changed:
            self._record_perturbation("remove_landing_support", tick)
        if world_changed:
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
        sampled = projected.tick_input
        assert sampled is not None
        was_on_ground = state.on_ground
        falling_from = state.fall_distance_blocks
        result = physics_step(state, sampled, self.world, JAVA_1_21_RULESET)
        if result.next_state is None:
            raise RuntimeError(f"calculator stopped: {result.status} {getattr(result, 'reasons', ())}")
        next_state = result.next_state
        if self._external_impulses_enabled() and tick in self.perturbations.impulses:
            impulse_kind = (
                "external_push"
                if self.perturbations.impulses[tick][2] >= 0.0
                else "external_push_backward"
            )
            self._record_perturbation_dispatch(impulse_kind, tick)
            vx, vy, vz = next_state.velocity_blocks_per_tick
            ix, iy, iz = self.perturbations.impulses[tick]
            next_state = replace(next_state, velocity_blocks_per_tick=(vx + ix, vy + iy, vz + iz))
            self._record_perturbation(impulse_kind, tick)
        if next_state.on_ground and not was_on_ground:
            damage = max(0.0, math.ceil(max(falling_from, next_state.fall_distance_blocks) - 3.0))
            self.health -= damage
            self.damage_taken += damage
        self.state = next_state
        self.movement_tick = tick
        self.applied.append(movement)
        self.sampled_inputs.append(sampled)

    def step(self, action, deadline, *, observation_request=None):
        if type(action) is not ActionSnapshotV1:
            raise AssertionError("wrong action type")
        self.actions.append(action)
        self.sequence += 1
        self.clock[0] += 50_000_000
        applied_request, receipt_request, sample_state = self._sample_command(action)
        observation = replace(
            self.observation(request_sequence_id=action.request_sequence_id,
                             air_positions=() if observation_request is None
                             else observation_request.air_positions),
            episode_id=action.episode_id,
        )
        leased = sample_state in {"neutral", "leased"}
        configured_omissions = (
            self.perturbations.omitted_receipt_ticks
            if self._external_perturbations_enabled() else frozenset()
        )
        self._omitted_sample_ticks.update(
            sample["movement_tick_id"]
            for sample in self._sample_records[self._receipt_sample_cursor:]
            if sample["movement_tick_id"] in configured_omissions
        )
        if self.movement_tick in configured_omissions:
            self._record_perturbation_dispatch(
                "omit_receipt", self.movement_tick,
            )
            self._record_perturbation("omit_receipt", self.movement_tick)
        samples = [sample for sample in
                   self._sample_records[self._receipt_sample_cursor:]
                   if sample["movement_tick_id"] not in
                   self._omitted_sample_ticks]
        self._receipt_sample_cursor = len(self._sample_records)
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id, generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observation.world_time_ticks.value,
                input_samples=self.movement_tick, leased_input_samples=1 if leased else 0,
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": len(self._omitted_sample_ticks),
            "oldest_retained_input_tick": (
                samples[0]["movement_tick_id"] if samples else
                self.movement_tick
            ),
            "input_applications": samples,
        })
        return BackendStepResultV1(observation, 0, False, False, receipt)

    def free_tick(self) -> None:
        """Advance client physics with any accepted in-flight lease after release."""
        self.clock[0] += 50_000_000
        self._sample_command(None)

    def stop_external_perturbations(self) -> None:
        """Stop future test disturbances without discarding accepted commands.

        The task may already be terminal while an accepted input lease still has
        to settle.  Clearing only the injector keeps that real client behaviour
        while preventing the test harness from manufacturing post-task events.
        """
        self.perturbations = Perturbations()

    def _record_perturbation(self, kind: str, tick: int) -> None:
        key = (kind, tick)
        if key not in self._recorded_perturbations:
            self._recorded_perturbations.add(key)
            self.applied_perturbations.append(key)

    def _record_perturbation_dispatch(self, kind: str, tick: int) -> None:
        key = (kind, tick)
        if key not in self._recorded_perturbation_dispatches:
            self._recorded_perturbation_dispatches.add(key)
            self.dispatched_perturbations.append(key)

    def _sample_command(
        self, action: ActionSnapshotV1 | None,
    ) -> tuple[int | None, int | None, str]:
        tick = self.movement_tick + 1
        if action is not None:
            delayed = (
                self._external_perturbations_enabled()
                and tick in self.perturbations.late_ticks
            )
            arrival_tick = tick + int(delayed)
            if delayed:
                self._record_perturbation_dispatch("late_input", tick)
                self._record_perturbation("late_input", tick)
            self.submitted_commands.append((action.request_sequence_id, action.movement,
                                            arrival_tick, action.deadline_monotonic_ns))
            self._pending_actions.append((arrival_tick, action))
        ready = [item for item in self._pending_actions if item[0] <= tick]
        if ready:
            arrival, selected = max(ready, key=lambda item: item[1].request_sequence_id)
            self._pending_actions = [item for item in self._pending_actions
                                     if item[0] > tick]
            self._active_action = selected
            self._active_samples_left = selected.valid_for_ticks
        active = self._active_action
        if active is None:
            sample_state = "no_accepted_command"
            movement = MovementV1()
            applied_request = None
        elif self.clock[0] >= active.deadline_monotonic_ns:
            sample_state = "expired"
            movement = MovementV1()
            applied_request = active.request_sequence_id
        elif self._active_samples_left == 0:
            sample_state = "lease_exhausted"
            movement = MovementV1()
            applied_request = active.request_sequence_id
        else:
            movement = active.movement
            applied_request = active.request_sequence_id
            sample_state = "neutral" if movement == MovementV1() else "leased"
            self._active_samples_left -= 1
        # The accepted command's look delta executes once; only its movement
        # may be sampled on later ticks of the same lease.
        active_look = active if ready and sample_state in {"neutral", "leased"} else None
        look_yaw = 0.0 if active_look is None else active_look.look.yaw_delta_degrees
        look_pitch = 0.0 if active_look is None else active_look.look.pitch_delta_degrees
        self.advance(movement, look_yaw, look_pitch)
        self.applied_commands.append((tick, applied_request if sample_state in {"neutral", "leased"} else None, movement))
        self.sample_states.append(sample_state)
        movement_owner = (applied_request if sample_state in {"neutral", "leased"}
                          else None)
        sampled = self.sampled_inputs[-1]
        self._sample_records.append({
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": self.movement_tick,
            "episode_id": None if applied_request is None else active.episode_id,
            "request_sequence_id": applied_request,
            "sampled_at_jvm_ns": self.movement_tick,
            "state": "disallowed" if sample_state == "no_accepted_command" else sample_state,
            "forward": sampled.forward, "strafe": sampled.strafe,
            "jump": sampled.jump, "sneak": sampled.sneak,
            "sprint": sampled.sprint,
        })
        self.command_events.append({
            "tick": tick,
            "ready": tuple(item[1].request_sequence_id for item in ready),
            "accepted": None if not ready else selected.request_sequence_id,
            "superseded": tuple(item[1].request_sequence_id for item in ready
                                if item[1] is not selected),
            "sample_state": sample_state,
            "applied_request": movement_owner,
            "receipt_request": applied_request,
        })
        return movement_owner, applied_request, sample_state

    def close(self):
        pass


def _air_result(value):
    from mc2p.contracts.observation_v3 import AirQueryResultV3
    return AirQueryResultV3(tuple(value["position"]), value["status"],
                            value["observer_distance_blocks"], value["lower_region_visible"])
