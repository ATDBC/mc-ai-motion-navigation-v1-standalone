"""A frame without a state anchor while a verified JumpGap command is in flight.

Run from the repository root of a checkout of commit d1dad57:

    PYTHONPATH=. python reviews/2026-09-25-c1r-rereview-2-repro/verified_motion_without_anchor.py

``RuntimeNavigationDriver.prepare_proposals`` passes
``NavigationSession.execution_anchor(...)``, which returns ``None`` whenever the
residual tracker could not advance its anchor to this observation (for example
``NEEDS_WORLD`` after unplanned motion, or ``NEEDS_INPUT`` after dropped input
samples).  ``propose`` then bypasses the coordinator and
``ActionRouteExecutor.decide`` raises for an active ``VerifiedMotionExecutor``.

Observed at d1dad57: the verified command is submitted, then the anchor-less
frame raises ``ContractViolation`` instead of keeping landing responsibility.
"""
from __future__ import annotations

from gap_session import drive_until_verified_command, gap_session


def main() -> None:
    session, current, anchor = gap_session()
    try:
        proposal, ledger = drive_until_verified_command(session, current, anchor)
        print("verified command submitted:", proposal.route_decision.reason_code,
              "| controller:", type(session._executor._controller).__name__)
        try:
            proposal = session.propose(
                current, None, 2_000_000_000, input_ledger=ledger,
            )
            print("anchor=None frame ->", proposal.report.state.value,
                  proposal.report.reason)
        except Exception as error:  # noqa: BLE001 - the exception is the finding
            print("anchor=None frame -> raised", type(error).__name__, ":", error)
    finally:
        session.close()


if __name__ == "__main__":
    main()
