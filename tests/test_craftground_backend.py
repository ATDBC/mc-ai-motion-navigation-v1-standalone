from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from dataclasses import asdict, FrozenInstanceError
import json
import unittest
from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.action import (
    ActionSnapshotV0,
    CameraActionV0,
    GuiActionV0,
    HotbarActionV0,
    InteractionActionV0,
    LocomotionActionV0,
)
from mc2p.backends.craftground import (
    CraftGroundBackendV0,
    action_to_craftground_v2,
    configure_craftground_observation_mode,
    observation_from_craftground,
    remaining_timeout_seconds,
)
from mc2p.backends.craftground_runtime import (
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    prepare_runtime_sandbox,
)
from mc2p.diagnostics.debug_frame import DebugFrameV0
from mc2p.contracts.report import FailureCodeV0
from mc2p.contracts.reset import ResetRequestV0
from tests.test_craftground_runtime import _write_source
from tests.observation_v2_fixtures import valid_payload_bytes


class _Descriptor:
    def __init__(self, name: str) -> None:
        self.name = name


class _Full(SimpleNamespace):
    def ListFields(self):
        return [(_Descriptor(name), object()) for name in self.present_fields]

    def SerializeToString(self) -> bytes:
        return self.serialized


class _Pov:
    shape = (2, 3, 3)
    dtype = "uint8"

    def tobytes(self, order: str = "C") -> bytes:
        del order
        raise AssertionError("formal observation must not read POV bytes")


class _DebugPov:
    shape = (2, 3, 3)
    dtype = "uint8"

    def tobytes(self, order: str = "C") -> bytes:
        if order != "C":
            raise AssertionError("debug frames must preserve row-major byte order")
        return bytes(range(18))


class _DebugSink:
    def __init__(self) -> None:
        self.frames: list[DebugFrameV0] = []

    def write(self, frame: DebugFrameV0) -> None:
        self.frames.append(frame)


class _ConversionEnvironment:
    def __init__(self) -> None:
        self.queued_commands = ["stale"]

    def convert_observation_v2(self, raw):
        frame = _DebugPov()
        raw.yaw = ((raw.yaw + 180) % 360) - 180
        self.queued_commands = []
        return {"full": raw, "pov": frame, "rgb": frame}


class _FakeSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class _FakeEnvironment:
    def __init__(self, port: int) -> None:
        self.ipc = SimpleNamespace(port=port, sock=_FakeSocket())
        self.process = None
        self.close_called = False

    def reset(self, *, seed: int):
        del seed
        return _raw_observation(), {}

    def close(self) -> None:
        self.close_called = True


def _varint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            return bytes(result)


def _raw_observation(
    *,
    present_fields: tuple[str, ...] = (),
    generation_id: int = 0,
    include_payload: bool = True,
):
    payload = valid_payload_bytes(
        generation_id=generation_id,
        sample_world_tick=100,
    )
    serialized = b""
    if include_payload:
        serialized = (
            _varint((50000 << 3) | 2)
            + _varint(len(payload))
            + payload
        )
    full = _Full(
        x=1.0,
        y=2.0,
        z=3.0,
        yaw=90.0,
        pitch=-10.0,
        world_time=100,
        is_on_ground=True,
        is_dead=False,
        health=20.0,
        food_level=18.0,
        present_fields=present_fields,
        serialized=serialized,
    )
    return {"full": full, "pov": _Pov()}


