"""Export the frozen vanilla block identity set from a Minecraft client JAR."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


_PREFIX = "assets/minecraft/blockstates/"
_SUFFIX = ".json"


def export_registry(source_jar: Path, target: Path, *, minecraft_version: str) -> dict:
    if not source_jar.is_file():
        raise FileNotFoundError(source_jar)
    with zipfile.ZipFile(source_jar) as archive:
        materials = sorted({
            "minecraft:" + name[len(_PREFIX):-len(_SUFFIX)]
            for name in archive.namelist()
            if name.startswith(_PREFIX) and name.endswith(_SUFFIX)
            and "/" not in name[len(_PREFIX):]
        })
    if not materials:
        raise ValueError("Minecraft client JAR contains no vanilla blockstate assets")
    document = {
        "schema_version": "mc2p.vanilla-block-registry.v1",
        "minecraft_version": minecraft_version,
        "source_jar_sha256": hashlib.sha256(source_jar.read_bytes()).hexdigest(),
        "material_count": len(materials),
        "materials": materials,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_jar", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--minecraft-version", default="1.21")
    args = parser.parse_args()
    document = export_registry(
        args.source_jar,
        args.target,
        minecraft_version=args.minecraft_version,
    )
    print(json.dumps({
        "target": str(args.target),
        "material_count": document["material_count"],
        "source_jar_sha256": document["source_jar_sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
