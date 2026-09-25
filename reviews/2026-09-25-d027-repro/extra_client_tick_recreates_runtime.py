"""One extra client tick during a verified jump now fails the whole Runtime.

Run from the repository root of a checkout of commit f115920:

    PYTHONPATH=. python reviews/2026-09-25-d027-repro/extra_client_tick_recreates_runtime.py

D027 attaches the proof's movement-tick window to every verified movement
intent, and ``PlayerRuntimeV1._submit_input_record`` requires the window's
earliest tick to equal ``observation.movement_tick_id + 1``.
``VerifiedMotionExecutor`` computes the next command's tick as
``start_tick + command_index`` and never compares it with the current anchor.

If the client runs one extra movement tick before the observation is sampled
(the command was applied on time, then a ``lease_exhausted`` tick followed,
as the real client does when it catches up after a stall), the next verified
command still asks for the old tick.  The Runtime raises
"input execution window is detached from the current movement tick" before
dispatch, classifies it as ``observation_invariant`` with disposition
``recreate_runtime``, and the navigation driver fails and releases its source
while the executor is still RUNNING in the air.  The designed path for a
command that cannot take effect on its proven tick is INPUT_LOST followed by
neutral landing responsibility.

The fixture is ``tests/motion_nav/test_runtime_navigation_verified_handoff.py``
(the same one D027 uses for its late-start test) with one extra client tick
after the second airborne command.

Observed at f115920: frame 1 -> runtime failure observation_invariant,
Runtime state failed, disposition recreate_runtime, executor running,
navigation source None.
"""
from __future__ import annotations

from dataclasses import replace

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.runtime.backend_v1 import BackendStepResultV1
from tests.motion_nav import test_runtime_navigation_verified_handoff as fixture
from tests.test_action_receipt import receipt_value


_BaseBackend = fixture._GapRuntimeBackend


class _ExtraTickBackend(_BaseBackend):
    """After the second airborne command the client samples one tick later."""

    def __init__(self, clock, **kwargs):
        super().__init__(clock, **kwargs)
        self.airborne_commands = 0
        self.extra_tick_done = False

    def step(self, action, deadline, *, observation_request=None):
        result = super().step(
            action, deadline, observation_request=observation_request,
        )
        if action.movement != MovementV1() and self.airborne:
            self.airborne_commands += 1
        if self.airborne_commands != 2 or self.extra_tick_done:
            return result
        self.extra_tick_done = True
        applied_tick = self.movement_tick
        self.movement_tick += 1
        observation = replace(
            self.observation(request_sequence_id=action.request_sequence_id),
            episode_id=action.episode_id,
        )
        rows = [{
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": item.movement_tick_id,
            "episode_id": item.episode_id,
            "request_sequence_id": item.request_sequence_id,
            "sampled_at_jvm_ns": item.sampled_at_jvm_ns,
            "state": item.state,
            "forward": item.forward, "strafe": item.strafe, "jump": item.jump,
            "sneak": item.sneak, "sprint": item.sprint,
        } for item in result.receipt.input_applications]
        # The real client keeps the last request identity on an exhausted lease.
        rows.append({
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": self.movement_tick,
            "episode_id": action.episode_id,
            "request_sequence_id": action.request_sequence_id,
            "sampled_at_jvm_ns": self.movement_tick,
            "state": "lease_exhausted",
            "forward": 0.0, "strafe": 0.0, "jump": False, "sneak": False,
            "sprint": False,
        })
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id, generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observation.world_time_ticks.value,
                input_samples=self.movement_tick, leased_input_samples=1,
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": 0,
            "oldest_retained_input_tick": applied_tick,
            "input_applications": rows,
        })
        print(f"client: command applied at tick {applied_tick}, "
              f"observation sampled after tick {self.movement_tick}")
        return BackendStepResultV1(observation, 0, False, False, receipt)


def main() -> None:
    fixture._GapRuntimeBackend = _ExtraTickBackend
    case = fixture.RuntimeVerifiedMotionHandoffTests()
    try:
        clock, _, runtime, session, driver = case._running_gap()
    finally:
        fixture._GapRuntimeBackend = _BaseBackend
    try:
        for frame in range(6):
            result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            failure = result.report.failure
            print(f"frame {frame}: runtime failure="
                  f"{None if failure is None else (failure.code.value, failure.message)} "
                  f"driver={driver.state}/{driver.reason}")
            if failure is not None or driver.state in {"failed", "success"}:
                disposition = runtime.last_failure_disposition
                executor = session._executor._controller
                print("  runtime state:", runtime.state.value,
                      "| disposition:",
                      None if disposition is None else disposition.disposition.value,
                      "| verified executor:", executor.state.value,
                      "| navigation source:", driver.source)
                break
    finally:
        runtime.close()
        session.close()


if __name__ == "__main__":
    main()
