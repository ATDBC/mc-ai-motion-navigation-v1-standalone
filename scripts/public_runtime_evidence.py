"""Build and verify the small public corpus of representative Fabric runs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
from typing import BinaryIO, Iterable, Iterator, Mapping


SCHEMA_VERSION = "mc2p.public-runtime-evidence.v1"
RUN_SCHEMA_VERSION = "mc2p.public-runtime-evidence-run.v1"
MAX_TOTAL_ARCHIVE_BYTES = 30 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 64
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
HASH_FILE = "SHA256SUMS.txt"


class EvidenceViolation(ValueError):
    """The selected evidence is missing, unsafe, or inconsistent."""


@dataclass(frozen=True, slots=True)
class RunSpec:
    run_id: str
    stage: str
    outcome: str
    status: str
    failure_type: str | None
    failure_message: str | None
    trial_rows: int
    positive_passed: int | None = None
    positive_total: int | None = None
    negative_passed: int | None = None
    negative_total: int | None = None

    @property
    def archive_name(self) -> str:
        return f"{self.stage}-{self.outcome}-{self.run_id}.tar.gz"


@dataclass(frozen=True, slots=True)
class VerificationReport:
    archive_count: int
    total_archive_bytes: int
    stages: tuple[str, ...]
    run_ids: tuple[str, ...]


RUN_SPECS = (
    RunSpec(
        "20260923T063950738376Z-5a9f5ab9", "b10c", "pass", "passed",
        None, None, 82,
    ),
    RunSpec(
        "20260923T083302580358Z-a019b110", "b10c", "fail", "failed",
        "ContractViolation", "verified executor already has an in-flight command", 82,
    ),
    RunSpec(
        "20260925T015540321761Z-d008bbc3", "c1b", "pass", "passed",
        None, None, 30, 20, 20, 10, 10,
    ),
    RunSpec(
        "20260925T014651611158Z-8fbbd0bb", "c1b", "fail", "failed",
        "DeploymentEvidenceFailure",
        "['client-0:c1b_20_of_20_positive_tasks']", 30, 19, 20, 10, 10,
    ),
    RunSpec(
        "20260925T013056709842Z-97bc0db8", "c1c", "pass", "passed",
        None, None, 30, 20, 20, 10, 10,
    ),
    RunSpec(
        "20260924T164017683290Z-1c95ef4a", "c1c", "fail", "failed",
        "DeploymentEvidenceFailure",
        "['client-0:c1c_20_of_20_positive_tasks']", 30, 18, 20, 10, 10,
    ),
)

_SENSITIVE_MARKERS = (
    b'"access_token"', b'"authorization"', b'"session_token"',
    b"c:\\users\\", b"d:\\my_project\\",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _load_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceViolation(f"cannot read {label}: {path}") from error
    if type(value) is not dict:
        raise EvidenceViolation(f"{label} must be a JSON object: {path}")
    return value


def _safe_member_name(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise EvidenceViolation(f"unsafe archive member: {value!r}")
    return path


def _scan_public_bytes(path: Path) -> None:
    overlap = b""
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            lowered = (overlap + chunk).lower()
            marker = next((item for item in _SENSITIVE_MARKERS if item in lowered), None)
            if marker is not None:
                raise EvidenceViolation(
                    f"selected evidence contains a private marker {marker!r}: {path}"
                )
            overlap = lowered[-64:]


def _source_paths(run: Path, spec: RunSpec) -> tuple[Path, ...]:
    relative = [Path("result.json")]
    client = Path("client-0")
    if spec.stage == "b10c":
        relative.extend((client / "trace.jsonl", client / "b10-gap-solver-trials.jsonl"))
        if spec.outcome == "pass":
            relative.append(client / "b10-gap-solver.json")
            relative.extend(
                path.relative_to(run)
                for path in sorted((run / client / "physics-tick-events").glob("*"))
                if path.is_file()
            )
    else:
        prefix = "c1-moving-melee" if spec.stage == "c1b" else "c1-external-motion"
        relative.extend(
            client / f"{prefix}-{suffix}"
            for suffix in ("manifest.json", "replay.json", "summary.json", "trials.jsonl")
        )
        if spec.stage == "c1c":
            relative.append(client / "c1-external-motion-fixture-commands.jsonl")
        relative.extend(
            path.relative_to(run)
            for path in sorted((run / client / "runtime-trace" / "trace").glob("*"))
            if path.is_file()
        )
    paths = tuple(run / path for path in relative)
    missing = tuple(str(path) for path in paths if not path.is_file())
    if missing:
        raise EvidenceViolation(f"run {spec.run_id} is missing selected files: {missing}")
    return paths


def _failure_parts(value: object) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if type(value) is not dict:
        raise EvidenceViolation("primary_failure must be null or an object")
    failure_type = value.get("type")
    message = value.get("message")
    if type(failure_type) is not str or type(message) is not str:
        raise EvidenceViolation("primary_failure is incomplete")
    return failure_type, message


def _check_recorded_result(result: Mapping[str, object], spec: RunSpec) -> None:
    if result.get("status") != spec.status:
        raise EvidenceViolation(
            f"run {spec.run_id} recorded status is {result.get('status')!r}, "
            f"expected {spec.status!r}"
        )
    failure_type, message = _failure_parts(result.get("primary_failure"))
    if (failure_type, message) != (spec.failure_type, spec.failure_message):
        raise EvidenceViolation(
            f"run {spec.run_id} recorded failure differs from the frozen result"
        )


def _count_jsonl(path: Path) -> int:
    count = 0
    line_number = 0
    try:
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                json.loads(line)
                count += 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceViolation(f"invalid JSONL at {path}:{line_number}") from error
    return count


def _check_source_summary(run: Path, spec: RunSpec) -> None:
    client = run / "client-0"
    if spec.stage == "b10c":
        count = _count_jsonl(client / "b10-gap-solver-trials.jsonl")
        if count != spec.trial_rows:
            raise EvidenceViolation(
                f"run {spec.run_id} has {count} B10-C trials, expected {spec.trial_rows}"
            )
        return
    prefix = "c1-moving-melee" if spec.stage == "c1b" else "c1-external-motion"
    summary = _load_json(client / f"{prefix}-summary.json", "C1 summary")
    expected = {
        "completed_trials": spec.trial_rows,
        "planned_trials": spec.trial_rows,
        "positive_passed": spec.positive_passed,
        "positive_total": spec.positive_total,
        "negative_passed": spec.negative_passed,
        "negative_total": spec.negative_total,
        "fixture_invalid": 0,
    }
    mismatches = {
        key: (summary.get(key), value)
        for key, value in expected.items()
        if summary.get(key) != value
    }
    if mismatches:
        raise EvidenceViolation(f"run {spec.run_id} summary differs: {mismatches}")
    count = _count_jsonl(client / f"{prefix}-trials.jsonl")
    if count != spec.trial_rows:
        raise EvidenceViolation(
            f"run {spec.run_id} has {count} C1 trials, expected {spec.trial_rows}"
        )


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _write_archive(archive: Path, run: Path, spec: RunSpec, paths: Iterable[Path]) -> None:
    files = []
    for path in sorted(paths, key=lambda item: item.relative_to(run).as_posix()):
        _scan_public_bytes(path)
        files.append({
            "path": path.relative_to(run).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        })
    public_run = _json_bytes({
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": spec.run_id,
        "stage": spec.stage,
        "recorded_outcome": spec.outcome,
        "recorded_status": spec.status,
        "recorded_failure": (
            None if spec.failure_type is None else {
                "type": spec.failure_type,
                "message": spec.failure_message,
            }
        ),
        "current_replay": {
            "included": False,
            "reason": "历史结果与当前代码重放分开记录；本证据包不改写原运行结论。",
        },
        "members": files,
    })
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as tar:
                tar.addfile(_tar_info("public-run.json", len(public_run)), io.BytesIO(public_run))
                for entry, path in zip(files, sorted(paths, key=lambda item: item.relative_to(run).as_posix())):
                    with path.open("rb") as source:
                        tar.addfile(_tar_info(entry["path"], entry["bytes"]), source)


def _readme() -> str:
    return """# 代表性真实运行证据

