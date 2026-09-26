"""Check navigation reason coverage inside the B12-B active-target windows.

Run from the repository root of commit 3a1fdf0:

    python -B <this-review-branch>/reviews/2026-09-26-b12-pursuit-repro/active_trial_reason_coverage.py \
        evidence/motion_navigation/representative-v1/archives/b12b-pass-20260926T074601049372Z-1e50bcb7.tar.gz

For each active-target trial it compares the evaluated control window with
the ``navigation_route_decision`` records of the same navigation session.
"""
from __future__ import annotations

from collections import Counter
import json
import sys
import tarfile


def _moves(movement: dict) -> bool:
    return bool(movement.get("forward") or movement.get("strafe"))


def main(path: str) -> None:
    rows = []
    with tarfile.open(path) as archive:
        evidence = json.load(archive.extractfile(
            "client-0/b12b-partial-combat-evidence.json"))
        for name in sorted(
            member.name for member in archive.getmembers()
            if member.isfile() and "runtime-trace/trace/segment" in member.name
        ):
            rows.extend(json.loads(line) for line in archive.extractfile(name))

    actions = {}
    navigation: dict[str, dict[int, dict]] = {}
    first_goal: dict[str, int] = {}
    for row in rows:
        payload = row.get("payload") or {}
        kind = row.get("record_type")
        if kind == "dispatch":
            action = payload["decision"]["action"]
            actions[action["request_sequence_id"]] = action
        elif kind == "navigation_route_decision":
            navigation.setdefault(payload["session_id"], {})[
                payload["observation_sequence_id"]] = payload
        elif kind == "moving_goal_decision" and payload.get("adopted"):
            first_goal.setdefault(payload["goal_id"],
                                  payload["observation_sequence_id"])

    for trial in evidence["trials"]:
        if trial.get("classification") != "active_target":
            continue
        records = navigation.get("b12b-" + trial["trial_id"], {})
        window = trial["control_request_sequences"]
        missing = [q for q in window if q not in records]
        turns = [q for q in window
                 if abs(actions[q]["look"]["yaw_delta_degrees"]) > 0.0]
        still_turns = [q for q in turns if not _moves(actions[q]["movement"])]
        first_route = min(records) if records else None
        goal = first_goal.get("b12b-combat-goal-" + trial["trial_id"])
        print(f"{trial['trial_id']} ({trial.get('active_mode')})")
        print(f"  evaluated window: {window[0]}..{window[-1]} "
              f"({len(window)} frames)")
        print(f"  goal adopted at {goal}; first navigation record at "
              f"{first_route}; frames without any navigation record: "
              f"{len(missing)}")
        print(f"  turn frames {len(turns)}, turning without travel "
              f"{len(still_turns)}: " + ", ".join(
                  f"{q}={records.get(q, {}).get('reason_code', '<no record>')}"
                  for q in still_turns))
        print("  reasons inside window: " + str(dict(Counter(
            records[q]["reason_code"] for q in window if q in records))))
        after = sorted(q for q in records if q > window[-1])
        print("  reasons after window (counted by the report): " + str(dict(
            Counter(records[q]["reason_code"] for q in after)))
              + f" at {after[:1]}..{after[-1:]}")
        print("  report navigation_reason_counts: "
              + str(trial.get("navigation_reason_counts")))


if __name__ == "__main__":
    main(sys.argv[1])
