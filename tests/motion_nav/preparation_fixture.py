"""Apply one real neutral/preparation tick in transport-only unit fixtures."""
from dataclasses import replace
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from tests.motion_nav import test_b10_motion_candidate as receipts


def apply_tick(anchor, world, ledger, movement, sequence=100):
    projected = project_movement_command(anchor.physics_state, movement)
    result = step(anchor.physics_state, projected.tick_input, world, JAVA_1_21_RULESET)
    assert result.next_state is not None, result
    tick = result.next_state.movement_tick_id
    receipts.VerifiedMotionExecutorTests.applied(
        ledger, anchor, sequence, tick, movement, requested_tick=tick)
    return replace(anchor, observation_sequence_id=anchor.observation_sequence_id + 1,
                   movement_tick_id=tick, physics_state=result.next_state)
