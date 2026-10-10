"""Frozen P0 metadata and the three bounded Task 5A executable representatives."""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from .contracts import SearchBudget, SearchStatus, TimingBranch, TrajectorySearchRequest


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
        for y in range(-5, 7):
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
    request = TrajectorySearchRequest(
        f"reference-{scenario_id}-v1", (anchor, late), session, 3, goal, 1,
        TaskDamageBudget(), "anchor-10", "ledger-99", P0_BUDGET, tuple(TimingBranch),
        (11, 12), (), (walk, neutral) if scenario_id == "flat_walk" else (walk, jump, neutral),
        ((), (prelude,)), 100, ((.5, height, 2.),))
    return RepresentativeFixture(scenario_id, request, local,
                                 "One fixed south-facing fixture, finite ordered tick inputs.")
