"""Scenario runner and invariant monitor for the step-0 prototype.

Formal chain: PlayerRuntimeV1 -> RuntimeNavigationDriver -> NavigationSession ->
(inline) planner, (inline) motion solver, admission, executors; the game is
CalculatorBackend.  Two test substitutions are made, both synchronous versions of
existing worker interfaces:
  * InlinePlannerWorker, which runs planner_worker._execute_job (the same
    function, with the same error wrapping, as the real planner process);
  * InlineMotionWorker, which runs motion_worker._execute_job on poll.  The
    coordinator checks `type(worker) is MotionSolverWorker`, so the prototype
    rebinds that module name; the plan replaces the exact-type check with a
    small protocol.

The invariant monitor reads a few session internals (_executor, _request,
_edge_probe).  In the plan these become explicit read-only properties
(current controller, request generation), so the monitor does not depend on
private state.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import random
from typing import Callable

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav import motion_coordination, motion_worker, planner_worker
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import Aabb
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport
from mc2p.motion_nav.movement_transition import MovementMode
from tests.test_player_runtime import _RecordingTrace

from sim_backend import CalculatorBackend, Perturbations, Scene

CONFIG = Path("config/motion-navigation")
TERMINAL_DRIVER = {"success", "failed", "cancelled", "stopped", "interaction_required"}
AIR_ACTIONS = {"ControlledDropSegment", "JumpGapSegment", "JumpUpSegment", "StepSegment"}


def _goal(position, risk_policy_id="no_expected_damage") -> GoalState:
    """Same goal region as scripts/continuous_height_runtime.py (formal Fabric probe)."""
    x, y, z = position
    return GoalState(Aabb(x - .20, y - .08, z - .20, x + .20, y + .08, z + .20),
                     GoalSupport.SOLID, frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
                     risk_policy_id=risk_policy_id)


class InlinePlannerWorker:
    """Synchronous stand-in for PlannerWorker using the worker's own job function."""

    def __init__(self):
        self._job = None

    def is_alive(self) -> bool:
        return True

    def submit_surface_snapshot(self, snapshot, ground_profile, step_profile, request,
                                jump_profile=None, *, air_profiles=(), ground_mode_profile=None) -> bool:
        self._job = planner_worker._PlanningJob(None, snapshot, ground_profile, ground_mode_profile,
                                                step_profile, jump_profile, air_profiles, request)
        return True

    def poll_latest(self):
        job, self._job = self._job, None
        return None if job is None else planner_worker._execute_job(job)

    def close(self) -> None:
        self._job = None


class InlineMotionWorker:
    """Synchronous stand-in with MotionSolverWorker's interface."""

    pid = None

    def __init__(self, *_, **__):
        self._pending = []

    def is_alive(self) -> bool:
        return True

    def submit(self, job) -> bool:
        self._pending.append(job)
        return True

    def poll_available(self):
        done = tuple(motion_worker._execute_job(job) for job in self._pending)
        self._pending = []
        return done

    def close(self) -> None:
        self._pending = []


motion_coordination.MotionSolverWorker = InlineMotionWorker


@dataclass
class Event:
    """Run `action(context)` once, on the first tick where `when(context)` holds."""

    name: str
    when: Callable[["Context"], bool]
    action: Callable[["Context"], None]
    fired_at: int | None = None


@dataclass
class Scenario:
    name: str
    scene: Scene
    start: tuple[float, float, float]
    goal: tuple[float, float, float]
    yaw_degrees: float = 0.0
    damage_points: float = 0.0
    perturbations: Perturbations = field(default_factory=Perturbations)
    events: list[Event] = field(default_factory=list)
    max_ticks: int = 400
    expect: str = "success"          # what a correct navigation stack should do


@dataclass
class Context:
    tick: int
    backend: CalculatorBackend
    session: NavigationSession
    driver: RuntimeNavigationDriver
    clock: list[int]


