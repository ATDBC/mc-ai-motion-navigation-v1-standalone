"""Verify frozen reference code or normalize an existing sealed run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mc2p.motion_nav.evidence.reference_baselines import (
    load_reference_catalog,
    verify_scene_sources,
    verify_snapshot,
    verify_version_snapshot_trees,
)
from mc2p.motion_nav.evidence.reference_evidence import normalize_legacy_evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--snapshot-root", type=Path, required=True)
    verify.add_argument(
        "--checksums",
        type=Path,
        default=Path("config/motion-navigation/reference-SHA256SUMS.txt"),
    )
    verify.add_argument(
        "--versions",
        type=Path,
        default=Path("config/motion-navigation/reference-versions-v1.json"),
    )
    verify.add_argument(
        "--scenes",
        type=Path,
        default=Path("config/motion-navigation/reference-scenes-v1.json"),
    )
    normalize = commands.add_parser("normalize")
    normalize.add_argument("--evidence", type=Path, required=True)
    normalize.add_argument("--version", required=True)
    normalize.add_argument("--scene", required=True)
    normalize.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "verify":
        report = verify_snapshot(args.snapshot_root, args.checksums)
        catalog = load_reference_catalog(args.versions, args.scenes)
        trees = verify_version_snapshot_trees(catalog, args.checksums)
        value = {
            "ok": report.ok,
            "file_count": report.file_count,
            "byte_count": report.byte_count,
            "manifest_sha256": report.manifest_sha256,
            "version_count": len(catalog.versions),
            "scene_count": len(catalog.scenes),
            "scene_source_files_verified": verify_scene_sources(catalog, Path.cwd()),
            "version_snapshot_file_counts": trees,
        }
    else:
        value = normalize_legacy_evidence(
            args.evidence,
            args.output,
            version_id=args.version,
            scene_id=args.scene,
        )
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