class CraftGroundActionMappingTests(unittest.TestCase):
    def test_complete_snapshot_maps_every_v2_field(self) -> None:
        action = ActionSnapshotV0(
            action_sequence_id=4,
            locomotion=LocomotionActionV0(
                forward=True,
                left=True,
                jump=True,
                sprint=True,
            ),
            camera=CameraActionV0(pitch_delta=-2.0, yaw_delta=15.0),
            interaction=InteractionActionV0(attack=True, use=False),
            hotbar=HotbarActionV0(selected_slot=2),
            gui=GuiActionV0(drop=True, inventory=False),
        )

        mapped = action_to_craftground_v2(action)

        self.assertEqual(len(mapped), 22)
        self.assertTrue(mapped["forward"])
        self.assertTrue(mapped["left"])
        self.assertTrue(mapped["jump"])
        self.assertTrue(mapped["sprint"])
        self.assertTrue(mapped["attack"])
        self.assertTrue(mapped["drop"])
        self.assertTrue(mapped["hotbar.2"])
        self.assertFalse(mapped["hotbar.1"])
        self.assertEqual(mapped["camera_pitch"], -2.0)
        self.assertEqual(mapped["camera_yaw"], 15.0)

    def test_neutral_mapping_releases_every_input(self) -> None:
        mapped = action_to_craftground_v2(ActionSnapshotV0.neutral(0))

        bool_values = [value for value in mapped.values() if type(value) is bool]
        self.assertEqual(len(bool_values), 20)
        self.assertFalse(any(bool_values))
        self.assertEqual(mapped["camera_pitch"], 0.0)
        self.assertEqual(mapped["camera_yaw"], 0.0)


class CraftGroundObservationMappingTests(unittest.TestCase):
    def test_structured_conversion_returns_only_full_without_reading_image(self) -> None:
        env = _ConversionEnvironment()
        raw = SimpleNamespace(yaw=190.0)

        monitor = configure_craftground_observation_mode(
            env,
            CraftGroundObservationModeV0.STRUCTURED_ONLY,
            debug_frame_sink=None,
        )
        converted = env.convert_observation_v2(raw)

        self.assertEqual(converted, {"full": raw})
        self.assertEqual(raw.yaw, -170.0)
        self.assertEqual(env.queued_commands, [])
        self.assertEqual(monitor.snapshot().observation_count, 1)
        self.assertEqual(monitor.snapshot().last_raw_keys, ("full",))

    def test_pov_debug_conversion_uses_only_the_explicit_sink(self) -> None:
        env = _ConversionEnvironment()
        sink = _DebugSink()
        raw = _raw_observation()["full"]
        monitor = configure_craftground_observation_mode(
            env,
            CraftGroundObservationModeV0.POV_DEBUG,
            debug_frame_sink=sink,
        )

        converted = env.convert_observation_v2(raw)
        snapshot = observation_from_craftground(
            converted,
            episode_id="episode-debug",
            sequence_id=0,
            request_sequence_id=None,
            request_started_at_monotonic_ns=100,
            received_at_monotonic_ns=100,
            controller_clock_id="controller-test",
        )

        self.assertEqual(tuple(converted), ("full",))
        self.assertEqual(len(sink.frames), 1)
        self.assertIsInstance(sink.frames[0], DebugFrameV0)
        self.assertEqual(sink.frames[0].payload, bytes(range(18)))
        self.assertNotIn("pov", json.dumps(asdict(snapshot)))
        diagnostics = monitor.snapshot()
        self.assertEqual(diagnostics.observation_count, 1)
        self.assertEqual(diagnostics.debug_frame_count, 1)
        self.assertEqual(diagnostics.last_raw_keys, ("full", "pov", "rgb"))
        with self.assertRaises(FrozenInstanceError):
            diagnostics.observation_count = 2

    def test_core_fields_map_without_reading_upstream_pov(self) -> None:
        snapshot = observation_from_craftground(
            _raw_observation(generation_id=2),
            episode_id="episode-1",
            sequence_id=2,
            request_sequence_id=1,
            request_started_at_monotonic_ns=100,
            received_at_monotonic_ns=110,
            controller_clock_id="controller-test",
        )

        self.assertEqual(snapshot.position.value.x, 1.0)
        self.assertEqual(snapshot.yaw_degrees.value, 90.0)
        self.assertEqual(snapshot.pitch_degrees.value, 0.0)
        self.assertEqual(snapshot.world_time_ticks.value, 100)
        self.assertFalse(hasattr(snapshot, "pov"))
        self.assertFalse(hasattr(snapshot, "rgb"))
        self.assertEqual(snapshot.request_sequence_id, 1)

    def test_privileged_fields_record_names_without_contents(self) -> None:
        snapshot = observation_from_craftground(
            _raw_observation(
                present_fields=(
                    "surrounding_blocks",
                    "height_info",
                    "x",
                )
            ),
            episode_id="episode-1",
            sequence_id=0,
            request_sequence_id=None,
            request_started_at_monotonic_ns=100,
            received_at_monotonic_ns=100,
            controller_clock_id="controller-test",
        )

        self.assertEqual(
            snapshot.privileged_fields_present,
            ("height_info", "surrounding_blocks"),
        )
        self.assertFalse(hasattr(snapshot, "surrounding_blocks"))

    def test_missing_client_payload_is_rejected_without_v1_fallback(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "field 50000"):
            observation_from_craftground(
                _raw_observation(include_payload=False),
                episode_id="episode-1",
                sequence_id=0,
                request_sequence_id=None,
                request_started_at_monotonic_ns=100,
                received_at_monotonic_ns=100,
                controller_clock_id="controller-test",
            )

    def test_missing_upstream_pov_does_not_change_formal_observation(self) -> None:
        raw = _raw_observation()
        del raw["pov"]

        snapshot = observation_from_craftground(
            raw,
            episode_id="episode-1",
            sequence_id=0,
            request_sequence_id=None,
            request_started_at_monotonic_ns=100,
            received_at_monotonic_ns=100,
            controller_clock_id="controller-test",
        )

        self.assertEqual(snapshot.schema_version, "mc2p.observation.v2")
        self.assertFalse(hasattr(snapshot, "pov"))


