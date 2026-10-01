"""Frozen independent asynchronous combinations, with explicit coverage failures."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import traceback
from dataclasses import asdict, is_dataclass

from tests.sim.async_work_sequences import run_gap_sequence, run_information_sequence, run_placement_sequence
from tests.sim.run_navigation_matrix import _source_identity


def _encode(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported evidence type: {type(value).__name__}")


def run_manifest(manifest, output_root):
    raw = manifest.read_bytes()
    config = json.loads(raw)
    if config["schema_version"] != "mc2p.async-work-sequences.v1":
        raise ValueError("unsupported asynchronous sequence manifest")
    if output_root.exists():
        raise FileExistsError("refusing to overwrite asynchronous sequence evidence")
    output_root.mkdir(parents=True)
    counts, guards, results = Counter(), Counter(), []
    for family in config["families"]:
        for seed in range(config["seed_start"], config["seed_end"] + 1):
            name = f"{family['name']}-{seed}"
            counts["generated"] += 1
            try:
                if family["name"] == "information_exit":
                    result = run_information_sequence(seed)
                elif family["name"] == "gap_successor":
                    result = run_gap_sequence(seed)
                else:
                    result = run_placement_sequence(seed, mode="failed" if family["name"] == "placement_revocation" else None,
                        partial=family["name"] == "placement_revocation")
                fired = set(result["events"])
                missing = set(family["events"]) - fired
                result["coverage_skipped"] = sorted(missing)
                passed = result["passed"] and not missing and result["task_outcome"] == family["outcome"]
                result["passed"] = passed
                counts["dispatched"] += int(not missing)
                counts["applied"] += int(passed)
                counts["skipped"] += int(bool(missing))
                counts["passed" if passed else "failed"] += 1
                counts[f"task_{result['task_outcome']}"] += 1
                guards.update(result["events"])
                for kind, events in result["verification"]["coverage"].items():
                    for operation, number in events.items():
                        guards[f"{kind}_{operation}"] += number
                guards["source_safely_released"] += int(result.get("safe_release", result["passed"]))
            except Exception as error:
                result = {"seed": seed, "passed": False, "error": f"{type(error).__name__}: {error}",
                          "traceback": traceback.format_exc()}
                counts["failed"] += 1
            (output_root / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_encode), encoding="utf-8")
            results.append({"family": family["name"], "seed": seed, "passed": result["passed"]})
    control = run_placement_sequence(config["seed_start"], mode="normal")
    (output_root / "normal-placement.json").write_text(json.dumps(control, ensure_ascii=False, indent=2, default=_encode), encoding="utf-8")
    summary = {"schema_version": config["schema_version"], "source_identity": _source_identity(),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(), "counts": dict(counts), "guards": dict(guards),
        "normal_control_passed": control["passed"], "results": results}
    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    result = run_manifest(args.manifest, args.output_root)
    print(json.dumps({"counts": result["counts"], "normal_control_passed": result["normal_control_passed"]}))
    return int(result["counts"].get("failed", 0) > 0 or not result["normal_control_passed"])


if __name__ == "__main__":
    raise SystemExit(main())
