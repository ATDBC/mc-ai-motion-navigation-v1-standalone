"""The formal navigation path records a one-tick window for a two-tick verified start.

Run from the repository root of a checkout of commit 1ea37d7:

    PYTHONPATH=.:reviews/2026-09-25-final-repro python reviews/2026-09-25-final-repro/formal_path_drops_verified_start_window.py

The JumpGap proof allows its first command to take effect at either of two
adjacent movement ticks.  After D026 the B10 probe hands that window to
``PlayerRuntimeV1.control_frame(input_execution_window=...)``.
``RuntimeNavigationDriver`` (the path used by combat and B11 navigation) does
not, so ``PlayerRuntimeV1._submit_input_record`` registers
``requested_first_tick == latest_allowed_first_tick == movement_tick + 1``.

This script submits the same verified command to the Runtime-style ledger in
both ways and applies it on the proof's latest allowed tick.

Observed at 1ea37d7: with the proof window the executor continues with
command 1; with the formal driver's one-tick record the ledger reports
``applied_outside_window`` and the executor gives up the verified motion.
"""
from __future__ import annotations

from dataclasses import replace

from gap_session import drive_until_verified_command, gap_session
from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.contracts.action_v1 import ActionSnapshotV1


def run(label: str, *, pass_window: bool) -> None:
    session, current, anchor = gap_session()
    try:
        proposal, ledger = drive_until_verified_command(session, current, anchor)
        decision = proposal.route_decision
        earliest = decision.expected_movement_tick
        latest = decision.latest_movement_tick
        movement = decision.movement
        ledger.submit(
            anchor.session,
            ActionSnapshotV1("episode", 41, 41, 1_000_000,
                             movement=movement, valid_for_ticks=1),
            requested_first_tick=earliest,
            latest_allowed_first_tick=latest if pass_window else earliest,
        )
        session.register_verified_submission(proposal, control_sequence=41)
        record = ledger.observe_sample(ClientInputApplicationV1(
            "mc2p.input-application.v1", latest, "episode", 41, 100 + latest,
            "leased", float(movement.forward), float(movement.strafe),
            movement.jump, movement.sneak, movement.sprint,
        ))
        later = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=latest,
            confirmed_control_sequence=41,
            confirmed_control_tick_range=(latest, latest),
            physics_state=replace(anchor.physics_state, movement_tick_id=latest),
        )
        after = session.propose(current, later, 2_000_000_000, input_ledger=ledger)
        route = after.route_decision
        print(f"{label}: proof window {earliest}..{latest}, applied at {latest}, "
              f"ledger={record.status.value} -> {route.reason_code} "
              f"(verified index {route.verified_command_index})")
    finally:
        session.close()


if __name__ == "__main__":
    run("B10 probe (window passed)", pass_window=True)
    run("RuntimeNavigationDriver (Runtime default)", pass_window=False)