class DeadlineMappingTests(unittest.TestCase):
    def test_timeout_is_bounded_by_remaining_deadline_and_socket_cap(self) -> None:
        self.assertAlmostEqual(
            remaining_timeout_seconds(2_000_000_000, 1_000_000_000),
            1.0,
        )
        self.assertEqual(
            remaining_timeout_seconds(100_000_000_000, 0),
            30.0,
        )

    def test_expired_deadline_is_rejected_before_io(self) -> None:
        with self.assertRaises(TimeoutError):
            remaining_timeout_seconds(100, 100)

    def test_expired_reset_returns_structured_deadline_failure(self) -> None:
        backend = CraftGroundBackendV0(
            observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
            debug_frame_sink=_DebugSink(),
            clock_ns=lambda: 100,
        )

        result = backend.reset(
            ResetRequestV0(
                request_id="reset-1",
                episode_id="episode-1",
                scenario_id="flat-safe",
                seed=7,
                deadline_monotonic_ns=100,
            )
        )
        backend.close()

        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure.code, FailureCodeV0.DEADLINE_EXCEEDED)


class CraftGroundBackendConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        source = self.root / "source"
        source.mkdir()
        expected = _write_source(source)
        self.accelerated = prepare_runtime_sandbox(
            source_root=source,
            sandbox_parent=self.root / "sandboxes",
            sandbox_id="accelerated",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            expected_fingerprints=expected,
        )
        self.reference = prepare_runtime_sandbox(
            source_root=source,
            sandbox_parent=self.root / "sandboxes",
            sandbox_id="reference",
            clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
            expected_fingerprints=expected,
        )
        self.lockstep = prepare_runtime_sandbox(
            source_root=source,
            sandbox_parent=self.root / "sandboxes",
            sandbox_id="lockstep",
            clock_mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
            expected_fingerprints=expected,
        )
        self.pov_debug = prepare_runtime_sandbox(
            source_root=source,
            sandbox_parent=self.root / "sandboxes",
            sandbox_id="pov-debug",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
            expected_fingerprints=expected,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reference_mode_adds_only_the_fixed_tick_override(self) -> None:
        backend = CraftGroundBackendV0(
            runtime_env_path=self.reference.path,
            clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
        )

        self.assertEqual(
            backend.environment_guard_commands,
            ("difficulty peaceful", "gamemode creative", "tick rate 20"),
        )
        backend.close()

    def test_accelerated_mode_does_not_add_a_tick_override(self) -> None:
        backend = CraftGroundBackendV0(
            runtime_env_path=self.accelerated.path,
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
        )

        self.assertEqual(
            backend.environment_guard_commands,
            ("difficulty peaceful", "gamemode creative"),
        )
        backend.close()

    def test_reference_mode_requires_a_matching_explicit_sandbox(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "explicit sandbox"):
            CraftGroundBackendV0(
                clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
            )
        with self.assertRaisesRegex(ContractViolation, "clock mode"):
            CraftGroundBackendV0(
                runtime_env_path=self.accelerated.path,
                clock_mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
            )

    def test_lockstep_mode_requires_a_matching_explicit_sandbox(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "explicit sandbox"):
            CraftGroundBackendV0(
                clock_mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
            )
        with self.assertRaisesRegex(ContractViolation, "clock mode"):
            CraftGroundBackendV0(
                runtime_env_path=self.accelerated.path,
                clock_mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
            )

        backend = CraftGroundBackendV0(
            runtime_env_path=self.lockstep.path,
            clock_mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
        )
        backend.close()

    def test_explicit_sandbox_path_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "absolute"):
            CraftGroundBackendV0(runtime_env_path=Path("relative/runtime"))

    def test_structured_mode_requires_an_explicit_matching_sandbox(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "explicit sandbox"):
            CraftGroundBackendV0()
        with self.assertRaisesRegex(ContractViolation, "observation mode"):
            CraftGroundBackendV0(
                runtime_env_path=self.pov_debug.path,
                observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
            )

    def test_pov_debug_requires_an_explicit_debug_frame_sink(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "debug frame sink"):
            CraftGroundBackendV0(
                observation_mode=CraftGroundObservationModeV0.POV_DEBUG,
                debug_frame_sink=None,
            )

    def test_backend_has_no_public_arbitrary_command_escape_hatch(self) -> None:
        backend = CraftGroundBackendV0(runtime_env_path=self.accelerated.path)

        self.assertFalse(hasattr(backend, "add_command"))
        self.assertFalse(hasattr(backend, "send_commands"))
        self.assertFalse(hasattr(backend, "queue_command"))
        backend.close()

    def test_explicit_sandbox_is_passed_to_craftground(self) -> None:
        fake = _FakeEnvironment(8130)
        backend = CraftGroundBackendV0(
            port=8130,
            runtime_env_path=self.accelerated.path,
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            clock_ns=lambda: 100,
        )
        deadline = 10_000_000_000

        with patch("craftground.make", return_value=fake) as make:
            result = backend.reset(
                ResetRequestV0(
                    request_id="reset-1",
                    episode_id="episode-1",
                    scenario_id="flat-safe",
                    seed=7,
                    deadline_monotonic_ns=deadline,
                )
            )

        self.assertTrue(result.succeeded)
        self.assertEqual(make.call_args.kwargs["env_path"], str(self.accelerated.path))
        backend.close()

    def test_craftground_automatic_port_change_is_closed_and_rejected(self) -> None:
        fake = _FakeEnvironment(8131)
        backend = CraftGroundBackendV0(
            port=8130,
            runtime_env_path=self.accelerated.path,
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            clock_ns=lambda: 100,
        )

        with patch("craftground.make", return_value=fake):
            result = backend.reset(
                ResetRequestV0(
                    request_id="reset-1",
                    episode_id="episode-1",
                    scenario_id="flat-safe",
                    seed=7,
                    deadline_monotonic_ns=1_000_000_000,
                )
            )

        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure.code, FailureCodeV0.BACKEND_START)
        self.assertIn("requested port 8130", result.failure.message)
        self.assertTrue(fake.close_called)
        backend.close()


if __name__ == "__main__":
    unittest.main()
