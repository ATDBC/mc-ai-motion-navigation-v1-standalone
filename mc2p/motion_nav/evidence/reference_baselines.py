"""Read and verify the frozen navigation reference catalog offline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any


_VERSION_SCHEMA = "mc2p.motion-navigation-reference-versions.v1"
_SCENE_SCHEMA = "mc2p.motion-navigation-reference-scenes.v1"
_ROLES = {
    "motion_performance_reference",
    "knowledge_semantics_reference",
    "planning_lifecycle_reference",
}


@dataclass(frozen=True)
class ReferenceVersion:
    id: str
    role: str
    snapshot_path: str
    report_path: str
    full_source_tree_sha256: str
    published_snapshot_tree_sha256: str
    promoted_as_new_base: bool
    existing_evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReferenceScene:
    id: str
    name: str
    purpose: str
    identity: dict[str, Any]


@dataclass(frozen=True)
class ReferenceCatalog:
    published_repository: dict[str, Any]
    versions: dict[str, ReferenceVersion]
    scenes: dict[str, ReferenceScene]


@dataclass(frozen=True)
class SnapshotVerification:
    file_count: int
    byte_count: int
    manifest_sha256: str
    ok: bool = True


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _safe_relative(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    if "\\" in value:
        raise ValueError(f"{field} must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"unsafe {field}: {value}")
    return value


def _sha256(value: Any, field: str, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length:
        raise ValueError(f"{field} must be a {length}-character hexadecimal digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field} is not hexadecimal") from exc
    return value.lower()


def _unique(items: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError(f"invalid {kind} id")
        if item["id"] in result:
            raise ValueError(f"duplicate {kind} id: {item['id']}")
        result[item["id"]] = item
    return result


def load_reference_catalog(versions_path: Path, scenes_path: Path) -> ReferenceCatalog:
    """Load strict, immutable identities without selecting a preferred implementation."""
    versions_doc = _read_object(Path(versions_path))
    scenes_doc = _read_object(Path(scenes_path))
    if versions_doc.get("schema_version") != _VERSION_SCHEMA:
        raise ValueError("unsupported reference version catalog schema")
    if scenes_doc.get("schema_version") != _SCENE_SCHEMA:
        raise ValueError("unsupported reference scene catalog schema")

    repository = versions_doc.get("published_repository")
    if not isinstance(repository, dict):
        raise ValueError("published_repository is required")
    _sha256(repository.get("commit"), "published_repository.commit", length=40)

    raw_versions = versions_doc.get("versions")
    raw_scenes = scenes_doc.get("scenes")
    if not isinstance(raw_versions, list) or not isinstance(raw_scenes, list):
        raise ValueError("versions and scenes must be arrays")

    versions: dict[str, ReferenceVersion] = {}
    for version_id, item in _unique(raw_versions, "version").items():
        role = item.get("role")
        if role not in _ROLES:
            raise ValueError(f"invalid role for {version_id}: {role}")
        promoted = item.get("promoted_as_new_base")
        if promoted is not False:
            raise ValueError(f"reference {version_id} must not be promoted as a new base")
        evidence = item.get("existing_evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(row, dict) for row in evidence):
            raise ValueError(f"existing_evidence must be an array for {version_id}")
        versions[version_id] = ReferenceVersion(
            id=version_id,
            role=role,
            snapshot_path=_safe_relative(item.get("snapshot_path"), "snapshot_path"),
            report_path=_safe_relative(item.get("report_path"), "report_path"),
            full_source_tree_sha256=_sha256(
                item.get("full_source_tree_sha256"), "full_source_tree_sha256"
            ),
            published_snapshot_tree_sha256=_sha256(
                item.get("published_snapshot_tree_sha256", "0" * 64),
                "published_snapshot_tree_sha256",
            ),
            promoted_as_new_base=promoted,
            existing_evidence=tuple(evidence),
        )

    scenes: dict[str, ReferenceScene] = {}
    for scene_id, item in _unique(raw_scenes, "scene").items():
        identity = item.get("identity")
        if not isinstance(identity, dict):
            raise ValueError(f"scene identity is required for {scene_id}")
        layout = identity.get("layout_path")
        if layout is not None:
            _safe_relative(layout, "layout_path")
        for field in ("runtime_layout_sha256", "source_file_sha256"):
            digest = identity.get(field)
            if digest is not None:
                _sha256(digest, field)
        scenes[scene_id] = ReferenceScene(
            id=scene_id,
            name=str(item.get("name", "")),
            purpose=str(item.get("purpose", "")),
            identity=dict(identity),
        )

    return ReferenceCatalog(dict(repository), versions, scenes)


def verify_scene_sources(catalog: ReferenceCatalog, project_root: Path) -> int:
    """Verify scene source files when a scene is backed by a tracked layout file."""
    root = Path(project_root).resolve()
    verified = 0
    for scene in catalog.scenes.values():
        relative = scene.identity.get("layout_path")
        wanted = scene.identity.get("source_file_sha256")
        if relative is None and wanted is None:
            continue
        if not isinstance(relative, str) or not isinstance(wanted, str):
            raise ValueError(f"scene {scene.id} must pair layout_path with source_file_sha256")
        target = (root / Path(*PurePosixPath(relative).parts)).resolve()
        if root not in target.parents or not target.is_file():
            raise ValueError(f"missing scene source: {scene.id}")
        if hashlib.sha256(target.read_bytes()).hexdigest() != wanted:
            raise ValueError(f"scene source checksum mismatch: {scene.id}")
        verified += 1
    return verified


def _parse_sums(path: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as exc:
            raise ValueError(f"invalid checksum line {number}") from exc
        digest = _sha256(digest, f"checksum line {number}")
        relative = _safe_relative(relative, f"checksum line {number} path")
        if relative in expected:
            raise ValueError(f"duplicate checksum path: {relative}")
        expected[relative] = digest
    if not expected:
        raise ValueError("empty checksum manifest")
    return expected


def verify_version_snapshot_trees(
    catalog: ReferenceCatalog, sums_path: Path
) -> dict[str, int]:
    """Verify each configured source excerpt against its checksum-derived tree identity."""
    expected = _parse_sums(Path(sums_path))
    counts: dict[str, int] = {}
    for version in catalog.versions.values():
        prefix = version.snapshot_path + "/"
        files = {
            relative[len(prefix) :]: digest
            for relative, digest in expected.items()
            if relative.startswith(prefix)
        }
        if not files:
            raise ValueError(f"snapshot has no files: {version.id}")
        payload = json.dumps(
            files, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != version.published_snapshot_tree_sha256:
            raise ValueError(f"published snapshot tree mismatch: {version.id}")
        if version.report_path not in expected:
            raise ValueError(f"missing published report: {version.id}")
        counts[version.id] = len(files)
    return counts


def verify_snapshot(snapshot_root: Path, sums_path: Path) -> SnapshotVerification:
    """Verify every published file and reject additions that change the frozen snapshot."""
    root = Path(snapshot_root).resolve()
    sums = Path(sums_path).resolve()
    expected = _parse_sums(sums)
    byte_count = 0
    for relative, wanted in expected.items():
        target = (root / Path(*PurePosixPath(relative).parts)).resolve()
        if root not in target.parents:
            raise ValueError(f"checksum path escapes snapshot: {relative}")
        if not target.is_file():
            raise ValueError(f"missing snapshot file: {relative}")
        payload = target.read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != wanted:
            raise ValueError(f"checksum mismatch: {relative}")
        byte_count += len(payload)

    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.resolve() == sums:
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "SHA256SUMS.txt" and path.read_bytes() == sums.read_bytes():
            continue
        actual_files.add(relative)
    unexpected = sorted(actual_files - set(expected))
    if unexpected:
        raise ValueError(f"unexpected file in snapshot: {unexpected[0]}")

    manifest_hash = hashlib.sha256(sums.read_bytes()).hexdigest()
    return SnapshotVerification(len(expected), byte_count, manifest_hash)
