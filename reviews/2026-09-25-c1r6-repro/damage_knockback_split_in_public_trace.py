"""Find damage/knockback attribution splits in the public C1-C runtime trace.

Run from the repository root of a checkout of commit 7827f25:

    python reviews/2026-09-25-c1r6-repro/damage_knockback_split_in_public_trace.py

Reads ``evidence/motion_navigation/representative-v1`` directly from the
archive (nothing is extracted) and prints:

* totals of external-motion detection reasons and recovery stages;
* every place where ``damage_without_motion_residual`` is immediately followed
  by ``unattributed_external_motion_confirmed`` for the same task, with the
  damage tick and residual intervals.

Observed at 7827f25: one split in trial
``c1c-task-positive-post-recovery-rehit-north-05`` (sequences 445 -> 446), and
zero ``navigation_reanchored`` recovery stages in the whole batch.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import tarfile


ARCHIVE = Path(
    "evidence/motion_navigation/representative-v1/archives/"
    "c1c-pass-20260925T013056709842Z-97bc0db8.tar.gz"
)


def _records():
    with tarfile.open(ARCHIVE, "r:gz") as archive:
        segments = sorted(
            (member for member in archive.getmembers()
             if member.isfile() and "/runtime-trace/trace/segment-" in member.name),
            key=lambda item: item.name,
        )
        for member in segments:
            handle = archive.extractfile(member)
            assert handle is not None
            for line in handle.read().decode("utf-8").splitlines():
                yield json.loads(line)


def main() -> None:
    reasons: Counter[str] = Counter()
    stages: Counter[str] = Counter()
    previous: dict[str, tuple[int, dict]] = {}
    splits = []
    for record in _records():
        kind = record.get("record_type")
        payload = record.get("payload", {})
        if kind == "external_motion_recovery":
            stages[payload.get("stage")] += 1
        if kind != "external_motion_detection":
            continue
        detection = payload.get("detection") or {}
        reason = detection.get("reason")
        reasons[reason] += 1
        if reason == "duplicate_observation":
            continue
        task = payload.get("task_id")
        sequence = payload.get("observation_sequence_id")
        before = previous.get(task)
        if (reason == "unattributed_external_motion_confirmed" and before is not None
                and before[1].get("reason") == "damage_without_motion_residual"):
            splits.append((task, before[0], before[1], sequence, detection))
        previous[task] = (sequence, detection)

    print("detection reasons:", dict(reasons.most_common()))
    print("recovery stages:", dict(stages))
    for task, first_seq, first, second_seq, second in splits:
        fact = first.get("damage_fact") or first.get("fact") or {}
        first_residual = first.get("motion_residual") or {}
        second_residual = second.get("motion_residual") or {}
        print(f"split in {task}:")
        print(f"  seq {first_seq}: damage tick {fact.get('movement_tick_id')}, "
              f"health {fact.get('health_delta_points')}, residual "
              f"{first_residual.get('anchor_tick')}->{first_residual.get('observed_tick')} "
              f"{first_residual.get('status')}")
        print(f"  seq {second_seq}: residual "
              f"{second_residual.get('anchor_tick')}->{second_residual.get('observed_tick')} "
              f"{second_residual.get('status')}, position error "
              f"{second_residual.get('position_error_blocks')}")


if __name__ == "__main__":
    main()
