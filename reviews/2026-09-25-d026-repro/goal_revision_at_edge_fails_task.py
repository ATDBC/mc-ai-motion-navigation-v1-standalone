"""A goal revision during the edge phase of a placement fails the whole task.

Run from the repository root of a checkout of commit dd38c6b:

    PYTHONPATH=. python reviews/2026-09-25-d026-repro/goal_revision_at_edge_fails_task.py

D026 item 5: before the click is dispatched, ``replace_goal`` cancels the
placement, resumes navigation and applies the new goal.  That works while the
body is still at the work node centre (the new unit test revises on the first
placement frame).  Once the crouched edge approach has finished, the body
centre is 0.62 block past the support centre, i.e. above the air cell that
the bridge block was meant to fill.  The restarted request cannot find a
current support surface and the task fails.

Uses the Runtime integration fixture from
``tests/motion_nav/test_b11_world_change_navigation.py`` (one-cell gap,
``maximum_blocks=1``).  The Fabric "goal_revision_changed" negative revises in
``interaction_required``, before any placement driver exists, so neither path
below is exercised in game.

Observed at dd38c6b: revision at placement start -> success with one
confirmed block; revision at the edge (reason ``target_not_aligned``) ->
``failed/current_surface_unavailable`` with no block placed.
"""
from __future__ import annotations

from mc2p.contracts.behavior import BehaviorProfileV0
from tests.motion_nav import test_b11_world_change_navigation as fixture


def run(trigger: str) -> None:
    case = fixture.WorldChangeNavigationIntegrationTests()
    clock, backend, driver = case._fixture(maximum_blocks=1)
    try:
        for _ in range(300):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            placement = driver.placement
            if placement is not None and (
                    trigger == "placement_start"
                    or placement.transaction.report.reason == trigger):
                break
        print(f"[{trigger}] body x={backend.position_x:.2f} "
              f"(support cell 0..1, gap cell 1..2) sneaking={backend.sneaking} "
              f"placement={driver.placement.transaction.report.reason}")
        driver.replace_goal(
            "goal-b11", 2, fixture._revised_goal(backend), clock[0],
        )
        print(f"  right after revision: {driver.report.state}/{driver.report.reason}")
        for _ in range(400):
            if driver.report.terminal:
                break
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        report = driver.report
        print(f"  end: {report.state}/{report.reason} "
              f"confirmed={report.confirmed_placements} "
              f"placed_cells={sorted(backend.placed_cells)}")
    finally:
        case.doCleanups()


if __name__ == "__main__":
    run("placement_start")
    run("target_not_aligned")
