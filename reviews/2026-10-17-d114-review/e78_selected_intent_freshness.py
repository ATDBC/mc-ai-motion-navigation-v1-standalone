"""E78：只读核对 D114 唯一 Fabric 批次中，每一帧获胜的意图是否都是本帧新提议的。

输入是 D114 公开的 b10-coordinator-control-frames.jsonl(.gz) 与
b10-coordinator-trials.jsonl(.gz)。不调用项目代码。逐帧检查：
  - driver_selected_intents 的每个意图都在同帧 driver_proposed_intents 里；
  - 候选意图里没有前一帧及更早提议过、本帧未重新提议的意图；
  - 选中意图都属于该场景的会话来源（不是夹具 manual 来源）；
  - 本帧没有移动提议时，没有选中任何移动意图；
并按场景类别汇总等待帧数（submit_input=false）与“本帧无移动提议”帧数。
"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def find(raw: Path, name: str) -> Path:
    for candidate in (raw / name, raw / f"{name}.gz"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(name)


def main() -> int:
    raw = Path(sys.argv[1])
    trials = {t["name"]: t for t in rows(find(raw, "b10-coordinator-trials.jsonl"))}
    frames = defaultdict(list)
    for row in rows(find(raw, "b10-coordinator-control-frames.jsonl")):
        frames[row["trial"]].append(row)
    totals = defaultdict(lambda: defaultdict(int))
    problems = []
    for name, own in frames.items():
        trial = trials[name]
        kind = f"{trial['scenario_kind']}:{trial.get('worker_delay_ticks')}"
        source = trial["ordered_source_id"]
        seen = set()
        bucket = totals[kind]
        bucket["trials"] += 1
        for index, row in enumerate(own):
            proposed = set(row["driver_proposed_intents"])
            selected = {intent for _, intent in row["driver_selected_intents"]}
            candidates = set(row["driver_candidate_intents"])
            movement_selected = [intent for channel, intent in row["driver_selected_intents"]
                                 if channel == "movement"]
            bucket["frames"] += 1
            bucket["waiting_submit_false"] += not row["submit_input"]
            bucket["no_selected_movement"] += not movement_selected
            if not selected <= proposed:
                problems.append((name, index, "selected_not_proposed", sorted(selected - proposed)))
            stale = (seen - proposed) & candidates
            if stale:
                problems.append((name, index, "stale_candidate", sorted(stale)))
            foreign = {i for i in selected | candidates if not i.startswith(f"{source}/")}
            if foreign:
                problems.append((name, index, "foreign_source", sorted(foreign)))
            seen |= proposed
        bucket["recorded_waiting_polls"] += trial["waiting_polls"]
    print("类别 | 场景 | 控制帧 | submit_input=false 帧 | 无移动获胜帧 | 记录 waiting_polls 合计")
    for kind, b in sorted(totals.items()):
        print(f"{kind} | {b['trials']} | {b['frames']} | {b['waiting_submit_false']} | "
              f"{b['no_selected_movement']} | {b['recorded_waiting_polls']}")
    print(f"\n问题帧：{len(problems)}")
    for item in problems[:20]:
        print(" ", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
