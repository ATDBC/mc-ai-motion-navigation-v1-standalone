from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

from scripts.public_runtime_evidence import (
    EvidenceViolation,
    build_corpus,
    verify_corpus,
)


RUNS = (
    ("20260925T123521898324Z-5d0a66dc", "b10c", "pass", "passed", None),
    (
        "20260923T083302580358Z-a019b110",
        "b10c",
        "fail",
        "failed",
        ("ContractViolation", "verified executor already has an in-flight command"),
    ),
    ("20260925T015540321761Z-d008bbc3", "c1b", "pass", "passed", None),
    (
        "20260925T014651611158Z-8fbbd0bb",
        "c1b",
        "fail",
        "failed",
        ("DeploymentEvidenceFailure", "['client-0:c1b_20_of_20_positive_tasks']"),
    ),
    ("20260925T124344500270Z-ac8d6347", "c1c", "pass", "passed", None),
    (
        "20260924T164017683290Z-1c95ef4a",
        "c1c",
        "fail",
        "failed",
        ("DeploymentEvidenceFailure", "['client-0:c1c_20_of_20_positive_tasks']"),
    ),
    ("20260925T120057978030Z-c17b889e", "b11", "pass", "passed", None),
    ("20260926T055043431485Z-e2dc4fff", "b12a", "pass", "passed", None),
    ("20260926T063032338012Z-780e0386", "b12b", "pass", "passed", None),
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def _make_sources(root: Path) -> None:
    for run_id, stage, _outcome, status, failure in RUNS:
        run = root / run_id
        primary_failure = None
        if failure is not None:
            primary_failure = {"type": failure[0], "message": failure[1]}
        _write_json(
            run / "result.json",
            {
                "schema_version": "mc2p.fabric-deployment-probe.v2",
                "status": status,
                "primary_failure": primary_failure,
                "seed": 21000,
            },
        )
        client = run / "client-0"
        if stage == "b12a":
            diagnostics = {
                "schema_version": "mc2p.b12a-damage-source-diagnostics.v1",
                "player_attack": {
                    "damage_type": "minecraft:player_attack",
                    "evidence_grade": "source_confirmed",
                    "outcome": "source_confirmed_hit",
                },
                "environment_damage": {
                    "damage_type": "minecraft:on_fire",
                    "source_entity_present": False,
                    "direct_entity_present": False,
                    "source_is_self": False,
                    "direct_source_is_self": False,
                },
            }
            trials = [
                {"trial_id": f"negative-{index}", "classification": "negative",
                 "passed": True}
                for index in range(8)
            ]
            _write_json(client / "b12a-fabric-manifest.json", {
                "schema_version": "mc2p.b12a-fabric-manifest.v1",
                "world_seed": 21001,
                "code_hashes": {"example.py": "0" * 64},
                "trials": trials,
            })
            _write_json(client / "b12a-fabric-online.json", {
                "schema_version": "mc2p.b12a-fabric-evidence.v1",
                "trial_count": 8,
                "damage_source_diagnostics": diagnostics,
                "trials": trials,
            })
            _write_json(client / "b12a-fabric-evidence.json", {
                "schema_version": "mc2p.b12a-fabric-evidence.v1",
                "trial_count": 8,
                "damage_source_diagnostics": diagnostics,
                "trace_stats": {"accepted_records": 10, "dropped_records": 0},
                "trials": trials,
            })
            _write_jsonl(
                client / "b12a-fixture-commands.jsonl",
                [{"trial_id": row["trial_id"], "commands": []} for row in trials],
            )
            trace = client / "runtime-trace" / "trace"
            _write_jsonl(trace / "manifest.jsonl", [{"segment": 0}])
            _write_json(trace / "complete.json", {"complete": True})
            _write_jsonl(trace / "segment-00000000.jsonl", [{"tick": 1}])
            continue
        if stage == "b12b":
            _write_json(client / "b12b-partial-combat-manifest.json", {
                "schema_version": "mc2p.b12b-partial-combat-manifest.v1",
                "world_seed": 21001,
                "code_hashes": {"example.py": "0" * 64},
                "trials": [],
            })
            positives = [
                {"trial_id": f"positive-{index}", "classification": "positive",
                 "passed": True}
                for index in range(24)
            ]
            boundaries = [
                {"trial_id": f"boundary-{index}", "classification": "boundary",
                 "passed": True}
                for index in range(8)
            ]
            _write_json(client / "b12b-partial-combat-runtime.json", {
                "schema_version": "mc2p.b12b-partial-combat-runtime.v1",
                "completed_trials": 33,
                "planned_trials": 33,
                "control_decision_ms": {
                    "count": 889, "p95": 5.0015, "p99": 6.2107,
                },
                "evaluated_boundaries": boundaries,
                "trials": positives + boundaries,
            })
            _write_json(client / "b12b-partial-combat-evidence.json", {
                "trials": positives
                + [{"trial_id": "active-target", "classification": "active_target",
                    "passed": True}]
                + boundaries,
                "checks": [
                    {"name": "same_tick", "passed": True},
                    {"name": "right_target", "passed": True},
                ],
            })
            _write_jsonl(
                client / "b12b-partial-combat-trials.jsonl",
                positives + [{"trial_id": "active-target", "passed": True}]
                + boundaries,
            )
            _write_jsonl(
                client / "b12b-fixture-commands.jsonl",
                [{"trial_id": row["trial_id"], "commands": []}
                 for row in positives + [{"trial_id": "active-target"}]
                 + boundaries],
            )
            for directory in (
                client / "runtime-trace" / "trace",
                client / "control-events",
            ):
                _write_jsonl(directory / "manifest.jsonl", [{"segment": 0}])
                _write_json(directory / "complete.json", {"complete": True})
                _write_jsonl(directory / "segment-00000000.jsonl", [{"tick": 1}])
            continue
        if stage == "b11":
            _write_jsonl(client / "trace.jsonl", [{"event": "observation"}])
            _write_json(client / "b11-summary.json", {
                "schema_version": "mc2p.b11-world-change-summary.v1",
                "positive_trials": 60,
                "passed_trials": 60,
                "confirmed_placements": 90,
                "all_passed": True,
                "negative_trials": 24,
                "negative_passed_trials": 24,
                "negative_all_passed": True,
            })
            _write_jsonl(
                client / "b11-trials.jsonl",
                [{"trial_id": f"positive-{index}", "passed": True}
                 for index in range(60)],
            )
            _write_jsonl(
                client / "b11-negative-trials.jsonl",
                [{"trial_id": f"negative-{index}", "passed": True}
                for index in range(24)],
            )
            _write_jsonl(
                client / "b11-fixture-commands.jsonl",
                [{"trial_id": "positive-0", "commands": []}],
            )
            continue
        if stage == "b10c":
            _write_jsonl(client / "trace.jsonl", [{"event": "observation"}])
            _write_jsonl(
                client / "b10-gap-solver-trials.jsonl",
                [{"name": f"trial-{index}"}
                 for index in range(142 if status == "passed" else 82)],
            )
            if status == "passed":
                _write_json(client / "b10-gap-solver.json", {
                    "coordinator_validation_count": 10,
                    "coordinator_validation_success_count": 10,
                })
                _write_jsonl(
                    client / "b10-coordinator-control-frames.jsonl",
                    [{"actual_minus_latest_ticks": 0} for _ in range(210)],
                )
                _write_jsonl(
                    client / "b10-coordinator-trials.jsonl",
                    [{"name": f"coordinator-{index}"} for index in range(10)],
                )
                trace = client / "physics-tick-events"
                _write_jsonl(trace / "manifest.jsonl", [{"segment": 0}])
                _write_json(trace / "complete.json", {"complete": True})
                _write_jsonl(trace / "segment-00000000.jsonl", [{"tick": 1}])
            continue

        prefix = "c1-moving-melee" if stage == "c1b" else "c1-external-motion"
        positive = 20 if status == "passed" else (19 if stage == "c1b" else 18)
        summary = {
            "schema_version": f"mc2p.{prefix}-summary.v1",
            "completed_trials": 30,
            "planned_trials": 30,
            "fixture_invalid": 0,
            "positive_passed": positive,
            "positive_total": 20,
            "negative_passed": 10,
            "negative_total": 10,
        }
        _write_json(client / f"{prefix}-summary.json", summary)
        _write_json(
            client / f"{prefix}-manifest.json",
            {
                "schema_version": f"mc2p.{prefix}-manifest.v1",
                "world_seed": 21000,
                "code_hashes": {"example.py": "0" * 64},
                "trials": [],
            },
        )
        _write_json(client / f"{prefix}-replay.json", {"matched": True})
        trials = [
            {
                "trial_id": f"trial-{index}",
                "classification": "positive" if index < 20 else "negative",
                "passed": index < positive or index >= 20,
            }
            for index in range(30)
        ]
        _write_jsonl(client / f"{prefix}-trials.jsonl", trials)
        if stage == "c1c":
            _write_jsonl(
                client / "c1-external-motion-fixture-commands.jsonl",
                [{"command": "attack"}],
            )
        trace = client / "runtime-trace" / "trace"
        _write_jsonl(trace / "manifest.jsonl", [{"segment": 0}])
        _write_json(trace / "complete.json", {"complete": True})
        _write_jsonl(trace / "segment-00000000.jsonl", [{"tick": 1}])


def _rewrite_checksums(root: Path) -> None:
    lines = []
    for path in sorted(
        (path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS.txt"),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
    (root / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")


def _replace_archive(root: Path, archive: Path, member: tarfile.TarInfo) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as output:
        output.addfile(member, io.BytesIO(b"{}\n") if member.isreg() else None)
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(buffer.getvalue())
    index_path = root / "index.json"
    index = json.loads(index_path.read_text("utf-8"))
    relative = archive.relative_to(root).as_posix()
    for entry in index["runs"]:
        if entry["archive"] == relative:
            entry["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            entry["archive_size_bytes"] = archive.stat().st_size
            break
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _rewrite_checksums(root)


class PublicRuntimeEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="public-evidence-"))
        self.sources = self.temp / "artifacts"
        self.corpus = self.temp / "corpus"
        _make_sources(self.sources)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_build_and_verify_nine_frozen_batches(self) -> None:
        build_corpus(self.sources, self.corpus)

        report = verify_corpus(self.corpus)

        self.assertEqual(report.archive_count, 9)
        self.assertLessEqual(report.total_archive_bytes, 30 * 1024 * 1024)
        self.assertEqual(
            report.stages, ("b10c", "b11", "b12a", "b12b", "c1b", "c1c"),
        )

        b10 = next((self.corpus / "archives").glob("b10c-pass-*.tar.gz"))
        with tarfile.open(b10, mode="r:gz") as archive:
            names = set(archive.getnames())
        self.assertIn("client-0/b10-coordinator-control-frames.jsonl", names)
        self.assertNotIn("client-0/trace.jsonl", names)

        b12b = next((self.corpus / "archives").glob("b12b-pass-*.tar.gz"))
        with tarfile.open(b12b, mode="r:gz") as archive:
            names = set(archive.getnames())
        self.assertIn("client-0/b12b-partial-combat-evidence.json", names)
        self.assertIn("client-0/control-events/segment-00000000.jsonl", names)
        self.assertIn(
            "client-0/runtime-trace/trace/segment-00000000.jsonl", names,
        )

        b12a = next((self.corpus / "archives").glob("b12a-pass-*.tar.gz"))
        with tarfile.open(b12a, mode="r:gz") as archive:
            names = set(archive.getnames())
        self.assertIn("client-0/b12a-fabric-evidence.json", names)
        self.assertIn(
            "client-0/runtime-trace/trace/segment-00000000.jsonl", names,
        )

    def test_build_is_byte_deterministic(self) -> None:
        second = self.temp / "corpus-second"
        build_corpus(self.sources, self.corpus)
        build_corpus(self.sources, second)

        first_hashes = (self.corpus / "SHA256SUMS.txt").read_bytes()
        second_hashes = (second / "SHA256SUMS.txt").read_bytes()
        self.assertEqual(first_hashes, second_hashes)

    def test_changed_or_extra_archive_is_rejected(self) -> None:
        build_corpus(self.sources, self.corpus)
        archive = next((self.corpus / "archives").glob("*.tar.gz"))
        archive.write_bytes(archive.read_bytes() + b"changed")
        with self.assertRaisesRegex(EvidenceViolation, "changed"):
            verify_corpus(self.corpus)

        build_corpus(self.sources, self.corpus)
        shutil.copyfile(archive, archive.with_name("unexpected.tar.gz"))
        with self.assertRaisesRegex(EvidenceViolation, "extra"):
            verify_corpus(self.corpus)

    def test_source_status_must_match_frozen_historical_result(self) -> None:
        result = self.sources / RUNS[1][0] / "result.json"
        value = json.loads(result.read_text("utf-8"))
        value["status"] = "passed"
        value["primary_failure"] = None
        _write_json(result, value)

        with self.assertRaisesRegex(EvidenceViolation, "recorded status"):
            build_corpus(self.sources, self.corpus)

    def test_unsafe_tar_member_is_rejected_before_extraction(self) -> None:
        build_corpus(self.sources, self.corpus)
        archive = next((self.corpus / "archives").glob("*.tar.gz"))
        member = tarfile.TarInfo("../outside.json")
        member.size = 3
        _replace_archive(self.corpus, archive, member)

        with self.assertRaisesRegex(EvidenceViolation, "unsafe archive member"):
            verify_corpus(self.corpus)

    def test_symbolic_link_member_is_rejected(self) -> None:
        build_corpus(self.sources, self.corpus)
        archive = next((self.corpus / "archives").glob("*.tar.gz"))
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "result.json"
        _replace_archive(self.corpus, archive, member)

        with self.assertRaisesRegex(EvidenceViolation, "regular files"):
            verify_corpus(self.corpus)


if __name__ == "__main__":
    unittest.main()
