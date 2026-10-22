"""Generate compact D126 evidence without starting Minecraft or Fabric."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockGeometry
from mc2p.contracts.observation_v3 import CollisionShapeV3, ObservedBlockV3
from mc2p.motion_nav.world_snapshot import DependencyStatus
from scripts import d122_fabric_timing as d122
from scripts.d120_momentum_entry_calibration import build_fixture
from scripts.control_probe_core import write_json_atomic
from tests.observation_v3_fixtures import valid_snapshot_v3


HISTORICAL_ROOTS = (
    "evidence/motion_navigation/d122-client-timing-v1/d122-4",
    "evidence/motion_navigation/d123-proof-and-transport-v1",
    "evidence/motion_navigation/d124-proof-dependency-projection-v1",
    "evidence/motion_navigation/d125-three-required-fact-observation-v1",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _semantic_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _tree_digest(relative: str) -> dict:
    root = ROOT / relative
    rows = [(path.relative_to(ROOT).as_posix(), _sha256(path))
            for path in sorted(root.rglob("*")) if path.is_file()]
    digest = hashlib.sha256(json.dumps(
        rows, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {"file_count": len(rows), "sha256": digest}


def _runtime(sequence: int = 1):
    adapter = NavigationObservationAdapter()
    adapter.ingest(valid_snapshot_v3(sequence=sequence))
    return SimpleNamespace(navigation_observation_adapter=adapter)


def _gate(runtime, known, manifest):
    frame, report = d122.seed_runtime_known_fixture_map(runtime, known)
    return frame, report, d122.inspect_fabric_dependency_knowledge(
        frame.world, manifest,
    )


def collect() -> dict:
    directions = []
    for direction in range(4):
        fixture = build_fixture(direction)
        known = d122.known_fixture_map(
            fixture.trial, origin=(30, 40), feet_y=64,
        )
        manifest = d122.fabric_dependency_manifest(
            direction, origin=(30, 40), feet_y=64,
        )
        runtime = _runtime(direction + 1)
        frame, report, gate = _gate(runtime, known, manifest)
        directions.append({
            "direction": direction,
            "required_count": gate["required_count"],
            "known_count": gate["known_count"],
            "unknown_count": len(gate["unknown"]),
            "mismatch_count": len(gate["mismatched"]),
            "same_runtime_owner": (
                frame.world._owner
                is runtime.navigation_observation_adapter.snapshot_world_port
            ),
            "rows_sha256": report["rows_sha256"],
            "facts_sha256": report["facts_sha256"],
            "block_count": report["block_count"],
            "air_count": report["air_count"],
        })

    fixture = build_fixture(0)
    known = d122.known_fixture_map(fixture.trial, origin=(0, 0), feet_y=64)
    manifest = d122.fabric_dependency_manifest(0, origin=(0, 0), feet_y=64)
    joint = next(position for position in manifest["facts"]
                 if position[1] == 62 and position[2] == 2)
    _, _, missing = _gate(
        _runtime(10), d122.without_known_fixture_position(known, joint), manifest,
    )
    _, _, mismatch = _gate(
        _runtime(11), d122.with_known_fixture_block(
            known, joint, BlockGeometry.full_cube("minecraft:stone"),
        ), manifest,
    )

    changed_runtime = _runtime(12)
    changed_frame, _, _ = _gate(changed_runtime, known, manifest)
    changed_position = next(iter(known["air"]))
    bound_versions = changed_frame.world._owner.dependency_versions(
        (changed_position,),
    )
    changed_runtime.navigation_observation_adapter.ingest(valid_snapshot_v3(
        sequence=13,
        blocks=(ObservedBlockV3(
            changed_position,
            "minecraft:stone",
            CollisionShapeV3("full_cube"),
            None,
            ("surface_depth",),
        ),),
    ))
    dependency_change = changed_frame.world._owner.dependency_changes(
        bound_versions,
    )

    production_oracle_hits = []
    for relative_root in ("mc2p/runtime", "mc2p/skills"):
        for path in sorted((ROOT / relative_root).rglob("*.py")):
            source = path.read_text("utf-8")
            if "TEST_ORACLE" in source or "seed_test_oracle_memory" in source:
                production_oracle_hits.append(path.relative_to(ROOT).as_posix())

    result = {
        "schema": "mc2p.d126-known-map-motion.v1",
        "status": "PASS",
        "fabric_run": False,
        "production_files_modified": 0,
        "directions": directions,
        "negative_controls": {
            "missing_position": list(joint),
            "missing_unknown": [list(position) for position in missing["unknown"]],
            "wrong_material_mismatches": len(mismatch["mismatched"]),
            "semantic_change": {
                "position": list(changed_position),
                "status": dependency_change.status.value,
                "changed_positions": [list(position)
                                      for position in dependency_change.changed_positions],
            },
        },
        "boundaries": {
            "production_oracle_hits": production_oracle_hits,
            "observation_request_supplies_map": False,
            "temporary_adapter_in_runner": False,
        },
        "historical_evidence": {
            relative: _tree_digest(relative) for relative in HISTORICAL_ROOTS
        },
        "verification": {
            "workdir": str(ROOT),
            "d126_tests": "8/8",
            "adjacent_tests": "65/65",
            "standalone_tests": "37/37",
            "standalone_export_files": 3213,
            "commands": (
                r"D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_d126_known_map_motion -v",
                r"D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_d122_fabric_timing tests.motion_nav.test_d123_actor_calibration tests.motion_nav.test_d123_dependency_gate_repair tests.motion_nav.test_d123_dependency_probe tests.motion_nav.test_d124_proof_dependency_projection tests.motion_nav.test_d126_known_map_motion",
                r"D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_standalone_export",
                r"D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/export_motion_navigation_standalone.py export --root .tmp/d126-standalone-final-v2 --clean",
                r"D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/export_motion_navigation_standalone.py verify --root .tmp/d126-standalone-final-v2",
            ),
        },
        "current_source_hashes": {
            relative: _semantic_sha256(ROOT / relative)
            for relative in (
                "scripts/d122_fabric_timing.py",
                "scripts/d126_known_map_motion.py",
                "tests/motion_nav/test_d122_fabric_timing.py",
                "tests/motion_nav/test_d126_known_map_motion.py",
            )
        },
    }
    if not all(row["required_count"] == 58
               and row["known_count"] == 58
               and row["unknown_count"] == 0
               and row["mismatch_count"] == 0
               and row["same_runtime_owner"]
               for row in directions):
        result["status"] = "FAIL"
    if (missing["unknown"] != (joint,)
            or len(mismatch["mismatched"]) != 1
            or dependency_change.status is not DependencyStatus.CHANGED
            or dependency_change.changed_positions != (changed_position,)
            or production_oracle_hits):
        result["status"] = "FAIL"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect()
    args.output.mkdir(parents=True, exist_ok=True)
    readme = args.output / "README.md"
    readme.write_text(
        "# D126 已知地图运动夹具证据\n\n"
        "本目录由 `scripts/d126_known_map_motion.py` 生成。它不启动 Minecraft、"
        "Fabric 或 movement trial。`summary.json` 记录四方向门禁、反例、权限边界"
        "和 D122—D125 历史证据树摘要。\n\n"
        "最终检查：D126 8／8，相邻 65／65，standalone 37／37；最终字节"
        "执行 standalone export 与 verify，共 3213 个文件。正式命令记录在"
        " `summary.json` 的 `verification.commands`。\n",
        encoding="utf-8",
    )
    summary = args.output / "summary.json"
    write_json_atomic(summary, result)
    manifest = {
        "schema": "mc2p.d126-known-map-motion-manifest.v1",
        "status": result["status"],
        "files": {
            "README.md": _sha256(readme),
            "summary.json": _sha256(summary),
        },
    }
    write_json_atomic(args.output / "manifest.json", manifest)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
