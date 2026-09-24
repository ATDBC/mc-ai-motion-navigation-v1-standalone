"""Real loopback protocol tests; fixtures are not Minecraft integration evidence."""
from dataclasses import asdict, replace
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.reset import ResetRequestV0
from tests.deployment_fixtures import backend_peer, sample_value
from tests.observation_v3_fixtures import tracked_entity_value, valid_payload_value
from tests.test_deployment_python_transport import deadline

V3 = "mc2p.client_observation.v3"


def sample_v3(sequence=0, profile="navigation_v1"):
    envelope = sample_value(sequence)
    value = valid_payload_value(profile)
    value["generation_id"] = sequence
    value["client_sample"].update(started_at_monotonic_ns=9000+sequence*100,
                                   completed_at_monotonic_ns=9010+sequence*100)
    envelope.update(schema_version="mc2p.deployment_sample.v2", observation=value)
    return envelope


def reset(backend):
    return backend.reset(ResetRequestV0("reset", "ep", "remote-session", 0, deadline()))


class ObservationRequestTransportTests(unittest.TestCase):
    def test_v3_request_accepts_only_one_bounded_registered_entity_id(self):
        request = ObservationRequestV3(
            "interaction_v1", entity_track_id="entity-world-7"
        )
        self.assertEqual(request.entity_track_id, "entity-world-7")
        with self.assertRaises(ContractViolation):
            ObservationRequestV3("interaction_v1", entity_track_id="")
        with self.assertRaises(ContractViolation):
            ObservationRequestV3("interaction_v1", entity_track_id="x" * 129)

    def test_fabric_rejects_a_tracked_entity_for_another_request(self):
        current = sample_v3(1, "interaction_v1")
        current["observation"]["tracked_entity"] = dict(
            status="valid", sample_world_tick=100,
            source_kind="client_registered_entity", reason_code=None,
            value=tracked_entity_value("entity-other"),
        )
        with backend_peer([sample_v3(), current], observation_schema_version=V3) as (
                backend, transport, _):
            self.assertTrue(reset(backend).succeeded)
            with self.assertRaisesRegex(ContractViolation, "entity_query_mismatch"):
                backend.step(
                    ActionSnapshotV1("ep", 0, 0, deadline()), deadline(),
                    observation_request=ObservationRequestV3(
                        "interaction_v1", entity_track_id="entity-world-7"
                    ),
                )
            self.assertTrue(transport.closed)

    def test_air_request_canonicalizes_and_respects_transport_payload_budget(self):
        request = ObservationRequestV3("navigation_v1", ((2, 64, 0), (1, 64, 0), (2, 64, 0)))
        self.assertEqual(request.air_positions, ((1, 64, 0), (2, 64, 0)))
        with self.assertRaisesRegex(ContractViolation, "32-bit"):
            ObservationRequestV3("navigation_v1", ((2 ** 31, 64, 0),))
        with self.assertRaisesRegex(ContractViolation, "payload budget"):
            ObservationRequestV3("navigation_v1", tuple(
                (-(2 ** 31) + index, -(2 ** 31), -(2 ** 31)) for index in range(512)
            ))

    def test_v3_cannot_start_unpatched_pov_environment(self):
        from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
        from mc2p.backends.craftground_runtime import CraftGroundObservationModeV0
        from tests.test_craftground_backend import _DebugSink
        with self.assertRaises(ContractViolation):
            CraftGroundBehaviorBackendV1(observation_schema_version=V3,
                observation_mode=CraftGroundObservationModeV0.POV_DEBUG,debug_frame_sink=_DebugSink())

    def test_recipe_schema_is_frozen_and_old_manifest_is_read_only(self):
        from mc2p.backends.craftground_runtime import (prepare_runtime_sandbox, CraftGroundClockModeV0,
            load_sandbox_manifest, validate_sandbox_for_mode, RuntimePreparationError)
        from tests.test_craftground_runtime import _write_source
        with TemporaryDirectory() as directory:
            root = Path(directory)
            expected = _write_source(root/"source")
            config = dict(source_root=root/"source",sandbox_parent=root/"sandboxes",sandbox_id="recipe",
                          clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,expected_fingerprints=expected)
            legacy = prepare_runtime_sandbox(**config)
            current = prepare_runtime_sandbox(**config,observation_schema_version=V3)
            self.assertNotEqual(legacy.path,current.path)
            self.assertNotEqual(legacy.manifest.patch_recipe_fingerprint,current.manifest.patch_recipe_fingerprint)
            with self.assertRaises(RuntimePreparationError):
                validate_sandbox_for_mode(current.path,config["clock_mode"],observation_schema_version="mc2p.client_observation.v2")
            path = legacy.path/"sandbox-manifest.json"
            old = json.loads(path.read_text())
            old.pop("observation_schema_version")
            old["schema_version"] = "mc2p.craftground-sandbox.v2"
            path.write_text(json.dumps(old))  # Test-owned temporary artifact only.
            self.assertEqual(load_sandbox_manifest(legacy.path).observation_schema_version,"mc2p.client_observation.v2")
            with self.assertRaisesRegex(RuntimePreparationError,"legacy"):
                validate_sandbox_for_mode(legacy.path,config["clock_mode"])

    def test_craftground_v3_recipe_reset_step_and_profile_correlation(self):
        from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
        from mc2p.backends.craftground_runtime import prepare_runtime_sandbox, CraftGroundClockModeV0
        from mc2p.backends.client_observation_payload import extract_length_delimited_field
        from tests.test_craftground_runtime import _write_source
        from tests.test_craftground_backend import _raw_observation, _varint, _FakeSocket
        from tests.test_craftground_behavior import EPOCH_1, EPOCH_2
        def frame(sequence, profile="navigation_v1"):
            envelope = sample_v3(sequence, profile)
            full = _raw_observation()["full"]
            full.serialized = b""
            fields = {50000: json.dumps(envelope["observation"]).encode(), 50003: EPOCH_1.encode()}
            if sequence:
                fields[50002] = json.dumps(envelope["receipt"]).encode()
            for number, payload in fields.items():
                full.serialized += _varint((number << 3)|2)+_varint(len(payload))+payload
            return full
        with TemporaryDirectory() as directory:
            root = Path(directory)
            expected = _write_source(root/"source")
            mode = CraftGroundClockModeV0.REFERENCE_20_TPS
            sandbox = prepare_runtime_sandbox(source_root=root/"source", sandbox_parent=root/"sandboxes",
                sandbox_id="v3", clock_mode=mode, expected_fingerprints=expected, observation_schema_version=V3)
            self.assertEqual(sandbox.manifest.observation_schema_version, V3)
            self.assertIn("mc2p.observationSchema", (sandbox.path/"build.gradle").read_text())
            backend = CraftGroundBehaviorBackendV1(runtime_env_path=sandbox.path, clock_mode=mode,
                                                   observation_schema_version=V3, clock_ns=lambda: 10)
            sent, frames = [], iter([frame(1), frame(2,"interaction_v1"), frame(3), frame(4,"interaction_v1")])
            env = SimpleNamespace(queued_commands=[], reset=lambda **_: ({"full":frame(0)},{}),
                ipc=SimpleNamespace(sock=_FakeSocket(), send_action=lambda message, _:sent.append(message),
                                    read_observation=lambda:next(frames)), convert_observation_v2=lambda raw:{"full":raw})
            with patch.object(backend,"_create_environment",return_value=env):
                initial = backend.reset(ResetRequestV0("r","ep","flat-safe",1,100))
            self.assertTrue(initial.succeeded,initial.failure)
            for i, request in enumerate([None,ObservationRequestV3("interaction_v1"),None]):
                result = backend.step(ActionSnapshotV1("ep",i,i,100),100,observation_request=request)
                self.assertEqual(result.observation.field_profile,(request or ObservationRequestV3()).field_profile)
                self.assertEqual(json.loads(extract_length_delimited_field(sent[-1].SerializeToString(),field_number=50004)),
                                 json.loads(json.dumps(asdict(request or ObservationRequestV3()))))
            with self.assertRaisesRegex(ContractViolation,"profile"):
                backend.step(ActionSnapshotV1("ep",3,3,100),100)
            with self.assertRaisesRegex(ContractViolation,"recreat"):
                backend.step(ActionSnapshotV1("ep",4,3,100),100)
            self.assertEqual(len(sent),4)
            for clock_change in ({"clock_id":"other-jvm"},
                                 {"started_at_monotonic_ns":1,"completed_at_monotonic_ns":2}):
                bad = frame(1)
                payload = json.loads(extract_length_delimited_field(bad.serialized,field_number=50000))
                payload["client_sample"].update(clock_change)
                raw = json.dumps(payload).encode()
                receipt = extract_length_delimited_field(bad.serialized,field_number=50002)
                bad.serialized = b""
                for number,data in {50000:raw,50002:receipt,50003:EPOCH_1.encode()}.items():
                    bad.serialized += _varint((number<<3)|2)+_varint(len(data))+data
                fresh = CraftGroundBehaviorBackendV1(runtime_env_path=sandbox.path,clock_mode=mode,
                    observation_schema_version=V3,clock_ns=lambda:10)
                env.ipc.read_observation = lambda:bad
                with patch.object(fresh,"_create_environment",return_value=env):
                    self.assertTrue(fresh.reset(ResetRequestV0("r","ep","flat-safe",1,100)).succeeded)
                with self.assertRaisesRegex(ContractViolation,"clock"):
                    fresh.step(ActionSnapshotV1("ep",0,0,100),100)
                self.assertTrue(fresh._requires_recreation)
            from mc2p.backends import craftground_behavior
            for late in (100,101):
                clock = [10]
                fresh = CraftGroundBehaviorBackendV1(runtime_env_path=sandbox.path,clock_mode=mode,
                    observation_schema_version=V3,clock_ns=lambda:clock[0])
                env.ipc.read_observation = lambda:frame(1)
                with patch.object(fresh,"_create_environment",return_value=env):
                    self.assertTrue(fresh.reset(ResetRequestV0("r","ep","flat-safe",1,100)).succeeded)
                original = craftground_behavior.decode_behavior_receipt
                def delayed(payload):
                    result = original(payload)
                    clock[0] = late
                    return result
                with patch.object(craftground_behavior,"decode_behavior_receipt",side_effect=delayed):
                    with self.assertRaises(TimeoutError):
                        fresh.step(ActionSnapshotV1("ep",0,0,100),100)
                self.assertTrue(fresh._requires_recreation)
                self.assertEqual(fresh._sequence_id,0)
                self.assertIsNone(fresh.last_behavior_receipt)
            from mc2p.backends import craftground
            for late in (100,101):
                clock = [10]
                fresh = CraftGroundBehaviorBackendV1(runtime_env_path=sandbox.path,clock_mode=mode,
                    observation_schema_version=V3,clock_ns=lambda:clock[0])
                original = craftground.observation_from_craftground
                def delayed_reset(*args,**kwargs):
                    result = original(*args,**kwargs)
                    clock[0] = late
                    return result
                with patch.object(fresh,"_create_environment",return_value=env), patch.object(
                        craftground,"observation_from_craftground",side_effect=delayed_reset):
                    result = fresh.reset(ResetRequestV0("r","ep","flat-safe",1,100))
                self.assertFalse(result.succeeded)
                self.assertEqual(result.failure.code.value,"deadline_exceeded")
                self.assertTrue(fresh._requires_recreation)
                self.assertIsNone(fresh._episode_id)
            for clock_change in ({"clock_id":"other-jvm"},
                                 {"started_at_monotonic_ns":1,"completed_at_monotonic_ns":2},
                                 {"started_at_monotonic_ns":9200,"completed_at_monotonic_ns":9210}):
                fresh = CraftGroundBehaviorBackendV1(runtime_env_path=sandbox.path,clock_mode=mode,
                    observation_schema_version=V3,clock_ns=lambda:10)
                with patch.object(fresh,"_create_environment",return_value=env):
                    self.assertTrue(fresh.reset(ResetRequestV0("r","ep","flat-safe",1,100)).succeeded)
                old_sample = fresh._last_client_sample
                raw = frame(0)
                payload = json.loads(extract_length_delimited_field(raw.serialized,field_number=50000))
                payload["client_sample"].update(clock_change)
                data = json.dumps(payload).encode()
                raw.serialized = _varint((50000<<3)|2)+_varint(len(data))+data
                data = EPOCH_2.encode()
                raw.serialized += _varint((50003<<3)|2)+_varint(len(data))+data
                env.ipc.read_observation = lambda:raw
                result = fresh.reset(ResetRequestV0("r2","ep2","flat-safe",1,100))
                if clock_change.get("started_at_monotonic_ns")==9200:
                    self.assertTrue(result.succeeded,result.failure)
                else:
                    self.assertFalse(result.succeeded)
                    self.assertEqual(result.failure.code.value,"observation_invariant")
                    self.assertTrue(fresh._requires_recreation)
                    self.assertEqual(fresh._last_client_sample,old_sample)
                    self.assertEqual(fresh._episode_id,"ep")

    def test_fabric_same_session_profiles_and_default_do_not_inherit_targeting(self):
        requests = [ObservationRequestV3(), ObservationRequestV3("interaction_v1"), None]
        samples = [sample_v3()] + [sample_v3(i+1, (r or ObservationRequestV3()).field_profile)
                                  for i, r in enumerate(requests)]
        with backend_peer(samples, observation_schema_version=V3) as (backend, _, sent):
            self.assertTrue(reset(backend).succeeded)
            for i, request in enumerate(requests):
                result = backend.step(ActionSnapshotV1("ep", i, i, deadline()), deadline(),
                                      observation_request=request)
                self.assertEqual(result.observation.field_profile, (request or ObservationRequestV3()).field_profile)
            self.assertEqual(result.observation.targeting.reason_code, "not_requested")
            self.assertEqual(set(sent[0]), {"schema_version", "episode_id", "token", "observation_schema_version"})
            self.assertEqual(sent[0]["schema_version"], "mc2p.deployment_session.v2")
            self.assertEqual(sent[0]["observation_schema_version"], V3)
            for wire, request in zip(sent[1:], requests):
                self.assertEqual(set(wire), {"schema_version", "action", "observation_request"})
                self.assertEqual(wire["schema_version"], "mc2p.client_step.v3")
                self.assertEqual(wire["action"]["schema_version"], "mc2p.client_action.v1")
                self.assertEqual(wire["observation_request"],
                                 json.loads(json.dumps(asdict(request or ObservationRequestV3()))))

    def test_fabric_profile_or_schema_mismatch_seals_without_replay(self):
        for bad in (sample_v3(1, "interaction_v1"), sample_value(1)):
            with self.subTest(bad=bad["schema_version"]), backend_peer(
                    [sample_v3(), bad], observation_schema_version=V3) as (backend, transport, sent):
                self.assertTrue(reset(backend).succeeded)
                with self.assertRaises(ContractViolation):
                    backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline())
                self.assertTrue(transport.closed)
                self.assertEqual(len(sent), 2)

    def test_v3_envelope_corruption_rejects_before_snapshot_reuse(self):
        changes = [lambda s:s["observation"].update(generation_id=9),
            lambda s:s["observation"]["client_sample"].update(clock_id="other"),
            lambda s:s["observation"]["client_sample"].update(started_at_monotonic_ns=1,completed_at_monotonic_ns=2),
            lambda s:s["observation"].update(field_profile="unknown"),
            lambda s:s["observation"].update(pov="forbidden"),
            lambda s:s["receipt"].update(request_sequence_id=9),
            lambda s:s["receipt"].update(action_keyboard_callbacks=1),
            lambda s:s["diagnostics"].update(framebuffer_capture_attempts=1)]
        for i, mutate in enumerate(changes):
            bad = sample_v3(1)
            mutate(bad)
            with self.subTest(case=i), backend_peer([sample_v3(),bad], observation_schema_version=V3) as (backend,transport,sent):
                self.assertTrue(reset(backend).succeeded)
                with self.assertRaises(ContractViolation):
                    backend.step(ActionSnapshotV1("ep",0,0,deadline()),deadline())
                self.assertTrue(transport.closed)
                self.assertIsNone(backend.last_behavior_receipt)
                self.assertEqual(len(sent),2)

    def test_initial_profile_old_schema_and_duplicate_json_are_not_negotiated_silently(self):
        good = json.dumps(sample_v3(),separators=(",", ":")).encode()
        for bad in (sample_v3(profile="interaction_v1"),sample_value(),
                    good[:-1]+b',"episode_id":"ep"}'):
            with backend_peer([bad],observation_schema_version=V3) as (backend,transport,_):
                self.assertFalse(reset(backend).succeeded)
                self.assertTrue(transport.closed)

    def test_runtime_requires_declared_version_and_rejects_wrong_snapshot(self):
        from tests.test_player_runtime_v1 import Backend
        from tests.test_player_runtime import _RecordingTrace
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        for schema in (None,"automatic",False):
            backend = Backend()
            backend.observation_schema_version = schema
            with self.assertRaises(ContractViolation):
                PlayerRuntimeV1(backend,_RecordingTrace())
        backend = Backend()
        backend.observation_schema_version = V3
        runtime = PlayerRuntimeV1(backend,_RecordingTrace())
        try:
            self.assertFalse(reset(runtime).succeeded)
            self.assertEqual(runtime.state.value,"failed")
        finally:
            runtime.close()

    def test_legacy_runtime_does_not_silently_accept_v3_reset_or_step(self):
        from mc2p.runtime.player_runtime import PlayerRuntimeV0
        from mc2p.runtime.backend import BackendStepResultV0
        from mc2p.contracts.reset import ResetResultV0
        from tests.test_player_runtime import _FakeBackend, _RecordingTrace, _reset_request, _task
        from tests.observation_v3_fixtures import valid_snapshot_v3
        for phase in ("reset","step"):
            backend = _FakeBackend()
            runtime = PlayerRuntimeV0(backend,_RecordingTrace(),clock_ns=lambda:100)
            obs = replace(valid_snapshot_v3(sequence=0 if phase=="reset" else 1,request_start_ns=0,received_at_ns=1),
                          episode_id="episode-1",request_sequence_id=None if phase=="reset" else 0)
            try:
                if phase=="reset":
                    with patch.object(backend,"reset",return_value=ResetResultV0("reset-1","episode-1",True,obs)):
                        self.assertFalse(runtime.reset(_reset_request()).succeeded)
                else:
                    self.assertTrue(runtime.reset(_reset_request()).succeeded)
                    with patch.object(backend,"step",return_value=BackendStepResultV0(obs,0.,False,False)):
                        result = runtime.step(_task(),BehaviorProfileV0(),800)
                    self.assertIsNone(result.observation)
                    self.assertEqual(result.report.status.value,"failed")
            finally:
                runtime.close()

    def test_v2_rejects_request_before_socket_and_v3_rejects_wrong_type(self):
        for version, initial in (("mc2p.client_observation.v2", sample_value()), (V3, sample_v3())):
            with backend_peer([initial], observation_schema_version=version) as (backend, _, sent):
                self.assertTrue(reset(backend).succeeded)
                request = ObservationRequestV3() if version.endswith("v2") else {"field_profile": "navigation_v1"}
                with self.assertRaises(ContractViolation):
                    backend.step(ActionSnapshotV1("ep", 0, 0, deadline()), deadline(), observation_request=request)
                self.assertEqual(len(sent), 1)

    def test_runtime_dispatch_records_request_and_cancel_releases_with_navigation(self):
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        from tests.test_player_runtime import _RecordingTrace, _task
        trace = _RecordingTrace()
        with backend_peer([sample_v3(), sample_v3(1, "interaction_v1"), sample_v3(2)],
                          observation_schema_version=V3) as (backend, _, sent):
            runtime = PlayerRuntimeV1(backend, trace)
            try:
                self.assertTrue(reset(runtime).succeeded)
                first = runtime.step(_task(deadline=deadline()), BehaviorProfileV0(), deadline(),
                                     observation_request=ObservationRequestV3("interaction_v1"))
                self.assertIsNotNone(first.observation, first.report)
                runtime.cancel("stop")
                second = runtime.step(_task(deadline=deadline()), BehaviorProfileV0(), deadline())
                self.assertEqual(second.report.status.value, "cancelled")
                self.assertIsNone(sent[2]["action"]["operation"])
                dispatch = [p for k, p in trace.records if k == "dispatch"]
                self.assertEqual(dispatch[0]["observation_request"].field_profile, "interaction_v1")
                self.assertEqual(dispatch[1]["observation_request"].field_profile, "navigation_v1")
            finally:
                runtime.close()

    def test_craftground_action_request_is_independent_length_delimited_field(self):
        from mc2p.backends.client_behavior_payload import behavior_action_message
        from mc2p.backends.client_observation_payload import extract_length_delimited_field
        request = ObservationRequestV3("interaction_v1")
        message = behavior_action_message(ActionSnapshotV1("ep", 0, 0, 1000), now_ns=10,
                                          observation_request=request)
        raw = message.SerializeToString()
        self.assertEqual(json.loads(extract_length_delimited_field(raw, field_number=50004)),
                         json.loads(json.dumps(asdict(request))))
        self.assertEqual(json.loads(extract_length_delimited_field(raw, field_number=50001))["schema_version"],
                         "mc2p.client_action.v1")
