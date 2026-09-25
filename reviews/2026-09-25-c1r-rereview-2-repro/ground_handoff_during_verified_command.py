"""A goal revision while a verified jump command is in flight on the ground.

Run from the repository root of a checkout of commit d1dad57:

    PYTHONPATH=. python reviews/2026-09-25-c1r-rereview-2-repro/ground_handoff_during_verified_command.py

When a replacement route is admitted, ``_advance_planning`` only checks
``frame.body.is_on_ground`` before discarding the old executor.  Route
admission checks position (within 0.8 blocks), not velocity or an in-flight
verified command.

Observed at d1dad57: the old VerifiedMotionExecutor is replaced on the next
frame and the new executor submits no input while it waits for a new proof.
"""
from __future__ import annotations

import tests.motion_nav.test_navigation_session as fixtures
from gap_session import drive_until_verified_command, gap_session


def main() -> None:
    session, current, anchor = gap_session()
    try:
        proposal, ledger = drive_until_verified_command(session, current, anchor)
        old_executor = session._executor
        print("before revision: on_ground =", current.body.is_on_ground,
              "| controller =", type(old_executor._controller).__name__,
              "| movement =", proposal.route_decision.movement)
        goal = fixtures.query_support_surfaces(current.world, 0, 2, 64, 64).surfaces[0]
        session.update_goal("gap-goal", 2, fixtures._goal(goal.position))
        for frame_index in range(1, 6):
            proposal = session.propose(
                current, anchor, 2_000_000_000, input_ledger=ledger,
            )
            decision = proposal.route_decision
            replaced = session._executor is not old_executor
            print(f"frame {frame_index}: state={proposal.report.state.value} "
                  f"reason={proposal.report.reason} executor_replaced={replaced} "
                  f"submit_input={None if decision is None else decision.submit_input}")
            if replaced:
                break
    finally:
        session.close()


if __name__ == "__main__":
    main()
