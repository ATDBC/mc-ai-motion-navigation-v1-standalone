"""A neutral landing frame still breaks the B10 probe, now inside its local ledger.

Run from the repository root of a checkout of commit dd38c6b:

    PYTHONPATH=.:reviews/2026-09-25-d026-repro python reviews/2026-09-25-d026-repro/neutral_landing_frame_breaks_b10_probe_ledger.py

D026 item 3 lets the probe send a neutral landing frame without verified
command identity.  ``scripts/b10_gap_solver_runtime.py`` therefore skips
``ledger.submit`` for that frame, but still passes the frame's own client
application sample to ``ledger.observe_sample``.  The probe's ledger is a
per-trial ``InputApplicationLedger`` (not ``runtime.input_ledger``), so the
sample has no submitted command and the ledger raises ``ContractViolation``.

This repeats the probe loop's bookkeeping after one late verified input, using
the probe's own ``_verified_submission_window`` helper.

Observed at dd38c6b: ``_verified_submission_window`` returns None for the
neutral landing frame, then ``observe_sample`` raises
"input sample has no submitted command".
"""
from __future__ import annotations

from dataclasses import replace

from gap_session import drive_until_verified_command, gap_session
from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.contracts.action_v1 import ActionSnapshotV1
from scripts.b10_gap_solver_runtime import _verified_submission_window


def _sample(sequence: int, tick: int, movement) -> ClientInputApplicationV1:
    return ClientInputApplicationV1(
        "mc2p.input-application.v1", tick, "episode", sequence, 100 + tick,
        "leased", float(movement.forward), float(movement.strafe),
        movement.jump, movement.sneak, movement.sprint,
    )


def main() -> None:
    session, current, anchor = gap_session()
    try:
        proposal, ledger = drive_until_verified_command(session, current, anchor)
        decision = proposal.route_decision
        window = _verified_submission_window(decision)
        ledger.submit(
            anchor.session,
            ActionSnapshotV1("episode", 41, 41, 1_000_000,
                             movement=decision.movement, valid_for_ticks=1),
            requested_first_tick=window[1], latest_allowed_first_tick=window[2],
        )
        session.register_verified_submission(proposal, control_sequence=41)
        late_tick = window[2] + 1
        ledger.observe_sample(_sample(41, late_tick, decision.movement))

        airborne = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=late_tick,
            confirmed_control_sequence=41,
            confirmed_control_tick_range=(late_tick, late_tick),
            physics_state=replace(
                anchor.physics_state, movement_tick_id=late_tick,
                on_ground=False,
            ),
        )
        neutral = session.propose(current, airborne, 2_000_000_000,
                                  input_ledger=ledger)
        route = neutral.route_decision
        neutral_window = _verified_submission_window(route)
        print("neutral frame:", route.reason_code,
              "| submit_input", route.submit_input,
              "| probe window", neutral_window)
        # The probe sends the frame, skips ledger.submit because the window is
        # None, then observes the frame's own application sample.
        try:
            ledger.observe_sample(_sample(42, late_tick + 1, route.movement))
            print("probe ledger accepted the neutral sample")
        except Exception as error:  # noqa: BLE001 - the exception is the finding
            print("probe ledger raised", type(error).__name__, ":", error)
    finally:
        session.close()


if __name__ == "__main__":
    main()
