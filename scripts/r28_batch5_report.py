"""Verify archived traces and compare common observations; never relabel failures."""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.navigation_coordination_metrics import digest, quantile


PHYSICAL = ("movement_tick", "position", "velocity", "on_ground", "pose",
            "source_bound", "applied_movement", "input_window", "goal_satisfied", "driver_state")


def records(root):
    rows = [json.loads(line) for line in (root / "runs.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == len({row["id"] for row in rows})
    result = {}
    transport_path = root / "trace-transport.json"
    transport = json.loads(transport_path.read_text("utf-8")) if transport_path.exists() else None
    for row in rows:
        if transport is None:
            data = (root / row["trace_file"]).read_bytes()
            assert digest(data) == row["trace_sha256"], row["id"]
            raw = json.loads(gzip.decompress(data))
        else:
            entry = transport["traces"][row["trace_file"]]
            assert entry["source_compressed_sha256"] == row["trace_sha256"]
            data = (root / entry["archive_path"]).read_bytes()
            assert digest(data) == entry["decompressed_sha256"], row["id"]
            raw = json.loads(data)
        assert raw["record"] == {k: v for k, v in row.items() if k != "trace_sha256"}
        result[row["id"]] = row, raw
    return result


def report(old_root, new_root, player_before):
    old, new, player = records(old_root), records(new_root), records(player_before)
    physical_differences, strict_differences, owner_ticks = [], [], 0
    for name, (a, raw_a) in old.items():
        b, raw_b = new[name]
        assert a["parameters"] == b["parameters"] and a["strict"] == b["strict"]
        ta, tb = raw_a["trace"], raw_b["trace"]
        if [[row.get(k) for k in PHYSICAL] for row in ta] != [[row.get(k) for k in PHYSICAL] for row in tb]:
            physical_differences.append(name)
        owner_ticks += sum(x["controller_ids"] != y["controller_ids"] for x, y in zip(ta, tb))
        if a["strict"]:
            # Old owner reports are known to be incomplete. Only this read-only
            # field is excluded; commands, windows, release and risk stay exact.
            def common_strict(raw):
                return [{k: v for k, v in row.items() if k != "controller_ids"}
                        for row in raw["strict_trace"]]
            if common_strict(raw_a) != common_strict(raw_b):
                strict_differences.append(name)
    lost = [name for name, (row, _) in {**old, **player}.items()
            if row["metrics"]["success"] and not new[name][0]["metrics"]["success"]]
    gained = [name for name, (row, _) in player.items()
              if not row["metrics"]["success"] and new[name][0]["metrics"]["success"]]
    stalls, screens, unowned = Counter(), [], 0
    for row, raw in new.values():
        stalls.update(row["metrics"]["net_stall_ticks"])
        trace = raw["trace"]
        screens.extend(trace[-1].get("terminal_screening", ()) if trace else ())
        unowned += sum(not t.get("applied_body_activity")
                       and (t["applied_movement"]["forward"] or t["applied_movement"]["strafe"])
                       for t in trace)
    return dict(old_pairs=len(old), product_tasks=len(new), lost_successes=lost,
        player_gained=gained, strict_pairs=sum(row["strict"] for row, _ in old.values()),
        strict_physical_and_risk_differences=strict_differences,
        ordinary_physical_differences=physical_differences,
        corrected_owner_ticks=owner_ticks, moving_ticks_without_applied_owner=unowned,
        stall_ticks=dict(stalls), screening_count=len(screens),
        screening_status=dict(Counter(s["status"] for s in screens)),
        screening_p95_ms=quantile([s["elapsed_ns"] / 1e6 for s in screens]),
        screening_p99_ms=quantile([s["elapsed_ns"] / 1e6 for s in screens], .99),
        limits="Diagnostic owner changes are explicit; this is a fixed baseline comparison, not a statistical release gate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--player-before", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.old, args.new, args.player_before)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "ordinary_physical_differences"}, ensure_ascii=False))
    raise SystemExit(int(bool(result["lost_successes"] or result["strict_physical_and_risk_differences"]
                              or result["moving_ticks_without_applied_owner"])))
