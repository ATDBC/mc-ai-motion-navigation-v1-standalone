"""Deterministic short-action controller used only by the D098 experiment."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import _goal_check, _potential_goal
from mc2p.motion_nav.physics_types import CalculationStatus


GROUND_GAITS = ("W", "S")
JUMP_GAITS = ("WJ", "SJ")
AIR_GAITS = ("W", "S", "N")
TAKEOFF_GRID = tuple(round(i / 20, 2) for i in range(-6, 31))
BRAKE_GRID = tuple(round(i / 10, 1) for i in range(21))
AIR_TICKS = tuple(range(17))
MAX_GROUND_TICKS = 30
MAX_TOTAL_TICKS = 40
_GAIT_FLAGS = {"W": (False, False), "S": (False, True),
               "WJ": (True, False), "SJ": (True, True)}

LEFT_GROUND = "left_ground_before_takeoff"
NO_TRIGGER = "no_trigger"
TICK_BUDGET = "tick_budget"
GOAL_NOT_REACHED = "goal_not_reached"
COLLISION = "horizontal_collision"
NEEDS_WORLD = "needs_world"
GOAL_UNKNOWN = "goal_unknown"
GOAL_REJECTED = "goal_rejected"


def grid_description() -> dict:
    return {"ground_gaits": GROUND_GAITS, "jump_gaits": JUMP_GAITS,
            "air_gaits": AIR_GAITS, "takeoff": TAKEOFF_GRID,
            "brake": BRAKE_GRID, "air_ticks": AIR_TICKS,
            "max_ground_ticks": MAX_GROUND_TICKS,
            "max_total_ticks": MAX_TOTAL_TICKS}


def grid_hash() -> str:
    return hashlib.sha256(json.dumps(grid_description(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ControllerParams:
    ground_gait: str
    takeoff_d: float | None
    jump_gait: str | None = None
    air_gait: str | None = None
    air_ticks: int = 0
    brake_b: float | None = None

    def __post_init__(self) -> None:
        if self.ground_gait not in GROUND_GAITS:
            raise ValueError(f"ground gait {self.ground_gait!r}")
        if self.takeoff_d is None:
            if (self.brake_b not in BRAKE_GRID or self.jump_gait is not None
                    or self.air_gait is not None or self.air_ticks != 0):
                raise ValueError("no-jump params need only a brake trigger on the grid")
        elif (self.takeoff_d not in TAKEOFF_GRID or self.jump_gait not in JUMP_GAITS
              or self.air_gait not in AIR_GAITS or self.air_ticks not in AIR_TICKS
              or self.brake_b is not None):
            raise ValueError("jump params must lie on the grid and carry no brake trigger")

    def to_dict(self) -> dict:
        return {"ground_gait": self.ground_gait, "takeoff_d": self.takeoff_d,
                "jump_gait": self.jump_gait, "air_gait": self.air_gait,
                "air_ticks": self.air_ticks, "brake_b": self.brake_b}

    @classmethod
    def from_dict(cls, value: dict) -> "ControllerParams":
        return cls(value["ground_gait"], value["takeoff_d"], value["jump_gait"],
                   value["air_gait"], value["air_ticks"], value["brake_b"])


@dataclass(frozen=True, slots=True)
class TemplateGeometry:
    feature_z: float | None
    goal_centre: tuple[float, float, float]
    body_half_width: float = .3

    @classmethod
    def for_template(cls, template_id: str) -> "TemplateGeometry":
        from .frozen_templates import TEMPLATES, goal_centre
        return cls(TEMPLATES[template_id].feature_z, goal_centre(template_id))

    def feature_distance(self, state) -> float:
        if self.feature_z is None:
            raise ValueError("geometry has no takeoff feature")
        return self.feature_z - (state.position[2] + self.body_half_width)

    def goal_distance(self, state) -> float:
        return math.hypot(self.goal_centre[0] - state.position[0],
                          self.goal_centre[2] - state.position[2])


class ControllerNode:
    __slots__ = ("inputs", "states", "children")

    def __init__(self, inputs, states):
        self.inputs, self.states, self.children = inputs, states, {}


class ControllerTree:
    """Memoised lockstep rollout.  One request owns one typed physics counter."""

    def __init__(self, request, world, geometry: TemplateGeometry,
                 counter: CountedPhysics | None = None):
        if request.input_prefix:
            raise ValueError("controller does not support a shared input prefix")
        if counter is not None and counter.budget != request.budget:
            raise ValueError("counter must own the unchanged request budget")
        self.request, self.world, self.geometry = request, world, geometry
        self.counter = counter or CountedPhysics(request.budget)
        self.root = ControllerNode((), request.entry_states)
        self.stop = request.stop_input
        self.gaits = {
            gait: tuple((index, command.movement_yaw_radians, command)
                        for index, command in enumerate(request.supported_inputs)
                        if command.forward == 1. and command.strafe == 0.
                        and not command.sneak
                        and (command.jump, command.sprint) == flags)
            for gait, flags in _GAIT_FLAGS.items()
        }

    def steer(self, gait: str, state):
        choices = self.gaits[gait]
        if len(choices) == 1:
            return choices[0][2]
        cx, _, cz = self.geometry.goal_centre
        wanted = math.atan2(-(cx - state.position[0]), cz - state.position[2])
        return min(choices, key=lambda item: (
            abs(math.remainder(item[1] - wanted, math.tau)), item[0]))[2]

    def advance(self, node: ControllerNode, command) -> ControllerNode | str:
        child = node.children.get(command)
        if child is None:
            child = node.children[command] = self._step(node, command)
        return child

    def _step(self, node: ControllerNode, command) -> ControllerNode | str:
        states = []
        for state in node.states:
            result = self.counter.step(state, command, self.world)
            if result.status is CalculationStatus.NEEDS_WORLD:
                return NEEDS_WORLD
            if result.status is not CalculationStatus.OK:
                return f"step_{result.status.value}"
            if result.next_state.horizontal_collision:
                return COLLISION
            states.append(result.next_state)
        return ControllerNode(node.inputs + (command,), tuple(states))

    def goal_pass(self, node: ControllerNode) -> str | None:
        for branch, state in zip(self.request.timing_branches, node.states):
            check, missing = _goal_check(self.request, branch, state, self.world, self.counter)
            if missing:
                return GOAL_UNKNOWN
            if check is None or not check.accepted:
                return GOAL_REJECTED
        return None


def _ground_phase(tree: ControllerTree, params: ControllerParams):
    jumping = params.takeoff_d is not None
    if jumping and tree.geometry.feature_z is None:
        raise ValueError("jump params need a takeoff reference line")
    node = tree.root
    for ticks in range(MAX_GROUND_TICKS + 1):
        lead = node.states[0]
        if jumping:
            if not all(state.on_ground for state in node.states):
                return None, LEFT_GROUND
            if tree.geometry.feature_distance(lead) <= params.takeoff_d:
                return node, None
        elif tree.geometry.goal_distance(lead) <= params.brake_b:
            return node, None
        if ticks == MAX_GROUND_TICKS:
            break
        node = tree.advance(node, tree.steer(params.ground_gait, lead))
        if isinstance(node, str):
            return None, node
    return None, NO_TRIGGER


def _brake_phase(tree: ControllerTree, node: ControllerNode):
    while True:
        if _potential_goal(tree.request, node.states):
            return node, None
        speeds = [20. * math.hypot(state.velocity_blocks_per_tick[0],
                                   state.velocity_blocks_per_tick[2])
                  for state in node.states]
        if (len(node.inputs) >= MAX_TOTAL_TICKS
                or (all(state.on_ground for state in node.states) and max(speeds) == 0.)
                or max(speeds) + 1.e-12
                < tree.request.minimum_terminal_speed_blocks_per_second):
            return None, GOAL_NOT_REACHED
        node = tree.advance(node, tree.stop)
        if isinstance(node, str):
            return None, node


def run(tree: ControllerTree, params: ControllerParams):
    node, reason = _ground_phase(tree, params)
    if node is None:
        return None, reason
    if params.takeoff_d is not None:
        if len(node.inputs) + 1 + params.air_ticks > MAX_TOTAL_TICKS:
            return None, TICK_BUDGET
        node = tree.advance(node, tree.steer(params.jump_gait, node.states[0]))
        for _ in range(params.air_ticks):
            if isinstance(node, str):
                return None, node
            command = tree.stop if params.air_gait == "N" else tree.steer(params.air_gait, node.states[0])
            node = tree.advance(node, command)
        if isinstance(node, str):
            return None, node
    return _brake_phase(tree, node)


def evaluate(tree: ControllerTree, params: ControllerParams):
    node, reason = run(tree, params)
    if node is None:
        return None, reason
    reason = tree.goal_pass(node)
    return (node, "ok") if reason is None else (None, reason)
