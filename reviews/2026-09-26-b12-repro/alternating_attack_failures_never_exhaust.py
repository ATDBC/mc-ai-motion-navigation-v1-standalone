"""Alternating attack failure classes never exhaust any retry budget.

Run from the repository root of a checkout of commit 2c6401c:

    PYTHONPATH=. python reviews/2026-09-26-b12-repro/alternating_attack_failures_never_exhaust.py

``advance_attack_retry`` keeps one consecutive counter per failure class,
but a failure of one class also resets the other two counters.  With the
production limits (``MAX_UNCONFIRMED_STRIKES`` = 2 for every class), a
target that alternately fails the client gate (for example a wall edge) and
then times out confirmation never produces a task outcome.  Deferred
attempts never count at all.  The only remaining bound is the task
deadline.

Observed at 2c6401c: 20 terminal attempts (10 gate rejections, 10
confirmation timeouts), zero confirmed hits, no task outcome.
"""
from __future__ import annotations

from mc2p.skills.attack_evidence import (
    AttackAttemptKeyV1, AttackAttemptOutcome, AttackAttemptPhase,
    AttackAttemptReportV1, AttackEvidenceGrade, AttackRetryLedgerV1,
    advance_attack_retry,
)
from mc2p.skills.moving_melee_driver import MAX_UNCONFIRMED_STRIKES


def _attempt(sequence: int, outcome: AttackAttemptOutcome) -> AttackAttemptReportV1:
    return AttackAttemptReportV1(
        AttackAttemptKeyV1("episode", "task", "goal", 1, "target", sequence),
        AttackAttemptPhase.TERMINAL, outcome, AttackEvidenceGrade.NONE,
    )


def main() -> None:
    ledger = AttackRetryLedgerV1()
    outcomes = []
    for sequence in range(1, 21):
        outcome = (AttackAttemptOutcome.GATE_REJECTED if sequence % 2
                   else AttackAttemptOutcome.CONFIRMATION_TIMEOUT)
        ledger, task_outcome = advance_attack_retry(
            ledger, _attempt(sequence, outcome),
            gate_limit=MAX_UNCONFIRMED_STRIKES,
            input_limit=MAX_UNCONFIRMED_STRIKES,
            confirmation_limit=MAX_UNCONFIRMED_STRIKES,
        )
        outcomes.append(task_outcome)
    print("limits:", MAX_UNCONFIRMED_STRIKES)
    print("gate rejections:", ledger.gate_rejections_total,
          "| confirmation timeouts:", ledger.confirmation_timeouts_total,
          "| confirmed hits:", ledger.confirmed_hits_total)
    print("task outcomes produced:", [o for o in outcomes if o is not None])


if __name__ == "__main__":
    main()