class InvariantMonitor:
    """Per-tick checks of the long-lived invariants the reviews kept finding broken."""

    def __init__(self, backend: CalculatorBackend, session: NavigationSession, budget: float):
        self.backend, self.session, self.budget = backend, session, budget
        self.violations: list[tuple[int, str, str]] = []
        self._support_y = backend.state.position[1]
        self._airborne_ticks = 0
        self._airborne_owner: str | None = None
        self._unowned_episode = False
        self._progress_key = None
        self._requests_without_progress = 0
        self._last_request = None
        self._still_ticks = 0
        self._last_position = backend.state.position

    def _record(self, tick, name, detail):
        if not any(v[1] == name for v in self.violations):
            self.violations.append((tick, name, detail))

    def airborne_owner(self) -> str | None:
        session = self.session
        executor = session._executor
        if executor is None or session.report.state.value not in {"executing", "cancelling"}:
            return None
        route = getattr(executor, "route", None)
        index = getattr(executor, "action_index", -1)
        if route is None or not 0 <= index < len(route.actions):
            return None
        action = route.actions[index]
        name = type(action).__name__
        if name == "WalkSegment" and getattr(action, "traversal_plan", None) is not None:
            return "WalkSegment+traversal"
        return name

    def check(self, tick: int, *, released: bool = False) -> None:
        state = self.backend.state
        report = self.session.report
        # I1: the body leaves support only under an owner that is responsible for the landing.
        if not state.on_ground:
            owner = None if released else self.airborne_owner()
            self._airborne_ticks += 1
            if self._airborne_ticks == 1:
                self._airborne_owner = owner
            allowed = owner in AIR_ACTIONS or owner == "WalkSegment+traversal" or (
                owner == "WalkSegment" and self._airborne_ticks < 3)
            if not allowed:
                self._unowned_episode = True
                self._record(tick, "unowned_airborne",
                             f"session {report.state.value}/{report.reason}, owner {owner}, "
                             f"released={released}, y {state.position[1]:.2f}")
        else:
            drop = self._support_y - state.position[1]
            if self._unowned_episode and drop >= .99:
                self._record(tick, "unowned_fall", f"fell {drop:.1f} blocks, landed at "
                             f"({state.position[0]:.2f}, {state.position[1]:.2f}, {state.position[2]:.2f})")
            self._support_y = state.position[1]
            self._airborne_ticks = 0
            self._unowned_episode = False
        # I2: movement damage stays inside the task budget.
        if self.backend.damage_taken > self.budget + 1e-9:
            self._record(tick, "damage_over_budget",
                         f"took {self.backend.damage_taken:g}, budget {self.budget:g}")
        # I3: re-planning is bounded without measurable progress.
        request = self.session._request
        sequence = None if request is None else request.sequence
        key = (tuple(math.floor(v) for v in state.position), report.goal_revision)
        if key != self._progress_key:
            self._progress_key = key
            self._requests_without_progress = 0
        elif sequence is not None and sequence != self._last_request:
            self._requests_without_progress += 1
            if self._requests_without_progress > 6:
                self._record(tick, "replanning_without_progress",
                             f"{self._requests_without_progress} requests at {key[0]}, reason {report.reason}")
        self._last_request = sequence
        # I4: a non-terminal session does not stand still forever.
        moved = math.dist(state.position, self._last_position)
        self._last_position = state.position
        terminal = report.state.value in {"complete", "failed", "cancelled", "closed"}
        self._still_ticks = 0 if (moved > .01 or terminal) else self._still_ticks + 1
        if self._still_ticks >= 100:
            self._record(tick, "no_progress_nonterminal",
                         f"still for 100 ticks in {report.state.value}/{report.reason}")


def seed_memory(runtime: PlayerRuntimeV1, scene: Scene) -> None:
    """Pre-load the Runtime-owned world with earlier, non-visual knowledge of the scene.

    Stands in for a map explored earlier: blocks and air are known, but no cell
    carries near lower-part visual evidence.  (Prototype shortcut through the
    adapter's private world; the plan adds a test-only seeding entry point.)
    """
    adapter = runtime.navigation_observation_adapter
    world = adapter._world
    stamp = adapter.latest_frame.body.stamp
    world.observe_blocks(stamp, {p: scene.geometry(b) for p, b in scene.solids.items()})
    world.confirm_air(stamp, scene.air_cells())
    # The cached frame's world view expired with the write; give it a fresh one.
    adapter._latest_frame = replace(adapter._latest_frame, world=world.view())


