"""The placement preparation budget also runs while a dispatched block awaits confirmation.

Run from the repository root of a checkout of commit dd38c6b:

    PYTHONPATH=. python reviews/2026-09-25-d026-repro/preparation_timeout_during_confirmation.py

``RuntimeBlockPlacementDriver`` starts the preparation clock at the first
prepared observation and never stops or resets it.  Once the operation is
dispatched the transaction is ``AWAITING_CONFIRMATION``, which D026 says is
bounded by the transaction's own confirmation ticks and two attempts.  The
driver still calls ``transaction.fail("preparation_timeout")`` when the
preparation age is reached, even though the block may appear on the very next
observation.

The fixture reuses ``tests/motion_nav/test_b11_block_placement.py``: the fake
client shows the block two observations after the click (inside the 4-tick
confirmation window).  A preparation budget of 3 observations stands in for
the default 120 after a slow approach.

Observed at dd38c6b: with a budget of 3 the transaction fails with
``preparation_timeout`` while awaiting confirmation; the same client with the
default budget confirms the block on the next observation.
"""
from __future__ import annotations

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action_v1 import InteractBlockV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.world_interaction import BlockPlacementTransaction
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.block_placement_driver import RuntimeBlockPlacementDriver
from tests.motion_nav.test_b11_block_placement import (
    SUPPORT, _PlacementBackend, requirement,
)
from tests.test_action_receipt import receipt_value
from tests.test_player_runtime import _RecordingTrace


class _DelayedPlacementBackend(_PlacementBackend):
    """The server applies the click two observations after dispatch."""

    def __init__(self, clock):
        super().__init__(clock)
        self.dispatched_at: int | None = None

    def step(self, action, deadline, *, observation_request=None):
        self.actions.append(action)
        self.sequence += 1
        self.clock[0] += 50_000_000
        clicked = action.operation == InteractBlockV1(*SUPPORT, "east")
        if clicked and self.dispatched_at is None:
            self.dispatched_at = self.sequence
        placed = (self.dispatched_at is not None
                  and self.sequence >= self.dispatched_at + 2)
        observed = self._observation(
            placed=placed, request_sequence_id=action.request_sequence_id,
        )
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id,
                generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observed.world_time_ticks.value,
                status="pending_confirmation" if clicked else "executed",
                reason="block_use_dispatched" if clicked else "neutral",
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": 0,
            "oldest_retained_input_tick": self.sequence,
            "input_applications": [],
        })
        return BackendStepResultV1(observed, 0.0, False, False, receipt)


def run(preparation_timeout_observations: int) -> None:
    clock = [200_000_000]
    backend = _DelayedPlacementBackend(clock)
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
    try:
        reset = runtime.reset(ResetRequestV0(
            "reset-delayed", "episode-1", "test", 1, 5_000_000_000,
        ))
        assert reset.succeeded, reset.failure
        transaction = BlockPlacementTransaction(requirement())
        driver = RuntimeBlockPlacementDriver(
            runtime, transaction,
            preparation_timeout_observations=preparation_timeout_observations,
            clock_ns=lambda: clock[0],
        )
        driver.start()
        print(f"preparation budget {preparation_timeout_observations}:")
        for _ in range(8):
            before = transaction.report.state.value
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            report = transaction.report
            print(f"  observation {runtime.observation.sequence_id}: "
                  f"{before} -> {report.state.value} ({report.reason})")
            if report.terminal:
                break
    finally:
        runtime.close()


def main() -> None:
    run(3)
    run(120)


if __name__ == "__main__":
    main()
