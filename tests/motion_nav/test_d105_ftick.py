from __future__ import annotations

import unittest
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from scripts.d105_ftick import (
    _ordered_source_release, _run_probe, analyze_run, classify_tick_trace,
    _source_release_is_accepted, fabric_probe_command, summarize_ftick,
    write_evidence,
)


def _trace(*, request: int = 7, observation: int = 40,
           movement_tick: int = 101) -> tuple[dict, ...]:
    return (
        {
            "record_type": "step",
            "payload": {
                "decision": {"action": {
                    "episode_id": "fabric-21001-0",
                    "observation_sequence_id": observation - 1,
                    "request_sequence_id": request - 1,
                }},
                "backend_result": {
                    "observation": {
                        "episode_id": "fabric-21001-0",
                        "sequence_id": observation,
                        "received_at_monotonic_ns": 9_000_000_000,
                    },
                    "receipt": {
                        "episode_id": "fabric-21001-0",
                        "generation_id": observation,
                        "request_sequence_id": request - 1,
                        "input_applications": [],
                    },
                },
            },
        },
        {
            "record_type": "dispatch",
            "payload": {"decision": {"action": {
                "episode_id": "fabric-21001-0",
                "observation_sequence_id": observation,
                "request_sequence_id": request,
                "movement": {
                    "forward": 1, "strafe": 0, "jump": True,
                    "sneak": False, "sprint": True,
                },
            }}},
        },
        {
            "record_type": "step",
            "payload": {
                "decision": {"action": {
                    "episode_id": "fabric-21001-0",
                    "observation_sequence_id": observation,
                    "request_sequence_id": request,
                }},
                "backend_result": {
                    "observation": {
                        "episode_id": "fabric-21001-0",
                        "sequence_id": observation + 1,
                        "received_at_monotonic_ns": 9_100_000_000,
                    },
                    "receipt": {
                        "episode_id": "fabric-21001-0",
                        "generation_id": observation + 1,
                        "request_sequence_id": request,
                        "dropped_input_samples": 0,
                        "input_applications": [{
                            "episode_id": "fabric-21001-0",
                            "request_sequence_id": request,
                            "movement_tick_id": movement_tick,
                            "state": "leased",
                            "forward": 1.0,
                            "strafe": 0.0,
                            "jump": True,
                            "sneak": False,
                            "sprint": True,
                        }],
                    },
                },
            },
        },
    )


def _client(*, request: int = 7, observation: int = 40,
            movement_tick: int = 101) -> tuple[dict, ...]:
    common = {
        "schema_version": "mc2p.client-ftick-event.v1",
        "episode_id": "fabric-21001-0",
        "observation_sequence_id": observation,
        "request_sequence_id": request,
    }
    return (
        {
            **common,
            "event_sequence": 20,
            "kind": "CLIENT_FRAME_RECEIVED",
            "generation_id": None,
            "movement_tick_id": None,
            "monotonic_ns": 10,
        },
        {
            **common,
            "event_sequence": 21,
            "kind": "INPUT_SAMPLED",
            "generation_id": None,
            "movement_tick_id": movement_tick,
            "monotonic_ns": 20,
        },
        {
            **common,
            "event_sequence": 22,
            "kind": "MOVEMENT_APPLIED",
            "generation_id": None,
            "movement_tick_id": movement_tick,
            "monotonic_ns": 30,
        },
        {
            **common,
            "event_sequence": 23,
            "kind": "CLIENT_OBSERVATION_SENT",
            "generation_id": observation + 1,
            "movement_tick_id": movement_tick,
            "monotonic_ns": 40,
        },
    )


def _trial(*, direction: int = 0, repetition: int = 0,
           observation: int = 40, movement_tick: int = 100) -> dict:
    return {
        "name": f"validation-{direction}-{repetition}",
        "group": "validation",
        "direction_index": direction,
        "repetition": repetition,
        "start_observation_sequence_id": observation,
        "anchor_movement_tick_id": movement_tick,
        "landed": True,
        "horizontal_collisions": 0,
        "jump_pulses": 1,
        "horizontal_error_blocks": 0.05,
        "level_error_blocks": 0.0,
    }


