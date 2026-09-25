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
    ("20260923T063950738376Z-5a9f5ab9", "b10c", "pass", "passed", None),
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
    ("20260925T013056709842Z-97bc0db8", "c1c", "pass", "passed", None),
    (
        "20260924T164017683290Z-1c95ef4a",
        "c1c",
        "fail",
        "failed",
        ("DeploymentEvidenceFailure", "['client-0:c1c_20_of_20_positive_tasks']"),
    ),
    ("20260925T104441670754Z-64a4005e", "b11", "pass", "passed", None),
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
        if stage == "b11":
            _write_jsonl(client / "trace.jsonl", [{"event": "observation"}])
            _write_json(client / "b11-summary.json", {
                "schema_version": "mc2p.b11-world-change-summary.v1",
                "positive_trials": 60,
                "passed_trials": 60,
                "confirmed_placements": 90,
                "all_passed": True,
                "negative_trials": 20,
                "negative_passed_trials": 20,
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
                 for index in range(20)],
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
                [{"name": f"trial-{index}"} for index in range(82)],
            )
            if status == "passed":
                _write_json(client / "b10-gap-solver.json", {"passed": True})
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

    def test_build_and_verify_seven_frozen_batches(self) -> None:
        build_corpus(self.sources, self.corpus)

        report = verify_corpus(self.corpus)

        self.assertEqual(report.archive_count, 7)
        self.assertLessEqual(report.total_archive_bytes, 30 * 1024 * 1024)
        self.assertEqual(report.stages, ("b10c", "b11", "c1b", "c1c"))

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
