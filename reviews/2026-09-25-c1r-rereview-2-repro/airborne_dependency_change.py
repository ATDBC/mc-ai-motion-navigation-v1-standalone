"""A route-dependency change while airborne during verified JumpGap motion.

Run from the repository root of a checkout of commit d1dad57:

    PYTHONPATH=. python reviews/2026-09-25-c1r-rereview-2-repro/airborne_dependency_change.py

D020 keeps the executor through goal revision and cancellation, but
``observe`` -> ``_replan_from_current("active_route_dependency_changed")`` ->
``_replace_request(...)`` still uses the default ``preserve_active_route=False``
and retires the executor, even when the body is off the ground.

Observed at d1dad57: the verified executor is dropped and the session returns
to ``snapshotting`` with no landing owner.
"""
from __future__ import annotations

from dataclasses import replace

import tests.motion_nav.test_navigation_session as fixtures
from gap_session import drive_until_verified_command, gap_session


def main() -> None:
    session, current, anchor = gap_session()
    try:
        drive_until_verified_command(session, current, anchor)
        executor = session._executor
        dependency = session.active_route.action_route.dependencies[0]
        airborne = replace(
            current.body,
            sequence_id=current.body.sequence_id + 1,
            is_on_ground=False,
        )
        changed = fixtures.NavigationFrame(
            current.session, airborne, current.world, "fabric", (dependency,),
        )
        session.observe(changed, changed.changed_cells)
        print("verified executor before:", type(executor._controller).__name__)
        print("after an airborne dependency change: executor kept =",
              session._executor is executor,
              "| state =", session.report.state.value,
              "| reason =", session.report.reason)
    finally:
        session.close()


if __name__ == "__main__":
    main()
