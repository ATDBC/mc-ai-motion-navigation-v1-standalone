"""D118-0 freezes calculator facts and the calibration harness contract."""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import closing
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.evidence.physics_validation import (
    declared_fixture_validation_world,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_types import (
    CalculationStatus,
    JAVA_1_21_RULESET,
    PhysicsState,
    TickInput,
)
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.segmented_trace import SegmentedJsonlWriter
from mc2p.motion_nav.world_model import WorldKnowledge, WorldSessionId
from scripts import d118_momentum_calibration_runtime as calibration
from scripts import d118_momentum_calibration as calibration_runner
from scripts import probe_fabric_deployment_observation as deployment
from tests.sim.backend import CalculatorBackend, Scene
from tests.test_player_runtime import _RecordingTrace


ROOT = Path(__file__).resolve().parents[2]


def _physics_state_json(state: PhysicsState) -> dict:
    return {
        "position": dict(zip(("x", "y", "z"), state.position)),
        "velocity": dict(zip(
            ("x", "y", "z"), state.velocity_blocks_per_tick,
        )),
        "yaw": math.degrees(state.yaw_radians),
        "pitch": math.degrees(state.pitch_radians),
        "on_ground": state.on_ground,
        "horizontal_collision": state.horizontal_collision,
        "vertical_collision": state.vertical_collision,
        "actual_sprinting": state.sprinting,
        "actual_sneaking": state.sneaking,
        "pose": state.pose,
    }


def _valid_physics_replay_fixture():
    command = calibration.FixedCommand(
        0, 0, False, False, False, "ground", "hold",
    )
    trial = calibration.CalibrationTrial(
        "physics-schema-south-0", "physics_schema", 0, 0,
        calibration.TimingKind.INPUT_STREAM_START,
        (0.5, 64.0, 0.5), 0, 4, 0, (command, command),
    )
    manifest = calibration.CalibrationManifest(
        "mc2p.d118-calibration-manifest.v1", (trial,), "fixture-only",
    )
    fixture_rows = calibration.trial_fixture_rows(
        trial, origin=(0, 0), feet_y=64,
    )
    with tempfile.TemporaryDirectory() as directory:
        fixture_path = Path(directory) / "fixture.jsonl"
        fixture_path.write_text(
            "".join(json.dumps(row) + "\n" for row in fixture_rows),
            encoding="utf-8",
        )
        world = declared_fixture_validation_world(
            fixture_path, JAVA_1_21_RULESET,
        )
    state = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id,
        JAVA_1_21_RULESET.state_schema,
        world.session,
        0,
        (0.5, 64.0, 0.5),
        (0.0, -0.0784000015258789, 0.0),
        0.0, 0.0, "standing", 0.6, 1.8,
        True, False, True, False, False, 0, 0.0,
        0.1, 0.6, 0.08, 0.42, 20, 5.0, "survival", (),
        False, False, False, False, False, False, False,
    )
    receipts, physics_rows = [], []
    contact_names = {
        "left_ground", "landed", "horizontal_collision", "vertical_collision",
    }
    for index, fixed in enumerate(trial.commands):
        movement = asdict(fixed.movement())
        movement_tick = 101 + index
        receipts.append(calibration.runtime_receipt_row(
            trial=trial, command_index=index, episode_id="episode",
            request_sequence_id=11 + index,
            observation_sequence_id=21 + index,
            generation_id=31 + index,
            movement_tick_id=movement_tick,
            expected_movement=movement, applied_movement=movement,
        ))
        calculated = physics_step(
            state, TickInput(
                float(fixed.forward), float(fixed.strafe), fixed.jump,
                fixed.sneak, fixed.sprint, 0.0,
            ), world,
            JAVA_1_21_RULESET,
        )
        assert calculated.status is CalculationStatus.OK
        assert calculated.next_state is not None
        row = calibration.physics_sidecar_row(
            episode_id="episode", request_sequence_id=11 + index,
            movement_tick_id=movement_tick, actual_input=movement,
        )
        row.update(
            movement_yaw=0.0,
            pre_state=_physics_state_json(state),
            post_state=_physics_state_json(calculated.next_state),
            contact_events=[
                event for event in calculated.events if event in contact_names
            ],
        )
        physics_rows.append(row)
        state = calculated.next_state
    return manifest, tuple(receipts), tuple(physics_rows), fixture_rows


