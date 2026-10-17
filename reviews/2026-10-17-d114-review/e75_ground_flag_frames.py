"""E75：只读复算 D114 唯一 Fabric 批次中每个协调器场景的“未着地”帧。

输入是 D114 公开的原始 b10-coordinator-control-frames.jsonl(.gz) 与
b10-coordinator-trials.jsonl(.gz)。不调用项目代码，不改任何文件。
对每个已执行场景报告：控制帧数、未着地帧的序号／位置／会话状态，
以及这些帧相对场景起点（第一帧位置）的位移。另报告只读修正
（analysis.json）覆盖的 tick 范围之外还有多少控制帧。
"""
import gzip
import json
import math
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
    analysis = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else None
    ranges = {}
    if analysis is not None:
        for item in analysis["measurement_correction"]["trials"]:
            ranges[item["name"]] = (item["trace_tick_start"], item["trace_tick_end"])
    frames = defaultdict(list)
    for row in rows(find(raw, "b10-coordinator-control-frames.jsonl")):
        frames[row["trial"]].append(row)
    trials = [t for t in rows(find(raw, "b10-coordinator-trials.jsonl")) if t.get("fabric_supported")]
    by_kind = defaultdict(lambda: [0, 0, 0])
    print("trial | 帧数 | 未着地帧(序号:状态:位置) | 最大位移 | 记录 left_start_support | 修正范围外帧数")
    for trial in trials:
        name = trial["name"]
        own = frames[name]
        start = own[0]["position"]
        airborne = [
            (index, row["session_state"], row["reason_code"], row["position"], row["submit_input"])
            for index, row in enumerate(own) if not row["on_ground"]
        ]
        max_disp = max(math.dist(row["position"], start) for row in own)
        outside = ""
        if name in ranges:
            lo, hi = ranges[name]
            outside = str(sum(not lo <= row["grant_movement_tick"] <= hi for row in own))
        kind = f"{trial['scenario_kind']}:{trial.get('worker_delay_ticks')}"
        by_kind[kind][0] += 1
        by_kind[kind][1] += bool(airborne)
        if trial["verified_command_count"] == 0:
            by_kind[kind][2] += 1
        if airborne and trial["verified_command_count"] == 0 or name in ranges:
            desc = "; ".join(f"{i}:{s}/{r}:{p}:submit={u}" for i, s, r, p, u in airborne[:4]) or "-"
            print(f"{name} | {len(own)} | {desc} | {max_disp:.4f} | {trial['left_start_support']} | {outside}")
    print()
    print("类别 | 已执行 | 含未着地帧 | 无已验证命令")
    for kind, (n, air, nocmd) in sorted(by_kind.items()):
        print(f"{kind} | {n} | {air} | {nocmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
