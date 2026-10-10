"""Frozen P0 metadata and executable Task 5A/R2 representatives."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import math

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from .contracts import InputTier, SearchBudget, SearchStatus, TimingBranch, TrajectorySearchRequest


@dataclass(frozen=True, slots=True)
class P0Scenario:
    scenario_id: str
    stage: str
    world_fixture_id: str
    input_categories: tuple[str, ...]
    timing_branches: tuple[TimingBranch, ...]
    task_damage_budget: TaskDamageBudget
    budget: SearchBudget
    expected_status: SearchStatus
    coverage_scope: str
    scan_landing_removal: bool = False


# Provisional P0 measurement caps, not P1 performance or reachability claims.
P0_BUDGET = SearchBudget(4096, 65536, 40, 2)


def _scenario(name: str, inputs: tuple[str, ...], *,
              status: SearchStatus = SearchStatus.FOUND, scan: bool = False,
              budget: SearchBudget = P0_BUDGET) -> P0Scenario:
    return P0Scenario(name, "P0", f"p0-v1/{name}", inputs, tuple(TimingBranch),
                      TaskDamageBudget(), budget, status,
                      "Frozen small fixture; finite declared inputs; no global completeness claim.", scan)


P0_SCENARIOS: tuple[P0Scenario, ...] = (
    _scenario("flat_walk", ("WALK", "BRAKE")),
    _scenario("flat_sprint", ("SPRINT", "BRAKE")),
    _scenario("turn_90", ("WALK", "TURN", "BRAKE")),
    _scenario("jump_up_straight", ("RUN", "JUMP", "BRAKE")),
    _scenario("jump_up_after_turn", ("RUN", "TURN", "JUMP", "BRAKE")),
    *(_scenario(f"jump_gap_{width}", ("RUN", "JUMP", "BRAKE")) for width in (1, 2, 3)),
    _scenario("half_slab_chain", ("WALK", "BRAKE")),
    _scenario("stairs_chain", ("WALK", "BRAKE")),
    _scenario("short_landing_after_input_loss", ("WALK", "BRAKE")),
    _scenario("unknown_landing", ("RUN", "JUMP", "BRAKE"), status=SearchStatus.NEEDS_INFORMATION),
    _scenario("known_blocked", ("WALK", "BRAKE"), status=SearchStatus.BLOCKED),
    _scenario("budget_exhausted", ("WALK", "BRAKE"),
              status=SearchStatus.NO_TRAJECTORY_IN_BUDGET, budget=SearchBudget(1, 1, 1, 1)),
    _scenario("jump_up_commitment_scan", ("RUN", "JUMP", "BRAKE"), scan=True),
    *(_scenario(f"jump_gap_{width}_commitment_scan", ("RUN", "JUMP", "BRAKE"), scan=True)
      for width in (1, 2, 3)),
)


@dataclass(frozen=True, slots=True)
class RepresentativeFixture:
    scenario_id: str
    request: TrajectorySearchRequest
    world: PhysicsWorldView
    coverage: str


def representative_fixture(scenario_id: str) -> RepresentativeFixture:
    """Detached representatives; no test-helper or action-solver dependency."""
    from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
    from mc2p.motion_nav.physics_1_21 import step
    from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState, TickInput
    from mc2p.motion_nav.world_model import (
        Aabb, BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldSessionId, WorldView,
    )
    from .contracts import ApplicationEvidence, KnownInputApplication

    if scenario_id not in ("flat_walk", "jump_up_straight", "jump_gap_1"):
        raise ValueError("Task 5A has not frozen this executable representative")
    session = WorldSessionId("p0-reference-v1")
    stamp = ObservationStamp(session, 0, 0, "p0-reference-v1", 0)
    facts = {}
    for x in range(-3, 4):
        for y in range(-50, 8):
            for z in range(-3, 13):
                solid = (y == 0 and not (scenario_id == "jump_gap_1" and z == 1))
                solid |= scenario_id == "jump_up_straight" and y == 1 and z >= 1
                facts[(x, y, z)] = CellFact(
                    CellKnowledge.BLOCK if solid else CellKnowledge.AIR, stamp,
                    BlockGeometry.full_cube("minecraft:stone") if solid else None)
    local = PhysicsWorldView(WorldView.detached(session, 3, 0, facts), JAVA_1_21_RULESET)
    anchor = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema, session, 10,
        (.5, 1., .5), (0., -.0784, 0.), 0., 0., "standing", .6, 1.8,
        True, False, True, False, False, 0, 0., .1, .6, .08, .42,
        20, 5., "survival", (), False, False, False, False, False, False)
    walk = TickInput(1., 0., False, False, False, 0.)
    jump = TickInput(1., 0., True, False, False, 0.)
    neutral = TickInput(0., 0., False, False, False, 0.)
    late = step(anchor, neutral, local, JAVA_1_21_RULESET).next_state
    prelude = KnownInputApplication(session, 99, 11, neutral, "known-wait-99",
                                    ApplicationEvidence.PREDICTED)
    height = 2. if scenario_id == "jump_up_straight" else 1.
    target_z = 2.3 if scenario_id == "jump_gap_1" else 1.5
    goal = GoalState(Aabb(.35, height, target_z, .65, height + .05, target_z + 1.), GoalSupport.SOLID,
                     frozenset({MovementMode.WALK}), frozenset({"standing"}), 0.,
                     required_yaw_radians=0., maximum_yaw_error_radians=0.)
    supported = (walk, jump, neutral)
    request = TrajectorySearchRequest(
        f"reference-{scenario_id}-v1", (anchor, late), session, 3, goal, 1,
        TaskDamageBudget(), "anchor-10", "ledger-99", P0_BUDGET, tuple(TimingBranch),
        (11, 12), (), supported, (InputTier("A3", supported),),
        ((), (prelude,)), 100, ((.5, height, 2.),), stop_input=neutral)
    return RepresentativeFixture(scenario_id, request, local,
                                 "One fixed south-facing fixture, finite ordered tick inputs.")


def _input_tiers(final_tier: str):
    """Return the frozen cumulative A3/A5/A15 alphabets through final_tier."""
    from mc2p.motion_nav.physics_types import TickInput

    walk = TickInput(1., 0., False, False, False, 0.)
    jump = replace(walk, jump=True)
    stop = TickInput(0., 0., False, False, False, 0.)
    sprint = replace(walk, sprint=True)
    sprint_jump = replace(jump, sprint=True)
    a3 = (walk, jump, stop)
    a5 = a3 + (sprint, sprint_jump)
    headings = (math.pi / 2., -math.pi / 2., math.pi, math.pi / 4., -math.pi / 4.)
    directional = tuple(command for yaw in headings
                        for command in (replace(walk, movement_yaw_radians=yaw),
                                        replace(jump, movement_yaw_radians=yaw)))
    a15 = a5 + directional
    tiers = (InputTier("A3", a3), InputTier("A5", a5), InputTier("A15", a15))
    index = {tier.tier_id: position for position, tier in enumerate(tiers)}
    if final_tier not in index:
        raise ValueError(f"unknown primitive input tier: {final_tier}")
    return tiers[:index[final_tier] + 1]


def primitive_fixture(scenario_id: str, final_tier: str | None = None) -> RepresentativeFixture:
    """Build one detached D096 R2 matrix fixture without solver/test imports."""
    from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode, ResourceState
    from mc2p.motion_nav.physics_1_21 import step
    from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
    from mc2p.motion_nav.world_model import (
        Aabb, BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldSessionId, WorldView,
    )
    from .contracts import ApplicationEvidence, KnownInputApplication

    if scenario_id in ("flat_walk", "jump_up_straight", "jump_gap_1"):
        base = representative_fixture(scenario_id)
        tiers = _input_tiers(final_tier or "A3")
        request = replace(base.request, supported_inputs=tiers[-1].inputs, input_tiers=tiers)
        return replace(base, request=request, coverage="D096 R2 cumulative primitive fixture.")

    aliases = {"gap_start_4_width_3_a3": "gap_start_4_width_3"}
    world_scenario = aliases.get(scenario_id, scenario_id)
    tier_id = final_tier or ("A3" if scenario_id != "turn_90" else "A15")
    if scenario_id in ("jump_up_after_turn", "jump_up_after_turn_continue"):
        tier_id = final_tier or "A15"
    if scenario_id == "flat_sprint":
        tier_id = final_tier or "A5"
    tiers = _input_tiers(tier_id)
    stop = tiers[0].inputs[2]

    session = WorldSessionId("p0-primitive-v1")
    stamp = ObservationStamp(session, 0, 0, "p0-primitive-v1", 0)
    gap_start = gap_width = None
    if world_scenario.startswith("gap_start_"):
        parts = world_scenario.split("_")
        gap_start, gap_width = int(parts[2]), int(parts[4])
    elif world_scenario in ("jump_gap_continue", "unknown_landing", "one_twelfth_support"):
        gap_start, gap_width = 1, 1
    turn_up = world_scenario in ("jump_up_after_turn", "jump_up_after_turn_continue")
    facts = {}
    for x in range(-9, 10):
        for y in range(-50, 8):
            for z in range(-4, 17):
                solid = y == 0 and not (
                    gap_start is not None and gap_start <= z < gap_start + gap_width)
                if turn_up:
                    solid |= y == 1 and x <= -1
                if world_scenario == "collision_only":
                    solid |= z == 1 and y in (1, 2)
                if world_scenario == "one_twelfth_support" and y == 0 and z >= 2 and x >= 1:
                    solid = False
                position = (x, y, z)
                facts[position] = CellFact(
                    CellKnowledge.BLOCK if solid else CellKnowledge.AIR, stamp,
                    BlockGeometry.full_cube("minecraft:stone") if solid else None,
                )
    if world_scenario == "unknown_landing":
        del facts[(0, 0, 2)]
    local = PhysicsWorldView(WorldView.detached(session, 3, 0, facts), JAVA_1_21_RULESET)
    anchor_x = 1.25 if world_scenario == "one_twelfth_support" else .5
    anchor = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema, session, 10,
        (anchor_x, 1., .5), (0., -.0784, 0.), 0., 0., "standing", .6, 1.8,
        True, False, True, False, False, 0, 0., .1, .6, .08, .42,
        20, 5., "survival", (), False, False, False, False, False, False,
    )
    late = step(anchor, stop, local, JAVA_1_21_RULESET).next_state
    prelude = KnownInputApplication(session, 99, 11, stop, "known-wait-99",
                                    ApplicationEvidence.PREDICTED)

    height = 2. if turn_up else 1.
    if gap_start is not None:
        landing = gap_start + gap_width
        region = Aabb(anchor_x - .2, height, landing + .02, anchor_x + .2,
                      height + .05, landing + 1.15)
    elif world_scenario == "flat_sprint":
        region = Aabb(.3, 1., 7.5, .7, 1.05, 8.3)
    elif world_scenario in ("turn_90", "jump_up_after_turn", "jump_up_after_turn_continue"):
        region = Aabb(-3.2, height, .3, -1.05, height + .05, .7)
    elif world_scenario == "collision_only":
        region = Aabb(.3, 1., 2.2, .7, 1.05, 3.)
    elif world_scenario in ("resource_goal", "tiny_budget"):
        region = Aabb(.3, 1., 1.3, .7, 1.05, 2.2)
    else:
        raise ValueError(f"unknown R2 primitive fixture: {scenario_id}")

    continuation = world_scenario in ("jump_gap_continue", "jump_up_after_turn_continue")
    minimum_speed = 1. if continuation else 0.
    maximum_speed = 5. if continuation else 0.
    resources = (ResourceState((("food_points", 20.),))
                 if world_scenario == "resource_goal" else ResourceState())
    goal = GoalState(
        region, GoalSupport.SOLID, frozenset({MovementMode.WALK, MovementMode.SPRINT}),
        frozenset({"standing"}), maximum_speed, minimum_resources=resources,
    )
    budget = SearchBudget(4096, 1, 40, 2) if world_scenario == "tiny_budget" else P0_BUDGET
    request = TrajectorySearchRequest(
        f"primitive-{scenario_id}-{tier_id}", (anchor, late), session, 3, goal, 1,
        TaskDamageBudget(), "anchor-10", "ledger-99", budget, tuple(TimingBranch),
        (11, 12), (), tiers[-1].inputs, tiers, ((), (prelude,)), 100,
        ((.5 * (region.min_x + region.max_x), height,
          .5 * (region.min_z + region.max_z)),),
        minimum_terminal_speed_blocks_per_second=minimum_speed, stop_input=stop,
    )
    return RepresentativeFixture(scenario_id, request, local,
                                 "D096 R2 detached primitive matrix fixture.")
