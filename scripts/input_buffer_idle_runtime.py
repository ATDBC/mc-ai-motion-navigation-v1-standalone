"""Fabric regression for bounded input-sample retention across a long idle gap."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import time

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV3
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import receipt_confirms_input


_SOURCE = "input-buffer-idle"


def run_input_buffer_idle_runtime(
        runtime, backend, episode: str, directory: Path, deadline_ns: int,
        *, idle_seconds: float = 10.5,
) -> tuple[dict, list[dict], list[dict]]:
    """Leave the client unpolled past 64 ticks, then prove control can resume."""
    if idle_seconds < 10.0:
        raise ValueError("input-buffer idle regression requires at least ten seconds")
    task = TaskIntentV0(
        "input-buffer-idle", "resume_after_input_sample_overflow", "{}",
        (SuccessCriterionV0("control_resumed", ComparisonOperatorV0.EQUAL, 1, "boolean"),),
        20, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    rows: list[dict] = []
    counter = 0

    def diagnostic() -> None:
        row = dict(
            episode_id=episode,
            observation_sequence_id=runtime.observation.sequence_id,
            diagnostics=backend.last_diagnostics,
        )
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement: MovementV1):
        nonlocal counter
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"input-buffer-idle-{counter}", _SOURCE, episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK, now,
            min(deadline_ns, now + 750_000_000), movement=movement,
            valid_for_ticks=1,
        ))
        result = runtime.step(task, behavior, min(deadline_ns, now + 5_000_000_000))
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"input-buffer idle Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("input-buffer idle command bypassed the formal arbiter")
        diagnostic()
        return result

    # The robot input object is installed by the first admitted command.  Idle
    # before this point would only test a normal player input, not this ledger.
    diagnostic()
    primed = step(MovementV1())
    primed_receipt = primed.backend_result.receipt
    if not isinstance(primed_receipt, ClientBehaviorReceiptV3):
        raise RuntimeError("input-buffer idle priming requires a V3 receipt")

    idle_started = time.perf_counter()
    time.sleep(idle_seconds)
    actual_idle_seconds = time.perf_counter() - idle_started

    resumed = step(MovementV1(forward=1))
    resumed_receipt = resumed.backend_result.receipt
    if not isinstance(resumed_receipt, ClientBehaviorReceiptV3):
        raise RuntimeError("input-buffer idle regression requires a V3 receipt")
    matching = tuple(
        sample for sample in resumed_receipt.input_applications
        if sample.episode_id == episode
        and sample.request_sequence_id == resumed_receipt.request_sequence_id
        and sample.state == "leased"
        and sample.forward > 0.0
    )

    released = step(MovementV1())
    released_receipt = released.backend_result.receipt
    if not isinstance(released_receipt, ClientBehaviorReceiptV3):
        raise RuntimeError("input-buffer release requires a V3 receipt")
    release_matching = tuple(
        sample for sample in released_receipt.input_applications
        if sample.episode_id == episode
        and sample.request_sequence_id == released_receipt.request_sequence_id
        and sample.state == "neutral"
        and sample.forward == 0.0 and sample.strafe == 0.0
        and not sample.jump and not sample.sneak and not sample.sprint
    )

    oldest_matches = (
        bool(resumed_receipt.input_applications)
        and resumed_receipt.oldest_retained_input_tick
        == resumed_receipt.input_applications[0].movement_tick_id
    )
    summary = dict(
        schema_version="mc2p.input-buffer-idle.v1",
        requested_idle_seconds=idle_seconds,
        actual_idle_seconds=actual_idle_seconds,
        primed_receipt=asdict(primed_receipt),
        resumed_receipt=asdict(resumed_receipt),
        released_receipt=asdict(released_receipt),
        retained_sample_count=len(resumed_receipt.input_applications),
        matching_resume_samples=len(matching),
        matching_release_samples=len(release_matching),
    )
    write_json_atomic(directory / "input-buffer-idle.json", summary)
    checks = [
        dict(name="input_buffer_idle_exceeds_ten_seconds",
             passed=actual_idle_seconds >= 10.0),
        dict(name="input_buffer_overflow_is_reported_without_client_failure",
             passed=resumed_receipt.dropped_input_samples > 0
             and 0 < len(resumed_receipt.input_applications) <= 64
             and oldest_matches),
        dict(name="control_resumes_after_reported_sample_loss",
             passed=receipt_confirms_input(resumed_receipt.status) and bool(matching)),
        dict(name="resumed_control_can_be_released_normally",
             passed=receipt_confirms_input(released_receipt.status)
             and bool(release_matching)),
    ]
    return summary, rows, checks