class D105FTickTests(unittest.TestCase):
    def test_probe_output_decode_cannot_hide_a_completed_run_marker(self):
        command = [
            sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(b'\\x8e\\nFABRIC_DEPLOYMENT_RUN_DIR=run-1\\n')",
        ]

        result = _run_probe(command, Path.cwd())

        self.assertEqual(result.returncode, 0)
        self.assertIn("FABRIC_DEPLOYMENT_RUN_DIR=run-1", result.stdout)

    def test_script_entrypoint_can_load_repository_modules(self):
        script = Path(__file__).resolve().parents[2] / "scripts/d105_ftick.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_run_loader_marks_every_timeline_incomplete_when_sidecar_is_unsealed(self):
        with TemporaryDirectory(prefix="d105-ftick-unsealed-") as temporary:
            root = Path(temporary)
            client = root / "client-0"
            client.mkdir()
            trials = []
            trace = []
            for direction in range(4):
                for repetition in range(10):
                    observation = 1000 + direction * 100 + repetition * 2
                    request = 2000 + direction * 100 + repetition
                    movement_tick = 3000 + direction * 100 + repetition * 2
                    trials.append(_trial(
                        direction=direction, repetition=repetition,
                        observation=observation, movement_tick=movement_tick,
                    ))
                    trace.extend(_trace(
                        request=request, observation=observation,
                        movement_tick=movement_tick + 1,
                    ))
            (client / "b10-gap-solver-trials.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in trials), "utf-8",
            )
            (client / "trace.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in trace), "utf-8",
            )
            (root / "result.json").write_text(json.dumps({
                "seed": 21001, "status": "passed",
            }), "utf-8")

            summary = analyze_run(root)

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["classifications"]["INCOMPLETE"], 40)
        self.assertEqual(summary["sidecar_status"], "unsealed")

    def test_fabric_command_reuses_b10_path_with_fixed_seed_and_read_only_diagnostics(self):
        command = fabric_probe_command(server_port=25597, ipc_port=8140)

        self.assertEqual(command.count("--seed"), 1)
        self.assertEqual(command[command.index("--seed") + 1], "21001")
        self.assertIn("--b10-gap-solver-probe", command)
        self.assertIn("--time-diagnostics", command)
        self.assertIn("--physics-tick-diagnostics", command)
        self.assertIn("--ftick-diagnostics", command)

    def test_complete_trace_uses_ids_and_classifies_next_tick_across_unrelated_clocks(self):
        result = classify_tick_trace(
            _trial(), _trace(), _client(),
            sidecar_sealed=True, dropped_events=0,
        )

        self.assertEqual(result["classification"], "NEXT_MOVEMENT_TICK")
        self.assertEqual(result["request_sequence_id"], 7)
        self.assertEqual(result["generation_id"], 41)
        self.assertEqual(result["movement_tick_id"], 101)
        self.assertEqual(result["errors"], [])

    def test_later_movement_tick_is_complete_and_reported(self):
        result = classify_tick_trace(
            _trial(), _trace(movement_tick=103), _client(movement_tick=103),
            sidecar_sealed=True, dropped_events=0,
        )

        self.assertEqual(result["classification"], "LATER_MOVEMENT_TICK")
        self.assertEqual(result["movement_tick_offset"], 3)

    def test_missing_duplicate_wrong_identity_overflow_and_unsealed_are_incomplete(self):
        cases = {
            "missing": _client()[:-1],
            "duplicate": _client() + (_client()[0],),
            "wrong_identity": _client(request=8),
        }
        for label, client in cases.items():
            with self.subTest(label=label):
                result = classify_tick_trace(
                    _trial(), _trace(), client,
                    sidecar_sealed=True, dropped_events=0,
                )
                self.assertEqual(result["classification"], "INCOMPLETE")
                self.assertTrue(result["errors"])
        overflow = classify_tick_trace(
            _trial(), _trace(), _client(),
            sidecar_sealed=True, dropped_events=1,
        )
        unsealed = classify_tick_trace(
            _trial(), _trace(), _client(),
            sidecar_sealed=False, dropped_events=0,
        )
        self.assertEqual(overflow["classification"], "INCOMPLETE")
        self.assertIn("client_event_overflow", overflow["errors"])
        self.assertEqual(unsealed["classification"], "INCOMPLETE")
        self.assertIn("client_sidecar_unsealed", unsealed["errors"])

    def test_response_episode_and_sent_observation_identity_are_required(self):
        wrong_response = list(deepcopy(_trace()))
        wrong_response[-1]["payload"]["backend_result"]["observation"]["episode_id"] = "wrong"
        missing_sent_observation = list(deepcopy(_client()))
        missing_sent_observation[-1]["observation_sequence_id"] = None

        for label, trace, client in (
            ("response_episode", tuple(wrong_response), _client()),
            ("sent_observation", _trace(), tuple(missing_sent_observation)),
        ):
            with self.subTest(label=label):
                result = classify_tick_trace(
                    _trial(), trace, client,
                    sidecar_sealed=True, dropped_events=0,
                )
                self.assertEqual(result["classification"], "INCOMPLETE")
                self.assertTrue(result["errors"])

    def test_b10_ordered_source_must_be_explicitly_released(self):
        source = {
            "episode_id": "fabric-21001-0", "generation": 1,
            "scope_id": "scope", "slot": 0,
            "source_id": "ordered/scope/0/1",
        }
        registered = {
            "record_type": "ordered_source_registered",
            "payload": {"label": "b10-gap-solver", "source": source},
        }
        released = {
            "record_type": "ordered_source_unregistered",
            "payload": {"source": source, "removed_intent_ids": []},
        }

        self.assertEqual(_ordered_source_release((registered,)), "not_released")
        self.assertEqual(
            _ordered_source_release((registered, released)), "explicitly_released",
        )

    def test_native_source_lifecycle_distinguishes_close_fallback(self):
        self.assertEqual(
            _ordered_source_release((), {
                "b10_lifecycle": {"ordered_source_release": {
                    "status": "cleared_on_runtime_close",
                }},
            }),
            "cleared_on_close",
        )
        self.assertEqual(
            _ordered_source_release((), {
                "b10_lifecycle": {"ordered_source_release": {
                    "status": "not_released",
                }},
            }),
            "not_released",
        )

    def test_source_release_classification_rejects_registration_and_unknown_status(self):
        source = {
            "episode_id": "fabric-21001-0", "generation": 1,
            "scope_id": "scope", "slot": 0,
            "source_id": "ordered/scope/0/1",
        }
        registered = {
            "record_type": "ordered_source_registered",
            "payload": {"label": "b10-gap-solver", "source": source},
        }
        self.assertEqual(_ordered_source_release(()), "registration_invalid")
        self.assertEqual(
            _ordered_source_release((registered, registered)),
            "registration_invalid",
        )
        self.assertEqual(
            _ordered_source_release((), {
                "b10_lifecycle": {"ordered_source_release": {
                    "status": "future_status",
                }},
            }),
            "invalid_status",
        )
        self.assertFalse(_source_release_is_accepted("registration_invalid"))
        self.assertFalse(_source_release_is_accepted("not_released"))
        self.assertFalse(_source_release_is_accepted("invalid_status"))
        self.assertTrue(_source_release_is_accepted("explicitly_released"))
        self.assertTrue(_source_release_is_accepted("cleared_on_close"))

    def test_transitional_released_status_remains_compatible(self):
        self.assertEqual(
            _ordered_source_release((), {
                "b10_lifecycle": {"ordered_source_release": {
                    "status": "released",
                }},
            }),
            "explicitly_released",
        )

    def test_replay_index_records_replay_command_and_source_return_code(self):
        with TemporaryDirectory(prefix="d105-ftick-index-") as temporary:
            root = Path(temporary)
            output = root / "evidence"
            summary = {
                "status": "failed", "timelines": [],
                "source_run": str(root / "source"),
            }
            (root / "source").mkdir()
            write_evidence(
                output, summary,
                command=[sys.executable, "scripts/d105_ftick.py", "--run-dir", str(root / "source")],
                probe_return_code=2,
                evidence_mode="replay",
                source_probe_return_code=1,
            )
            index = json.loads((output / "index.json").read_text("utf-8"))

        self.assertEqual(index["evidence_mode"], "replay")
        self.assertIn("--run-dir", index["command"])
        self.assertEqual(index["analysis_return_code"], 2)
        self.assertEqual(index["source_probe_return_code"], 1)

    def test_summary_requires_four_directions_ten_complete_b10_trials(self):
        timelines = []
        for direction in range(4):
            for repetition in range(10):
                observation = 1000 + direction * 100 + repetition * 2
                request = 2000 + direction * 100 + repetition
                movement_tick = 3000 + direction * 100 + repetition * 2
                timelines.append(classify_tick_trace(
                    _trial(direction=direction, repetition=repetition,
                           observation=observation, movement_tick=movement_tick),
                    _trace(request=request, observation=observation,
                           movement_tick=movement_tick + 1),
                    _client(request=request, observation=observation,
                            movement_tick=movement_tick + 1),
                    sidecar_sealed=True, dropped_events=0,
                ))

        summary = summarize_ftick(tuple(timelines), seed=21001)

        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["timeline_count"], 40)
        self.assertEqual(summary["classifications"], {
            "NEXT_MOVEMENT_TICK": 40,
            "LATER_MOVEMENT_TICK": 0,
            "INCOMPLETE": 0,
        })
        self.assertEqual(summary["direction_counts"], {
            "south": 10, "east": 10, "north": 10, "west": 10,
        })

    def test_missing_trials_are_explicit_incomplete_timelines(self):
        one = classify_tick_trace(
            _trial(), _trace(), _client(),
            sidecar_sealed=True, dropped_events=0,
        )

        summary = summarize_ftick((one,), seed=21001)

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["timeline_count"], 40)
        self.assertEqual(summary["observed_timeline_count"], 1)
        self.assertEqual(summary["classifications"], {
            "NEXT_MOVEMENT_TICK": 1,
            "LATER_MOVEMENT_TICK": 0,
            "INCOMPLETE": 39,
        })
        self.assertEqual(
            sum(row["errors"] == ["trial_missing"] for row in summary["timelines"]),
            39,
        )


if __name__ == "__main__":
    unittest.main()
