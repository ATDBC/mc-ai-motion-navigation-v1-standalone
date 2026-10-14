from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.audit_d107_b10_trace import analyze_run, build_report


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _step(*, received: int | None, dispatched: int | None,
          applications: list[dict] | None) -> dict:
    payload: dict = {
        "backend_result": {"receipt": {}},
    }
    if received is not None:
        payload["decision_observation_received_ns"] = received
    if dispatched is not None:
        payload["dispatched_monotonic_ns"] = dispatched
    if applications is not None:
        payload["backend_result"]["receipt"]["input_applications"] = applications
    return {
        "schema_version": "mc2p.trace-record.v1",
        "record_type": "step",
        "payload": payload,
    }


class D107B10TraceAuditTests(unittest.TestCase):
    def test_reports_control_receipts_gaps_and_optional_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            _write_jsonl(run / "client-0" / "trace.jsonl", [
                _step(received=1_000_000_000, dispatched=1_010_000_000,
                      applications=[{"movement_tick_id": 10, "state": "leased"}]),
                _step(received=2_000_000_000, dispatched=2_060_000_000,
                      applications=[
                          {"movement_tick_id": 11, "state": "lease_exhausted"},
                          {"movement_tick_id": 13, "state": "leased"},
                      ]),
                _step(received=None, dispatched=None, applications=None),
            ])
            _write_jsonl(run / "client-0" / "b10-coordinator-control-frames.jsonl", [
                {"control_wall_ns": 12_000_000, "reason_code": "planning_submitted"},
                {"control_wall_ns": 55_000_000, "reason_code": "executing"},
            ])
            _write_jsonl(run / "client-0" / "ftick-events" / "segment-00000000.jsonl", [
                {"kind": "INPUT_SAMPLED", "movement_tick_id": 10},
                {"kind": "MOVEMENT_APPLIED", "movement_tick_id": 10},
                {"kind": "INPUT_SAMPLED", "movement_tick_id": 11},
            ])

            result = analyze_run("fixture", run)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["trace"]["step_frames"], 3)
        self.assertEqual(result["trace"]["control_prepare_ms"]["sample_count"], 2)
        self.assertEqual(result["trace"]["control_prepare_ms"]["over_50ms_count"], 1)
        self.assertEqual(result["trace"]["receipt_applications_per_frame"]["maximum"], 2)
        self.assertEqual(result["trace"]["lease_exhausted"]["application_count"], 1)
        self.assertEqual(result["trace"]["movement_ticks"]["gaps"], [
            {"after": 11, "before": 13, "missing_count": 1},
        ])
        self.assertEqual(result["trace"]["missing_fields"]["timing_frames"], 1)
        self.assertEqual(result["control_frames"]["control_wall_ms"]["over_50ms_count"], 1)
        self.assertEqual(result["ftick"]["kind_counts"], {
            "INPUT_SAMPLED": 2,
            "MOVEMENT_APPLIED": 1,
        })

    def test_missing_optional_files_and_missing_run_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            _write_jsonl(run / "client-0" / "trace.jsonl", [
                _step(received=1, dispatched=2, applications=[]),
            ])
            available = analyze_run("partial", run)
            report = build_report(
                (("partial", run),),
                (("d103_pre", "2026-09-25 original run directory is absent"),),
            )

        self.assertEqual(available["control_frames"]["status"], "unavailable")
        self.assertEqual(available["ftick"]["status"], "unavailable")
        self.assertEqual(report["runs"][1]["status"], "unavailable")
        self.assertIn("2026-09-25", report["runs"][1]["reason"])
        self.assertEqual(report["causal_conclusion"], "unavailable")

    def test_cli_writes_deterministic_json_and_rejects_bad_run_argument(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "audit_d107_b10_trace.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            output = root / "report.json"
            _write_jsonl(run / "client-0" / "trace.jsonl", [
                _step(received=1, dispatched=2, applications=[]),
            ])
            command = [
                sys.executable, str(script), "--run", f"d106={run}",
                "--unavailable", "d103_pre=raw run absent", "--output", str(output),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            bad = subprocess.run(
                [sys.executable, str(script), "--run", "missing-separator"],
                capture_output=True, text=True, check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["schema_version"], "mc2p.d107-b10-trace-audit.v1")
            self.assertEqual([run["label"] for run in written["runs"]], ["d106", "d103_pre"])
            self.assertNotEqual(bad.returncode, 0)


if __name__ == "__main__":
    unittest.main()
