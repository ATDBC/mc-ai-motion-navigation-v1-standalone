"""Goal revisions and the B11 three-block bridge budget.

Run from the repository root of a checkout of commit ba4acda:

    PYTHONPATH=. python reviews/2026-09-25-b11-repro/goal_revision_and_bridge_budget.py

Uses the Runtime integration fixture from
``tests/motion_nav/test_b11_world_change_navigation.py`` (a straight 3-cell
gap, three dirt blocks in hand, ``maximum_blocks=3``).

Case 1 revises the goal after two confirmed placements.  ``update_goal``
resets ``_bridge_remaining`` to the policy maximum, so the "at most three
consecutive placements" budget is per goal revision, not per task.

Case 2 revises the goal while the placement transaction owns input.  The
navigation driver has released its source, so ``replace_goal`` raises
``ContractViolation`` and ``RuntimeWorldChangeNavigationDriver`` offers no
other revision entry.

Observed at ba4acda: case 1 prints remaining 1 -> 3; case 2 raises
"runtime navigation driver has no active goal".
"""
from __future__ import annotations

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.world_model import Aabb
from tests.motion_nav.test_b11_world_change_navigation import (
    WorldChangeNavigationIntegrationTests,
)


def _revised_goal(backend) -> GoalState:
    return GoalState(
        Aabb(backend.goal_x + 0.35, 63.95, 0.35,
             backend.goal_x + 0.65, 64.05, 0.65),
        GoalSupport.SOLID, frozenset({MovementMode.WALK}),
        frozenset({"standing"}), 0.6,
    )


def _fixture():
    case = WorldChangeNavigationIntegrationTests()
    return case, case._fixture(gap_count=3, item_count=3, maximum_blocks=3)


def case_budget_after_revision() -> None:
    case, (clock, backend, driver) = _fixture()
    try:
        for _ in range(400):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if (driver.report.confirmed_placements == 2
                    and driver.report.state == "interaction_required"):
                break
        print("case 1: confirmed", driver.report.confirmed_placements,
              "| state", driver.report.state,
              "| bridge_remaining", driver.session.bridge_remaining)
        driver.navigation.replace_goal("goal-b11", 2, _revised_goal(backend), clock[0])
        print("case 1: after goal revision 2 -> bridge_remaining",
              driver.session.bridge_remaining)
    finally:
        case.doCleanups()


def case_revision_while_placing() -> None:
    case, (clock, backend, driver) = _fixture()
    try:
        for _ in range(400):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if driver.report.state == "placing":
                break
        print("case 2: state", driver.report.state,
              "| navigation source", driver.navigation.source)
        try:
            driver.navigation.replace_goal(
                "goal-b11", 2, _revised_goal(backend), clock[0],
            )
            print("case 2: revision accepted")
        except Exception as error:  # noqa: BLE001 - the exception is the finding
            print("case 2: revision raised", type(error).__name__, ":", error)
    finally:
        case.doCleanups()


if __name__ == "__main__":
    case_budget_after_revision()
    case_revision_while_placing()
