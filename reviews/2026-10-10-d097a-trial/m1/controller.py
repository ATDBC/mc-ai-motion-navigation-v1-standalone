"""D097-A M1: deterministic parameterised controller (pure function of Params and the entry).

Phases (state-relative triggers only, no absolute tick counts):
  ground  <= 30 ticks of the ground gait, steered towards the goal centre; ends when
          - takeoff_d is set: all branches grounded and the leading face is within takeoff_d
            of the takeoff reference line (a branch off the ground first -> failure), or
          - takeoff_d is None: horizontal distance to the goal centre <= brake_b.
  jump    one steered tick of the jump gait, then air_ticks ticks of the air gait.
  brake   STOP ticks until the first prefix whose two branches satisfy `_potential_goal`.
Both timing branches are stepped in lockstep with the same input (CountedPhysics.step).
The controller only proposes a candidate; acceptance is `verify()` (= entries.verify_candidate).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math

import entries as E
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import _goal_check, _potential_goal
from mc2p.motion_nav.physics_types import CalculationStatus

GROUND_GAITS = ("W", "S")
JUMP_GAITS = ("WJ", "SJ")
AIR_GAITS = ("W", "S", "N")
TAKEOFF_GRID = tuple(round(i / 20, 2) for i in range(-6, 31))      # -0.30 .. 1.50, 37 values
BRAKE_GRID = tuple(round(i / 10, 1) for i in range(0, 21))         # 0.0 .. 2.0, 21 values
AIR_TICKS = tuple(range(17))                                        # 0 .. 16
MAX_GROUND_TICKS = 30
MAX_TOTAL_TICKS = 40
UNBOUNDED = SearchBudget(10_000_000, 100_000_000, 40, 2)
_GAIT_FLAGS = {"W": (False, False), "S": (False, True), "WJ": (True, False), "SJ": (True, True)}  # jump, sprint

# typed failure reasons
LEFT_GROUND = "left_ground_before_takeoff"
NO_TRIGGER = "no_trigger"
TICK_BUDGET = "tick_budget"
GOAL_NOT_REACHED = "goal_not_reached"
COLLISION = "horizontal_collision"
NEEDS_WORLD = "needs_world"
GOAL_UNKNOWN = "goal_unknown"
GOAL_REJECTED = "goal_rejected"


def grid_description() -> dict:
    return {"ground_gaits": GROUND_GAITS, "jump_gaits": JUMP_GAITS, "air_gaits": AIR_GAITS,
            "takeoff": TAKEOFF_GRID, "brake": BRAKE_GRID, "air_ticks": AIR_TICKS,
            "max_ground_ticks": MAX_GROUND_TICKS, "max_total_ticks": MAX_TOTAL_TICKS}


def grid_hash() -> str:
    return hashlib.sha256(json.dumps(grid_description(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Params:
    ground_gait: str
    takeoff_d: float | None
    jump_gait: str | None = None
    air_gait: str | None = None
    air_ticks: int = 0
    brake_b: float | None = None

    def __post_init__(self):
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

    def sort_key(self) -> tuple:
        return (self.ground_gait, self.takeoff_d is not None, self.takeoff_d or 0., self.jump_gait or "",
                self.air_gait or "", self.air_ticks, self.brake_b or 0.)

    def to_dict(self) -> dict:
        return {"ground_gait": self.ground_gait, "takeoff_d": self.takeoff_d, "jump_gait": self.jump_gait,
                "air_gait": self.air_gait, "air_ticks": self.air_ticks, "brake_b": self.brake_b}

    @classmethod
    def from_dict(cls, value: dict) -> "Params":
        return cls(value["ground_gait"], value["takeoff_d"], value["jump_gait"], value["air_gait"],
                   value["air_ticks"], value["brake_b"])


@dataclass(frozen=True, slots=True)
class Geometry:
    """What the controller needs to know about the task: takeoff reference line and goal centre."""
    feature_z: float | None
    goal_centre: tuple[float, float, float]

    @classmethod
    def for_template(cls, template_id: str) -> "Geometry":
        return cls(E.TEMPLATES[template_id].feature_z, E.goal_centre(template_id))

    def feature_distance(self, state) -> float:
        return self.feature_z - (state.position[2] + E.BODY_HALF_WIDTH)

    def goal_distance(self, state) -> float:
        return math.hypot(self.goal_centre[0] - state.position[0], self.goal_centre[2] - state.position[2])


def unbounded(request):
    return request if request.budget == UNBOUNDED else replace(request, budget=UNBOUNDED)


def verify(request, world, inputs, counter: CountedPhysics | None = None):
    """Acceptance check = entries.verify_candidate with a budget the shared counter may own.

    entries.verify_candidate builds its own counter whose budget differs from request.budget, which
    scan_commitment rejects (ContractViolation); running it on an unbounded-budget copy of the
    request (as m0_probe does) is the only change.
    """
    request = unbounded(request)
    return E.verify_candidate(request, world, inputs, counter or CountedPhysics(UNBOUNDED))


class Node:
    """One input prefix with the states of every timing branch after it."""
    __slots__ = ("inputs", "states", "children")

    def __init__(self, inputs, states):
        self.inputs, self.states, self.children = inputs, states, {}


class Tree:
    """Memoised lockstep rollout of both branches; a prefix is stepped (and counted) once."""

    def __init__(self, request, world, geometry: Geometry, counter: CountedPhysics | None = None):
        if request.input_prefix:
            raise ValueError("controller does not support a shared input prefix")
        self.request, self.world, self.geometry = unbounded(request), world, geometry
        self.counter = counter or CountedPhysics(UNBOUNDED)
        self.root = Node((), request.entry_states)
        self.stop = request.stop_input
        supported = request.supported_inputs
        self.gaits = {gait: tuple((i, c.movement_yaw_radians, c) for i, c in enumerate(supported)
                                  if c.forward == 1. and c.strafe == 0. and not c.sneak
                                  and (c.jump, c.sprint) == flags)
                      for gait, flags in _GAIT_FLAGS.items()}

    def steer(self, gait: str, state):
        """Gait input whose heading is closest to the goal centre (ties: lowest alphabet index)."""
        choices = self.gaits[gait]
        if len(choices) == 1:
            return choices[0][2]
        cx, _, cz = self.geometry.goal_centre
        wanted = math.atan2(-(cx - state.position[0]), cz - state.position[2])
        return min(choices, key=lambda c: (abs(math.remainder(c[1] - wanted, math.tau)), c[0]))[2]

    def advance(self, node: Node, command) -> Node | str:
        child = node.children.get(command)
        if child is None:
            child = node.children[command] = self._step(node, command)
        return child

    def _step(self, node: Node, command) -> Node | str:
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
        return Node(node.inputs + (command,), tuple(states))

    def goal_pass(self, node: Node) -> str | None:
        """None when both branches pass the per-branch goal check, else a typed reason."""
        for branch, state in zip(self.request.timing_branches, node.states):
            check, missing = _goal_check(self.request, branch, state, self.world, self.counter)
            if missing:
                return GOAL_UNKNOWN
            if check is None or not check.accepted:
                return GOAL_REJECTED
        return None


def ground_phase(tree: Tree, params: Params) -> tuple[Node | None, str | None]:
    """Node at which the trigger fires (before its input is applied), or a failure reason."""
    geometry, jump = tree.geometry, params.takeoff_d is not None
    if jump and geometry.feature_z is None:
        raise ValueError("jump params need a takeoff reference line")
    node = tree.root
    for ticks in range(MAX_GROUND_TICKS + 1):
        lead = node.states[0]
        if jump:
            if not all(state.on_ground for state in node.states):
                return None, LEFT_GROUND
            if geometry.feature_distance(lead) <= params.takeoff_d:
                return node, None
        elif geometry.goal_distance(lead) <= params.brake_b:
            return node, None
        if ticks == MAX_GROUND_TICKS:
            break
        node = tree.advance(node, tree.steer(params.ground_gait, lead))
        if type(node) is str:
            return None, node
    return None, NO_TRIGGER


def brake_phase(tree: Tree, node: Node) -> tuple[Node | None, str | None]:
    request = tree.request
    while True:
        if _potential_goal(request, node.states):
            return node, None
        speeds = [20. * math.hypot(s.velocity_blocks_per_tick[0], s.velocity_blocks_per_tick[2])
                  for s in node.states]
        if (len(node.inputs) >= MAX_TOTAL_TICKS
                or (all(s.on_ground for s in node.states) and max(speeds) == 0.)
                or max(speeds) + 1.e-12 < request.minimum_terminal_speed_blocks_per_second):
            return None, GOAL_NOT_REACHED
        node = tree.advance(node, tree.stop)
        if type(node) is str:
            return None, node


def run(tree: Tree, params: Params) -> tuple[Node | None, str | None]:
    """Candidate node (first prefix satisfying the potential goal) or a typed failure reason."""
    node, reason = ground_phase(tree, params)
    if node is None:
        return None, reason
    if params.takeoff_d is not None:
        if len(node.inputs) + 1 + params.air_ticks > MAX_TOTAL_TICKS:
            return None, TICK_BUDGET
        node = tree.advance(node, tree.steer(params.jump_gait, node.states[0]))
        for _ in range(params.air_ticks):
            if type(node) is str:
                return None, node
            command = tree.stop if params.air_gait == "N" else tree.steer(params.air_gait, node.states[0])
            node = tree.advance(node, command)
        if type(node) is str:
            return None, node
    return brake_phase(tree, node)


def goal_slack(request, node: Node) -> float:
    """Smallest distance (blocks, both branches) from the final position to the goal-region walls."""
    region = request.goal.region
    return min(min(s.position[0] - region.min_x, region.max_x - s.position[0],
                   s.position[2] - region.min_z, region.max_z - s.position[2]) for s in node.states)


def evaluate(tree: Tree, params: Params) -> tuple[Node | None, str]:
    """Rollout + potential goal + per-branch goal check (the cheap, pre-scan part). Reason 'ok' on pass."""
    node, reason = run(tree, params)
    if node is None:
        return None, reason
    reason = tree.goal_pass(node)
    return (node, "ok") if reason is None else (None, reason)