@dataclass
class Result:
    scenario: str
    expect: str
    outcome: str
    reason: str
    ticks: int
    final_position: tuple[float, float, float]
    damage: float
    violations: list[tuple[int, str, str]]
    events: list[str]

    @property
    def verdict(self) -> str:
        ok = (self.outcome == self.expect) and not self.violations
        return "PASS" if ok else "FAIL"


def run(scenario: Scenario, *, after_terminal_ticks: int = 20) -> Result:
    clock = [100_000_000]
    backend = CalculatorBackend(clock, Scene(dict(scenario.scene.solids), scenario.scene.volume).with_floor(),
                                scenario.start, scenario.yaw_degrees,
                                perturbations=scenario.perturbations)
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
    reset = runtime.reset(ResetRequestV0("reset-sim", backend.episode, "test", 1, 10_000_000_000))
    if not reset.succeeded:
        raise RuntimeError("reset failed")
    seed_memory(runtime, backend.scene)
    profiles = NavigationSessionProfiles.load(CONFIG)
    session = NavigationSession("sim", profiles, planner_worker=InlinePlannerWorker(),
                                motion_worker=InlineMotionWorker(), clock_ns=lambda: clock[0])
    driver = RuntimeNavigationDriver(runtime, session, clock_ns=lambda: clock[0])
    policy = "sim-budget" if scenario.damage_points else "no_expected_damage"
    goal = _goal(scenario.goal, policy)
    driver.start("goal", 1, goal, clock[0],
                 damage_budget=TaskDamageBudget(policy, scenario.damage_points))
    monitor = InvariantMonitor(backend, session, scenario.damage_points)
    context = Context(0, backend, session, driver, clock)
    released_ticks = 0
    tick = 0
    try:
        for tick in range(1, scenario.max_ticks + 1):
            context.tick = tick
            for event in scenario.events:
                if event.fired_at is None and driver.source is not None and event.when(context):
                    event.fired_at = tick
                    event.action(context)
            if driver.source is None or driver.state in TERMINAL_DRIVER:
                # Nobody owns input any more; the game keeps simulating.
                backend.advance(MovementV1())
                monitor.check(tick, released=True)
                released_ticks += 1
                if released_ticks >= after_terminal_ticks:
                    break
                continue
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            monitor.check(tick)
        report = session.report
        driver_state = driver.state
    finally:
        session.close()
        runtime.close()
    outcome = {"complete": "success"}.get(report.state.value, report.state.value)
    if driver_state == "failed" and outcome != "failed":
        outcome = f"driver_failed/{outcome}"
    return Result(scenario.name, scenario.expect, outcome, str(report.reason), tick,
                  tuple(round(v, 2) for v in backend.state.position), backend.damage_taken,
                  monitor.violations,
                  [f"{e.name}@{e.fired_at}" for e in scenario.events if e.fired_at is not None])


# ----------------------------------------------------------------- scenes
STONE = "minecraft:stone"


def lane(columns: list[list], *, width: int = 1, extra=None) -> Scene:
    """columns[z] lists the solid y levels (int = stone, (y, SLAB_ID) = bottom slab)."""
    solids = {}
    for z, column in enumerate(columns):
        for x in range(-(width // 2), width - width // 2):
            for item in column:
                y, block = item if isinstance(item, tuple) else (item, STONE)
                solids[(x, y, z)] = block
    solids.update(extra or {})
    xs = [p[0] for p in solids]
    zs = [p[2] for p in solids]
    return Scene(solids, ((min(xs) - 3, max(xs) + 3), (52, 72), (min(zs) - 3, max(zs) + 3)))


def late_ticks(probability: float, seed: int, horizon: int = 600) -> frozenset[int]:
    rng = random.Random(seed)
    return frozenset(t for t in range(2, horizon) if rng.random() < probability)