class D118MomentumCalibrationTests(unittest.TestCase):
    def test_frozen_manifest_separates_four_families_and_three_timing_kinds(self):
        manifest = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        )
        self.assertEqual(manifest.schema_version, "mc2p.d118-calibration-manifest.v1")
        self.assertEqual(
            {trial.family for trial in manifest.trials},
            {
                "four_gap_sprint_jump",
                "landing_immediate_jump",
                "last_grounded_edge_jump",
                "momentum_landing_fallback",
            },
        )
        self.assertEqual(
            {trial.timing_kind for trial in manifest.trials},
            {
                calibration.TimingKind.INPUT_STREAM_START,
                calibration.TimingKind.MID_STREAM_DEVIATION,
            },
        )
        self.assertNotIn(calibration.TimingKind.WORKER_DELIVERY, {
            trial.timing_kind for trial in manifest.trials
        })
        self.assertEqual(manifest.sha256, calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        ).sha256)
        self.assertEqual(len(manifest.trials), 7)
        self.assertEqual(sum(len(trial.commands) for trial in manifest.trials), 273)
        self.assertEqual(
            manifest.sha256,
            "23379a353a87e914c8d61490ee58f866a4ceadb69740c97b371c1a2dc8a83b6f",
        )
        full = calibration.build_calibration_manifest(
            directions=(0, 1, 2, 3), repetitions=3,
        )
        self.assertEqual(len(full.trials), 84)
        self.assertEqual(sum(len(trial.commands) for trial in full.trials), 3276)
        self.assertEqual(
            full.sha256,
            "4b6866f13abb92ff3f4fa3315d6e763620e85d42043387744ea5ac526c833c95",
        )

    def test_direction_transform_rotates_geometry_without_changing_inputs(self):
        manifest = calibration.build_calibration_manifest(
            directions=(0, 1, 2, 3), repetitions=1,
        )
        variants = [trial for trial in manifest.trials
                    if trial.family == "four_gap_sprint_jump"]
        self.assertEqual(len(variants), 4)
        self.assertTrue(all(
            trial.commands == variants[0].commands for trial in variants
        ))
        origins = [calibration._local_to_world(
            (10, 20), trial.direction_index, 0.5, 6.5,
        ) for trial in variants]
        self.assertEqual(origins, [
            (10.5, 26.5), (16.5, 19.5), (9.5, 13.5), (3.5, 20.5),
        ])

    def test_manifest_hash_detects_any_runtime_input_change(self):
        manifest = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        )
        trial = manifest.trials[0]
        changed = replace(
            trial,
            commands=trial.commands[:-1] + (
                replace(trial.commands[-1], sneak=not trial.commands[-1].sneak),
            ),
        )
        self.assertNotEqual(
            manifest.sha256,
            calibration.manifest_sha256(replace(
                manifest, trials=(changed, *manifest.trials[1:]), sha256="",
            )),
        )

    def test_tick_zero_fixture_gate_rejects_unknown_world_facts(self):
        trial = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        ).trials[0]
        world = WorldKnowledge(WorldSessionId("unknown-d118")).view()
        frame = SimpleNamespace(
            world=world,
            body=SimpleNamespace(
                position=(0.5, 64.0, -4.0),
                body_box=SimpleNamespace(max_y=65.8),
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "tick-zero fixture fact"):
            calibration._assert_observed_fixture(frame, trial, (0, 0), 64)

    def test_fixture_log_row_binds_each_geometry_phase_to_its_trial(self):
        row = calibration.fixture_command_row(
            trial_id="four-gap-south-0", phase="support",
            origin=(10, 20), feet_y=64,
            material="minecraft:grass_block", positions=((1, 63, 2),),
        )
        self.assertEqual(row, {
            "schema_version": "mc2p.d118-fixture-command.v1",
            "trial_id": "four-gap-south-0",
            "phase": "support",
            "origin": [10, 20],
            "feet_y": 64,
            "material": "minecraft:grass_block",
            "positions": [[1, 63, 2]],
        })
        for changed in (
            dict(trial_id="", phase="support", material="minecraft:grass_block"),
            dict(trial_id="four-gap-south-0", phase="other",
                 material="minecraft:grass_block"),
        ):
            with self.assertRaises(ValueError):
                calibration.fixture_command_row(
                    origin=(10, 20), feet_y=64,
                    positions=((1, 63, 2),), **changed,
                )

    def test_timeline_join_rejects_duplicate_or_cross_request_sidecar_rows(self):
        trial = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        ).trials[0]
        receipt = calibration.runtime_receipt_row(
            trial=trial,
            command_index=0,
            episode_id="fabric-21001-0",
            request_sequence_id=41,
            observation_sequence_id=7,
            generation_id=8,
            movement_tick_id=12,
            expected_movement={
                "forward": 1.0, "strafe": 0.0, "jump": False,
                "sneak": False, "sprint": True,
            },
            applied_movement={
                "forward": 1.0, "strafe": 0.0, "jump": False,
                "sneak": False, "sprint": True,
            },
        )
        physics = calibration.physics_sidecar_row(
            episode_id="fabric-21001-0",
            request_sequence_id=41,
            movement_tick_id=12,
        )
        ftick = calibration.ftick_sidecar_rows(
            episode_id="fabric-21001-0",
            request_sequence_id=41,
            observation_sequence_id=7,
            generation_id=8,
            movement_tick_id=12,
        )
        self.assertIsNone(ftick[0]["movement_tick_id"])
        self.assertIsNone(ftick[1]["observation_sequence_id"])
        self.assertEqual(ftick[-1]["generation_id"], 8)
        joined = calibration.join_command_timeline(receipt, (physics,), ftick)
        self.assertEqual(joined["request_sequence_id"], 41)
        self.assertEqual(joined["movement_tick_id"], 12)
        self.assertEqual(joined["timing_kind"], trial.timing_kind.value)
        with self.assertRaisesRegex(ValueError, "exactly one physics"):
            calibration.join_command_timeline(receipt, (physics, physics), ftick)
        wrong = dict(physics, request_sequence_id=99)
        with self.assertRaisesRegex(ValueError, "exactly one physics"):
            calibration.join_command_timeline(receipt, (wrong,), ftick)
        changed_input = dict(physics, actual_input={
            **physics["actual_input"], "jump": True,
        })
        with self.assertRaisesRegex(ValueError, "input differs at jump"):
            calibration.join_command_timeline(receipt, (changed_input,), ftick)

    def test_timeline_join_accepts_only_neutral_nonleased_tail_ticks(self):
        trial = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        ).trials[0]
        movement = {
            "forward": 1.0, "strafe": 0.0, "jump": False,
            "sneak": False, "sprint": True,
        }
        receipt = calibration.runtime_receipt_row(
            trial=trial,
            command_index=0,
            episode_id="fabric-21001-0",
            request_sequence_id=41,
            observation_sequence_id=7,
            generation_id=8,
            movement_tick_id=12,
            expected_movement=movement,
            applied_movement=movement,
        )
        command_physics = calibration.physics_sidecar_row(
            episode_id="fabric-21001-0",
            request_sequence_id=41,
            movement_tick_id=12,
        )
        ftick = list(calibration.ftick_sidecar_rows(
            episode_id="fabric-21001-0",
            request_sequence_id=41,
            observation_sequence_id=7,
            generation_id=8,
            movement_tick_id=12,
        ))
        neutral = {
            "forward": 0.0, "strafe": 0.0, "jump": False,
            "sneak": False, "sprint": False,
        }
        tails = []
        event_sequence = 5
        for offset in range(40):
            movement_tick = 13 + offset
            tail = calibration.physics_sidecar_row(
                episode_id="fabric-21001-0",
                request_sequence_id=41,
                movement_tick_id=movement_tick,
                actual_input=neutral,
            )
            tail["input_state"] = (
                "lease_exhausted" if offset < 14 else "expired"
            )
            tails.append(tail)
            for kind in ("INPUT_SAMPLED", "MOVEMENT_APPLIED"):
                ftick.append({
                    "schema_version": "mc2p.client-ftick-event.v1",
                    "event_sequence": event_sequence,
                    "kind": kind,
                    "episode_id": "fabric-21001-0",
                    "generation_id": None,
                    "observation_sequence_id": None,
                    "request_sequence_id": 41,
                    "movement_tick_id": movement_tick,
                    "monotonic_ns": event_sequence * 10,
                })
                event_sequence += 1

        joined = calibration.join_command_timeline(
            receipt, (command_physics, *tails), tuple(ftick),
        )
        self.assertEqual(joined["tail_application_count"], 40)
        self.assertEqual(joined["tail_first_movement_tick_id"], 13)
        self.assertEqual(joined["tail_last_movement_tick_id"], 52)

        leased_tail = dict(tails[0], input_state="leased")
        with self.assertRaisesRegex(ValueError, "tail application is leased"):
            calibration.join_command_timeline(
                receipt, (command_physics, leased_tail, *tails[1:]),
                tuple(ftick),
            )
        nonneutral_tail = dict(tails[0], actual_input={
            **neutral, "forward": 1.0,
        })
        with self.assertRaisesRegex(ValueError, "tail input is not neutral"):
            calibration.join_command_timeline(
                receipt, (command_physics, nonneutral_tail, *tails[1:]),
                tuple(ftick),
            )

    def test_frozen_manifest_rejects_jointly_mutated_command_evidence(self):
        manifest = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        )
        trial = manifest.trials[0]
        movement = asdict(trial.commands[0].movement())
        receipt = calibration.runtime_receipt_row(
            trial=trial, command_index=0, episode_id="episode",
            request_sequence_id=1, observation_sequence_id=2,
            generation_id=3, movement_tick_id=4,
            expected_movement=movement, applied_movement=movement,
        )
        receipt.update(
            stream_id=trial.commands[0].stream_id,
            phase=trial.commands[0].phase,
        )
        calibration_runner.validate_receipts_against_manifest(
            replace(manifest, trials=(replace(
                trial, commands=(trial.commands[0],),
            ),)),
            (receipt,),
        )
        jointly_changed = {
            **receipt,
            "expected_movement": {**movement, "jump": True},
            "applied_movement": {**movement, "jump": True},
        }
        with self.assertRaisesRegex(ValueError, "frozen command"):
            calibration_runner.validate_receipts_against_manifest(
                replace(manifest, trials=(replace(
                    trial, commands=(trial.commands[0],),
                ),)),
                (jointly_changed,),
            )
        extra_key = {
            **receipt,
            "expected_movement": {**movement, "undeclared": False},
        }
        with self.assertRaisesRegex(ValueError, "key set"):
            calibration_runner.validate_receipts_against_manifest(
                replace(manifest, trials=(replace(
                    trial, commands=(trial.commands[0],),
                ),)),
                (extra_key,),
            )

    def test_manifest_document_hash_is_recomputed_before_frozen_lookup(self):
        manifest = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        )
        document = {
            **calibration._manifest_payload(manifest),
            "sha256": manifest.sha256,
        }
        parsed = calibration_runner.validate_manifest_document(document)
        self.assertEqual(parsed.sha256, manifest.sha256)
        changed = json.loads(json.dumps(document))
        changed["trials"][0]["commands"][0]["jump"] = True
        with self.assertRaisesRegex(ValueError, "content hash"):
            calibration_runner.validate_manifest_document(changed)

    def test_wrapper_reads_and_joins_real_sidecar_shapes_before_recording(self):
        manifest = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        )
        receipts, physics_rows, ftick_rows = [], [], []
        sequence = 40
        for trial in manifest.trials:
            for command_index, command in enumerate(trial.commands):
                sequence += 1
                movement = asdict(command.movement())
                receipt = calibration.runtime_receipt_row(
                    trial=trial, command_index=command_index,
                    episode_id="fabric-21001-0",
                    request_sequence_id=sequence,
                    observation_sequence_id=sequence + 1000,
                    generation_id=sequence + 1001,
                    movement_tick_id=sequence + 2000,
                    expected_movement=movement, applied_movement=movement,
                )
                receipt.update(stream_id=command.stream_id, phase=command.phase)
                receipts.append(receipt)
                physics_rows.append(calibration.physics_sidecar_row(
                    episode_id="fabric-21001-0",
                    request_sequence_id=sequence,
                    movement_tick_id=sequence + 2000,
                    actual_input=movement,
                ))
                ftick_rows.extend(calibration.ftick_sidecar_rows(
                    episode_id="fabric-21001-0",
                    request_sequence_id=sequence,
                    observation_sequence_id=sequence + 1000,
                    generation_id=sequence + 1001,
                    movement_tick_id=sequence + 2000,
                ))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            client = source / "client-0"
            client.mkdir(parents=True)
            (source / "result.json").write_text(
                json.dumps({"status": "passed"}), encoding="utf-8",
            )
            (client / "d118-input-manifest.json").write_text(
                json.dumps({
                    **calibration._manifest_payload(manifest),
                    "sha256": manifest.sha256,
                }), encoding="utf-8",
            )
            (client / "d118-calibration-runtime.json").write_text(
                json.dumps({
                    "status": "recorded", "manifest_sha256": manifest.sha256,
                    "trial_count": len(manifest.trials),
                    "command_count": len(receipts),
                }),
                encoding="utf-8",
            )
            (client / "d118-runtime-receipts.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in receipts),
                encoding="utf-8",
            )
            for name in (
                "d118-trials.jsonl", "d118-fixture-commands.jsonl",
                "d118-teleports.jsonl",
            ):
                (client / name).write_text("{}\n", encoding="utf-8")
            with closing(SegmentedJsonlWriter(
                    client / "physics-tick-events")) as writer:
                for row in physics_rows:
                    writer.write(row)
            with closing(SegmentedJsonlWriter(
                    client / "ftick-events")) as writer:
                for row in ftick_rows:
                    writer.write(row)
                writer.write({
                    "schema_version": "mc2p.client-ftick-summary.v1",
                    "kind": "SUMMARY", "event_count": len(ftick_rows),
                    "dropped_event_count": 0,
                })

            recorded = calibration_runner.seal_run(
                root / "recorded", source, command=["probe"], return_code=0,
                stdout="", stderr="",
            )
            self.assertEqual(recorded["status"], "unverified")
            self.assertEqual(recorded["capture_status"], "captured")
            self.assertEqual(recorded["acceptance_status"], "unverified")
            self.assertEqual(recorded["timeline_identity_status"], "passed")
            self.assertEqual(recorded["physics_replay_status"], "unverified")
            self.assertEqual(recorded["stage_status"], "in_progress")
            self.assertEqual(recorded["joined_command_count"], 273)
            replay = json.loads(
                (root / "recorded" / "physics-replay.json").read_text("utf-8")
            )
            self.assertEqual(replay["status"], "unverified")
            self.assertIn("fixture_identity", replay["issues"][0])
            self.assertEqual(calibration_runner.exit_code_for_summary(recorded), 3)
            for name in (
                "client-0/d118-trials.jsonl",
                "client-0/d118-fixture-commands.jsonl",
                "client-0/d118-teleports.jsonl",
                "client-0/physics-tick-events/manifest.jsonl",
                "client-0/physics-tick-events/segment-00000000.jsonl",
                "client-0/ftick-events/manifest.jsonl",
                "client-0/ftick-events/segment-00000000.jsonl",
            ):
                self.assertIn(name, recorded["raw_files"])

            jointly_mutated_receipts = list(receipts)
            jointly_mutated_physics = list(physics_rows)
            changed_movement = {
                **jointly_mutated_receipts[0]["expected_movement"],
                "jump": True,
            }
            jointly_mutated_receipts[0] = {
                **jointly_mutated_receipts[0],
                "expected_movement": changed_movement,
                "applied_movement": changed_movement,
            }
            jointly_mutated_physics[0] = {
                **jointly_mutated_physics[0],
                "actual_input": changed_movement,
            }
            joint_client = root / "jointly-mutated-source" / "client-0"
            joint_client.mkdir(parents=True)
            (root / "jointly-mutated-source" / "result.json").write_bytes(
                (source / "result.json").read_bytes()
            )
            for name in (
                "d118-input-manifest.json", "d118-calibration-runtime.json",
                "d118-trials.jsonl", "d118-fixture-commands.jsonl",
                "d118-teleports.jsonl",
            ):
                (joint_client / name).write_bytes((client / name).read_bytes())
            (joint_client / "d118-runtime-receipts.jsonl").write_text(
                "".join(json.dumps(row) + "\n"
                        for row in jointly_mutated_receipts),
                encoding="utf-8",
            )
            with closing(SegmentedJsonlWriter(
                    joint_client / "physics-tick-events")) as writer:
                for row in jointly_mutated_physics:
                    writer.write(row)
            with closing(SegmentedJsonlWriter(
                    joint_client / "ftick-events")) as writer:
                for row in ftick_rows:
                    writer.write(row)
                writer.write({
                    "schema_version": "mc2p.client-ftick-summary.v1",
                    "kind": "SUMMARY", "event_count": len(ftick_rows),
                    "dropped_event_count": 0,
                })
            joint_result = calibration_runner.seal_run(
                root / "jointly-mutated", root / "jointly-mutated-source",
                command=["probe"], return_code=0, stdout="", stderr="",
            )
            self.assertEqual(joint_result["capture_status"], "failed")
            self.assertIn(
                "frozen command", joint_result["timeline_identity_error"],
            )

            bad_rows = list(ftick_rows)
            bad_rows[2] = dict(bad_rows[2], movement_tick_id=13)
            bad_client = root / "bad-source" / "client-0"
            bad_client.mkdir(parents=True)
            for name in ("result.json",):
                (root / "bad-source" / name).write_bytes((source / name).read_bytes())
            for name in (
                "d118-input-manifest.json", "d118-calibration-runtime.json",
                "d118-runtime-receipts.jsonl", "d118-trials.jsonl",
                "d118-fixture-commands.jsonl", "d118-teleports.jsonl",
            ):
                (bad_client / name).write_bytes((client / name).read_bytes())
            with closing(SegmentedJsonlWriter(
                    bad_client / "physics-tick-events")) as writer:
                for row in physics_rows:
                    writer.write(row)
            with closing(SegmentedJsonlWriter(
                    bad_client / "ftick-events")) as writer:
                for row in bad_rows:
                    writer.write(row)
                writer.write({
                    "schema_version": "mc2p.client-ftick-summary.v1",
                    "kind": "SUMMARY", "event_count": len(bad_rows),
                    "dropped_event_count": 0,
                })
            rejected = calibration_runner.seal_run(
                root / "rejected", root / "bad-source", command=["probe"],
                return_code=0, stdout="", stderr="",
            )
            self.assertEqual(rejected["status"], "failed")
            self.assertEqual(rejected["capture_status"], "failed")
            self.assertEqual(rejected["acceptance_status"], "failed")
            self.assertEqual(calibration_runner.exit_code_for_summary(rejected), 2)
            self.assertEqual(rejected["timeline_identity_status"], "failed")

            (source / "result.json").write_text(
                json.dumps({"status": "failed"}), encoding="utf-8",
            )
            replayed_failure = calibration_runner.seal_run(
                root / "source-failed", source,
                command=["probe"], return_code=0, stdout="", stderr="",
            )
            self.assertEqual(replayed_failure["status"], "failed")
            self.assertEqual(replayed_failure["source_result_status"], "failed")

    def test_physics_replay_uses_a_separate_declared_world_per_trial(self):
        manifest, receipts, physics_rows, fixture_rows = (
            _valid_physics_replay_fixture()
        )
        second = replace(
            manifest.trials[0], trial_id="physics-schema-south-1",
        )
        worlds = calibration_runner.declared_trial_fixture_worlds(
            replace(manifest, trials=(manifest.trials[0], second)),
            (*fixture_rows,
             {**fixture_rows[0], "trial_id": second.trial_id},
             {**fixture_rows[1], "trial_id": second.trial_id}),
        )
        self.assertEqual(set(worlds), {
            manifest.trials[0].trial_id, second.trial_id,
        })
        self.assertIsNotNone(worlds[manifest.trials[0].trial_id].cell(
            (0, 63, 0),
        ).block)
        self.assertIsNotNone(worlds[second.trial_id].cell((0, 63, 0)).block)
        self.assertIsNot(
            worlds[manifest.trials[0].trial_id], worlds[second.trial_id],
        )

        changed = [dict(row) for row in fixture_rows]
        changed[1] = {
            **changed[1], "positions": changed[1]["positions"][:-1],
        }
        with self.assertRaisesRegex(ValueError, "frozen trial"):
            calibration_runner.declared_trial_fixture_worlds(
                manifest, changed,
            )

    def test_physics_replay_checks_numeric_and_all_discrete_fields(self):
        manifest, receipts, physics_rows, fixture_rows = (
            _valid_physics_replay_fixture()
        )
        report = calibration_runner.analyze_physics_replay(
            manifest, receipts, physics_rows, fixture_rows,
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["trial_count"], 1)
        self.assertEqual(report["tick_count"], 2)
        self.assertEqual(report["position_error_max_by_axis"], [0.0, 0.0, 0.0])
        self.assertEqual(report["velocity_error_max_by_axis"], [0.0, 0.0, 0.0])
        self.assertEqual(report["yaw_error_max_degrees"], 0.0)
        self.assertEqual(report["pitch_error_max_degrees"], 0.0)
        self.assertEqual(report["discrete_mismatch_count"], 0)
        self.assertEqual(report["entry_margin"]["status"], "unverified")
        self.assertEqual(
            report["entry_margin"]["reason"],
            "no_sound_entry_envelope_is_frozen",
        )

        mutations = []
        sneaking = [dict(row) for row in physics_rows]
        sneaking[0] = dict(sneaking[0], post_state={
            **sneaking[0]["post_state"], "actual_sneaking": True,
        })
        mutations.append(("actual_sneaking", sneaking))
        position = [dict(row) for row in physics_rows]
        position[0] = dict(position[0], post_state={
            **position[0]["post_state"],
            "position": {**position[0]["post_state"]["position"], "z": 0.51},
        })
        mutations.append(("position_z", position))
        contacts = [dict(row) for row in physics_rows]
        contacts[0] = dict(contacts[0], contact_events=[])
        mutations.append(("contact_events", contacts))
        ground = [dict(row) for row in physics_rows]
        ground[0] = dict(ground[0], post_state={
            **ground[0]["post_state"], "on_ground": False,
        })
        mutations.append(("on_ground", ground))
        for field, value in (
            ("horizontal_collision", True),
            ("vertical_collision", False),
            ("actual_sprinting", True),
            ("pose", "crouching"),
        ):
            changed = [dict(row) for row in physics_rows]
            changed[0] = dict(changed[0], post_state={
                **changed[0]["post_state"], field: value,
            })
            mutations.append((field, changed))
        for name, changed in mutations:
            with self.subTest(name=name):
                changed_report = calibration_runner.analyze_physics_replay(
                    manifest, receipts, tuple(changed), fixture_rows,
                )
                self.assertEqual(changed_report["status"], "failed")
                self.assertTrue(changed_report["issues"])

    def test_physics_replay_marks_missing_trial_fixture_identity_unverified(self):
        manifest, receipts, physics_rows, fixture_rows = (
            _valid_physics_replay_fixture()
        )
        incomplete = tuple(
            {key: value for key, value in row.items() if key != "trial_id"}
            for row in fixture_rows
        )
        report = calibration_runner.analyze_physics_replay(
            manifest, receipts, physics_rows, incomplete,
        )
        self.assertEqual(report["status"], "unverified")
        self.assertIn("fixture_identity", report["issues"][0])

        missing_tick = calibration_runner.analyze_physics_replay(
            manifest, receipts, physics_rows[:-1], fixture_rows,
        )
        self.assertEqual(missing_tick["status"], "unverified")
        self.assertTrue(any(
            "physics_identity" in issue for issue in missing_tick["issues"]
        ))

    def test_all_south_trials_replay_in_declared_world_and_freeze_fallbacks(self):
        manifest = calibration.build_calibration_manifest(
            directions=(0,), repetitions=1,
        )
        origin = (0, 0)
        feet_y = 64
        fixture_rows = tuple(
            row
            for trial in manifest.trials
            for row in calibration.trial_fixture_rows(
                trial, origin=origin, feet_y=feet_y,
            )
        )
        contexts = calibration_runner.declared_trial_fixture_contexts(
            manifest, fixture_rows,
        )
        receipts = []
        physics_rows = []
        request = 100
        contact_names = {
            "left_ground", "landed", "horizontal_collision",
            "vertical_collision",
        }
        for trial in manifest.trials:
            context = contexts[trial.trial_id]
            x, z = calibration._local_to_world(
                origin, trial.direction_index,
                trial.start_local[0], trial.start_local[2],
            )
            yaw = math.radians(calibration._DIRECTIONS[
                trial.direction_index
            ][2])
            state = PhysicsState(
                JAVA_1_21_RULESET.ruleset_id,
                JAVA_1_21_RULESET.state_schema,
                context.world.session, 0,
                (x, float(feet_y), z),
                (0.0, -0.0784000015258789, 0.0),
                yaw, 0.0, "standing", 0.6, 1.8,
                True, False, True, False, False, 0, 0.0,
                0.1, 0.6, 0.08, 0.42, 20, 5.0, "survival", (),
                False, False, False, False, False, False, False,
            )
            for index, fixed in enumerate(trial.commands):
                request += 1
                movement = asdict(fixed.movement())
                tick_input = TickInput(
                    float(fixed.forward), float(fixed.strafe), fixed.jump,
                    fixed.sneak, fixed.sprint, yaw,
                )
                calculated = physics_step(
                    state, tick_input, context.world, JAVA_1_21_RULESET,
                )
                self.assertIs(
                    calculated.status, CalculationStatus.OK,
                    (trial.trial_id, index, calculated.status,
                     calculated.missing_cells),
                )
                self.assertIsNotNone(calculated.next_state)
                receipt = calibration.runtime_receipt_row(
                    trial=trial, command_index=index, episode_id="episode",
                    request_sequence_id=request,
                    observation_sequence_id=request + 10_000,
                    generation_id=request + 20_000,
                    movement_tick_id=request + 30_000,
                    expected_movement=movement, applied_movement=movement,
                )
                receipt.update(stream_id=fixed.stream_id, phase=fixed.phase)
                receipts.append(receipt)
                row = calibration.physics_sidecar_row(
                    episode_id="episode", request_sequence_id=request,
                    movement_tick_id=request + 30_000,
                    actual_input=movement,
                )
                row.update(
                    movement_yaw=math.degrees(yaw),
                    pre_state=_physics_state_json(state),
                    post_state=_physics_state_json(calculated.next_state),
                    contact_events=[
                        event for event in calculated.events
                        if event in contact_names
                    ],
                )
                physics_rows.append(row)
                state = calculated.next_state
        report = calibration_runner.analyze_physics_replay(
            manifest, receipts, physics_rows, fixture_rows,
        )
        self.assertEqual(report["status"], "passed", report["issues"])
        self.assertEqual(report["trial_count"], 7)
        fallback = {
            row["fallback_margin"]["fallback"]: row["fallback_margin"]
            for row in report["trials"]
            if row["fallback_margin"]["status"] == "measured"
        }
        self.assertFalse(fallback["neutral"]["safe"])
        self.assertFalse(fallback["back"]["safe"])
        self.assertTrue(fallback["sneak"]["safe"])
        self.assertGreater(
            fallback["sneak"]["minimum_platform_overlap_margin_blocks"], 0.0,
        )

        sneak_trial = next(
            trial for trial in manifest.trials if trial.fallback == "sneak"
        )
        sneak_receipt_index = next(
            index for index, receipt in enumerate(receipts)
            if receipt["trial_id"] == sneak_trial.trial_id
            and receipt["expected_movement"]["sneak"]
        )
        sneak_request = receipts[sneak_receipt_index]["request_sequence_id"]
        sneak_physics_index = next(
            index for index, row in enumerate(physics_rows)
            if row["request_sequence_id"] == sneak_request
        )

        late_receipts = [dict(row) for row in receipts]
        late_physics = [dict(row) for row in physics_rows]
        late_tick = late_receipts[sneak_receipt_index]["movement_tick_id"] + 1
        late_receipts[sneak_receipt_index] = {
            **late_receipts[sneak_receipt_index], "movement_tick_id": late_tick,
        }
        late_physics[sneak_physics_index] = {
            **late_physics[sneak_physics_index], "movement_tick_id": late_tick,
        }
        late_report = calibration_runner.analyze_physics_replay(
            manifest, late_receipts, late_physics, fixture_rows,
        )
        self.assertIn(
            f"{sneak_trial.trial_id}:sneak_fallback_application_outside_window",
            late_report["issues"],
        )

        no_landing = [dict(row) for row in physics_rows]
        landing_index = next(
            index for index, row in enumerate(no_landing)
            if row["request_sequence_id"] in {
                receipt["request_sequence_id"] for receipt in receipts
                if receipt["trial_id"] == sneak_trial.trial_id
            } and "landed" in row["contact_events"]
        )
        no_landing[landing_index] = {
            **no_landing[landing_index],
            "contact_events": [
                event for event in no_landing[landing_index]["contact_events"]
                if event != "landed"
            ],
        }
        no_landing_report = calibration_runner.analyze_physics_replay(
            manifest, receipts, no_landing, fixture_rows,
        )
        self.assertIn(
            f"{sneak_trial.trial_id}:sneak_fallback_landing_count",
            no_landing_report["issues"],
        )

        no_sneak = [dict(row) for row in physics_rows]
        sneak_requests = {
            receipt["request_sequence_id"] for receipt in receipts
            if receipt["trial_id"] == sneak_trial.trial_id
            and receipt["expected_movement"]["sneak"]
        }
        for index, row in enumerate(no_sneak):
            if row["request_sequence_id"] in sneak_requests:
                no_sneak[index] = {
                    **row,
                    "actual_input": {**row["actual_input"], "sneak": False},
                }
        no_sneak_report = calibration_runner.analyze_physics_replay(
            manifest, receipts, no_sneak, fixture_rows,
        )
        self.assertIn(
            f"{sneak_trial.trial_id}:sneak_fallback_application_missing",
            no_sneak_report["issues"],
        )

    def test_event_timing_uses_authoritative_landing_and_actual_input_tick(self):
        commands = (
            calibration.FixedCommand(1, 0, False, False, True,
                                     "jump-1", "landing"),
            calibration.FixedCommand(1, 0, True, False, True,
                                     "jump-2", "takeoff"),
            calibration.FixedCommand(0, 0, False, True, False,
                                     "fallback", "fallback_sneak"),
        )
        trial = calibration.CalibrationTrial(
            "timing-south-0", "timing", 0, 0,
            calibration.TimingKind.INPUT_STREAM_START,
            (0.5, 64.0, 0.5), 1, 1, 4, commands, fallback="sneak",
        )
        rows = (
            {"movement_tick_id": 20, "contact_events": ["landed"],
             "actual_input": {"sneak": False}},
            {"movement_tick_id": 21, "contact_events": ["left_ground"],
             "actual_input": {"sneak": False}},
            {"movement_tick_id": 22, "contact_events": [],
             "actual_input": {"sneak": True}},
        )
        timing = calibration_runner._event_timing(trial, rows)
        self.assertEqual(timing["first_landing_tick"], 20)
        self.assertEqual(timing["second_jump_applied_tick"], 21)
        self.assertEqual(timing["second_jump_relative_to_landing"], 1)
        self.assertEqual(timing["sneak_applied_tick"], 22)
        self.assertEqual(timing["sneak_relative_to_landing"], 2)

    def test_sneak_fallback_requires_one_landing_and_application_by_l_plus_one(self):
        trial = calibration.CalibrationTrial(
            "fallback-sneak-south-0", "momentum_landing_fallback", 0, 0,
            calibration.TimingKind.MID_STREAM_DEVIATION,
            (0.5, 64.0, -5.17), 1, 1, 4,
            (calibration.FixedCommand(
                0, 0, False, True, False, "fallback", "fallback_sneak",
            ),), fallback="sneak",
        )
        accepted = {
            "landing_ticks": [20], "sneak_applied_tick": 21,
            "sneak_relative_to_landing": 1,
        }
        self.assertEqual(
            calibration_runner._fallback_timing_failures(trial, accepted), (),
        )
        mutations = (
            ({**accepted, "landing_ticks": []}, "landing_count"),
            ({**accepted, "landing_ticks": [20, 21]}, "landing_count"),
            ({**accepted, "sneak_applied_tick": None,
              "sneak_relative_to_landing": None}, "application_missing"),
            ({**accepted, "sneak_applied_tick": 22,
              "sneak_relative_to_landing": 2}, "application_outside_window"),
        )
        for timing, expected in mutations:
            with self.subTest(expected=expected):
                self.assertIn(
                    expected,
                    calibration_runner._fallback_timing_failures(trial, timing),
                )

    def test_committed_runtime_final_bytes_compile_and_import(self):
        runtime_path = ROOT / "scripts/d118_momentum_calibration_runtime.py"
        compile_result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(runtime_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(
            compile_result.returncode, 0,
            compile_result.stdout + compile_result.stderr,
        )
        import_result = subprocess.run(
            [sys.executable, "-c",
             "import scripts.d118_momentum_calibration_runtime"],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(
            import_result.returncode, 0,
            import_result.stdout + import_result.stderr,
        )

    def test_calculator_replays_frozen_timing_and_fallback_claims(self):
        facts = calibration.calculate_frozen_facts()
        self.assertEqual(facts["standstill_four_gap"], {
            "feasible": 79,
            "input_stream_start_late_one_tolerant": 0,
        })
        self.assertEqual(facts["momentum_four_gap"], {
            "landings": 1528,
            "on_time": 530,
            "input_stream_start_late_one_tolerant": 530,
        })
        separation = facts["entry_window_separation"]
        self.assertEqual(
            separation["legacy_exit_maximum_blocks_per_second"], 0.15,
        )
        self.assertGreater(
            separation["momentum_entry_minimum_blocks_per_second"], 4.5,
        )
        self.assertLess(
            separation["momentum_entry_maximum_blocks_per_second"], 5.3,
        )
        self.assertFalse(separation["overlaps"])
        self.assertEqual(facts["fallback"], {
            "eligible_landings": 601,
            "neutral_l1_safe": 0,
            "back_l1_safe": 0,
            "sneak_l1_safe": 601,
            "sneak_l2_safe": 158,
        })

    def test_deployment_cli_requires_both_sidecars_and_is_mutually_exclusive(self):
        script = ROOT / "scripts/probe_fabric_deployment_observation.py"
        common = [sys.executable, str(script), "--d118-motion-calibration-probe"]
        for missing in (
            (),
            ("--time-diagnostics", "--physics-tick-diagnostics"),
            ("--time-diagnostics", "--ftick-diagnostics"),
        ):
            result = subprocess.run(
                [*common, *missing], capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("D118 calibration requires", result.stderr)
            self.assertNotIn("FABRIC_DEPLOYMENT_RUN_DIR", result.stdout)
        result = subprocess.run(
            [*common, "--time-diagnostics", "--physics-tick-diagnostics",
             "--ftick-diagnostics", "--b09-air-motion-probe"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr)
        result = subprocess.run(
            [sys.executable, str(script), "--d118-full-matrix"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --d118-motion-calibration-probe", result.stderr)

    def test_deployment_freeze_selects_d118_runtime_and_sidecars(self):
        sources = deployment.frozen_deployment_sources(
            b03_fixed_route_probe=False,
            d118_motion_calibration_probe=True,
        )
        for path in (
            "scripts/d118_momentum_calibration_runtime.py",
            "scripts/b10_gap_solver_runtime.py",
            "scripts/probe_fabric_deployment_observation.py",
            "config/motion-navigation/air-motions-b09-v1.json",
            "mc2p/runtime/player_runtime_v1.py",
            "mc2p/backends/fabric_behavior.py",
            "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java",
            "mc2p/backends/runtime_overlays/mc121_structured/BehaviorPlayerMixin.java",
            "mc2p/backends/runtime_overlays/mc121_diagnostics/ClientPhysicsTickDiagnostics.java",
            "mc2p/backends/runtime_overlays/mc121_diagnostics/ClientFtickDiagnostics.java",
        ):
            self.assertIn(path, sources)

    def test_d118_deployment_is_a_single_client_session_probe(self):
        self.assertTrue(deployment._uses_single_client_session({
            "d118_motion_calibration",
        }))
        self.assertFalse(deployment._uses_single_client_session({
            "b03_fixed_route",
        }))
        self.assertTrue(deployment._captures_close_diagnostics(
            {"d118_motion_calibration"}, time_diagnostics=True,
        ))
        self.assertFalse(deployment._captures_close_diagnostics(
            {"d118_motion_calibration"}, time_diagnostics=False,
        ))

    def test_d118_diagnostic_capture_writes_sync_pipeline_row(self):
        observation = SimpleNamespace(
            episode_id="d118-episode",
            sequence_id=4,
            perception=None,
        )
        runtime = SimpleNamespace(
            observation=observation,
            last_navigation_ingest_ns=17,
        )
        backend = SimpleNamespace(
            last_diagnostics={"schema_version": "diagnostics-test"},
            last_observation_timing={
                "transport_payload_bytes": 23,
                "json_decode_ns": 29,
                "typed_decode_ns": 31,
                "snapshot_build_ns": 37,
            },
        )
        rows = []
        with tempfile.TemporaryDirectory() as directory:
            row = deployment._record_observation_diagnostic(
                rows, Path(directory), runtime, backend,
            )
            written = [
                json.loads(line) for line in
                (Path(directory) / "diagnostics.jsonl").read_text(
                    encoding="utf-8",
                ).splitlines()
            ]
        self.assertEqual(rows, [row])
        self.assertEqual(written, [row])
        self.assertEqual(row["observation_sequence_id"], 4)
        self.assertEqual(
            row["observation_pipeline"]["navigation_ingest_ns"], 17,
        )

    def test_d118_close_diagnostic_adds_only_the_close_observation(self):
        backend = SimpleNamespace(last_diagnostics={"client_tick": 19})
        rows = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = deployment._runtime_trace(
                root, backend, capture_close_diagnostics=True,
            )
            for sequence, kind in ((0, "reset"), (1, "step")):
                observation = SimpleNamespace(
                    episode_id="d118-episode", sequence_id=sequence,
                    perception=None,
                )
                runtime = SimpleNamespace(
                    observation=observation,
                    last_navigation_ingest_ns=0,
                )
                payload = (
                    {"result": {"observation": {
                        "episode_id": "d118-episode",
                        "sequence_id": sequence,
                    }}}
                    if kind == "reset"
                    else {"backend_result": {"observation": {
                        "episode_id": "d118-episode",
                        "sequence_id": sequence,
                    }}}
                )
                trace.write(kind, payload)
                deployment._record_observation_diagnostic(
                    rows, root, runtime, backend,
                )
            trace.write("close_release", {
                "backend_result": {"observation": {
                    "episode_id": "d118-episode", "sequence_id": 2,
                }},
            })
            trace.close()
            diagnostics = [
                json.loads(line) for line in
                (root / "diagnostics.jsonl").read_text(
                    encoding="utf-8",
                ).splitlines()
            ]
        self.assertEqual(
            [row["observation_sequence_id"] for row in diagnostics],
            [0, 1, 2],
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(
            "observation_pipeline" in row for row in diagnostics[:2]
        ))
        self.assertNotIn("observation_pipeline", diagnostics[2])

    def test_smoke_runtime_sends_every_fixed_tick_through_formal_runtime(self):
        clock = [100_000_000]
        volume = ((0, 36), (20, 70), (0, 52))
        backend = CalculatorBackend(
            clock,
            Scene({(0, 63, 0): "minecraft:stone"}, volume),
            (0.5, 64.0, 0.5),
        )
        runtime = PlayerRuntimeV1(
            backend, _RecordingTrace(), clock_ns=lambda: clock[0],
        )
        deadline = clock[0] + 120_000_000_000
        reset = runtime.reset(ResetRequestV0(
            "d118-reset", backend.episode, "d118-calibration", 1, deadline,
        ))
        self.assertTrue(reset.succeeded, reset.failure)

        fixture_events = []

        def fixture_writer(
                positions, material, *, trial_id, phase, origin, feet_y):
            fixture_events.append((trial_id, phase, material))
            solids = dict(backend.scene.solids)
            for position in positions:
                if material == "minecraft:air":
                    solids.pop(position, None)
                else:
                    solids[position] = material
            backend.scene = Scene(solids, volume)
            backend._build_truth()
            backend.state = replace(backend.state, session=backend._truth_session)

        def teleport(x, y, z, yaw, _pitch):
            backend.state = replace(
                backend.state,
                position=(x, y, z),
                velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
                yaw_radians=math.radians(yaw),
                pitch_radians=0.0,
                on_ground=False,
                vertical_collision=False,
                sprinting=False,
                sneaking=False,
                jumping_cooldown_ticks=0,
                fall_distance_blocks=0.0,
            )

        with tempfile.TemporaryDirectory() as directory:
            manifest = calibration.build_calibration_manifest(
                directions=(0,), repetitions=1,
            )
            def capture_observation_diagnostic():
                return {
                    "observation_sequence_id": runtime.observation.sequence_id,
                    "observation_pipeline": {"schema_version": "test"},
                }

            summary, rows, checks = calibration.run_d118_momentum_calibration_runtime(
                runtime, backend, backend.episode, Path(directory), deadline,
                fixture_writer, teleport, smoke=True, manifest=manifest,
                clock_ns=lambda: clock[0],
                observation_diagnostic=capture_observation_diagnostic,
            )
            self.assertEqual(summary["trial_count"], 7)
            self.assertEqual(summary["trial_count"], len(manifest.trials))
            receipt_rows = [
                json.loads(line) for line in
                (Path(directory) / "d118-runtime-receipts.jsonl").read_text(
                    encoding="utf-8",
                ).splitlines()
            ]
            self.assertEqual(
                summary["command_count"],
                sum(len(trial.commands) for trial in manifest.trials),
            )
            self.assertEqual(len(receipt_rows), summary["command_count"])
            self.assertTrue(all(check["passed"] for check in checks))
            self.assertEqual(len(fixture_events), 14)
            self.assertTrue(all(
                fixture_events[index:index + 2] == [
                    (manifest.trials[index // 2].trial_id,
                     "clear", "minecraft:air"),
                    (manifest.trials[index // 2].trial_id,
                     "support", "minecraft:grass_block"),
                ]
                for index in range(0, len(fixture_events), 2)
            ))
            self.assertEqual(len({
                row["request_sequence_id"] for row in receipt_rows
            }), len(receipt_rows))
            self.assertEqual(
                [row["observation_sequence_id"] for row in rows],
                list(range(runtime.observation.sequence_id + 1)),
            )
            self.assertTrue(all(
                "observation_pipeline" in row for row in rows
            ))
        runtime.close()

    def test_setup_advances_tick_and_bounds_fixture_before_first_teleport(self):
        class StopAfterFirstTeleport(Exception):
            pass

        class TicklessResetBackend(CalculatorBackend):
            @staticmethod
            def _boundary_observation(observation, *, movement_tick):
                own = observation.self_state.value
                position = replace(own.position, y=-60.0)
                own = replace(
                    own, position=position, movement_tick_id=movement_tick,
                    hurt_animation_ticks=(
                        None if movement_tick is None
                        else own.hurt_animation_ticks
                    ),
                )
                return replace(
                    observation,
                    position=replace(
                        observation.position, value=position,
                    ),
                    self_state=replace(observation.self_state, value=own),
                )

            def reset(self, request):
                result = super().reset(request)
                return replace(
                    result,
                    observation=self._boundary_observation(
                        result.observation, movement_tick=None,
                    ),
                )

            def step(self, action, deadline, *, observation_request=None):
                result = super().step(
                    action, deadline, observation_request=observation_request,
                )
                return replace(
                    result,
                    observation=self._boundary_observation(
                        result.observation,
                        movement_tick=(
                            result.observation.self_state.value.movement_tick_id
                        ),
                    ),
                )

        clock = [100_000_000]
        volume = ((0, 36), (20, 70), (0, 52))
        backend = TicklessResetBackend(
            clock,
            Scene({(0, 63, 0): "minecraft:stone"}, volume),
            (0.5, 64.0, 0.5),
        )
        runtime = PlayerRuntimeV1(
            backend, _RecordingTrace(), clock_ns=lambda: clock[0],
        )
        deadline = clock[0] + 120_000_000_000
        reset = runtime.reset(ResetRequestV0(
            "d118-floor-reset", backend.episode, "d118-calibration", 1,
            deadline,
        ))
        self.assertTrue(reset.succeeded, reset.failure)
        self.assertEqual(
            runtime.observation.self_state.value.position.y, -60.0,
        )
        self.assertIsNone(
            runtime.observation.self_state.value.movement_tick_id,
        )
        fixture_calls = []
        teleport_ticks = []

        def fixture_writer(
                positions, material, *, trial_id, phase, origin, feet_y):
            fixture_calls.append((positions, material, feet_y))

        def teleport(_x, _y, _z, _yaw, _pitch):
            teleport_ticks.append(
                runtime.observation.self_state.value.movement_tick_id,
            )
            raise StopAfterFirstTeleport

        try:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(StopAfterFirstTeleport):
                    calibration.run_d118_momentum_calibration_runtime(
                        runtime, backend, backend.episode, Path(directory),
                        deadline, fixture_writer, teleport,
                        clock_ns=lambda: clock[0],
                    )
            self.assertTrue(teleport_ticks)
            self.assertIsInstance(teleport_ticks[0], int)
            self.assertGreaterEqual(teleport_ticks[0], 0)
            self.assertEqual(len(fixture_calls), 2)
            self.assertTrue(all(call[2] == 0 for call in fixture_calls))
            self.assertGreaterEqual(
                min(position[1]
                    for call in fixture_calls for position in call[0]),
                -24,
            )
        finally:
            runtime.close()

    def test_windows_wrapper_builds_the_preregistered_smoke_command(self):
        command = calibration_runner.fabric_probe_command(
            server_port=25597, ipc_port=8140,
        )
        self.assertEqual(command[0], sys.executable)
        self.assertIn("--d118-motion-calibration-probe", command)
        self.assertIn("--time-diagnostics", command)
        self.assertIn("--physics-tick-diagnostics", command)
        self.assertIn("--ftick-diagnostics", command)
        self.assertEqual(command[command.index("--seed") + 1], "21001")
        self.assertEqual(command[command.index("--timeout-seconds") + 1], "600")
        self.assertNotIn("--d118-full-matrix", command)
        full = calibration_runner.fabric_probe_command(
            server_port=25597, ipc_port=8140, full_matrix=True,
        )
        self.assertIn("--d118-full-matrix", full)

    def test_windows_wrapper_can_start_as_a_script(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/d118_momentum_calibration.py"),
             "--help"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output", result.stdout)


if __name__ == "__main__":
    unittest.main()
