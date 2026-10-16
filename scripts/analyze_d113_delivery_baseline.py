#!/usr/bin/env python3
"""Read-only D113 grouping and identity delivery analysis; never rewrites D112."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
from scripts.analyze_d112_delivery_baseline import _control_prepare, _iter_jsonl, _summary

SCHEMA_VERSION = "mc2p.d113-control-visible-baseline.v1"


def _trace_keys(row):
    payload = row.get("payload", {})
    action = (payload.get("decision") or {}).get("action") or {}
    observation = (payload.get("backend_result") or {}).get("observation") or {}
    episode = row.get("episode_id", payload.get("episode_id", action.get("episode_id")))
    return episode, action.get("request_sequence_id"), action.get("observation_sequence_id"), observation.get("sequence_id")


def group_control_prepare(trace_rows, coordinator_frames, probe_trials):
    steps = [row for row in trace_rows if row.get("record_type") == "step"]
    keys = [_trace_keys(row) for row in steps]
    # Index without episode too, for legacy evidence. Such a join must still
    # resolve globally to exactly one row; never guess by order or timestamp.
    indices = [defaultdict(list) for _ in range(3)]
    for index, key in enumerate(keys):
        for position, table in enumerate(indices, 1):
            if key[position] is not None:
                table[key[position]].append(index)
    issues = []
    assignments = {}
    source_counts = dict(production_control_prepare=0, synchronous_probe=0)

    def assign(source, position, value, group, source_index):
        candidates = indices[position - 1].get(value, []) if value is not None else []
        if source.get("episode_id") is not None:
            candidates = [i for i in candidates if keys[i][0] == source["episode_id"]]
        if len(candidates) != 1:
            issues.append(dict(group=group, source_index=source_index,
                               issue="missing_mapping" if not candidates else "ambiguous_mapping"))
            return
        index = candidates[0]
        if (source.get("control_sequence_id") is not None
                and source["control_sequence_id"] != keys[index][1]):
            issues.append(dict(group=group, source_index=source_index, issue="control_sequence_mismatch"))
            return
        if (source.get("decision_observation_sequence_id") is not None
                and source["decision_observation_sequence_id"] != keys[index][2]):
            issues.append(dict(group=group, source_index=source_index, issue="decision_observation_mismatch"))
            return
        if index in assignments:
            issues.append(dict(group=group, source_index=source_index, issue="duplicate_mapping"))
            return
        if group == "synchronous_probe":
            payload = steps[index]["payload"]
            values = [payload.get("decision_observation_received_ns"), source.get("solve_started_ns"),
                      source.get("solve_ended_ns"), payload.get("dispatched_monotonic_ns")]
            if not all(type(value) is int for value in values) or values != sorted(values):
                issues.append(dict(group=group, source_index=source_index, issue="invalid_probe_interval"))
        assignments[index] = group
        source_counts[group] += 1

    for index, frame in enumerate(coordinator_frames):
        if frame.get("schema_version") == "mc2p.b10-coordinator-control-frame.v2" and any(
                frame.get(name) is None for name in ("episode_id", "control_sequence_id",
                    "decision_observation_sequence_id", "result_observation_sequence_id")):
            issues.append(dict(group="production_control_prepare", source_index=index, issue="missing_v2_identifier"))
        assign(frame, 3, frame.get("result_observation_sequence_id", frame.get("observation_sequence_id")),
               "production_control_prepare", index)
    for index, trial in enumerate(probe_trials):
        assign(trial, 2, trial.get("start_observation_sequence_id"), "synchronous_probe", index)
    result = {"all_trace_diagnostic": _control_prepare(steps)}
    for group in ("production_control_prepare", "synchronous_probe", "other_fixture"):
        rows = [row for i, row in enumerate(steps) if assignments.get(i, "other_fixture") == group]
        result[group] = dict(_control_prepare(rows), source_association_count=source_counts.get(group, len(rows)))
    production = result["production_control_prepare"]
    result["mapping_issues"] = issues
    result["performance_signable"] = bool(production["samples"]) and not issues and not (
        production["missing"] or production["invalid"])
    result["production_thresholds_met"] = result["performance_signable"] and (
        production["p95"] <= 8 and production["p99"] <= 15 and production["maximum"] < 30)
    return result


def summarize_identity_delivery(receiver_rows):
    chains = defaultdict(dict)
    duplicates = []
    for row in receiver_rows:
        if not isinstance(row.get("identity"), str):
            continue
        key = (row.get("episode_id"), row.get("trial"), row["identity"])
        event = row.get("event")
        if event in chains[key]:
            duplicates.append(dict(episode_id=key[0], trial=key[1], identity=key[2], event=event))
        chains[key][event] = row
    identities = []
    for (episode_id, trial, identity), events in sorted(chains.items(), key=lambda pair: repr(pair[0])):
        submit = events.get("worker_submit", {})
        ticks = dict(worker_submit_tick=submit.get("worker_submit_tick", submit.get("movement_tick")),
                     job_anchor_movement_tick=submit.get("job_anchor_movement_tick"))
        for event in ("receiver_observed_result", "registry_publish", "first_control_visible",
                      "first_control_consume", "result_take"):
            ticks[event + "_tick"] = events.get(event, {}).get("movement_tick")
        order = [ticks[name] for name in ("worker_submit_tick", "receiver_observed_result_tick",
                 "registry_publish_tick", "first_control_visible_tick", "result_take_tick")]
        actual = (ticks["first_control_visible_tick"] - ticks["worker_submit_tick"]
                  if type(ticks["first_control_visible_tick"]) is int and type(ticks["worker_submit_tick"]) is int else None)
        observed = events.get("receiver_observed_result", {})
        consume_tick = ticks["first_control_consume_tick"]
        submit_tick = ticks["worker_submit_tick"]
        values = [value for value in order if type(value) is int]
        prepared_read = events.get("first_control_consume", {}).get("access_kind") == "prepared_ready_query"
        identities.append(dict(episode_id=episode_id, trial=trial, identity=identity, **ticks,
            actual_delivery_ticks=actual,
            endpoint_available=actual is not None,
            complete=all(type(value) is int for value in (order[:4] if prepared_read else order)),
            invalid_order=values != sorted(values),
            historical_consume_semantics="take_or_prepared_READY; never substituted for visibility",
            historical_submit_to_consume_ticks=(consume_tick - submit_tick
                if type(consume_tick) is int and type(submit_tick) is int else None),
            submit_to_receiver_ticks=(ticks["receiver_observed_result_tick"] - submit_tick
                if type(ticks["receiver_observed_result_tick"]) is int and type(submit_tick) is int else None),
            receiver_to_publish_ticks=(ticks["registry_publish_tick"] - ticks["receiver_observed_result_tick"]
                if type(ticks["registry_publish_tick"]) is int and type(ticks["receiver_observed_result_tick"]) is int else None),
            publish_to_first_control_visible_ticks=(ticks["first_control_visible_tick"] - ticks["registry_publish_tick"]
                if type(ticks["first_control_visible_tick"]) is int and type(ticks["registry_publish_tick"]) is int else None),
            first_control_visible_to_take_ticks=(ticks["result_take_tick"] - ticks["first_control_visible_tick"]
                if type(ticks["result_take_tick"]) is int and type(ticks["first_control_visible_tick"]) is int else None),
            result_take_applicable=not prepared_read,
            worker_elapsed_ns=observed.get("worker_elapsed_ns"),
            worker_completed_monotonic_ns=observed.get("worker_completed_monotonic_ns")))
    return dict(identities=identities, identity_count=len(identities), duplicate_events=duplicates,
                incomplete_count=sum(not item["complete"] for item in identities),
                visibility_endpoint_unavailable_count=sum(not item["endpoint_available"] for item in identities),
                historical_submit_to_consume_ticks=_summary([
                    item["historical_submit_to_consume_ticks"] for item in identities
                    if item["historical_submit_to_consume_ticks"] is not None]),
                historical_missing_consume_count=sum(
                    item["historical_submit_to_consume_ticks"] is None for item in identities),
                invalid_order_count=sum(item["invalid_order"] for item in identities),
                actual_delivery_ticks=_summary([item["actual_delivery_ticks"] for item in identities
                                                if item["actual_delivery_ticks"] is not None]))


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analyze_client_directory(client):
    client = Path(client).resolve()
    paths = {name: client / filename for name, filename in dict(
        trace="trace.jsonl", frames="b10-coordinator-control-frames.jsonl",
        receiver="b10-receiver-diagnostics.jsonl", probes="b10-gap-solver-trials.jsonl").items()}
    before = {name: _hash(path) for name, path in paths.items()}
    rows = {name: list(_iter_jsonl(path)) for name, path in paths.items()}
    result = dict(schema_version=SCHEMA_VERSION, purpose="D113_read_only_D112_baseline",
        historical_d112_status="FAILED/STOPPED", formal_d113_signing=False,
        client_directory=str(client), source_sha256=before,
        analyzer_sha256=_hash(Path(__file__)),
        analyzer_base_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        **group_control_prepare(rows["trace"], rows["frames"], rows["probes"]),
        identity_delivery=summarize_identity_delivery(rows["receiver"]))
    after = {name: _hash(path) for name, path in paths.items()}
    if before != after:
        raise RuntimeError("source evidence changed during read-only analysis")
    result["source_hashes_unchanged"] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    client = args.client.resolve()
    if (client / "client-0").is_dir():
        client /= "client-0"
    output = args.output.resolve()
    if output.is_relative_to(client) or "d112-worker-delivery-v1" in output.parts:
        raise ValueError("D113 output must not overwrite historical D112 evidence")
    if output.exists():
        raise FileExistsError(output)
    report = analyze_client_directory(client)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
