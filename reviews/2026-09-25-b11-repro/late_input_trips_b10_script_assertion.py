"""One late verified input makes the B10 session loop raise instead of recover.

Run from the repository root of a checkout of commit ba4acda:

    PYTHONPATH=.:reviews/2026-09-25-b11-repro python reviews/2026-09-25-b11-repro/late_input_trips_b10_script_assertion.py

The verified JumpGap command is submitted with its start window, then the
client applies it one movement tick after ``latest_movement_tick``.  The
executor keeps landing responsibility and sends a neutral input
(``retain_landing_after_input_loss``).  ``ActionRouteExecutor`` maps a neutral
movement to ``submit_input=True`` without verified command identity, which is
exactly what ``scripts/b10_gap_solver_runtime.py`` rejects with
"B10 session omitted verified command identity".

Observed at ba4acda: the second decision is a neutral retain-landing input with
``verified_command_index=None``; the B10 script predicate is True.
"""
from __future__ import annotations

from dataclasses import replace

from gap_session import drive_until_verified_command, gap_session
from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.contracts.action_v1 import ActionSnapshotV1


def _b10_script_would_raise(proposal) -> bool:
    decision = proposal.route_decision
    return bool(
        decision is not None and decision.submit_input
        and (proposal.control_frame is None
             or decision.verified_command_index is None
             or decision.expected_movement_tick is None
             or decision.latest_movement_tick is None)
    )


def main() -> None:
    session, current, anchor = gap_session()
    try:
        proposal, ledger = drive_until_verified_command(session, current, anchor)
        decision = proposal.route_decision
        movement = decision.movement
        print("verified command:", decision.reason_code,
              "| index", decision.verified_command_index,
              "| window", decision.expected_movement_tick,
              "..", decision.latest_movement_tick)
        sequence = 41
        ledger.submit(
            anchor.session,
            ActionSnapshotV1("episode", sequence, sequence, 1_000_000,
                             movement=movement, valid_for_ticks=1),
            requested_first_tick=decision.expected_movement_tick,
            latest_allowed_first_tick=decision.latest_movement_tick,
        )
        session.register_verified_submission(proposal, control_sequence=sequence)
        late_tick = decision.latest_movement_tick + 1
        record = ledger.observe_sample(ClientInputApplicationV1(
            "mc2p.input-application.v1", late_tick, "episode", sequence,
            100 + late_tick, "leased", float(movement.forward),
            float(movement.strafe), movement.jump, movement.sneak,
            movement.sprint,
        ))
        print("ledger status:", record.status.value, "at tick", late_tick)

        airborne = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=late_tick,
            confirmed_control_sequence=sequence,
            confirmed_control_tick_range=(late_tick, late_tick),
            physics_state=replace(
                anchor.physics_state, movement_tick_id=late_tick,
                on_ground=False,
            ),
        )
        after = session.propose(current, airborne, 2_000_000_000,
                                input_ledger=ledger)
        route = after.route_decision
        print("next decision:", route.reason_code,
              "| submit_input", route.submit_input,
              "| movement", route.movement,
              "| verified_command_index", route.verified_command_index)
        print("B10 script raises 'omitted verified command identity':",
              _b10_script_would_raise(after))
    finally:
        session.close()


if __name__ == "__main__":
    main()
