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
    control_frame_rows: int | None = None
    perception_status: str = "historical_legacy_ray_profile3"

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
        "20260926T091031597407Z-f7f68346", "b10c", "pass", "passed",
        None, None, 142, control_frame_rows=210,
    ),
    RunSpec(
        "20260923T083302580358Z-a019b110", "b10c", "fail", "failed",
        "ContractViolation", "verified executor already has an in-flight command", 82,
    ),
    RunSpec(
        "20260926T090526982665Z-de19f7bc", "c1b", "pass", "passed",
        None, None, 30, 20, 20, 10, 10,
    ),
    RunSpec(
        "20260925T014651611158Z-8fbbd0bb", "c1b", "fail", "failed",
        "DeploymentEvidenceFailure",
        "['client-0:c1b_20_of_20_positive_tasks']", 30, 19, 20, 10, 10,
    ),
    RunSpec(
        "20260925T124344500270Z-ac8d6347", "c1c", "pass", "passed",
        None, None, 30, 20, 20, 10, 10,
    ),
    RunSpec(
        "20260924T164017683290Z-1c95ef4a", "c1c", "fail", "failed",
        "DeploymentEvidenceFailure",
        "['client-0:c1c_20_of_20_positive_tasks']", 30, 18, 20, 10, 10,
    ),
    RunSpec(
        "20260925T120057978030Z-c17b889e", "b11", "pass", "passed",
        None, None, 84, 60, 60, 24, 24,
    ),
    RunSpec(
        "20260926T055043431485Z-e2dc4fff", "b12a", "pass", "passed",
        None, None, 8, None, None, 8, 8,
    ),
    RunSpec(
        "20260926T091904725912Z-deb6726e", "b12b", "pass", "passed",
        None, None, 34, 24, 24, 8, 8,
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
    if spec.stage == "b12a":
        relative.extend((
            client / "b12a-fabric-manifest.json",
            client / "b12a-fabric-online.json",
            client / "b12a-fabric-evidence.json",
            client / "b12a-fixture-commands.jsonl",
        ))
        directory = run / client / "runtime-trace" / "trace"
        relative.extend(
            path.relative_to(run)
            for path in sorted(directory.glob("*"))
            if path.is_file()
        )
    elif spec.stage == "b12b":
        relative.extend((
            client / "b12b-partial-combat-manifest.json",
            client / "b12b-partial-combat-runtime.json",
            client / "b12b-partial-combat-evidence.json",
            client / "b12b-partial-combat-trials.jsonl",
            client / "b12b-fixture-commands.jsonl",
        ))
        for directory in (
            run / client / "runtime-trace" / "trace",
            run / client / "control-events",
        ):
            relative.extend(
                path.relative_to(run)
                for path in sorted(directory.glob("*"))
                if path.is_file()
            )
    elif spec.stage == "b11":
        relative.extend((
            client / "trace.jsonl",
            client / "b11-summary.json",
            client / "b11-trials.jsonl",
            client / "b11-negative-trials.jsonl",
            client / "b11-fixture-commands.jsonl",
        ))
    elif spec.stage == "b10c":
        relative.append(client / "b10-gap-solver-trials.jsonl")
        if spec.outcome == "pass":
            relative.extend((
                client / "b10-gap-solver.json",
                client / "b10-coordinator-control-frames.jsonl",
                client / "b10-coordinator-trials.jsonl",
            ))
            relative.extend(
                path.relative_to(run)
                for path in sorted((run / client / "physics-tick-events").glob("*"))
                if path.is_file()
            )
        else:
            relative.append(client / "trace.jsonl")
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


def _b12b_active_trials_are_complete(trials: object) -> bool:
    if type(trials) is not list:
        return False
    active = [
        row for row in trials
        if type(row) is dict and row.get("classification") == "active_target"
    ]
    if {row.get("active_mode") for row in active} != {
        "induced_turn", "sustained_chase",
    }:
        return False
    sustained = next(
        (row for row in active if row.get("active_mode") == "sustained_chase"),
        None,
    )
    return (
        all(
            row.get("passed") is True
            and isinstance(
                row.get("time_to_first_non_neutral_command_seconds"),
                (int, float),
            )
            and row.get("first_movement_response_origin") in {
                "target_acquired", "navigation_information_ready",
            }
            and isinstance(
                row.get("first_movement_response_control_frames"), int,
            )
            and row["first_movement_response_control_frames"] <= 10
            and type(row.get("pre_movement_navigation_reason_counts")) is dict
            and type(row.get("navigation_reason_counts")) is dict
            and bool(row["navigation_reason_counts"])
            for row in active
        )
        and
        type(sustained) is dict
        and sustained.get("passed") is True
        and isinstance(sustained.get("control_frame_count"), int)
        and sustained["control_frame_count"] >= 30
        and isinstance(sustained.get("elapsed_seconds"), (int, float))
        and sustained["elapsed_seconds"] >= 1.5
        and isinstance(sustained.get("target_displacement_blocks"), (int, float))
        and sustained["target_displacement_blocks"] >= 0.25
        and isinstance(sustained.get("turn_frame_count"), int)
        and sustained["turn_frame_count"] > 0
        and isinstance(sustained.get("moving_turn_ratio"), (int, float))
        and sustained["moving_turn_ratio"] >= 0.5
        and type(sustained.get("navigation_reason_counts")) is dict
        and bool(sustained["navigation_reason_counts"])
    )


def _check_source_summary(run: Path, spec: RunSpec) -> None:
    client = run / "client-0"
    if spec.stage == "b12a":
        evidence = _load_json(
            client / "b12a-fabric-evidence.json", "B12-A evidence",
        )
        trials = evidence.get("trials")
        diagnostics = evidence.get("damage_source_diagnostics")
        trace_stats = evidence.get("trace_stats")
        if (
            evidence.get("trial_count") != spec.trial_rows
            or type(trials) is not list
            or len(trials) != spec.trial_rows
            or any(type(row) is not dict or row.get("passed") is not True
                   for row in trials)
            or type(diagnostics) is not dict
            or type(trace_stats) is not dict
            or trace_stats.get("dropped_records") != 0
        ):
            raise EvidenceViolation(
                f"run {spec.run_id} B12-A evidence differs from frozen acceptance"
            )
        _check_b12a_diagnostics(diagnostics, spec.run_id)
        return
    if spec.stage == "b12b":
        summary = _load_json(
            client / "b12b-partial-combat-runtime.json", "B12-B summary",
        )
        evidence = _load_json(
            client / "b12b-partial-combat-evidence.json", "B12-B evidence",
        )
        positives = evidence.get("trials")
        boundaries = summary.get("evaluated_boundaries")
        timing = summary.get("control_decision_ms")
        checks = evidence.get("checks")
        positive_count = (
            sum(type(row) is dict and row.get("classification") == "positive"
                for row in positives)
            if type(positives) is list else -1
        )
        active_count = (
            sum(type(row) is dict and row.get("classification") == "active_target"
                for row in positives)
            if type(positives) is list else -1
        )
        if (
            summary.get("completed_trials") != spec.trial_rows
            or summary.get("planned_trials") != spec.trial_rows
            or type(positives) is not list
            or len(positives) != spec.trial_rows
            or positive_count != spec.positive_total
            or active_count != 2
            or not _b12b_active_trials_are_complete(positives)
            or any(type(row) is not dict or row.get("passed") is not True
                   for row in positives)
            or type(boundaries) is not list
            or len(boundaries) != spec.negative_total
            or any(type(row) is not dict or row.get("passed") is not True
                   for row in boundaries)
            or type(checks) is not list
            or not checks
            or any(type(check) is not dict or check.get("passed") is not True
                   for check in checks)
            or type(timing) is not dict
            or not isinstance(timing.get("p95"), (int, float))
            or not isinstance(timing.get("p99"), (int, float))
            or timing["p95"] > 8.0
            or timing["p99"] > 15.0
        ):
            raise EvidenceViolation(
                f"run {spec.run_id} B12-B summary differs from frozen acceptance"
            )
        count = _count_jsonl(client / "b12b-partial-combat-trials.jsonl")
        if count != spec.trial_rows:
            raise EvidenceViolation(
                f"run {spec.run_id} has {count} B12-B trials, "
                f"expected {spec.trial_rows}"
            )
        return
    if spec.stage == "b11":
        summary = _load_json(client / "b11-summary.json", "B11 summary")
        expected = {
            "positive_trials": spec.positive_total,
            "passed_trials": spec.positive_passed,
            "negative_trials": spec.negative_total,
            "negative_passed_trials": spec.negative_passed,
            "confirmed_placements": 90,
            "all_passed": True,
            "negative_all_passed": True,
        }
        mismatches = {
            key: (summary.get(key), value)
            for key, value in expected.items()
            if summary.get(key) != value
        }
        if mismatches:
            raise EvidenceViolation(
                f"run {spec.run_id} B11 summary differs: {mismatches}"
            )
        count = (
            _count_jsonl(client / "b11-trials.jsonl")
            + _count_jsonl(client / "b11-negative-trials.jsonl")
        )
        if count != spec.trial_rows:
            raise EvidenceViolation(
                f"run {spec.run_id} has {count} B11 trials, "
                f"expected {spec.trial_rows}"
            )
        return
    if spec.stage == "b10c":
        count = _count_jsonl(client / "b10-gap-solver-trials.jsonl")
        if count != spec.trial_rows:
            raise EvidenceViolation(
                f"run {spec.run_id} has {count} B10-C trials, expected {spec.trial_rows}"
            )
        if spec.outcome == "pass":
            summary = _load_json(client / "b10-gap-solver.json", "B10-C summary")
            if (summary.get("coordinator_validation_count") != 10
                    or summary.get("coordinator_validation_success_count") != 10):
                raise EvidenceViolation(
                    f"run {spec.run_id} B10-C coordinator result differs"
                )
            control_path = client / "b10-coordinator-control-frames.jsonl"
            control_count = _count_jsonl(control_path)
            if control_count != spec.control_frame_rows:
                raise EvidenceViolation(
                    f"run {spec.run_id} has {control_count} B10-C control frames, "
                    f"expected {spec.control_frame_rows}"
                )
            with control_path.open("r", encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    row = json.loads(line)
                    late_by = row.get("actual_minus_latest_ticks")
                    if type(late_by) is int and late_by > 0:
                        raise EvidenceViolation(
                            f"run {spec.run_id} has a late B10-C input at "
                            f"control frame {line_number}"
                        )
            if _count_jsonl(client / "b10-coordinator-trials.jsonl") != 10:
                raise EvidenceViolation(
                    f"run {spec.run_id} B10-C coordinator trial count differs"
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


def _check_b12a_diagnostics(
        diagnostics: Mapping[str, object], run_id: str) -> None:
    player = diagnostics.get("player_attack")
    environment = diagnostics.get("environment_damage")
    if (
        type(player) is not dict
        or player.get("damage_type") != "minecraft:player_attack"
        or player.get("evidence_grade") != "source_confirmed"
        or player.get("outcome") != "source_confirmed_hit"
        or type(environment) is not dict
        or environment.get("damage_type") != "minecraft:on_fire"
        or environment.get("source_entity_present") is not False
        or environment.get("direct_entity_present") is not False
        or environment.get("source_is_self") is not False
        or environment.get("direct_source_is_self") is not False
    ):
        raise EvidenceViolation(
            f"run {run_id} B12-A damage source diagnostics differ"
        )


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
        "perception_status": spec.perception_status,
        "current_acceptance_eligible": False,
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

这里保留 B10-C 跨隙、C1-B 移动近战和 C1-C 外力恢复各一个完整通过批次、一个完整失败批次，并加入 B11 放置与有限搭桥、B12-A 伤害来源、B12-B 部分观察下战斗移动的历史批次。它们全部使用已经退出正式主线的 profile 3 稀疏射线，只用于核对当时的运行结论和复现旧问题，不能证明当前 profile 4 表面深度主线已经通过对应阶段。

B10-C 通过批次保留 210 个协调控制帧、10 个协调试次、142 个求解试次和物理 tick 片段。B12-A 批次保留玩家近战和环境伤害来源诊断。B12-B 批次保留 34 个 Fabric 场景、控制事件和分段 Runtime 轨迹。不复制普通日志、画面或缓存。

运行：

```powershell
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

验证器会检查总清单、SHA-256、归档成员边界、JSON/JSONL 可读性、完整批次数量、通过汇总和失败分类。失败归档仍表示当时真实运行失败；当前代码后来能够重放或已修复，不会改变历史结论。

这些样本能让审查者核对文档引用的真实数据和证据读取链。索引和每个归档中的 `public-run.json` 都把它们标成 `historical_legacy_ray_profile3`，并明确写出 `current_acceptance_eligible=false`。
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
                "perception_status": spec.perception_status,
                "current_acceptance_eligible": False,
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
                "perception_status": spec.perception_status,
                "current_acceptance_eligible": False,
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
            b10_late_inputs = 0
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
                    if name == "client-0/b10-coordinator-control-frames.jsonl":
                        for line in data.decode("utf-8").splitlines():
                            if not line.strip():
                                continue
                            row = json.loads(line)
                            late_by = row.get("actual_minus_latest_ticks")
                            if type(late_by) is int and late_by > 0:
                                b10_late_inputs += 1
            result = parsed.get("result.json")
            if type(result) is not dict:
                raise EvidenceViolation(f"run {spec.run_id} result.json is missing")
            _check_recorded_result(result, spec)
            if spec.stage == "b12a":
                evidence = parsed.get("client-0/b12a-fabric-evidence.json")
                if type(evidence) is not dict:
                    raise EvidenceViolation(
                        f"run {spec.run_id} B12-A evidence is missing"
                    )
                trials = evidence.get("trials")
                diagnostics = evidence.get("damage_source_diagnostics")
                trace_stats = evidence.get("trace_stats")
                if (
                    evidence.get("trial_count") != spec.trial_rows
                    or type(trials) is not list
                    or len(trials) != spec.trial_rows
                    or any(type(row) is not dict or row.get("passed") is not True
                           for row in trials)
                    or type(diagnostics) is not dict
                    or type(trace_stats) is not dict
                    or trace_stats.get("dropped_records") != 0
                ):
                    raise EvidenceViolation(
                        f"run {spec.run_id} B12-A evidence differs"
                    )
                _check_b12a_diagnostics(diagnostics, spec.run_id)
            elif spec.stage == "b12b":
                summary = parsed.get(
                    "client-0/b12b-partial-combat-runtime.json"
                )
                evidence = parsed.get(
                    "client-0/b12b-partial-combat-evidence.json"
                )
                if type(summary) is not dict or type(evidence) is not dict:
                    raise EvidenceViolation(
                        f"run {spec.run_id} B12-B summary or evidence is missing"
                    )
                positives = evidence.get("trials")
                boundaries = summary.get("evaluated_boundaries")
                checks = evidence.get("checks")
                timing = summary.get("control_decision_ms")
                positive_count = (
                    sum(type(row) is dict and row.get("classification") == "positive"
                        for row in positives)
                    if type(positives) is list else -1
                )
                active_count = (
                    sum(type(row) is dict and row.get("classification") == "active_target"
                        for row in positives)
                    if type(positives) is list else -1
                )
                if (
                    summary.get("completed_trials") != spec.trial_rows
                    or summary.get("planned_trials") != spec.trial_rows
                    or type(positives) is not list
                    or len(positives) != spec.trial_rows
                    or positive_count != spec.positive_total
                    or active_count != 2
                    or not _b12b_active_trials_are_complete(positives)
                    or any(type(row) is not dict or row.get("passed") is not True
                           for row in positives)
                    or type(boundaries) is not list
                    or len(boundaries) != spec.negative_total
                    or any(type(row) is not dict or row.get("passed") is not True
                           for row in boundaries)
                    or type(checks) is not list
                    or not checks
                    or any(type(check) is not dict or check.get("passed") is not True
                           for check in checks)
                    or type(timing) is not dict
                    or not isinstance(timing.get("p95"), (int, float))
                    or not isinstance(timing.get("p99"), (int, float))
                    or timing["p95"] > 8.0
                    or timing["p99"] > 15.0
                ):
                    raise EvidenceViolation(
                        f"run {spec.run_id} B12-B evidence differs"
                    )
                if jsonl_counts.get(
                    "client-0/b12b-partial-combat-trials.jsonl"
                ) != spec.trial_rows:
                    raise EvidenceViolation(
                        f"run {spec.run_id} B12-B trial count differs"
                    )
            elif spec.stage == "b11":
                summary = parsed.get("client-0/b11-summary.json")
                if type(summary) is not dict:
                    raise EvidenceViolation(f"run {spec.run_id} summary is missing")
                expected_summary = {
                    "positive_trials": spec.positive_total,
                    "passed_trials": spec.positive_passed,
                    "negative_trials": spec.negative_total,
                    "negative_passed_trials": spec.negative_passed,
                    "confirmed_placements": 90,
                    "all_passed": True,
                    "negative_all_passed": True,
                }
                for key, value in expected_summary.items():
                    if summary.get(key) != value:
                        raise EvidenceViolation(
                            f"run {spec.run_id} summary field {key} differs"
                        )
                positive = jsonl_counts.get("client-0/b11-trials.jsonl")
                negative = jsonl_counts.get(
                    "client-0/b11-negative-trials.jsonl"
                )
                if (positive != spec.positive_total
                        or negative != spec.negative_total):
                    raise EvidenceViolation(
                        f"run {spec.run_id} B11 trial count differs"
                    )
            elif spec.stage == "b10c":
                trial_name = "client-0/b10-gap-solver-trials.jsonl"
                if jsonl_counts.get(trial_name) != spec.trial_rows:
                    raise EvidenceViolation(f"run {spec.run_id} B10-C trial count differs")
                if spec.outcome == "pass":
                    summary = parsed.get("client-0/b10-gap-solver.json")
                    if (type(summary) is not dict
                            or summary.get("coordinator_validation_count") != 10
                            or summary.get("coordinator_validation_success_count") != 10):
                        raise EvidenceViolation(
                            f"run {spec.run_id} B10-C coordinator result differs"
                        )
                    control_name = (
                        "client-0/b10-coordinator-control-frames.jsonl"
                    )
                    if jsonl_counts.get(control_name) != spec.control_frame_rows:
                        raise EvidenceViolation(
                            f"run {spec.run_id} B10-C control frame count differs"
                        )
                    if b10_late_inputs:
                        raise EvidenceViolation(
                            f"run {spec.run_id} B10-C contains late inputs"
                        )
                    if jsonl_counts.get(
                        "client-0/b10-coordinator-trials.jsonl"
                    ) != 10:
                        raise EvidenceViolation(
                            f"run {spec.run_id} B10-C coordinator trial count differs"
                        )
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
        raise EvidenceViolation(
            f"public evidence must contain exactly {len(RUN_SPECS)} frozen runs"
        )
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
            or entry.get("perception_status") != spec.perception_status
            or entry.get("current_acceptance_eligible") is not False
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
