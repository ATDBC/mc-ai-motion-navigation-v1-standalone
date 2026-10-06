"""Build the deterministic evidence index used by structure-only cleanup.

This tool is opt-in.  It never rewrites an existing evidence archive or
changes the product/migration collectors' historical signatures.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


NORMALIZER_VERSION = "r28-structure-trajectory-v1"


def normalize_structure_value(value):
    """Round finite floats while retaining the value's recursive shape."""
    if type(value) is float:
        return round(value, 9) if math.isfinite(value) else value
    if type(value) is dict:
        return {key: normalize_structure_value(item) for key, item in value.items()}
    if type(value) is list:
        return [normalize_structure_value(item) for item in value]
    if type(value) is tuple:
        return tuple(normalize_structure_value(item) for item in value)
    return value


def _typed_value(value):
    """Encode container and scalar types so hashing cannot merge them."""
    if type(value) is dict:
        return ["dict", [[_typed_value(key), _typed_value(item)]
                         for key, item in value.items()]]
    if type(value) is list:
        return ["list", [_typed_value(item) for item in value]]
    if type(value) is tuple:
        return ["tuple", [_typed_value(item) for item in value]]
    if type(value) is bool:
        return ["bool", value]
    if value is None:
        return ["none", None]
    if type(value) is int:
        return ["int", value]
    if type(value) is float:
        if math.isnan(value):
            return ["float", "nan"]
        if math.isinf(value):
            return ["float", "inf" if value > 0 else "-inf"]
        return ["float", value]
    if type(value) is str:
        return ["str", value]
    raise TypeError(f"unsupported structure evidence type: {type(value).__name__}")


def structure_signature(value) -> str:
    normalized = normalize_structure_value(value)
    payload = json.dumps(
        [NORMALIZER_VERSION, _typed_value(normalized)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json_gzip(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def product_behavior_payload(record: dict, raw: dict) -> dict:
    """Select the signed product behaviour without retaining random IDs."""
    identities: dict[str, int] = {}

    def identity(value):
        if value is None:
            return None
        return identities.setdefault(value, len(identities))

    handoffs = []
    coverage = []
    planning_work = []
    for row in raw["trace"]:
        tick = row.get("movement_tick")
        handoff = row.get("body_handoff")
        if handoff is not None:
            normalized = dict(handoff)
            normalized["owner"] = identity(handoff.get("owner"))
            normalized["successor"] = identity(handoff.get("successor"))
            handoffs.append({"movement_tick": tick, "handoff": normalized})
        if "async_coverage" in row:
            coverage.append({"movement_tick": tick,
                             "async_coverage": row["async_coverage"]})
        submissions = row.get("planning_submissions", ())
        if submissions:
            planning_work.append({"movement_tick": tick,
                                  "submissions": len(submissions)})
    return {
        "id": record["id"],
        "parameters": record["parameters"],
        "outcome": record["metrics"]["outcome"],
        "reason": record["reason"],
        "metrics": record["metrics"],
        "motion_jobs": record.get("motion_jobs"),
        "strict_trace": raw["strict_trace"],
        "handoffs": handoffs,
        "async_coverage": coverage,
        "planning_work": planning_work,
    }


def product_index(root: Path) -> dict:
    from scripts.r28_baseline_alignment import raw_record

    records = [json.loads(line) for line in
               (root / "runs.jsonl").read_text("utf-8").splitlines()]
    cases = []
    for record in records:
        raw = raw_record(root, record)
        payload = product_behavior_payload(record, raw)
        cases.append({"id": record["id"], "signature": structure_signature(payload)})
    return {"kind": "product", "cases": cases}


def migration_index(root: Path) -> dict:
    summary = json.loads((root / "summary.json").read_text("utf-8"))
    cases = []
    for entry in summary["cases"]:
        path = root / entry["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("migration evidence checksum mismatch")
        raw = _read_json_gzip(path)
        payload = {
            "id": raw["id"],
            "input": raw["input"],
            "passed": raw["passed"],
            "exception": raw["exception"],
            "signature": raw["signature"],
        }
        cases.append({"id": raw["id"], "signature": structure_signature(payload)})
    return {"kind": "migration", "cases": cases}


def write_index(kind: str, source: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    content = product_index(source) if kind == "product" else migration_index(source)
    result = {
        "schema_version": "mc2p.navigation-structure-baseline.v1",
        "normalizer_version": NORMALIZER_VERSION,
        "source": str(source),
        **content,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return result


def compare_indexes(baseline: dict, candidate: dict) -> dict:
    for key in ("normalizer_version", "kind", "signature_schema_version"):
        if baseline.get(key) != candidate.get(key):
            raise ValueError(f"structure comparison basis differs: {key}")
    old = {row["id"]: row["signature"] for row in baseline["cases"]}
    new = {row["id"]: row["signature"] for row in candidate["cases"]}
    if len(old) != len(baseline["cases"]) or len(new) != len(candidate["cases"]):
        raise ValueError("structure comparison has duplicate case identity")
    if old.keys() != new.keys():
        raise ValueError("structure comparison denominator differs")
    differences = [identifier for identifier in old if old[identifier] != new[identifier]]
    return {"cases": len(old), "differences": differences,
            "equivalent": not differences}


def verify_manifest(root: Path) -> dict:
    """Verify every declared compact file without rewriting any evidence."""
    root = root.resolve()
    manifest = json.loads((root / "baseline-manifest.json").read_text("utf-8"))
    checked = set()

    def check(name, expected):
        path = (root / name).resolve()
        if Path(name).is_absolute() or not path.is_relative_to(root):
            raise ValueError("manifest reference escapes its evidence directory")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"manifest checksum mismatch: {name}")
        checked.add(name)

    for value in manifest["sets"].values():
        check(value["index"], value["index_sha256"])
        check(value["repeat_index"], value["repeat_index_sha256"])
    check("path-matrix.json", manifest["path_matrix_sha256"])
    check("deletion-inventory.json", manifest["deletion_inventory_sha256"])
    for name, digest in manifest["compact_summary_files"].items():
        check(name, digest)
    checksums = root / "SHA256SUMS.txt"
    if checksums.exists():
        for line in checksums.read_text("utf-8").splitlines():
            digest, name = line.split("  ", 1)
            if name == checksums.name:
                raise ValueError("SHA256SUMS must not hash itself")
            check(name, digest)
    return {"verified": True, "files_checked": len(checked), "sets": len(manifest["sets"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    index = subparsers.add_parser("index")
    index.add_argument("kind", choices=("product", "migration"))
    index.add_argument("--source", type=Path, required=True)
    index.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        report = verify_manifest(args.root)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.command == "index":
        result = write_index(args.kind, args.source, args.output)
        report = {"kind": result["kind"], "cases": len(result["cases"]),
                  "normalizer_version": result["normalizer_version"]}
    else:
        result = compare_indexes(
            json.loads(args.baseline.read_text("utf-8")),
            json.loads(args.candidate.read_text("utf-8")),
        )
        report = result
    print(json.dumps(report, ensure_ascii=False))
    return int(not result.get("equivalent", True))


if __name__ == "__main__":
    raise SystemExit(main())