这里保留 B10-C 跨隙、C1-B 移动近战和 C1-C 外力恢复各一个完整通过批次、一个完整失败批次。归档只含结构化结果、试次清单和轨迹，不含 Minecraft/Fabric JAR、世界、日志、画面或缓存。

运行：

```powershell
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

验证器会检查总清单、SHA-256、归档成员边界、JSON/JSONL 可读性、完整批次数量、通过汇总和失败分类。失败归档仍表示当时真实运行失败；当前代码后来能够重放或已修复，不会改变历史结论。

这些样本能让审查者核对文档引用的真实数据和证据读取链。它们不是新的 Fabric 实验，也不能单独证明当前代码在所有场景继续达到相同成功率。
"""


def _write_hashes(root: Path) -> None:
    lines = []
    for path in sorted(
        (path for path in root.rglob("*") if path.is_file() and path.name != HASH_FILE),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        lines.append(
            f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
        )
    (root / HASH_FILE).write_text("".join(lines), encoding="utf-8", newline="\n")


def _prepare_output(root: Path) -> None:
    if root.exists():
        if not root.is_dir():
            raise EvidenceViolation(f"evidence output is not a directory: {root}")
        index = root / "index.json"
        if any(root.iterdir()):
            current = _load_json(index, "existing evidence index")
            if current.get("schema_version") != SCHEMA_VERSION:
                raise EvidenceViolation(f"refusing to replace unrelated directory: {root}")
        shutil.rmtree(root)
    root.mkdir(parents=True)


def build_corpus(artifact_root: Path, output_root: Path) -> VerificationReport:
    """Build the frozen six-run corpus from local Fabric deployment artifacts."""
    artifacts = Path(artifact_root).resolve()
    output = Path(output_root).resolve()
    if not artifacts.is_dir():
        raise EvidenceViolation(f"artifact root is missing: {artifacts}")
    _prepare_output(output)
    entries = []
    try:
        for spec in RUN_SPECS:
            run = artifacts / spec.run_id
            result = _load_json(run / "result.json", "deployment result")
            _check_recorded_result(result, spec)
            _check_source_summary(run, spec)
            paths = _source_paths(run, spec)
            archive = output / "archives" / spec.archive_name
            _write_archive(archive, run, spec, paths)
            entries.append({
                "run_id": spec.run_id,
                "stage": spec.stage,
                "recorded_outcome": spec.outcome,
                "recorded_status": spec.status,
                "archive": archive.relative_to(output).as_posix(),
                "archive_sha256": _sha256_file(archive),
                "archive_size_bytes": archive.stat().st_size,
                "expected": {
                    "failure_type": spec.failure_type,
                    "failure_message": spec.failure_message,
                    "trial_rows": spec.trial_rows,
                    "positive_passed": spec.positive_passed,
                    "positive_total": spec.positive_total,
                    "negative_passed": spec.negative_passed,
                    "negative_total": spec.negative_total,
                },
            })
        total = sum(entry["archive_size_bytes"] for entry in entries)
        if total > MAX_TOTAL_ARCHIVE_BYTES:
            raise EvidenceViolation(
                f"archive total {total} exceeds {MAX_TOTAL_ARCHIVE_BYTES} bytes"
            )
        index = {
            "schema_version": SCHEMA_VERSION,
            "corpus_id": "representative-v1",
            "max_total_archive_bytes": MAX_TOTAL_ARCHIVE_BYTES,
            "total_archive_bytes": total,
            "runs": entries,
        }
        (output / "index.json").write_bytes(_json_bytes(index))
        (output / "README.md").write_text(_readme(), encoding="utf-8", newline="\n")
        _write_hashes(output)
        return verify_corpus(output)
    except Exception:
        if output.exists():
            shutil.rmtree(output)
        raise


def _parse_hash_file(root: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    try:
        lines = (root / HASH_FILE).read_text("utf-8").splitlines()
    except OSError as error:
        raise EvidenceViolation(f"missing {HASH_FILE}") from error
    for line in lines:
        digest, separator, name = line.partition("  ")
        if not separator or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise EvidenceViolation(f"invalid {HASH_FILE} entry")
        safe = _safe_member_name(name).as_posix()
        if safe in expected:
            raise EvidenceViolation(f"duplicate hash entry: {safe}")
        expected[safe] = digest
    return expected


def _verify_outer_hashes(root: Path) -> None:
    expected = _parse_hash_file(root)
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.name != HASH_FILE
    }
    missing = tuple(sorted(set(expected) - set(actual)))
    extra = tuple(sorted(set(actual) - set(expected)))
    changed = tuple(sorted(
        name for name in set(expected).intersection(actual)
        if _sha256_file(actual[name]) != expected[name]
    ))
    if missing or extra or changed:
        raise EvidenceViolation(
            f"public evidence differs: missing={missing}, extra={extra}, changed={changed}"
        )


def _read_member_bytes(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    source = tar.extractfile(member)
    if source is None:
        raise EvidenceViolation(f"cannot read archive member: {member.name}")
    return source.read()


def _parse_json_bytes(data: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceViolation(f"invalid JSON in {label}") from error
    if type(value) is not dict:
        raise EvidenceViolation(f"{label} must contain a JSON object")
    return value


def _validate_jsonl_bytes(data: bytes, label: str) -> int:
    count = 0
    try:
        text = data.decode("utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            json.loads(line)
            count += 1
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceViolation(f"invalid JSONL in {label}:{line_number}") from error
    return count


def _verify_archive(root: Path, entry: Mapping[str, object], spec: RunSpec) -> None:
    archive_value = entry.get("archive")
    if type(archive_value) is not str:
        raise EvidenceViolation(f"run {spec.run_id} has no archive path")
    archive = root / Path(_safe_member_name(archive_value))
    if _sha256_file(archive) != entry.get("archive_sha256"):
        raise EvidenceViolation(f"run {spec.run_id} archive changed")
    if archive.stat().st_size != entry.get("archive_size_bytes"):
        raise EvidenceViolation(f"run {spec.run_id} archive size changed")

    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise EvidenceViolation(f"run {spec.run_id} archive has too many members")
            names: set[str] = set()
            total = 0
            by_name = {}
            for member in members:
                safe = _safe_member_name(member.name).as_posix()
                if safe in names:
                    raise EvidenceViolation(f"duplicate archive member: {safe}")
                names.add(safe)
                if not member.isfile():
                    raise EvidenceViolation("archive members must be regular files")
                if member.size < 0:
                    raise EvidenceViolation(f"negative archive member size: {safe}")
                total += member.size
                if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise EvidenceViolation(f"run {spec.run_id} archive expands beyond its limit")
                by_name[safe] = member
            manifest_member = by_name.get("public-run.json")
            if manifest_member is None:
                raise EvidenceViolation(f"run {spec.run_id} has no public-run.json")
            manifest = _parse_json_bytes(
                _read_member_bytes(tar, manifest_member), "public-run.json"
            )
            expected_meta = {
                "schema_version": RUN_SCHEMA_VERSION,
                "run_id": spec.run_id,
                "stage": spec.stage,
                "recorded_outcome": spec.outcome,
                "recorded_status": spec.status,
            }
            for key, value in expected_meta.items():
                if manifest.get(key) != value:
                    raise EvidenceViolation(
                        f"run {spec.run_id} public manifest has wrong {key}"
                    )
            failure = manifest.get("recorded_failure")
            expected_failure = (
                None if spec.failure_type is None else {
                    "type": spec.failure_type,
                    "message": spec.failure_message,
                }
            )
            if failure != expected_failure:
                raise EvidenceViolation(
                    f"run {spec.run_id} public manifest has wrong recorded failure"
                )
            replay = manifest.get("current_replay")
            if type(replay) is not dict or replay.get("included") is not False:
                raise EvidenceViolation(
                    f"run {spec.run_id} must keep current replay separate"
                )
            listed = manifest.get("members")
            if type(listed) is not list:
                raise EvidenceViolation(f"run {spec.run_id} member list is missing")
            listed_by_name = {}
            for item in listed:
                if type(item) is not dict or type(item.get("path")) is not str:
                    raise EvidenceViolation(f"run {spec.run_id} member entry is invalid")
                name = _safe_member_name(item["path"]).as_posix()
                if name in listed_by_name:
                    raise EvidenceViolation(f"run {spec.run_id} repeats member {name}")
                listed_by_name[name] = item
            if set(by_name) != set(listed_by_name).union({"public-run.json"}):
                raise EvidenceViolation(f"run {spec.run_id} archive member list differs")

            parsed: dict[str, object] = {}
            jsonl_counts: dict[str, int] = {}
            for name, item in listed_by_name.items():
                member = by_name[name]
                data = _read_member_bytes(tar, member)
                if hashlib.sha256(data).hexdigest() != item.get("sha256"):
                    raise EvidenceViolation(f"run {spec.run_id} member changed: {name}")
                if len(data) != item.get("bytes"):
                    raise EvidenceViolation(f"run {spec.run_id} member size changed: {name}")
                if name.endswith(".json"):
                    parsed[name] = _parse_json_bytes(data, name)
                elif name.endswith(".jsonl"):
                    jsonl_counts[name] = _validate_jsonl_bytes(data, name)
            result = parsed.get("result.json")
            if type(result) is not dict:
                raise EvidenceViolation(f"run {spec.run_id} result.json is missing")
            _check_recorded_result(result, spec)
            if spec.stage == "b10c":
                trial_name = "client-0/b10-gap-solver-trials.jsonl"
                if jsonl_counts.get(trial_name) != spec.trial_rows:
                    raise EvidenceViolation(f"run {spec.run_id} B10-C trial count differs")
            else:
                prefix = (
                    "c1-moving-melee" if spec.stage == "c1b" else "c1-external-motion"
                )
                summary_name = f"client-0/{prefix}-summary.json"
                summary = parsed.get(summary_name)
                if type(summary) is not dict:
                    raise EvidenceViolation(f"run {spec.run_id} summary is missing")
                expected_summary = {
                    "completed_trials": spec.trial_rows,
                    "planned_trials": spec.trial_rows,
                    "positive_passed": spec.positive_passed,
                    "positive_total": spec.positive_total,
                    "negative_passed": spec.negative_passed,
                    "negative_total": spec.negative_total,
                    "fixture_invalid": 0,
                }
                for key, value in expected_summary.items():
                    if summary.get(key) != value:
                        raise EvidenceViolation(
                            f"run {spec.run_id} summary field {key} differs"
                        )
                trial_name = f"client-0/{prefix}-trials.jsonl"
                if jsonl_counts.get(trial_name) != spec.trial_rows:
                    raise EvidenceViolation(f"run {spec.run_id} C1 trial count differs")
    except (OSError, tarfile.TarError) as error:
        raise EvidenceViolation(f"cannot read archive for run {spec.run_id}") from error


def verify_corpus(root: Path) -> VerificationReport:
    """Verify hashes, archive safety, JSON structure, and the frozen run outcomes."""
    corpus = Path(root).resolve()
    if not corpus.is_dir():
        raise EvidenceViolation(f"public evidence root is missing: {corpus}")
    _verify_outer_hashes(corpus)
    index = _load_json(corpus / "index.json", "public evidence index")
    if index.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceViolation("unsupported public evidence schema")
    if index.get("max_total_archive_bytes") != MAX_TOTAL_ARCHIVE_BYTES:
        raise EvidenceViolation("public evidence size limit differs")
    runs = index.get("runs")
    if type(runs) is not list or len(runs) != len(RUN_SPECS):
        raise EvidenceViolation("public evidence must contain exactly six frozen runs")
    entries_by_id = {
        entry.get("run_id"): entry
        for entry in runs
        if type(entry) is dict and type(entry.get("run_id")) is str
    }
    if set(entries_by_id) != {spec.run_id for spec in RUN_SPECS}:
        raise EvidenceViolation("public evidence run set differs from the frozen set")
    archive_paths = {
        entry.get("archive") for entry in entries_by_id.values()
        if type(entry.get("archive")) is str
    }
    actual_archives = {
        path.relative_to(corpus).as_posix()
        for path in (corpus / "archives").glob("*.tar.gz")
    }
    if archive_paths != actual_archives:
        raise EvidenceViolation(
            f"public evidence archive set differs: expected={sorted(archive_paths)}, "
            f"actual={sorted(actual_archives)}"
        )
    total = 0
    for spec in RUN_SPECS:
        entry = entries_by_id[spec.run_id]
        if (
            entry.get("stage") != spec.stage
            or entry.get("recorded_outcome") != spec.outcome
            or entry.get("recorded_status") != spec.status
        ):
            raise EvidenceViolation(f"run {spec.run_id} index metadata differs")
        expected = entry.get("expected")
        frozen_expected = {
            "failure_type": spec.failure_type,
            "failure_message": spec.failure_message,
            "trial_rows": spec.trial_rows,
            "positive_passed": spec.positive_passed,
            "positive_total": spec.positive_total,
            "negative_passed": spec.negative_passed,
            "negative_total": spec.negative_total,
        }
        if expected != frozen_expected:
            raise EvidenceViolation(f"run {spec.run_id} expected result differs")
        _verify_archive(corpus, entry, spec)
        total += int(entry["archive_size_bytes"])
    if total != index.get("total_archive_bytes"):
        raise EvidenceViolation("public evidence total archive size differs")
    if total > MAX_TOTAL_ARCHIVE_BYTES:
        raise EvidenceViolation("public evidence exceeds the 30 MiB limit")
    stages = tuple(sorted({spec.stage for spec in RUN_SPECS}))
    return VerificationReport(
        len(RUN_SPECS), total, stages, tuple(spec.run_id for spec in RUN_SPECS)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--artifact-root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "build":
            report = build_corpus(arguments.artifact_root, arguments.output)
        else:
            report = verify_corpus(arguments.root)
        print(
            "PUBLIC_RUNTIME_EVIDENCE_OK "
            f"archives={report.archive_count} bytes={report.total_archive_bytes}"
        )
    except EvidenceViolation as error:
        print(f"PUBLIC_RUNTIME_EVIDENCE_ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
