"""D061 long-session evidence stays bounded, isolated, and auditable."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.benchmark_d061_long_session import (
    _FrameTimingRecorder,
    _control_period_outcome,
    _timing_gates,
    _gen2_coverage,
)
from scripts.f2_ground_route_evidence import (
    _FrameTimingRecorder as _HotpathFrameTimingRecorder,
)


ROOT = Path(__file__).resolve().parents[2]


class D061LongSessionBenchmarkTests(unittest.TestCase):
    def test_hotpath_recorder_reports_nested_exclusive_time_and_residual(self):
        class Clock:
            value = 0
            def __call__(self): return self.value
        clock = Clock()
        recorder = _HotpathFrameTimingRecorder(clock)
        clock.value += 2
        with recorder.segment("session_propose"):
            clock.value += 3
            with recorder.segment("fixed_route_verifier"):
                clock.value += 5
            clock.value += 7
        clock.value += 11
        result = recorder.finish()
        self.assertEqual(result["production_prepare_ms"], 28 / 1e6)
        self.assertEqual(
            result["production_segments_inclusive_ms"],
            {"fixed_route_verifier": 5 / 1e6, "session_propose": 15 / 1e6},
        )
        self.assertEqual(
            result["production_segments_exclusive_ms"],
            {"fixed_route_verifier": 5 / 1e6, "session_propose": 10 / 1e6},
        )
        self.assertEqual(result["production_unattributed_ms"], 13 / 1e6)

    def test_frame_recorder_directly_counts_runtime_shell_around_backend_without_overlap(self):
        class Clock:
            value = 0

            def __call__(self):
                return self.value

        def sample(backend_ns: int):
            clock = Clock()
            recorder = _FrameTimingRecorder(clock)
            recorder.start_frame()
            recorder.start_production("follow_update")
            clock.value += 5
            recorder.finish_production("follow_update")
            recorder.start_production("runtime_control_frame")
            clock.value += 3
            recorder.start_backend()
            clock.value += backend_ns
            recorder.finish_backend()
            clock.value += 2
            recorder.finish_production("runtime_control_frame")
            clock.value += 10
            return recorder.finish_frame(following_revision=True)

        first = sample(100)
        delayed = sample(200)
        self.assertEqual(first.production_ns, 10)
        self.assertEqual(first.simulation_backend_ns, 100)
        self.assertEqual(first.full_harness_ns, 120)
        self.assertEqual(
            first.production_segments,
            (("follow_update", 5), ("runtime_control_frame", 5)),
        )
        self.assertTrue(first.following_revision)
        self.assertEqual(delayed.production_ns, first.production_ns)
        self.assertEqual(
            delayed.simulation_backend_ns - first.simulation_backend_ns,
            100,
        )
        self.assertEqual(delayed.full_harness_ns - first.full_harness_ns, 100)
        self.assertLessEqual(
            first.production_ns + first.simulation_backend_ns,
            first.full_harness_ns,
        )

    def test_three_timing_layers_keep_every_gate_and_do_not_charge_backend_to_production(self):
        healthy = _timing_gates(
            production_ns=[5_000_000, 6_000_000],
            simulation_backend_ns=[35_000_000, 40_000_000],
            full_harness_ns=[45_000_000, 49_000_000],
            revision_ordinals=(1,),
        )
        self.assertEqual(set(healthy), {
            "production_prepare_p95",
            "production_prepare_p99",
            "production_prepare_maximum",
            "following_revision_production_p95",
            "following_revision_production_p99",
            "following_revision_production_maximum",
            "full_harness_maximum",
        })
        self.assertTrue(all(healthy.values()))

        only_p95_would_hide_tail = _timing_gates(
            production_ns=[7_000_000] * 96 + [16_000_000] * 4,
            simulation_backend_ns=[1] * 100,
            full_harness_ns=[20_000_000] * 100,
            revision_ordinals=(0,),
        )
        self.assertTrue(only_p95_would_hide_tail["production_prepare_p95"])
        self.assertFalse(only_p95_would_hide_tail["production_prepare_p99"])

        hidden_maximum = _timing_gates(
            production_ns=[5_000_000, 31_000_000],
            simulation_backend_ns=[1, 1],
            full_harness_ns=[32_000_000, 32_000_000],
            revision_ordinals=(0,),
        )
        self.assertFalse(hidden_maximum["production_prepare_maximum"])

        backend_mixed_into_production = _timing_gates(
            production_ns=[40_000_000],
            simulation_backend_ns=[35_000_000],
            full_harness_ns=[45_000_000],
            revision_ordinals=(0,),
        )
        self.assertFalse(backend_mixed_into_production[
            "production_prepare_maximum"
        ])

        missing_full_harness_gate = _timing_gates(
            production_ns=[5_000_000],
            simulation_backend_ns=[1],
            full_harness_ns=[51_000_000],
            revision_ordinals=(0,),
        )
        self.assertFalse(missing_full_harness_gate["full_harness_maximum"])

    def test_control_period_slack_uses_the_same_wall_duration(self):
        cases = (
            (40_000_000, 10_000_000, False),
            (50_000_000, 0, True),
            (55_000_000, -5_000_000, True),
        )
        for duration_ns, slack_ns, missed in cases:
            with self.subTest(duration_ns=duration_ns):
                self.assertEqual(
                    _control_period_outcome(duration_ns),
                    (slack_ns, missed),
                )

    def test_gen2_coverage_requires_retained_control_event_and_followup(self):
        cases = (
            (
                "limit_without_gen2",
                dict(
                    completed_control_frames=8292,
                    warmup_control_frames=100,
                    maximum_retained=8192,
                    post_gen2_frames=128,
                    first_gen2_completed_frames_before_event=None,
                    first_gen2_followup_start_control_ordinal=None,
                ),
                (True, False, False, False),
            ),
            (
                "late_gen2_without_followup",
                dict(
                    completed_control_frames=8292,
                    warmup_control_frames=100,
                    maximum_retained=8192,
                    post_gen2_frames=128,
                    first_gen2_completed_frames_before_event=8280,
                    first_gen2_followup_start_control_ordinal=8281,
                ),
                (True, True, False, False),
            ),
            (
                "retained_gen2_with_followup",
                dict(
                    completed_control_frames=4300,
                    warmup_control_frames=100,
                    maximum_retained=8192,
                    post_gen2_frames=128,
                    first_gen2_completed_frames_before_event=4100,
                    first_gen2_followup_start_control_ordinal=4101,
                ),
                (False, True, True, True),
            ),
            (
                "warmup_gen2_does_not_count",
                dict(
                    completed_control_frames=8292,
                    warmup_control_frames=100,
                    maximum_retained=8192,
                    post_gen2_frames=128,
                    first_gen2_completed_frames_before_event=99,
                    first_gen2_followup_start_control_ordinal=100,
                ),
                (True, False, False, False),
            ),
        )
        for name, arguments, expected in cases:
            with self.subTest(name=name):
                result = _gen2_coverage(**arguments)
                self.assertEqual(
                    (
                        result["sample_limit_reached"],
                        result["retained_gen2_observed"],
                        result["post_gen2_complete"],
                        result["passed"],
                    ),
                    expected,
                )

    def test_short_child_process_writes_complete_evidence_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="d061-long-session-") as root:
            output = Path(root) / "evidence"
            command = [
                sys.executable,
                "-m",
                "scripts.benchmark_d061_long_session",
                "--output",
                str(output),
                "--test-mode",
                "--warmup",
                "1",
                "--minimum-retained",
                "8",
                "--maximum-retained",
                "16",
                "--post-gen2-samples",
                "2",
            ]
            recorded_command = subprocess.list2cmdline([
                r"D:\Miniforge3\Scripts\conda.exe",
                "run",
                "--prefix",
                r"D:\My_project\mc_ai\.venv",
                "--no-capture-output",
                "python",
                *command[1:],
            ])
            environment = os.environ.copy()
            environment["MC2P_D061_RECORDED_COMMAND"] = recorded_command
            completed = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True,
                timeout=120, env=environment,
            )
            self.assertIn(completed.returncode, {0, 1}, completed.stderr)

            payload = json.loads(
                (output / "performance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload["schema_version"],
                "mc2p.d061-long-session-performance.v5",
            )
            self.assertTrue(payload["configuration"]["test_mode"])
            self.assertEqual(
                payload["source"]["hash_basis"],
                "windows_worktree_bytes",
            )
            self.assertEqual(
                payload["environment"]["hash_basis"],
                "windows_worktree_bytes",
            )
            self.assertIsInstance(
                payload["environment"]["control_thread_id"], int,
            )
            self.assertEqual(payload["configuration"]["trace_capacity"], 2048)
            self.assertEqual(
                payload["configuration"]["trace_projection_sample_capacity"],
                4096,
            )
            prepare = payload["complete_prepare"]
            control = payload["formal_control_path"]
            layers = payload["timing_layers"]
            self.assertEqual(set(layers), {
                "production_prepare_ms",
                "simulation_backend_ms",
                "full_harness_ms",
                "following_revision_frames",
            })
            self.assertIn("measurement_boundary", layers["production_prepare_ms"])
            self.assertIn("measurement_boundary", layers["simulation_backend_ms"])
            self.assertIn("measurement_boundary", layers["full_harness_ms"])
            self.assertNotIn("derived_by_subtraction", json.dumps(layers))
            frame_samples = payload["retained_frame_samples"]
            self.assertGreaterEqual(len(frame_samples), 8)
            self.assertEqual(len(frame_samples), layers["full_harness_ms"]["samples"])
            for ordinal, sample in enumerate(frame_samples):
                self.assertEqual(sample["retained_ordinal"], ordinal)
                self.assertGreaterEqual(sample["production_prepare_ns"], 0)
                self.assertGreaterEqual(sample["simulation_backend_ns"], 0)
                self.assertGreaterEqual(sample["full_harness_ns"], 0)
                self.assertIs(type(sample["following_revision"]), bool)
                self.assertEqual(
                    sample["production_prepare_ns"],
                    sum(item["duration_ns"]
                        for item in sample["production_segments_ns"]),
                )
            self.assertTrue(any(
                item["name"] == "runtime_control_frame"
                for sample in frame_samples
                for item in sample["production_segments_ns"]
            ))
            self.assertEqual(prepare["warmup_samples"], 1)
            self.assertGreaterEqual(prepare["retained_samples"], 8)
            self.assertLessEqual(prepare["retained_samples"], 16)
            self.assertEqual(
                len(prepare["raw_samples_ns"]), prepare["retained_samples"],
            )
            self.assertFalse(prepare["gc_diagnostic"]["overflowed"])
            self.assertEqual(control["warmup_frames"], 1)
            captured = control["captured"]
            retained = control["retained"]
            self.assertEqual(captured["frames"], retained["frames"] + 1)
            self.assertGreaterEqual(retained["frames"], 8)
            self.assertLessEqual(retained["frames"], 16)
            self.assertEqual(
                len(retained["raw_durations_ns"]), retained["frames"],
            )
            self.assertEqual(
                len(retained["raw_control_period_slack_ns"]),
                retained["frames"],
            )
            self.assertEqual(
                retained["raw_control_period_slack_ns"],
                [50_000_000 - value
                 for value in retained["raw_durations_ns"]],
            )
            self.assertEqual(
                payload["gates"]["full_harness_maximum"],
                layers["full_harness_ms"]["statistics"]["maximum_ms"] < 50.0,
            )
            for required_gate in (
                "production_prepare_p95",
                "production_prepare_p99",
                "production_prepare_maximum",
                "following_revision_production_p95",
                "following_revision_production_p99",
                "following_revision_production_maximum",
                "full_harness_maximum",
            ):
                self.assertIn(required_gate, payload["gates"])
            self.assertEqual(
                payload["gates"]["input_deadline_miss"],
                retained["deadline_miss_count"] == 0,
            )
            self.assertEqual(
                payload["gates"]["minimum_deadline_slack"],
                retained["minimum_slack_ns"] > 0,
            )
            self.assertEqual(
                completed.returncode, 0 if payload["passed"] else 1,
            )
            self.assertEqual(
                payload["gates"]["gen2_coverage"],
                control["gen2_coverage"]["passed"],
            )
            self.assertEqual(
                (output / "COMMAND.txt").read_text(encoding="utf-8").strip(),
                recorded_command,
            )
            self.assertTrue(recorded_command.startswith(
                "D:\\Miniforge3\\Scripts\\conda.exe run --prefix "
                "D:\\My_project\\mc_ai\\.venv --no-capture-output python "
            ))
            gc_diagnostic = prepare["gc_diagnostic"]
            self.assertGreaterEqual(gc_diagnostic["capacity"], 65536)
            self.assertFalse(gc_diagnostic["start_stop_mismatch"])
            self.assertFalse(gc_diagnostic["unmatched_start"])
            self.assertIn("0", gc_diagnostic["generation_counts"])
            self.assertEqual(
                sum(gc_diagnostic["generation_counts"].values()),
                gc_diagnostic["event_count"],
            )
            first_retained = gc_diagnostic.get(
                "first_retained_production_gen2"
            )
            if first_retained is not None:
                self.assertEqual(
                    first_retained["measurement_phase"], "retained",
                )
                self.assertIn(first_retained["origin"], {
                    "control_path", "trace_thread",
                })
            for event in gc_diagnostic["events"]:
                self.assertIn(event["measurement_phase"], {
                    "before_samples", "warmup", "retained",
                })
                self.assertIn(event["control_path_phase"], {
                    "active", "outside",
                })
                self.assertIn("active_control_ordinal", event)
                self.assertIn("active_prepare_ordinal", event)
                self.assertIn("last_completed_prepare_ordinal", event)
                self.assertIsInstance(event["thread_id"], int)
            self.assertEqual(payload["trace"]["capacity"], 2048)
            self.assertEqual(
                payload["trace"]["projection_sample_capacity"], 4096,
            )
            self.assertEqual(payload["trace"]["dropped_records"], 0)
            self.assertFalse(payload["trace"]["worker_failed"])
            self.assertGreater(payload["trace"]["verified_records"], 0)
            self.assertTrue(payload["identity"]["consistent"])
            self.assertEqual(payload["identity"]["violations"], [])
            self.assertEqual(payload["pacing"]["period_ns"], 50_000_000)
            self.assertGreater(payload["pacing"]["paced_ticks"], 0)
            self.assertTrue(payload["behavior"]["source_released"])
            self.assertEqual(
                payload["behavior"]["terminal_session_state"], "cancelled",
            )

            self.assertTrue((output / "trace" / "complete.json").is_file())
            self.assertTrue((output / "trace" / "manifest.jsonl").is_file())
            sums = {}
            for line in (output / "SHA256SUMS").read_text(
                    encoding="utf-8").splitlines():
                digest, name = line.split("  ", 1)
                sums[name] = digest
            expected_files = sorted(
                path for path in output.rglob("*")
                if path.is_file() and path.name != "SHA256SUMS"
            )
            self.assertEqual(set(sums), {
                path.relative_to(output).as_posix() for path in expected_files
            })
            for path in expected_files:
                self.assertEqual(
                    sums[path.relative_to(output).as_posix()],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )

            refused = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, timeout=30,
                env=environment,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("refusing to overwrite", refused.stderr)

    def test_source_does_not_force_or_disable_gc(self):
        source = (ROOT / "scripts/benchmark_d061_long_session.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("gc.collect", source)
        self.assertNotIn("gc.disable", source)
        self.assertNotIn("gc.set_threshold", source)
        self.assertNotIn("_RecordingTrace", source)


if __name__ == "__main__":
    unittest.main()
