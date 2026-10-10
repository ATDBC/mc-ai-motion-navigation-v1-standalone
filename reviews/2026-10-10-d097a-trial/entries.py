"""D097-A 试做：冻结模板、入口分箱和请求构造（审查方所有，子 agent 只读）。

运行环境：仓库根目录为项目 be38685 的 checkout，PYTHONPATH=<该根目录>:<本目录>。
本模块只读调用项目的 trajectory_proto 与 mc2p，不修改它们。
冻结内容见 ../2026-10-10-d097a-trial.md 第 3—4 节及其补记 1；改动这里的任何常量都等于改预登记。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import json
import math
import random

from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.contracts import (
    ApplicationEvidence, KnownInputApplication, TimingBranch, TrajectorySearchRequest,
)
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import (
    _boundary_evidence, _goal_check, _potential_goal,
)
from experiments.motion_navigation.trajectory_proto.scenarios import P0_BUDGET, _input_tiers
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, CalculationStatus, PhysicsState, TickInput
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldSessionId, WorldView,
)

SESSION = WorldSessionId("d097a-trial-v1")
GEOMETRY_REVISION = 3
ANCHOR_TICK = 20
Z0 = .5
X_BASE = .5
BODY_HALF_WIDTH = .3
TIERS = _input_tiers("A15")
ALPHABET = TIERS[-1].inputs
STOP = TIERS[0].inputs[2]
TRAIN_SEED = 20261010
VALIDATION_SEED = 20261011
SAMPLES_PER_BIN = 200


@dataclass(frozen=True, slots=True)
class Template:
    template_id: str
    gap: tuple[int, int] | None          # [z_start, z_end) with no floor at y=0
    step_from_z: int | None              # y=1 solid for z >= this
    goal_region: tuple[float, float, float, float, float, float]
    minimum_speed: float                 # blocks/second
    maximum_speed: float                 # blocks/second
    feature_z: float | None              # takeoff reference line; None = no jump
    expect_supported: bool = True


TEMPLATES: dict[str, Template] = {t.template_id: t for t in (
    Template("gap1", (4, 5), None, (.3, 1., 5.02, .7, 1.05, 6.15), 0., 0., 4.),
    Template("gap2", (4, 6), None, (.3, 1., 6.02, .7, 1.05, 7.15), 0., 0., 4.),
    Template("gap3", (4, 7), None, (.3, 1., 7.02, .7, 1.05, 8.15), 0., 0., 4.),
    Template("gap2c", (4, 6), None, (.3, 1., 6.02, .7, 1.05, 7.15), 1., 5., 4.),
    Template("jumpup", None, 3, (.3, 2., 3.5, .7, 2.05, 4.5), 0., 0., 3.),
    Template("turn", None, None, (-3.2, 1., .3, -1.05, 1.05, 1.8), 0., 0., None),
    # Addendum 1: the pre-registered gap4 negative is solvable from rest (selftest), so the
    # infeasible negative is a five-block gap instead.
    Template("gap5", (4, 9), None, (.3, 1., 9.02, .7, 1.05, 10.15), 0., 0., 4., expect_supported=False),
)}
SUPPORTED_TEMPLATES = tuple(t for t, spec in TEMPLATES.items() if spec.expect_supported)


@dataclass(frozen=True, slots=True)
class EntryBin:
    bin_id: str
    gait: str                            # "rest" | "walk" | "sprint"
    ticks: tuple[int, int]               # inclusive pre-roll tick range
    offset: tuple[float, float]          # lateral offset range (blocks)
    heading_deg: tuple[float, float]     # pre-roll heading range (degrees)


_OFFSETS = (("L", (-.15, -.05)), ("C", (-.05, .05)), ("R", (.05, .15)))
_HEADINGS = (("n", (-12., -4.)), ("s", (-4., 4.)), ("p", (4., 12.)))


def entry_bins() -> tuple[EntryBin, ...]:
    bins = [EntryBin(f"rest-{o}", "rest", (0, 0), off, (0., 0.)) for o, off in _OFFSETS]
    for gait, ticks in (("walk", (2, 10)), ("sprint", (3, 12))):
        for o, off in _OFFSETS:
            for h, head in _HEADINGS:
                bins.append(EntryBin(f"{gait}-{o}-{h}", gait, ticks, off, head))
    return tuple(bins)


@dataclass(frozen=True, slots=True)
class EntrySpec:
    template_id: str
    gait: str
    ticks: int
    offset: float
    heading_deg: float

    def key(self) -> str:
        return json.dumps([self.template_id, self.gait, self.ticks, round(self.offset, 12),
                           round(self.heading_deg, 12)])


class EntryInvalid(Exception):
    """The pre-rolled entry violates the scanner's waiting-branch preconditions."""


