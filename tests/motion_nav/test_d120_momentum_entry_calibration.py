"""Frozen D120 entry coverage: finite samples are never a sound envelope."""
from __future__ import annotations

import copy
from dataclasses import replace
import itertools
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mc2p.motion_nav.physics_types import CalculationStatus, StepResult
from mc2p.motion_nav.evidence.physics_validation import declared_fixture_validation_world
from scripts import d120_momentum_entry_calibration as calibration


class D120EntryCalibrationTests(unittest.TestCase):
    def test_manifest_freezes_full_cartesian_product_and_suffix_limits(self):
        manifest = calibration.build_manifest()
        self.assertEqual(manifest["axes"], [
            [-.002, 0., .002], [-.002, 0., .002],
            [-.0002, 0., .0002], [-.0002, 0., .0002], [-.1, 0., .1],
        ])
        self.assertEqual(manifest["offsets"], [list(v) for v in itertools.product(*manifest["axes"])])
        self.assertEqual(len(manifest["offsets"]), 243)
        self.assertEqual(manifest["suffix_limits"], {
            "landing-immediate": 42, "landing-immediate-late1": 42,
            "fallback-sneak": 30,
        })
        self.assertEqual(manifest["step_limits"], {"per_direction": 30000, "four_directions": 120000})
        self.assertEqual(manifest["sha256"], "89c5ee269da0b8ac969d69be781a589c8f28449faf0b153fe69a6b2c2a433dd6")
        calibration.validate_manifest(manifest)

    def test_manifest_rejects_changed_hash_and_reforged_shape(self):
        manifest = calibration.build_manifest()
        manifest["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "manifest"):
            calibration.validate_manifest(manifest)
        manifest = calibration.build_manifest()
        manifest["offsets"].pop()
        with self.assertRaisesRegex(ValueError, "manifest"):
            calibration.validate_manifest(manifest)

    def test_nominal_entry_is_actual_first_landed_post_state(self):
        fixture = calibration.build_fixture()
        entry = calibration.nominal_entry(fixture)
        self.assertTrue(entry.on_ground)
        self.assertEqual(entry.movement_tick_id, 26)
        self.assertAlmostEqual(entry.position[2], 2.05023666079027, places=5)
        self.assertGreater(entry.velocity_blocks_per_tick[2], .25)

    def test_post_state_cannot_be_replaced_by_pre_state(self):
        fixture = calibration.build_fixture()
        row = {
            "movement_tick_id": 26, "contact_events": ["landed"],
            "pre_state": {"on_ground": False},
            "post_state": {"on_ground": False},
        }
        with self.assertRaisesRegex(ValueError, "landed post-state"):
            calibration.landed_post_state(row, fixture.context.world)

    def test_valid_landed_post_state_preserves_tick_and_yaw_units(self):
        fixture = calibration.build_fixture(1)
        entry = calibration.nominal_entry(fixture)
        def projection(state):
            return {
                "position":dict(zip(("x","y","z"),state.position)),
                "velocity":dict(zip(("x","y","z"),state.velocity_blocks_per_tick)),
                "yaw":math.degrees(state.yaw_radians), "pitch":math.degrees(state.pitch_radians),
                "on_ground":state.on_ground, "horizontal_collision":state.horizontal_collision,
                "vertical_collision":state.vertical_collision, "actual_sprinting":state.sprinting,
                "actual_sneaking":state.sneaking, "pose":state.pose,
            }
        row = {"movement_tick_id":entry.movement_tick_id,"contact_events":["landed"],
               "pre_state":projection(replace(entry,on_ground=False)),"post_state":projection(entry)}
        actual = calibration.landed_post_state(row,fixture.context.world)
        self.assertEqual(actual.movement_tick_id,entry.movement_tick_id)
        self.assertEqual(actual.position,entry.position)
        self.assertEqual(actual.velocity_blocks_per_tick,entry.velocity_blocks_per_tick)
        self.assertEqual(actual.yaw_radians,entry.yaw_radians)
        row["post_state"] = row["pre_state"]
        with self.assertRaisesRegex(ValueError,"landed post-state"):
            calibration.landed_post_state(row,fixture.context.world)

    def test_offsets_are_applied_to_physics_in_local_axes(self):
        for direction in range(4):
            fixture = calibration.build_fixture(direction)
            entry = calibration.nominal_entry(fixture)
            shifted = calibration.perturb_entry(entry, (.002, -.002, .0002, -.0002, .1), direction)
            dx, dz = ((0, 1), (1, 0), (0, -1), (-1, 0))[direction]
            lateral = (dz, -dx)
            for axis, expected in ((0, .002*lateral[0]-.002*dx), (2, .002*lateral[1]-.002*dz)):
                self.assertAlmostEqual(shifted.position[axis]-entry.position[axis], expected)
            self.assertAlmostEqual(shifted.velocity_blocks_per_tick[0]-entry.velocity_blocks_per_tick[0], .0002*lateral[0]-.0002*dx)
            self.assertAlmostEqual(shifted.velocity_blocks_per_tick[2]-entry.velocity_blocks_per_tick[2], .0002*lateral[1]-.0002*dz)
            self.assertAlmostEqual(shifted.yaw_radians-entry.yaw_radians, math.radians(.1))
            self.assertEqual(shifted.position[1], entry.position[1])

    def test_nominal_suffixes_keep_l1_l2_distinct_and_fail_controls(self):
        fixture = calibration.build_fixture()
        entry = calibration.nominal_entry(fixture)
        for suffix, delay in (("landing-immediate", 1), ("landing-immediate-late1", 2), ("fallback-sneak", 1)):
            result = calibration.replay_suffix(entry, fixture, suffix)
            self.assertTrue(result["passed"], result)
            self.assertEqual(result["first_effect_relative_to_landing"], delay)
            self.assertEqual(result["first_effect_tick"], entry.movement_tick_id+delay)
            self.assertFalse(result["transport_late_injected"])
            self.assertEqual(result["damage"], 0.)
        for suffix in ("fallback-neutral", "fallback-back"):
            self.assertFalse(calibration.replay_suffix(entry, fixture, suffix)["passed"])

    def test_unknown_and_unsupported_never_pass(self):
        fixture = calibration.build_fixture()
        entry = calibration.nominal_entry(fixture)
        for result in (
            StepResult(CalculationStatus.NEEDS_WORLD, missing_cells=((0,64,2),)),
            StepResult(CalculationStatus.UNSUPPORTED, unsupported_reasons=("unmodeled_material",)),
        ):
            with patch.object(calibration, "physics_step", return_value=result):
                outcome = calibration.replay_suffix(entry, fixture, "landing-immediate")
            self.assertFalse(outcome["passed"])
            self.assertEqual(outcome["reason"], result.status.value)

    def test_actual_world_missing_target_support_and_unknown_never_pass(self):
        fixture = calibration.build_fixture()
        entry = calibration.nominal_entry(fixture)
        original_rows = calibration.frozen.trial_fixture_rows(
            fixture.trial, origin=fixture.context.origin, feet_y=fixture.context.feet_y)
        for mutation in ("unknown", "support_removed", "unsupported"):
            rows = copy.deepcopy(original_rows)
            target_cells = [p for p in rows[1]["positions"] if p[2] >= 6]
            rows[1]["positions"] = [p for p in rows[1]["positions"] if p[2] < 6]
            if mutation == "unknown":
                rows[0]["positions"] = [p for p in rows[0]["positions"] if p[2] < 6]
            elif mutation == "support_removed":
                rows[0]["positions"].extend(target_cells)
            else:
                rows = [*rows, {"material": "minecraft:honey_block", "positions": target_cells}]
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/"fixture.jsonl"
                path.write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf-8")
                world = declared_fixture_validation_world(path, calibration.JAVA_1_21_RULESET)
            changed = replace(fixture,context=replace(fixture.context,world=world))
            outcome = calibration.replay_suffix(entry,changed,"landing-immediate")
            self.assertFalse(outcome["passed"], (mutation,outcome))
            if mutation == "unknown":
                self.assertEqual(outcome["reason"], "needs_world")
            if mutation == "unsupported":
                self.assertEqual(outcome["reason"], "unsupported")

    def test_intermediate_support_loss_is_not_hidden_by_later_landing(self):
        fixture = calibration.build_fixture()
        entry = calibration.nominal_entry(fixture)
        real_step = calibration.physics_step
        def leave_platform(*args):
            result = real_step(*args)
            return replace(result, next_state=replace(result.next_state, on_ground=False))
        with patch.object(calibration, "physics_step", side_effect=leave_platform):
            outcome = calibration.replay_suffix(entry, fixture, "fallback-sneak")
        self.assertFalse(outcome["passed"])
        self.assertEqual(outcome["reason"], "middle_platform_support_lost")
        self.assertEqual(outcome["steps"], 1)

    def test_full_south_joint_coverage_and_no_continuous_claim(self):
        manifest = calibration.build_manifest()
        report = calibration.scan_direction(manifest)
        self.assertEqual(len(report["samples"]), 243)
        self.assertEqual(report["sample_coverage"], "passed")
        self.assertEqual(report["sound_envelope"], "unverified")
        self.assertLessEqual(report["main_physics_steps"], 30000)
        self.assertEqual(sum(len(s["suffixes"]) for s in report["samples"]), 729)
        calibration.validate_coverage(manifest, report)
        for mutation in ("missing", "duplicate", "offset", "physical_state", "timing"):
            altered = copy.deepcopy(report)
            if mutation == "missing":
                altered["samples"].pop()
            elif mutation == "duplicate":
                altered["samples"][-1] = copy.deepcopy(altered["samples"][0])
            elif mutation == "offset":
                altered["samples"][0]["offsets"][0] = .001
            elif mutation == "physical_state":
                altered["samples"][0]["entry"]["position"][0] += .001
            else:
                altered["samples"][0]["suffixes"]["landing-immediate-late1"]["first_effect_relative_to_landing"] = 1
            with self.assertRaisesRegex(ValueError, "coverage"):
                calibration.validate_coverage(manifest, altered)

    def test_reported_offsets_without_physical_change_are_detected(self):
        manifest = calibration.build_manifest()
        with patch.object(calibration, "perturb_entry", side_effect=lambda entry, offsets, direction: entry):
            with self.assertRaisesRegex(ValueError, "perturbation"):
                calibration.scan_direction(manifest)

    def test_raw_entry_yaw_uses_shortest_difference_across_north_wrap(self):
        from types import SimpleNamespace
        fixture = calibration.build_fixture(2)
        nominal = calibration.nominal_entry(fixture)
        self.assertEqual(nominal.yaw_radians, math.pi)
        trial = fixture.trial
        manifest = SimpleNamespace(trials=(trial,))
        receipts = [{"trial_id":trial.trial_id,"command_index":n,"episode_id":"e",
            "request_sequence_id":n,"movement_tick_id":n+1} for n in range(26)]
        physics = [{"schema_version":"mc2p.client-physics-tick.v1","episode_id":"e",
            "request_sequence_id":n,"movement_tick_id":n+1,
            "contact_events":["landed"] if n==25 else []} for n in range(26)]
        for extra_degrees, expected in ((0.,True),(.099,True),(-.099,True),(.101,False),(-.101,False)):
            actual = replace(nominal,yaw_radians=-math.pi+math.radians(extra_degrees))
            with self.subTest(extra_degrees=extra_degrees), \
                    patch.object(calibration,"_raw_hashes",return_value={}), \
                    patch.object(calibration.evidence,"analyze_timeline_identity",return_value=receipts), \
                    patch.object(calibration.evidence,"validate_manifest_document",return_value=manifest), \
                    patch.object(calibration.evidence,"_jsonl",side_effect=[receipts,[]]), \
                    patch.object(calibration.evidence,"declared_trial_fixture_contexts",return_value={trial.trial_id:fixture.context}), \
                    patch.object(calibration.evidence,"_event_timing",return_value={"second_jump_relative_to_landing":1}), \
                    patch.object(calibration.evidence,"analyze_physics_replay",return_value={"status":"passed"}), \
                    patch.object(calibration,"iter_segmented_jsonl",return_value=physics), \
                    patch.object(calibration,"landed_post_state",return_value=actual), \
                    patch.object(calibration,"replay_suffix",return_value={"passed":True,"steps":1}), \
                    patch.object(Path,"read_text",return_value="{}"):
                report = calibration.analyze_raw("unused-read-only-fixture")
                self.assertEqual(report["entries"][0]["within_registered_bounds"],expected,report)
                self.assertAlmostEqual(report["entries"][0]["offsets_from_nominal"][-1],extra_degrees)

    def test_raw_inside_bounds_without_suffix_replay_does_not_pass(self):
        self.assertFalse(calibration.raw_gate_passed({
            "identity": "passed", "raw_hashes_unchanged": True,
            "physics_replay": {"status": "passed"},
            "entries": [{"within_registered_bounds": True, "entry_phase": "landed_post_state"}],
        }))

    def test_four_direction_nominal_results_are_rotation_consistent(self):
        signatures = []
        for direction in range(4):
            fixture = calibration.build_fixture(direction)
            entry = calibration.nominal_entry(fixture)
            results = [calibration.replay_suffix(entry, fixture, suffix) for suffix in calibration.build_manifest()["suffix_limits"]]
            signatures.append([(r["passed"], r["steps"], r["first_effect_relative_to_landing"]) for r in results])
        self.assertTrue(all(value == signatures[0] for value in signatures))


class D120LaunchGateTests(unittest.TestCase):
    def test_only_initial_reset_health_may_precede_first_movement_tick(self):
        from scripts import d120_momentum_calibration as launch
        initial = {"schema_version":"mc2p.d120-health-observation.v1","episode_id":"e",
            "observation_sequence_id":0,"request_sequence_id":None,"movement_tick_id":None,
            "health_points":20.,"self_damage_event_ids":[],"damage_events_dropped":0}
        next_row = {**initial,"observation_sequence_id":1,"request_sequence_id":0,"movement_tick_id":1}
        self.assertEqual(launch.validate_health_evidence([initial,next_row],[])["status"],"passed")
        for rows in (
            [{**initial,"observation_sequence_id":1}],
            [{**initial,"request_sequence_id":0}],
            [{key:value for key,value in initial.items() if key!="request_sequence_id"}],
            [initial,{**next_row,"movement_tick_id":None}],
            [initial,{**next_row,"request_sequence_id":None,"movement_tick_id":None}],
        ):
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,"health"):
                launch.validate_health_evidence(rows,[])

    def test_health_missing_decrease_damage_and_dropped_events_are_rejected(self):
        from scripts import d120_momentum_calibration as launch
        row = {"schema_version":"mc2p.d120-health-observation.v1","episode_id":"e",
            "observation_sequence_id":0,"movement_tick_id":0,"health_points":20.,
            "self_damage_event_ids":[],"damage_events_dropped":0}
        self.assertEqual(launch.validate_health_evidence([row],[])["status"],"passed")
        for rows in ([],[{**row,"health_points":None}],
            [row,{**row,"observation_sequence_id":1,"health_points":19.}],
            [{**row,"self_damage_event_ids":[1]}],[{**row,"damage_events_dropped":1}]):
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,"health"):
                launch.validate_health_evidence(rows,[])

    def test_runtime_summary_fails_on_actual_observation_health_drop(self):
        from types import SimpleNamespace
        from scripts import d120_momentum_calibration as launch
        runtime = SimpleNamespace(observation=SimpleNamespace(episode_id="e",sequence_id=0,
            self_state=SimpleNamespace(value=SimpleNamespace(movement_tick_id=0,health_points=20.)),
            damage_events=(),damage_events_dropped=0),cancel_source=lambda source:())
        def shared(*args,observation_diagnostic,**kwargs):
            observation_diagnostic()
            runtime.observation.sequence_id = 1
            runtime.observation.self_state.value.health_points = 19.
            runtime.observation.self_state.value.movement_tick_id = 1
            observation_diagnostic()
            (Path(args[3])/"d118-runtime-receipts.jsonl").write_text("",encoding="utf-8")
            return {"status":"recorded"},[],[]
        with tempfile.TemporaryDirectory() as directory,patch.object(
                launch.frozen,"run_d118_momentum_calibration_runtime",side_effect=shared):
            summary,_,checks = launch.run_calibration_runtime(runtime,None,"e",Path(directory),123,None,None)
            self.assertEqual(summary["health_evidence"]["status"],"failed")
            self.assertFalse(next(c["passed"] for c in checks if c["name"]=="d120_real_observation_zero_damage"))
            recorded = launch.evidence._jsonl(Path(directory)/"d120-health-observations.jsonl")
            self.assertEqual([r["health_points"] for r in recorded],[20.,19.])

    @classmethod
    def setUpClass(cls):
        cls.offline_reports = [calibration.scan_direction(calibration.build_manifest(),n) for n in range(4)]

    def _offline(self, directory):
        target = Path(directory)
        target.mkdir()
        calibration._write(target/"manifest.json",calibration.build_manifest())
        for n,report in enumerate(self.offline_reports):
            calibration._write(target/f"coverage-{n}.json",report)
        raw = {"identity":"passed","physics_replay":{"status":"passed"},"raw_hashes_unchanged":True,
            "entries":[{"within_registered_bounds":True,"entry_phase":"landed_post_state",
                "raw_suffix_replay":{key:{"passed":True} for key in calibration.SUFFIX_LIMITS}}]}
        calibration._write(target/"old-raw-gate.json",raw)
        calibration._write(target/"summary.json",{
            "status":"passed","raw_gate_status":"passed","sound_envelope":"unverified",
            "manifest_sha256":calibration.build_manifest()["sha256"],"directions":4,
            "sample_count":972,"suffix_count":2916,"elapsed_seconds":1.})
        calibration.write_result_index(target)
        return target

    def test_prerequisite_hashes_bind_content_but_not_elapsed_clock(self):
        from scripts import d120_momentum_calibration as launch
        with tempfile.TemporaryDirectory() as directory:
            target = self._offline(Path(directory)/"offline")
            before = launch.validate_offline_prerequisite(target)
            summary = launch._read(target/"summary.json")
            summary["elapsed_seconds"] = 123456.
            calibration._write(target/"summary.json",summary)
            calibration.write_result_index(target)
            self.assertEqual(before,launch.validate_offline_prerequisite(target))
            coverage = target/"coverage-0.json"
            coverage.write_text(coverage.read_text()+" ",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"hash"):
                launch.validate_offline_prerequisite(target)
            calibration.write_result_index(target)
            summary["status"] = "failed"
            calibration._write(target/"summary.json",summary)
            calibration.write_result_index(target)
            with self.assertRaisesRegex(ValueError,"prerequisite"):
                launch.validate_offline_prerequisite(target)

    def test_full_cannot_launch_without_signed_south(self):
        from scripts import d120_momentum_calibration as launch
        with tempfile.TemporaryDirectory() as directory, patch.object(launch.subprocess,"run") as run:
            offline = self._offline(Path(directory)/"offline")
            with self.assertRaisesRegex(ValueError,"south prerequisite"):
                launch.main(["--output",str(Path(directory)/"full"),"--full-matrix",
                    "--offline-prerequisite",str(offline),"--south-prerequisite",str(Path(directory)/"missing")])
            run.assert_not_called()

    def test_probe_modes_are_mutually_exclusive_before_environment_launch(self):
        from scripts import probe_fabric_deployment_observation as probe
        for args in (
            ["--d120-motion-calibration-probe","--d118-motion-calibration-probe"],
            ["--d120-full-matrix"],
            ["--d120-motion-calibration-probe"],
        ):
            with self.assertRaises(SystemExit) as error:
                probe.main(args)
            self.assertEqual(error.exception.code,2)

    def test_component_runs_same_three_trials_and_full_summary_as_fabric(self):
        from contextlib import closing
        from dataclasses import asdict
        from scripts import d120_momentum_calibration as launch
        from scripts import d118_momentum_calibration_runtime as frozen
        from tests.motion_nav.test_d118_momentum_calibration import _physics_state_json
        from tests.sim.backend import CalculatorBackend, Scene
        from tests.test_player_runtime import _RecordingTrace
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        from mc2p.contracts.reset import ResetRequestV0
        from mc2p.runtime.segmented_trace import SegmentedJsonlWriter
        from mc2p.motion_nav.physics_1_21 import step
        clock = [100_000_000]
        volume = ((0,36),(20,70),(0,52))
        backend = CalculatorBackend(clock,Scene({(0,63,0):"minecraft:stone"},volume),(.5,64.,.5))
        runtime = PlayerRuntimeV1(backend,_RecordingTrace(),clock_ns=lambda:clock[0])
        deadline = clock[0]+120_000_000_000
        self.assertTrue(runtime.reset(ResetRequestV0("d120-reset",backend.episode,"d120",1,deadline)).succeeded)
        captured = []
        def tracked_step(state,command,world,rules):
            result = step(state,command,world,rules)
            captured.append((state,result))
            return result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = root/"run/client-0"
            owned = client/launch.OWNED_DIRECTORY
            owned.mkdir(parents=True)
            def fixture_writer(positions,material,**identity):
                solids = dict(backend.scene.solids)
                for position in positions:
                    if material == "minecraft:air": solids.pop(position,None)
                    else: solids[position] = material
                backend.scene = Scene(solids,volume)
                backend._build_truth()
                backend.state = replace(backend.state,session=backend._truth_session)
                with (owned/"d118-fixture-commands.jsonl").open("a",encoding="utf-8") as stream:
                    stream.write(json.dumps(frozen.fixture_command_row(positions=positions,material=material,**identity))+"\n")
            def teleport(x,y,z,yaw,pitch):
                backend.state = replace(backend.state,position=(x,y,z),
                    velocity_blocks_per_tick=(0.,-.0784000015258789,0.),yaw_radians=math.radians(yaw),
                    pitch_radians=0.,on_ground=False,vertical_collision=False,sprinting=False,sneaking=False,
                    jumping_cooldown_ticks=0,fall_distance_blocks=0.)
            with patch("tests.sim.backend.physics_step",side_effect=tracked_step), \
                    patch.object(runtime,"cancel_source",wraps=runtime.cancel_source) as cancel, \
                    patch.object(runtime,"step",wraps=runtime.step) as formal:
                summary,rows,checks = launch.run_calibration_runtime(runtime,backend,backend.episode,owned,deadline,
                    fixture_writer,teleport,clock_ns=lambda:clock[0])
                self.assertGreaterEqual(cancel.call_count,formal.call_count+1)
                self.assertGreaterEqual(formal.call_count,123)
            self.assertEqual((summary["trial_count"],summary["command_count"]),(3,123))
            self.assertEqual(summary["source_release_status"],"released")
            self.assertTrue(all(c["passed"] for c in checks))
            receipts = launch.evidence._jsonl(owned/"d118-runtime-receipts.jsonl")
            launch.evidence.validate_receipts_against_manifest(launch.build_manifest(),receipts)
            self.assertEqual(len(backend.actions),len(captured))
            by_request = {a.request_sequence_id:pair for a,pair in zip(backend.actions,captured)}
            physics_rows = []
            with closing(SegmentedJsonlWriter(client/"physics-tick-events")) as writer:
                for receipt in receipts:
                    state,result = by_request[receipt["request_sequence_id"]]
                    row = frozen.physics_sidecar_row(episode_id=receipt["episode_id"],
                        request_sequence_id=receipt["request_sequence_id"],movement_tick_id=receipt["movement_tick_id"],
                        actual_input=receipt["applied_movement"])
                    row.update(pre_state=_physics_state_json(state),post_state=_physics_state_json(result.next_state),
                        movement_yaw=math.degrees(state.yaw_radians),contact_events=[e for e in result.events
                            if e in {"left_ground","landed","horizontal_collision","vertical_collision"}])
                    writer.write(row)
                    physics_rows.append(row)
            ftick_rows = []
            with closing(SegmentedJsonlWriter(client/"ftick-events")) as writer:
                count = 0
                for receipt in receipts:
                    for row in frozen.ftick_sidecar_rows(**{key:receipt[key] for key in (
                            "episode_id","request_sequence_id","observation_sequence_id","generation_id","movement_tick_id")}):
                        writer.write(row)
                        ftick_rows.append(row)
                        count += 1
                writer.write({"schema_version":"mc2p.client-ftick-summary.v1","kind":"SUMMARY",
                    "dropped_event_count":0,"event_count":count})
            launch.write_json_atomic(root/"run/result.json",{"status":"passed"})
            analysis = launch.analyze_run(root/"run")
            self.assertEqual(analysis["status"],"passed",analysis)
            self.assertEqual(analysis["joined_command_count"],123)
            self.assertEqual(analysis["responsibility_cutoff"],summary["responsibility_cutoff"])
            self.assertEqual(analysis["post_release_tail_count"],0)
            self.assertEqual(analysis["post_release_tail_status"],"passed")
            nominal_entry = calibration.nominal_entry
            # An equivalent wrapped yaw passes; an actual >0.1 degree offset
            # still fails the frozen entry bounds in the complete analyzer.
            for degrees,expected in ((360.,"passed"),(360.101,"failed"),(-360.101,"failed")):
                with self.subTest(nominal_yaw_offset=degrees),patch.object(calibration,"nominal_entry",
                        side_effect=lambda fixture:replace(nominal_entry(fixture),
                            yaw_radians=nominal_entry(fixture).yaw_radians+math.radians(degrees))):
                    wrapped = launch.analyze_run(root/"run")
                self.assertEqual(wrapped["status"],expected,wrapped)
                self.assertTrue(all(item["within_registered_bounds"]==(expected=="passed")
                    for item in wrapped["entries"]),wrapped)
            sealed = launch.seal_run(root/"sealed",root/"run",command=[],return_code=0,
                stdout="",stderr="",offline_hash="a"*64)
            self.assertEqual(sealed["acceptance_status"],"passed",sealed)
            self.assertEqual(launch.validate_south_prerequisite(root/"sealed","a"*64),launch._hash(root/"sealed/index.json"))
            from types import SimpleNamespace
            from contextlib import redirect_stdout
            import io
            offline = self._offline(root/"offline")
            with patch.object(launch.subprocess,"run",return_value=SimpleNamespace(
                    returncode=0,stdout=f"FABRIC_DEPLOYMENT_RUN_DIR={root/'run'}\n",stderr="")) as process, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(launch.main(["--output",str(root/"cli"),
                    "--offline-prerequisite",str(offline)]),0)
            self.assertIn("--d120-motion-calibration-probe",process.call_args.args[0])
            self.assertEqual(launch._read(root/"cli/summary.json")["offline_prerequisite_sha256"],
                launch._hash(offline/"index.json"))
            health_path = owned/"d120-health-observations.jsonl"
            health_original = health_path.read_text(encoding="utf-8")
            health_rows = launch.evidence._jsonl(health_path)
            health_path.rename(owned/"health-original.jsonl")
            with self.assertRaises(OSError):
                launch.analyze_run(root/"run")
            health_path.write_text(health_original,encoding="utf-8")
            damaged = copy.deepcopy(health_rows)
            damaged[-1]["health_points"] -= 1.
            health_path.write_text("".join(json.dumps(r)+"\n" for r in damaged),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"health decreased"):
                launch.analyze_run(root/"run")
            health_path.write_text(health_original,encoding="utf-8")
            runtime_path = owned/"d118-calibration-runtime.json"
            original_runtime = launch._read(runtime_path)
            changed_runtime = copy.deepcopy(original_runtime)
            changed_runtime["responsibility_cutoff"]["movement_tick_id"] -= 1
            launch.write_json_atomic(runtime_path,changed_runtime)
            with self.assertRaisesRegex(ValueError,"cutoff"):
                launch.analyze_run(root/"run")
            launch.write_json_atomic(runtime_path,original_runtime)

            import shutil
            fixtures = launch.evidence._jsonl(owned/"d118-fixture-commands.jsonl")
            contexts = launch.evidence.declared_trial_fixture_contexts(launch.build_manifest(),fixtures)
            # A safe unleased tail is real extra physics and must still pass.
            # Corrupt versions at the end and between two receipts must fail.
            for kind,receipt_index in (("safe",-1),("end_off_platform",-1),("between_off_platform",-2),
                    ("target_off_platform",37),("safe_post_release",-1),
                    ("post_release_off_platform",-1),("post_release_physics_mismatch",-1)):
                copied = root/kind
                shutil.copytree(root/"run",copied)
                copied_client = copied/"client-0"
                selected_receipt = receipts[receipt_index]
                selected_row = next(row for row in physics_rows if row["request_sequence_id"]==selected_receipt["request_sequence_id"])
                world = contexts[selected_receipt["trial_id"]].world
                post = launch._post_state(selected_row,world)
                calculated = calibration.physics_step(post,calibration._input(calibration.NEUTRAL,post.yaw_radians),world,calibration.JAVA_1_21_RULESET)
                tail = copy.deepcopy(selected_row)
                tail.update(movement_tick_id=selected_row["movement_tick_id"]+1,input_state="lease_exhausted",
                    actual_input={"forward":0.,"strafe":0.,"jump":False,"sneak":False,"sprint":False},
                    pre_state=copy.deepcopy(selected_row["post_state"]),post_state=_physics_state_json(calculated.next_state),
                    contact_events=[e for e in calculated.events if e in {
                        "left_ground","landed","horizontal_collision","vertical_collision"}])
                if kind=="post_release_physics_mismatch":
                    tail["post_state"]["position"]["x"] += .01
                elif kind not in {"safe","safe_post_release"}:
                    tail["post_state"]["position"]["y"] = 50.
                    tail["post_state"]["on_ground"] = False
                all_rows = [*physics_rows,tail]
                extra_events = []
                for offset,event_kind in enumerate(("INPUT_SAMPLED","MOVEMENT_APPLIED"),5):
                    extra_events.append({"schema_version":"mc2p.client-ftick-event.v1","episode_id":tail["episode_id"],
                        "request_sequence_id":tail["request_sequence_id"],"movement_tick_id":tail["movement_tick_id"],
                        "kind":event_kind,"event_sequence":offset,"observation_sequence_id":None,"generation_id":None})
                for stream_name,records in (("physics-tick-events",all_rows),("ftick-events",[*ftick_rows,*extra_events,
                    {"schema_version":"mc2p.client-ftick-summary.v1","kind":"SUMMARY","dropped_event_count":0,
                    "event_count":len(ftick_rows)+2}])):
                    destination = copied_client/stream_name
                    destination.rename(copied_client/(stream_name+"-original"))
                    with closing(SegmentedJsonlWriter(destination)) as writer:
                        for row in records: writer.write(row)
                copied_owned = copied_client/launch.OWNED_DIRECTORY
                extended_health = health_rows if "post_release" in kind else [*health_rows,{**health_rows[-1],
                    "observation_sequence_id":health_rows[-1]["observation_sequence_id"]+1,
                    "movement_tick_id":max(r["movement_tick_id"] for r in all_rows)}]
                (copied_owned/"d120-health-observations.jsonl").write_text(
                    "".join(json.dumps(row)+"\n" for row in extended_health),encoding="utf-8")
                runtime_doc = launch._read(copied_owned/"d118-calibration-runtime.json")
                runtime_doc["health_evidence"] = launch.validate_health_evidence(extended_health,receipts)
                runtime_doc["responsibility_cutoff"] = launch._responsibility_cutoff(extended_health[-1])
                launch.write_json_atomic(copied_owned/"d118-calibration-runtime.json",runtime_doc)
                report = launch.analyze_run(copied)
                self.assertEqual(report["status"],"passed" if kind in {"safe","safe_post_release"} else "failed",report)
                changed_entry = next(item for item in report["entries"] if item["trial_id"]==selected_receipt["trial_id"])
                self.assertEqual(changed_entry["complete_physics_replay"]["tick_count"],39 if receipt_index==37 else 47)
                if kind=="target_off_platform":
                    self.assertFalse(changed_entry["target_landing_tail_supported"])
                if "post_release" in kind:
                    self.assertEqual(report["responsibility_cutoff"],summary["responsibility_cutoff"])
                    self.assertEqual(report["post_release_tail_count"],1)
                    self.assertEqual(report["post_release_tail_status"],"passed" if kind=="safe_post_release" else "failed")

            # Re-hashing a failed analysis cannot turn it into signed south evidence.
            forged = root/"forged"
            shutil.copytree(root/"cli",forged)
            launch.write_json_atomic(forged/"analysis.json",{"status":"failed","entries":[]})
            index = launch._read(forged/"index.json")
            index["files"]["analysis.json"] = launch._hash(forged/"analysis.json")
            launch.write_json_atomic(forged/"index.json",index)
            with self.assertRaisesRegex(ValueError,"analysis"):
                launch.validate_south_prerequisite(forged,launch._hash(offline/"index.json"))
            for kind in ("missing_cutoff","failed_post_release"):
                folder = root/kind
                shutil.copytree(root/"cli",folder)
                doc = launch._read(folder/"analysis.json")
                if kind=="missing_cutoff": doc.pop("responsibility_cutoff")
                else: doc["post_release_tail_status"] = "failed"
                launch.write_json_atomic(folder/"analysis.json",doc)
                index = launch._read(folder/"index.json")
                index["files"]["analysis.json"] = launch._hash(folder/"analysis.json")
                launch.write_json_atomic(folder/"index.json",index)
                with self.assertRaisesRegex(ValueError,"analysis"):
                    launch.validate_south_prerequisite(folder,launch._hash(offline/"index.json"))
            for kind in ("missing_raw","path_escape"):
                folder = root/kind
                shutil.copytree(root/"cli",folder)
                doc = launch._read(folder/"summary.json")
                if kind=="missing_raw": doc["raw_files"] = {"result.json":launch._hash(root/"run/result.json")}
                else: doc["raw_files"]["../escape.json"] = "0"*64
                launch.write_json_atomic(folder/"summary.json",doc)
                index = launch._read(folder/"index.json")
                index["files"]["summary.json"] = launch._hash(folder/"summary.json")
                launch.write_json_atomic(folder/"index.json",index)
                with self.assertRaises(ValueError):
                    launch.validate_south_prerequisite(folder,launch._hash(offline/"index.json"))
            (root/"sealed/analysis.json").write_text("{}",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"south prerequisite.*hash"):
                launch.validate_south_prerequisite(root/"sealed","a"*64)
            original_receipts = (owned/"d118-runtime-receipts.jsonl").read_text(encoding="utf-8")
            altered = copy.deepcopy(receipts)
            altered[0]["applied_movement"]["jump"] = True
            (owned/"d118-runtime-receipts.jsonl").write_text(
                "".join(json.dumps(row)+"\n" for row in altered),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"input differs"):
                launch.analyze_run(root/"run")
            (owned/"d118-runtime-receipts.jsonl").write_text(original_receipts,encoding="utf-8")
            runtime_doc = launch._read(owned/"d118-calibration-runtime.json")
            runtime_doc["source_release_status"] = "failed"
            launch.write_json_atomic(owned/"d118-calibration-runtime.json",runtime_doc)
            failed = launch.seal_run(root/"failed",root/"run",command=[],return_code=0,
                stdout="",stderr="",offline_hash="a"*64)
            self.assertEqual(failed["capture_status"],"failed")
            self.assertEqual(failed["acceptance_status"],"failed")
        runtime.close()

    def test_offline_main_seals_a_verifiable_result_index(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "offline"
            with patch.object(calibration, "scan_direction", return_value={
                    "main_physics_steps": 1, "samples": [{"suffixes": {"x": {}}}]}), \
                    patch.object(calibration, "validate_coverage"), \
                    patch.object(calibration, "analyze_raw", return_value={"status": "passed"}):
                self.assertEqual(calibration.main(["--run-dir", directory, "--output", str(target)]), 0)
            self.assertTrue((target / "index.json").is_file(), "offline prerequisite has no sealed index")

    def test_frozen_d120_manifests_are_separate_from_d118(self):
        from scripts import d120_momentum_calibration as launch
        from scripts import d118_momentum_calibration as old
        for full, expected_hash, shape in (
            (False, "0ee556cbd682b3692066dc0371b4936c0abe43c125c37f6629368df878e66f18", (3, 123)),
            (True, "9c54a00e635ccabd4224c79255645b03cafeab51332e669800fc10dc9f2e75c0", (36, 1476)),
        ):
            manifest = launch.build_manifest(full_matrix=full)
            self.assertEqual(manifest.sha256, expected_hash)
            self.assertEqual((len(manifest.trials), sum(len(t.commands) for t in manifest.trials)), shape)
            self.assertNotIn(expected_hash, old._FROZEN_MANIFESTS)
            launch.validate_manifest_document(launch.manifest_document(manifest))

    def test_launch_refuses_missing_prerequisite_before_subprocess(self):
        from scripts import d120_momentum_calibration as launch
        with tempfile.TemporaryDirectory() as directory, patch.object(launch.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "prerequisite"):
                launch.main(["--output", str(Path(directory)/"result"), "--offline-prerequisite", str(Path(directory)/"missing")])
            run.assert_not_called()

    def test_formal_command_has_d120_identity_and_both_sidecars(self):
        from scripts import d120_momentum_calibration as launch
        command = launch.fabric_probe_command(server_port=25597, ipc_port=8140)
        self.assertIn("--d120-motion-calibration-probe", command)
        self.assertNotIn("--d118-motion-calibration-probe", command)
        self.assertIn("--physics-tick-diagnostics", command)
        self.assertIn("--ftick-diagnostics", command)
        self.assertEqual(command[command.index("--seed")+1], "21001")
        self.assertEqual(command[command.index("--timeout-seconds")+1], "600")


if __name__ == "__main__":
    unittest.main()
