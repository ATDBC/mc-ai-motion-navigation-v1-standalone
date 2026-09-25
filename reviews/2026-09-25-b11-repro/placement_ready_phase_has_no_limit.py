"""A placement whose preconditions never hold keeps the input source forever.

Run from the repository root of a checkout of commit ba4acda:

    PYTHONPATH=. python reviews/2026-09-25-b11-repro/placement_ready_phase_has_no_limit.py

Uses the Runtime integration fixture from
``tests/motion_nav/test_b11_world_change_navigation.py`` with one change: the
fake client never reports the support block as its current target (for
example, another face of the block stays in front of the crosshair).  ``BlockPlacementTransaction`` only bounds
dispatched attempts and confirmation waits; the READY phase (approach, aim,
"operation not selected", non-pending receipts) has no attempt or time limit
in the transaction or either driver.

Observed at ba4acda: after 600 control frames the driver is still "placing"
with no terminal result and no operation sent.
"""
from __future__ import annotations

from collections import Counter

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_v3 import TargetingStateV3
from tests.motion_nav import test_b11_world_change_navigation as fixture


def _top_face_target(kind, position, entity, face, hit, distance):
    return TargetingStateV3(kind, position, entity, "up", hit, distance)


def main() -> None:
    fixture.TargetingStateV3 = _top_face_target
    case = fixture.WorldChangeNavigationIntegrationTests()
    try:
        clock, backend, driver = case._fixture(maximum_blocks=1)
        reasons: Counter[str] = Counter()
        for _ in range(600):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            reasons[f"{driver.report.state}/{driver.report.reason}"] += 1
            if driver.report.terminal:
                break
        print("terminal:", driver.report.terminal, "| state:", driver.report.state)
        print("frames by state/reason:", dict(reasons.most_common(4)))
        print("operations sent:", sum(
            action.operation is not None for action in backend.actions
        ))
    finally:
        fixture.TargetingStateV3 = TargetingStateV3
        case.doCleanups()


if __name__ == "__main__":
    main()