def sample_entries(template_id: str, entry_bin: EntryBin, count: int, seed: int) -> tuple[EntrySpec, ...]:
    """Deterministic uniform samples inside one bin (integer ticks, continuous offset/heading)."""
    digest = hashlib.sha256(f"{seed}/{template_id}/{entry_bin.bin_id}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    rows = []
    for _ in range(count):
        ticks = rng.randint(*entry_bin.ticks)
        offset = rng.uniform(*entry_bin.offset)
        heading = 0. if entry_bin.gait == "rest" else rng.uniform(*entry_bin.heading_deg)
        rows.append(EntrySpec(template_id, entry_bin.gait, ticks, offset, heading))
    return tuple(rows)


def bin_corner_points(template_id: str, entry_bin: EntryBin) -> tuple[EntrySpec, ...]:
    """Bin centre plus corners of the (ticks, offset, heading) box; rest bins vary offset only."""
    def mid(pair):
        return (pair[0] + pair[1]) / 2.
    centre = EntrySpec(template_id, entry_bin.gait, round(mid(entry_bin.ticks)),
                       mid(entry_bin.offset), mid(entry_bin.heading_deg))
    if entry_bin.gait == "rest":
        corners = [replace(centre, offset=value) for value in entry_bin.offset]
    else:
        corners = [EntrySpec(template_id, entry_bin.gait, t, o, h)
                   for t in entry_bin.ticks for o in entry_bin.offset for h in entry_bin.heading_deg]
    return (centre, *corners)


def _yaw(heading_deg: float) -> float:
    return math.radians(heading_deg)


def preroll_input(spec: EntrySpec) -> TickInput:
    if spec.gait == "rest":
        return STOP
    return TickInput(1., 0., False, False, spec.gait == "sprint", _yaw(spec.heading_deg))


def _rest_state(position, tick) -> PhysicsState:
    return PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema, SESSION, tick,
        position, (0., -.0784, 0.), 0., 0., "standing", .6, 1.8,
        True, False, True, False, False, 0, 0., .1, .6, .08, .42,
        20, 5., "survival", (), False, False, False, False, False, False,
    )


@lru_cache(maxsize=None)
def build_world(template_id: str) -> PhysicsWorldView:
    spec = TEMPLATES[template_id]
    stamp = ObservationStamp(SESSION, 0, 0, "d097a-trial-v1", 0)
    facts = {}
    for x in range(-9, 10):
        for y in range(-50, 8):
            for z in range(-14, 17):
                solid = y == 0 and not (spec.gap is not None and spec.gap[0] <= z < spec.gap[1])
                if spec.step_from_z is not None:
                    solid |= y == 1 and z >= spec.step_from_z
                facts[(x, y, z)] = CellFact(
                    CellKnowledge.BLOCK if solid else CellKnowledge.AIR, stamp,
                    BlockGeometry.full_cube("minecraft:stone") if solid else None)
    return PhysicsWorldView(WorldView.detached(SESSION, GEOMETRY_REVISION, 0, facts), JAVA_1_21_RULESET)


@lru_cache(maxsize=None)
def _flat_world() -> PhysicsWorldView:
    return build_world("turn")


@lru_cache(maxsize=4096)
def _preroll_displacement(gait: str, ticks: int, heading_deg: float) -> tuple[float, float]:
    state = _rest_state((X_BASE, 1., Z0), 0)
    command = preroll_input(EntrySpec("turn", gait, ticks, 0., heading_deg))
    for _ in range(ticks):
        state = step(state, command, _flat_world(), JAVA_1_21_RULESET).next_state
    return state.position[0] - X_BASE, state.position[2] - Z0


def goal_for(template_id: str) -> GoalState:
    spec = TEMPLATES[template_id]
    return GoalState(Aabb(*spec.goal_region), GoalSupport.SOLID,
                     frozenset({MovementMode.WALK, MovementMode.SPRINT}), frozenset({"standing"}),
                     spec.maximum_speed)


def goal_centre(template_id: str) -> tuple[float, float, float]:
    x0, y0, z0, x1, y1, z1 = TEMPLATES[template_id].goal_region
    return ((x0 + x1) / 2., y0, (z0 + z1) / 2.)


