"""E79：只读统计 D114 唯一 Fabric 批次中，每个协调器场景开头附近的 on_ground=false 瞬态。

输入是 D114 公开的 trace.jsonl(.gz)、b10-coordinator-control-frames.jsonl(.gz) 与
b10-coordinator-trials.jsonl(.gz)。不调用项目代码。

以场景第 0 帧结果观察的 movement tick 为 0 点：
  1. 统计 [-4, +4] 内 trace 中 is_on_ground=false 的相对位置（正偏移是 baseline 的真实起跳）；
  2. 对偏移 ≤0 的瞬态，看位置相对前一 tick 是否变化（变化说明这一 tick 就是传送到达）；
  3. 按执行顺序，把“上一场结束时已经在本场起点且朝向相同”与瞬态位置做交叉表。
夹具的 teleport() 在“到位、着地、速度≈0、朝向对齐”的第一帧就返回。
"""
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def lines(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield line


def find(raw: Path, name: str) -> Path:
    for candidate in (raw / name, raw / f"{name}.gz"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(name)


def main() -> int:
    raw = Path(sys.argv[1])
    bodies = {}
    for line in lines(find(raw, "trace.jsonl")):
        if "movement_tick_id" not in line:
            continue
        payload = json.loads(line).get("payload", {})
        body = payload.get("backend_result", {}).get("observation", {}).get("self_state", {}).get("value")
        if type(body) is dict and type(body.get("movement_tick_id")) is int:
            p = body["position"]
            bodies[body["movement_tick_id"]] = (body["is_on_ground"], (p["x"], p["y"], p["z"]))
    first = {}
    for line in lines(find(raw, "b10-coordinator-control-frames.jsonl")):
        row = json.loads(line)
        first.setdefault(row["trial"], row["grant_movement_tick"] + 1)
    trials = [json.loads(line) for line in lines(find(raw, "b10-coordinator-trials.jsonl"))]
    trials = [trial for trial in trials if trial.get("fabric_supported")]

    offsets = Counter()
    per_kind = defaultdict(Counter)
    moved = 0
    cross = Counter()
    listed = []
    previous = None
    for trial in trials:
        zero = first[trial["name"]]
        found = [offset for offset in range(-4, 5)
                 if zero + offset in bodies and not bodies[zero + offset][0]]
        for offset in found:
            offsets[offset] += 1
            per_kind[trial["scenario_kind"]][offset] += 1
            tick = zero + offset
            if offset <= 0 and tick - 1 in bodies and bodies[tick - 1][1] != bodies[tick][1]:
                moved += 1
        start_blips = tuple(offset for offset in found if offset <= 0)
        same_pose = bool(previous is not None
                         and previous["final_position"] == trial["samples"][0]["position"]
                         and previous["direction_index"] == trial["direction_index"])
        cross[(same_pose, start_blips)] += 1
        if same_pose or 0 in start_blips:
            listed.append((trial["name"], previous["name"] if previous else None, start_blips,
                           trial.get("left_start_support")))
        previous = trial

    print(f"场景数 {len(trials)}")
    print("相对第 0 帧结果观察的偏移 → 场景数:", dict(sorted(offsets.items())))
    for kind, counter in sorted(per_kind.items()):
        print(f"  {kind}: {dict(sorted(counter.items()))}")
    print(f"偏移 ≤0 的瞬态中，位置相对前一 tick 有变化（即传送到达）的次数: {moved}")
    print()
    print("上一场已在本场起点且朝向相同 | 偏移 ≤0 的瞬态位置 | 场景数")
    for (same_pose, blips), count in sorted(cross.items()):
        print(f"{same_pose} | {blips} | {count}")
    print()
    print("本场 | 上一场 | 瞬态偏移 | 记录 left_start_support")
    for item in listed:
        print(" | ".join(map(str, item)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
