"""Round-14 review: the B10 coast check treats missing or late input samples as neutral.

Run from the repository root of a b740098 checkout:
    PYTHONPATH=. python -B <this script>

It reuses the upstream test fixture (VerifiedMotionExecutorTests.admitted/applied) and
the same steps as test_non_neutral_sample_during_coast_enters_local_landing_recovery,
changing only when the non-neutral sample reaches the ledger.
"""
from dataclasses import replace

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutor, VerifiedMotionExecutorState
from mc2p.motion_nav.online_motion import InputApplicationLedger
from tests.motion_nav.test_b10_motion_candidate import VerifiedMotionExecutorTests


def coast(fixture):
    anchor, candidate = fixture.admitted()
    executor = VerifiedMotionExecutor()
    executor.start(candidate)
    ledger = InputApplicationLedger(max_records=64)
    proof = candidate.proof
    neutral = MovementV1()
    tail = next(i for i in range(len(proof.commands))
                if all(c.movement == neutral for c in proof.commands[i:]))
    for index in range(tail):
        decision = executor.decide(anchor, ledger)
        tick = 11 + index
        executor.register_submission(
            index, control_sequence=500 + index,
            requested_movement_tick=decision.expected_movement_tick,
            requested_latest_movement_tick=decision.latest_movement_tick)
        fixture.applied(ledger, anchor, 500 + index, tick, proof.commands[index].movement,
                        requested_tick=decision.expected_movement_tick,
                        requested_latest_tick=decision.latest_movement_tick)
        anchor = replace(anchor, observation_sequence_id=anchor.observation_sequence_id + 1,
                         movement_tick_id=tick, physics_state=proof.trajectory[index + 1])
    assert executor.decide(anchor, ledger).reason == "coast_to_verified_landing"
    return executor, ledger, anchor


def advance(anchor, tick):
    return replace(anchor, observation_sequence_id=anchor.observation_sequence_id + 1,
                   movement_tick_id=tick,
                   physics_state=replace(anchor.physics_state, movement_tick_id=tick, on_ground=False))


def main():
    fixture = VerifiedMotionExecutorTests()

    # 1. Upstream case: the strafe sample is in the ledger before the decision.
    executor, ledger, anchor = coast(fixture)
    tick = anchor.movement_tick_id + 1
    fixture.applied(ledger, anchor, 900, tick, MovementV1(strafe=1))
    print("sample present before decide :", executor.decide(advance(anchor, tick), ledger).reason)

    # 2. The same sample arrives one frame late (after the decision for its tick).
    executor, ledger, anchor = coast(fixture)
    tick = anchor.movement_tick_id + 1
    first = executor.decide(advance(anchor, tick), ledger)
    fixture.applied(ledger, anchor, 900, tick, MovementV1(strafe=1))
    second = executor.decide(advance(advance(anchor, tick), tick + 1), ledger)
    print("sample arrives one frame late:", first.reason, "->", second.reason,
          "| state", second.state.value)

    # 3. No sample at all for two coasting ticks.
    executor, ledger, anchor = coast(fixture)
    tick = anchor.movement_tick_id + 2
    decision = executor.decide(advance(anchor, tick), ledger)
    print("two ticks with no sample     :", decision.reason, "| state", decision.state.value,
          "| samples in ledger for those ticks:",
          len(ledger.samples_between(anchor.movement_tick_id + 1, tick)))


if __name__ == "__main__":
    main()