def make_request(spec: EntrySpec) -> tuple[TrajectorySearchRequest, PhysicsWorldView]:
    """Pre-roll on flat ground to the start line, then build both timing branches."""
    template = TEMPLATES[spec.template_id]
    world = build_world(spec.template_id)
    if spec.gait == "rest":
        anchor = _rest_state((X_BASE + spec.offset, 1., Z0), ANCHOR_TICK)
    else:
        dx, dz = _preroll_displacement(spec.gait, spec.ticks, spec.heading_deg)
        state = _rest_state((X_BASE + spec.offset - dx, 1., Z0 - dz), ANCHOR_TICK - spec.ticks)
        command = preroll_input(spec)
        for _ in range(spec.ticks):
            result = step(state, command, world, JAVA_1_21_RULESET)
            if result.status is not CalculationStatus.OK:
                raise EntryInvalid(f"pre-roll calculation {result.status}")
            state = result.next_state
        anchor = state
    prelude_input = preroll_input(spec)
    late_result = step(anchor, prelude_input, world, JAVA_1_21_RULESET)
    if late_result.status is not CalculationStatus.OK:
        raise EntryInvalid(f"late prelude calculation {late_result.status}")
    late = late_result.next_state
    if (anchor.movement_tick_id != ANCHOR_TICK or not anchor.on_ground or not late.on_ground
            or late.horizontal_collision or not math.isclose(anchor.position[1], late.position[1], abs_tol=1e-7)):
        raise EntryInvalid("entry or late prelude is not grounded on the same level")
    prelude = KnownInputApplication(SESSION, 99, ANCHOR_TICK + 1, prelude_input, "known-wait-99",
                                    ApplicationEvidence.PREDICTED)
    request = TrajectorySearchRequest(
        f"d097a-{spec.template_id}-{hashlib.sha256(spec.key().encode()).hexdigest()[:16]}",
        (anchor, late), SESSION, GEOMETRY_REVISION, goal_for(spec.template_id), 1,
        TaskDamageBudget(), f"anchor-{ANCHOR_TICK}", "ledger-99", P0_BUDGET, tuple(TimingBranch),
        (ANCHOR_TICK + 1, ANCHOR_TICK + 2), (), ALPHABET, TIERS, ((), (prelude,)), 100,
        (goal_centre(spec.template_id),),
        minimum_terminal_speed_blocks_per_second=template.minimum_speed, stop_input=STOP,
    )
    return request, world


def feature_distance(state: PhysicsState, template_id: str) -> float | None:
    """Distance from the body's leading face to the takeoff reference line (south-facing)."""
    feature = TEMPLATES[template_id].feature_z
    if feature is None:
        return None
    return feature - (state.position[2] + BODY_HALF_WIDTH)


def horizontal_goal_distance(state: PhysicsState, template_id: str) -> float:
    cx, _, cz = goal_centre(template_id)
    return math.hypot(cx - state.position[0], cz - state.position[2])


def heading_error(state: PhysicsState, command: TickInput, template_id: str) -> float:
    cx, _, cz = goal_centre(template_id)
    wanted = math.atan2(-(cx - state.position[0]), cz - state.position[2])  # vx=-sin(yaw), vz=cos(yaw)
    return abs(math.remainder(command.movement_yaw_radians - wanted, math.tau))


def verify_candidate(request, world, inputs, counter: CountedPhysics | None = None):
    """Project acceptance path: per-branch goal check, then the full commitment scan.

    Returns (accepted, reason, scan_or_None). Nothing here is a permit; it is the
    same check m0_probe uses and the reference against which M2 is compared.
    """
    counter = counter or CountedPhysics(replace(P0_BUDGET, max_nodes=10_000_000,
                                                max_physics_steps=100_000_000))
    states = []
    for entry in request.entry_states:
        state = entry
        for command in inputs:
            result = counter.step(state, command, world)
            if result.status is not CalculationStatus.OK:
                return False, f"rollout_{result.status.value}", None
            state = result.next_state
        states.append(state)
    if not _potential_goal(request, states):
        return False, "goal_band", None
    for branch, state in zip(request.timing_branches, states):
        check, missing = _goal_check(request, branch, state, world, counter)
        if missing:
            return False, "goal_unknown", None
        if check is None or not check.accepted:
            return False, "goal_rejected", None
    scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs), counter=counter)
    if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
        return False, f"scan_{scan.status.value}_{scan.reason}", scan
    return True, "verified", scan


def identity() -> dict:
    """Hashes that a generated table must bind (G4)."""
    templates = {k: [v.gap, v.step_from_z, v.goal_region, v.minimum_speed, v.maximum_speed,
                     v.feature_z, v.expect_supported] for k, v in TEMPLATES.items()}
    bins = [[b.bin_id, b.gait, b.ticks, b.offset, b.heading_deg] for b in entry_bins()]
    return {
        "ruleset_id": JAVA_1_21_RULESET.ruleset_id,
        "state_schema": JAVA_1_21_RULESET.state_schema,
        "alphabet": hashlib.sha256(repr(ALPHABET).encode()).hexdigest(),
        "templates": hashlib.sha256(json.dumps(templates, sort_keys=True).encode()).hexdigest(),
        "bins": hashlib.sha256(json.dumps(bins).encode()).hexdigest(),
        "entries_module": "d097a-entries-v1",
    }
