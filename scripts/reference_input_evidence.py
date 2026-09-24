"""Reference-speed evidence: request counters are not client tick counters.

This checks the current live-player probes, not acceleration equivalence or every game state.
"""
from __future__ import annotations

from pathlib import Path
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
from scripts.client_time_evidence import evaluate_time_events, export_time_evidence, _read_jsonl
from scripts.control_probe_core import write_json_atomic


def export_reference_input_evidence(source: Path, offset: int, run: Path, receipts: list[dict]) -> dict:
    try:
        observations = _read_jsonl(run / "observations.jsonl")
        attribution = export_time_evidence(source, offset, run, observations=observations)
        if attribution["status"] != "passed":
            raise ValueError("reference probe requires complete --time-diagnostics evidence")
        result = evaluate_reference_input_evidence(receipts, observations, _read_jsonl(run / "time-events.jsonl"))
    except (OSError, UnicodeError, ValueError) as error:
        result = {"name": "reference_input_matches_actual_client_ticks", "passed": False, "errors": [str(error)]}
    write_json_atomic(run / "input-time-report.json", result)
    return result


def evaluate_reference_input_evidence(receipts: list[dict], observations: list[dict], events: list[dict]) -> dict:
    attribution = evaluate_time_events(events, observations)
    errors, samples, tick_intervals = [], [], []
    try:
        if attribution["status"] != "passed":
            raise ValueError("native time attribution failed: " + "; ".join(attribution["errors"]))
        intervals = attribution["intervals"]
        if not receipts or len(intervals) != len(receipts):
            raise ValueError("input receipts do not cover all observed steps")
        index, previous, episode = 0, None, None
        seen_episodes: set[str] = set()
        for observation in observations:
            sequence = observation["sequence_id"]
            if sequence == 0:
                episode = observation["episode_id"]
                if not isinstance(episode, str) or not episode or episode in seen_episodes:
                    raise ValueError("reset episode identity missing or reused")
                seen_episodes.add(episode)
                previous = None
                continue
            receipt = ClientBehaviorReceiptV2.from_mapping(receipts[index])
            ticks = intervals[index]["client_tick_calls"]
            if (receipt.episode_id != episode or observation["episode_id"] != episode
                    or receipt.generation_id != sequence
                    or receipt.request_sequence_id != observation["request_sequence_id"]
                    or receipt.request_sequence_id != sequence - 1
                    or receipt.world_tick != observation["world_time_ticks"]["value"]):
                raise ValueError("input receipt is not associated with this episode/observation")
            if type(ticks) is not int or ticks < 1:
                raise ValueError("reference action observation has no actual client tick")
            # The executor installs its Input on the first action after each reset. Subsequent
            # samples include idle ticks, independently counted by the native time sidecar.
            if previous is None:
                if sequence != 1 or receipt.input_samples != 1:
                    raise ValueError("first input counter did not start cleanly")
            elif receipt.input_samples - previous.input_samples != ticks:
                raise ValueError("input sample delta disagrees with actual client ticks")
            samples.append(receipt.input_samples)
            tick_intervals.append(ticks)
            previous = receipt
            index += 1
        if index != len(receipts):
            raise ValueError("unassociated input receipts")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        errors.append(str(error))
    return {"name": "reference_input_matches_actual_client_ticks", "passed": not errors,
        "input_samples": samples, "client_tick_intervals": tick_intervals, "errors": errors,
        "meaning": "reference live-player sampling only; not one request per tick or acceleration equivalence"}
