"""D122-4 outer command, shared assertions and component-before-Fabric gate."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest

from scripts import d122_fabric_timing as probe
from scripts import probe_fabric_deployment_observation as deployment


class D122FabricTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preflight = probe.component_preflight()

    def test_smoke_and_full_plans_are_exact_and_never_is_last_per_session(self):
        smoke = probe.build_trial_plan(full_matrix=False)
        full = probe.build_trial_plan(full_matrix=True)
        self.assertEqual([(row.direction_index, row.repetition, row.policy)
                          for row in smoke], [
            (0, 0, "timely_l1"),
            (0, 0, "late_l2"),
            (0, 0, "never_delivered"),
        ])
        self.assertEqual(len(full), 36)
        for offset in range(0, 36, 3):
            self.assertEqual([row.policy for row in full[offset:offset + 3]],
                             list(probe.POLICIES))

    def test_component_runs_the_same_outer_three_trial_contract_before_fabric(self):
        self.assertEqual(self.preflight["result"], "PASS", self.preflight)
        self.assertEqual(len(self.preflight["trials"]), 3)
        for row in self.preflight["trials"]:
            self.assertEqual(probe.validate_trial_result(row), ())
            self.assertTrue(row["prepared_frame_clear"])
            if row["policy"] == "never_delivered":
                self.assertFalse(row["source_released"])
                self.assertFalse(row["driver_terminal"])
                self.assertEqual(row["driver_state"], "unresolved")
            else:
                self.assertTrue(row["source_released"])
                self.assertTrue(row["driver_terminal"])

    def test_shared_validator_rejects_raw_failure_errors_and_wrong_terminal_state(self):
        rows = {row["policy"]: row for row in self.preflight["trials"]}
        mutations = []

        raw_failed = deepcopy(rows["timely_l1"])
        raw_failed["raw_case_passed"] = False
        mutations.append((raw_failed, "raw_component_case_failed"))

        timely_error = deepcopy(rows["timely_l1"])
        timely_error["error"] = {"type": "RuntimeError", "message": "unexpected"}
        mutations.append((timely_error, "unexpected_trial_error"))

        timely_driver = deepcopy(rows["timely_l1"])
        timely_driver["driver_state"] = "failed"
        mutations.append((timely_driver, "timely_driver_not_success"))

        timely_goal = deepcopy(rows["timely_l1"])
        timely_goal["goal_satisfied"] = False
        mutations.append((timely_goal, "timely_goal_not_satisfied"))

        late_error = deepcopy(rows["late_l2"])
        late_error["error"] = {"type": "RuntimeError", "message": "unexpected"}
        mutations.append((late_error, "unexpected_trial_error"))

        for row, expected in mutations:
            with self.subTest(expected=expected):
                self.assertIn(expected, probe.validate_trial_result(row))

    def test_never_requires_registered_transport_loss_and_attempted_window(self):
        row = deepcopy(next(row for row in self.preflight["trials"]
                            if row["policy"] == "never_delivered"))
        self.assertEqual(row["runtime_failure"],
                         probe.NEVER_COMPONENT_RUNTIME_FAILURE)
        self.assertEqual(row["runtime_state"], "failed")
        self.assertEqual(row["driver_state"], "unresolved")
        self.assertFalse(row["source_released"])
        self.assertFalse(row["driver_terminal"])
        self.assertEqual(probe.validate_trial_result(row), ())

        # Fabric's TimeoutError is converted by PlayerRuntimeV1.step into a
        # typed failed RuntimeStepResult. It does not escape to the runner.
        row["backend"] = "fabric"
        row["runtime_state"] = "failed"
        row["runtime_failure"] = dict(probe.NEVER_FABRIC_RUNTIME_FAILURE)
        row["error"] = None
        self.assertEqual(probe.validate_trial_result(row), ())

        mutations = []
        wrong_failure = deepcopy(row)
        wrong_failure["runtime_failure"]["message"] = "other"
        mutations.append((wrong_failure, "unexpected_runtime_failure"))
        escaped_error = deepcopy(row)
        escaped_error["error"] = {"type": "TimeoutError", "message": "injected"}
        mutations.append((escaped_error, "unexpected_trial_error"))
        reached = deepcopy(row)
        reached["successor_reached_client"] = True
        mutations.append((reached, "never_reached_client"))
        no_attempt = deepcopy(row)
        no_attempt["attempted_successor"] = None
        mutations.append((no_attempt, "never_attempted_successor_missing"))
        wrong_window = deepcopy(row)
        wrong_window["attempted_successor"]["first_effect_window"]["latest_tick"] += 1
        mutations.append((wrong_window, "never_attempted_successor_window_differs"))
        for changed, expected in mutations:
            with self.subTest(expected=expected):
                self.assertIn(expected, probe.validate_trial_result(changed))

    def test_player_runtime_converts_fabric_style_timeout_to_typed_failure(self):
        from scripts import d122_formal_timing_probe as formal
        raw = formal.run_case("never_delivered", raise_never_timeout=True)
        self.assertIsNone(raw["error"])
        self.assertEqual(raw["runtime_state"], "failed")
        self.assertEqual(raw["runtime_failure"],
                         probe.NEVER_FABRIC_RUNTIME_FAILURE)
        self.assertEqual(raw["transport_events"][0]["attempted_request"][
            "first_effect_window"], {"earliest_tick": 27, "latest_tick": 27})

    def test_runtime_terminal_body_reads_typed_self_state_vec3(self):
        import inspect
        from unittest.mock import patch
        from mc2p.contracts.observation_v2 import SelfStateV2
        from mc2p.contracts.reset import ResetRequestV0
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        from tests.sim.backend import CalculatorBackend, Scene
        from tests.test_player_runtime import _RecordingTrace

        clock = [time.perf_counter_ns()]
        backend = CalculatorBackend(
            clock,
            Scene({(0, 63, 0): "minecraft:stone"},
                  ((-1, 1), (62, 66), (-1, 1))),
            (.5, 64., .5),
            action_timing_v2=True,
        )
        runtime = PlayerRuntimeV1(
            backend, _RecordingTrace(), clock_ns=time.perf_counter_ns,
        )
        try:
            self.assertTrue(runtime.reset(ResetRequestV0(
                "reset", backend.episode, "d122-terminal-body", 1,
                time.perf_counter_ns() + 30_000_000_000,
            )).succeeded)
            current = runtime.observation.self_state.value
            self.assertIs(type(current), SelfStateV2)
            with self.assertRaises(AttributeError):
                current.velocity_blocks_per_second

            terminal = probe._runtime_terminal_payload(
                runtime.observation, 20.,
            )
            row = probe._merge_runtime_terminal_payload(
                {"marker": "same-final-row-path"}, terminal,
            )
            self.assertIs(type(terminal), probe.RuntimeTerminalPayload)
            self.assertEqual(row["final_body"], {
                "position": [.5, 64., .5],
                "velocity": [0., -0.0784, 0.],
                "on_ground": True,
                "evidence": "runtime_observation",
            })
            self.assertEqual(row["health_evidence"], {
                "source": "runtime_observation",
                "decreased": False,
            })
            self.assertEqual(
                row["final_observation_sequence_id"],
                runtime.observation.sequence_id,
            )
            self.assertEqual(
                row["final_movement_tick_id"],
                current.movement_tick_id,
            )
            with self.assertRaises(TypeError):
                probe.RuntimeTerminalPayload(
                    final_body=terminal.final_body,
                    health_evidence=terminal.health_evidence,
                    movement_tick_id=terminal.movement_tick_id,
                )
            with self.assertRaises(TypeError):
                probe.RuntimeTerminalPayload(
                    final_body=terminal.final_body,
                    health_evidence=terminal.health_evidence,
                    final_observation_sequence_id=(
                        terminal.observation_sequence_id
                    ),
                    movement_tick_id=terminal.movement_tick_id,
                )
            with patch.object(
                probe.RuntimeTerminalPayload, "row_fields",
                return_value={
                    "final_body": terminal.final_body,
                    "health_evidence": terminal.health_evidence,
                    "final_movement_tick_id": terminal.movement_tick_id,
                },
            ):
                with self.assertRaises(RuntimeError):
                    probe._merge_runtime_terminal_payload({}, terminal)
            with patch.object(
                probe.RuntimeTerminalPayload, "row_fields",
                return_value={
                    "final_body": terminal.final_body,
                    "health_evidence": terminal.health_evidence,
                    "final_observation_id": terminal.observation_sequence_id,
                    "final_movement_tick_id": terminal.movement_tick_id,
                },
            ):
                with self.assertRaises(RuntimeError):
                    probe._merge_runtime_terminal_payload({}, terminal)
            with self.assertRaises(RuntimeError):
                probe._merge_runtime_terminal_payload({
                    "final_observation_sequence_id": (
                        terminal.observation_sequence_id
                    ),
                }, terminal)

            runner_source = inspect.getsource(probe.run_fabric_runtime)
            self.assertEqual(
                runner_source.count("_merge_runtime_terminal_payload("), 1,
            )
            self.assertNotIn("current", runner_source)
            self.assertNotIn(
                "velocity_blocks_per_second",
                inspect.getsource(probe._runtime_terminal_payload),
            )
            self.assertNotIn(
                "velocity_blocks_per_second",
                inspect.getsource(probe.RuntimeTerminalPayload.row_fields),
            )
            for field in probe._RUNTIME_TERMINAL_ROW_FIELDS:
                self.assertNotIn(f'"{field}"', runner_source)
        finally:
            runtime.close()

    def test_never_requires_parent_physics_terminal_and_fresh_health_evidence(self):
        row = deepcopy(next(row for row in self.preflight["trials"]
                            if row["policy"] == "never_delivered"))
        no_body = deepcopy(row)
        no_body["final_body"] = None
        self.assertIn("fallback_terminal_not_safe",
                      probe.validate_trial_result(no_body))
        no_health = deepcopy(row)
        no_health["health_evidence"] = None
        self.assertIn("health_evidence_missing",
                      probe.validate_trial_result(no_health))

        physics = [
            {"request_sequence_id": 4, "input_state": "default_active",
             "movement_tick_id": 20,
             "post_state": {"position": {"x": 1., "y": 2., "z": 3.},
                            "velocity": {"x": .2, "y": 0., "z": .1},
                            "on_ground": False}},
            {"request_sequence_id": 5, "input_state": "default_active",
             "movement_tick_id": 21,
             "post_state": {"position": {"x": 9., "y": 9., "z": 9.},
                            "velocity": {"x": 0., "y": 0., "z": 0.},
                            "on_ground": True}},
            {"request_sequence_id": 4, "input_state": "default_active",
             "movement_tick_id": 22,
             "post_state": {"position": {"x": 1., "y": 2., "z": 4.},
                            "velocity": {"x": 0., "y": -.0784, "z": 0.},
                            "on_ground": True}},
        ]
        terminal = probe._parent_terminal_body(physics, 4)
        self.assertEqual(terminal["request_sequence_id"], 4)
        self.assertEqual(terminal["movement_tick_id"], 22)
        self.assertEqual(terminal["position"], [1., 2., 4.])

    def test_duration_and_timing_events_are_typed_and_request_owned(self):
        rows = {row["policy"]: row for row in self.preflight["trials"]}
        for invalid in (0, -1, True, 1.5, None):
            changed = deepcopy(rows["late_l2"])
            changed["default_duration_ticks"] = invalid
            with self.subTest(duration=invalid):
                self.assertIn("default_duration_not_positive_int",
                              probe.validate_trial_result(changed))

        armed = deepcopy(rows["timely_l1"])
        armed["fallback_armed_event"]["request_sequence_id"] += 1
        self.assertIn("fallback_armed_not_owned_by_parent",
                      probe.validate_trial_result(armed))

        missed = deepcopy(rows["late_l2"])
        missed["window_missed_event"]["request_sequence_id"] += 1
        self.assertIn("window_missed_not_owned_by_successor",
                      probe.validate_trial_result(missed))

    def test_fabric_command_uses_real_transport_diagnostics_and_one_mode(self):
        command = probe.fabric_probe_command(server_port=25597, ipc_port=8140)
        self.assertIn("--d122-client-timing-probe", command)
        self.assertIn("--physics-tick-diagnostics", command)
        self.assertIn("--ftick-diagnostics", command)
        self.assertNotIn("--d122-full-matrix", command)
        full = probe.fabric_probe_command(server_port=25597, ipc_port=8140,
                                          full_matrix=True)
        self.assertIn("--d122-full-matrix", full)

    def test_frozen_source_set_contains_the_shared_trial_and_java_timing_owner(self):
        sources = deployment.frozen_deployment_sources(
            b03_fixed_route_probe=False, d122_client_timing_probe=True,
        )
        self.assertIn("scripts/d122_fabric_timing.py", sources)
        self.assertIn(
            "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java",
            sources,
        )

    def test_live_physics_reader_ignores_an_unfinished_tail(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            directory.mkdir(exist_ok=True)
            (directory / "segment-00000000.jsonl").write_text(
                '{"movement_tick_id":27,"input_state":"default_active"}\n{"movement_tick_id":',
                encoding="utf-8",
            )
            self.assertEqual(probe._live_physics_rows(directory), ({
                "movement_tick_id": 27, "input_state": "default_active",
            },))

    def test_fabric_runner_resolves_every_lazy_dependency_without_starting_minecraft(self):
        dependencies = probe.resolve_fabric_runner_dependencies()
        self.assertNotIn("NavigationObservationAdapter", dependencies)
        self.assertIn("RuntimeNavigationDriverState", dependencies)
        self.assertIn("MotionSolverWorker", dependencies)

    def test_prepared_expected_identity_is_bound_outside_backend_step(self):
        from types import SimpleNamespace
        from mc2p.contracts.action_v1 import (
            ActionSnapshotV1, MovementTickWindowV1, MovementV1,
        )
        from mc2p.contracts.motion_activation import ActivationStatus

        movement = MovementV1(forward=1, jump=True, sprint=True)
        window = MovementTickWindowV1(27, 27)
        proposal = SimpleNamespace(
            proof_token="proof", generation_id="generation",
            owner_id="owner", ledger_id="ledger", world_session_id="world",
            movement_intent_id="intent", expected_movement=movement,
            requested_window=window, handoff_token="handoff",
        )
        action = ActionSnapshotV1(
            "episode", 19, 18, time.perf_counter_ns() + 1_000_000_000,
            movement=movement, first_effect_window=window,
        )
        receipt = SimpleNamespace(
            episode_id="episode", request_sequence_id=19, generation_id=20,
        )
        result = SimpleNamespace(
            decision=SimpleNamespace(action=action),
            backend_result=SimpleNamespace(receipt=receipt),
            stream_activation=SimpleNamespace(
                status=ActivationStatus.ACTIVATED,
            ),
        )
        prepared = probe._prepared_successor_identity(proposal)
        expected = probe._bind_expected_successor(prepared, result)
        self.assertEqual(expected, {
            "episode_id": "episode", "request_sequence_id": 19,
            "observation_sequence_id": 18,
            "first_effect_window": {"earliest_tick": 27, "latest_tick": 27},
            "prepared_generation_id": "generation",
            "receipt_generation_id": 20,
            "movement": {"forward": 1, "strafe": 0, "jump": True,
                         "sneak": False, "sprint": True},
            "look": {"yaw_delta_degrees": 0.0, "pitch_delta_degrees": 0.0},
            "valid_for_ticks": 1, "safe_default": None,
            "proof_token": "proof", "owner_id": "owner",
            "ledger_id": "ledger", "world_session_id": "world",
            "movement_intent_id": "intent", "handoff_token": "handoff",
        })
        wrong_receipt = SimpleNamespace(
            decision=result.decision,
            backend_result=SimpleNamespace(receipt=SimpleNamespace(
                episode_id="episode", request_sequence_id=99,
                generation_id=20,
            )),
            stream_activation=result.stream_activation,
        )
        with self.assertRaisesRegex(RuntimeError, "receipt identity"):
            probe._bind_expected_successor(prepared, wrong_receipt)
        not_selected = SimpleNamespace(
            decision=result.decision,
            backend_result=result.backend_result,
            stream_activation=SimpleNamespace(
                status=ActivationStatus.NOT_SELECTED,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "was not activated"):
            probe._bind_expected_successor(prepared, not_selected)
        source = __import__("inspect").getsource(probe._FabricExecutionCase.tick)
        self.assertLess(
            source.index("_prepared_successor_identity"),
            source.index("advance_prepared"),
        )

    def test_component_runs_the_exact_fabric_pretrial_assembly_after_fixture_steps(self):
        from dataclasses import replace
        from mc2p.contracts.common import ContractViolation
        from mc2p.contracts.observation_request_v3 import ObservationRequestV3
        from mc2p.contracts.reset import ResetRequestV0
        from mc2p.motion_nav.motion_worker import PreparedMotionJob
        from mc2p.motion_nav.online_motion import CandidateExecutionWindow
        from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
        from scripts import d120_momentum_entry_calibration as calibration
        from tests.motion_nav.test_d121_successor_binding import (
            RecordingProcessWorker, fixed_case,
        )
        from tests.sim.backend import CalculatorBackend, Scene
        from tests.sim.runner import seed_memory
        from tests.test_player_runtime import _RecordingTrace

        for direction_index in range(4):
            with self.subTest(direction=direction_index):
                _request, fixture, template, _spec, _snapshot = fixed_case(
                    direction_index,
                )
                known = probe.known_fixture_map(
                    fixture.trial,
                    origin=fixture.context.origin,
                    feet_y=fixture.context.feet_y,
                )
                solids = {
                    position: geometry.material_key
                    for position, geometry in known["blocks"].items()
                }
                start_x, start_z = calibration.frozen._local_to_world(
                    fixture.context.origin, direction_index,
                    fixture.trial.start_local[0], fixture.trial.start_local[2],
                )
                clock = [time.perf_counter_ns()]
                backend = CalculatorBackend(
                    clock,
                    Scene(solids, ((-14, 14), (60, 70), (-14, 14))),
                    (start_x, float(fixture.context.feet_y), start_z),
                    action_timing_v2=True,
                )
                backend.state = replace(
                    template.entry_state,
                    session=backend._truth_session,
                    position=(start_x, float(fixture.context.feet_y), start_z),
                )
                backend.movement_tick = 0
                runtime = PlayerRuntimeV1(
                    backend, _RecordingTrace(), clock_ns=time.perf_counter_ns,
                )
                class NeverPublishingWorker(RecordingProcessWorker):
                    def __init__(self):
                        self.submit_movement_ticks = []
                        super().__init__()

                    def submit(self, job):
                        accepted = super().submit(job)
                        if accepted:
                            self.submit_movement_ticks.append(
                                backend.movement_tick,
                            )
                        return accepted

                    def poll_available(self):
                        return ()

                self.assertTrue(runtime.reset(ResetRequestV0(
                    "reset", backend.episode,
                    f"d122-pretrial-{direction_index}", 1,
                    time.perf_counter_ns() + 30_000_000_000,
                )).succeeded)
                seed_memory(runtime, backend.scene)
                deadline = time.perf_counter_ns() + 30_000_000_000
                request = ObservationRequestV3("navigation_v1", ())
                prewarm = probe.FabricExecutionPrewarm.create(
                    direction_index,
                    worker_factory=lambda **_kwargs: NeverPublishingWorker(),
                )
                for _ in range(6):
                    probe._fixture_action_step(
                        runtime, backend.episode,
                        f"d122-pretrial-fixture-{direction_index}",
                        deadline, request,
                    )

                case = None
                try:
                    case, first_result = probe._FabricExecutionCase.bind_and_start(
                        runtime, backend, backend.episode,
                        prewarm=prewarm,
                        origin=fixture.context.origin,
                        feet_y=fixture.context.feet_y,
                        deadline_ns=deadline,
                    )
                    self.assertIsNotNone(first_result.decision)
                    self.assertGreater(case.assembly.start_tick, 1)
                    self.assertEqual(
                        case.assembly.start_tick,
                        case.anchor.movement_tick_id + 1,
                    )
                    parent_action = first_result.decision.action
                    self.assertEqual(parent_action.movement.forward, 1)
                    self.assertTrue(parent_action.movement.sprint)
                    self.assertEqual(
                        parent_action.first_effect_window.earliest_tick,
                        case.assembly.start_tick,
                    )
                    self.assertEqual(
                        parent_action.first_effect_window.latest_tick,
                        case.assembly.start_tick,
                    )
                    self.assertTrue(
                        case.session._motion_result_inbox.wait_for_receiver(
                            timeout=10,
                        ),
                    )
                    job_deadline = time.monotonic() + 10.
                    while not case.worker.jobs and time.monotonic() < job_deadline:
                        time.sleep(.001)
                    prepared_jobs = tuple(
                        job for job in case.worker.jobs
                        if type(job) is PreparedMotionJob
                    )
                    self.assertEqual(len(prepared_jobs), 1)
                    self.assertGreaterEqual(
                        case.worker.submit_movement_ticks[0],
                        case.assembly.start_tick,
                    )
                    prepared_job = prepared_jobs[0]
                    self.assertEqual(
                        prepared_job.request.async_identity,
                        case.coordinator._prepared_fields.async_identity,
                    )
                    self.assertEqual(
                        prepared_job.request.action_revision,
                        prepared_job.request.async_identity.revision,
                    )
                    self.assertEqual(
                        case.context.candidate_revision,
                        case.assembly.candidate_revision,
                    )
                    self.assertEqual(
                        case.identity.revision,
                        case.assembly.candidate_revision,
                    )
                    self.assertEqual(
                        case.proof.world_dependencies,
                        case.assembly.replay_dependencies,
                    )
                    self.assertIs(case.source, runtime.snapshot_world_port)
                    assembly = case.assembly
                    self.assertEqual(assembly.request.direction, _request.direction)
                    self.assertEqual(
                        assembly.fixture.context.origin,
                        fixture.context.origin,
                    )
                    self.assertEqual(
                        assembly.fixture.context.feet_y,
                        fixture.context.feet_y,
                    )

                    if direction_index == 0:
                        mutations = (
                            lambda: replace(
                                assembly,
                                anchor_position=(
                                    assembly.anchor_position[0] + 1.0,
                                    *assembly.anchor_position[1:],
                                ),
                            ),
                            lambda: replace(
                                assembly,
                                request=replace(
                                    assembly.request,
                                    execution_window=CandidateExecutionWindow(
                                        assembly.start_tick + 1,
                                        assembly.start_tick + 1,
                                    ),
                                ),
                            ),
                            lambda: replace(
                                assembly.proof,
                                trajectory=assembly.proof.trajectory[1:],
                            ),
                            lambda: replace(
                                assembly,
                                anchor_movement_tick_id=(
                                    assembly.anchor_movement_tick_id + 1
                                ),
                            ),
                            lambda: replace(assembly, replay_dependencies=()),
                            lambda: replace(assembly, candidate_revision=2),
                        )
                        for index, mutation in enumerate(mutations):
                            with self.subTest(mutation=index):
                                with self.assertRaises(
                                    (RuntimeError, ContractViolation),
                                ):
                                    mutation()
                    close_report = case.close()
                    self.assertEqual(close_report["worker"]["live_workers"], 0)
                    self.assertEqual(close_report["worker"]["live_queue_feeders"], 0)
                    self.assertFalse(close_report["worker"]["timed_out"])
                    self.assertEqual(close_report["session"]["live_receivers"], 0)
                finally:
                    if case is not None:
                        case.close()
                    else:
                        prewarm.close()
                    runtime.close()

    def test_all_proof_dependencies_are_in_the_known_fixture_map_in_every_direction(self):
        from scripts import d120_momentum_entry_calibration as calibration
        for direction in range(4):
            fixture = calibration.build_fixture(direction)
            origin = (30, 40)
            feet_y = 5
            manifest = probe.fabric_dependency_manifest(
                direction, origin=origin, feet_y=feet_y,
            )
            required = set(manifest["facts"])
            known = probe.known_fixture_map(
                fixture.trial, origin=origin, feet_y=feet_y,
            )
            provided = set(known["blocks"]) | set(known["air"])
            self.assertEqual(manifest["unknown_canonical_facts"], ())
            self.assertGreater(manifest["legacy_request_missing_count"], 0)
            self.assertEqual(required - provided, set())
            self.assertTrue({"parent", "successor", "fallback"}
                            <= set(manifest["categories"]))

    def test_dependency_precheck_reports_unknown_instead_of_treating_it_as_air(self):
        from mc2p.motion_nav.world_model import CellFact, CellKnowledge
        from tests.motion_nav.test_d121_successor_binding import fixed_case
        _request, _fixture, _template, _spec, snapshot = fixed_case(0)
        manifest = probe.fabric_dependency_manifest(0, origin=(0, 0), feet_y=64)
        missing = next(position for position, expected in manifest["facts"].items()
                       if expected["knowledge"] == "air")

        class MaskedWorld:
            def cell(self, position):
                fact = snapshot.cell(position)
                return (CellFact(CellKnowledge.UNKNOWN)
                        if position == missing else fact)

        result = probe.inspect_fabric_dependency_knowledge(MaskedWorld(), manifest)
        self.assertEqual(result["known_count"], result["required_count"] - 1)
        self.assertEqual(result["unknown"], (missing,))
        self.assertEqual(result["mismatched"], ())

    def test_next_fabric_run_binds_all_frozen_production_semantics(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "preflight.json"
            binding = probe._production_semantic_binding()
            path.write_text(json.dumps({
                "schema": "mc2p.d122-fabric-preflight.v2",
                "status": "PASS",
                "production_semantics": binding,
            }) + "\n", encoding="utf-8")
            reference = probe.preflight_reference(path)
            self.assertEqual(reference["status"], "PASS")
            self.assertEqual(len(reference["sha256"]), 64)
            self.assertEqual(reference["production_semantics"]["file_count"], 32)
            self.assertTrue(reference["production_semantics"]["record"].endswith(
                "d124-proof-dependency-projection-v1/production-semantic-hashes.json"))
            self.assertTrue(reference["production_semantics"]["previous_record"].endswith(
                "d122-3/production-semantic-hashes.json"))

            changed = json.loads(path.read_text("utf-8"))
            changed["production_semantics"]["files"].pop(next(iter(
                changed["production_semantics"]["files"])))
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "current production chain"):
                probe.preflight_reference(path)

    def test_v1_v8_history_checks_boundaries_and_trial_counts_not_only_hashes(self):
        rows = probe.d122_smoke_history_rows()
        self.assertEqual(probe.validate_d122_smoke_history_rows(rows), ())
        with tempfile.TemporaryDirectory() as root:
            exported_rows = probe.d122_smoke_history_rows(
                artifacts_root=Path(root) / "artifacts-unavailable",
            )
        self.assertEqual(exported_rows, rows)
        for field, value, expected in (
            ("status", "captured", "v8_launch_boundary_differs"),
            ("source_run", "replacement", "v8_source_run_differs"),
            ("trial_count", 1, "v8_trial_count_differs"),
        ):
            changed = deepcopy(rows)
            changed[-1][field] = value
            with self.subTest(field=field):
                self.assertIn(expected,
                              probe.validate_d122_smoke_history_rows(changed))


if __name__ == "__main__":
    unittest.main()
