"""D122-4 outer command, shared assertions and component-before-Fabric gate."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
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
            self.assertTrue(row["source_released"])
            self.assertTrue(row["prepared_frame_clear"])

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
        self.assertEqual(
            dependencies["NavigationObservationAdapter"].__module__,
            "mc2p.motion_nav.runtime_adapter",
        )
        self.assertIn("RuntimeNavigationDriverState", dependencies)
        self.assertIn("PreparedMotionJob", dependencies)

    def test_all_proof_dependencies_are_declared_observed_facts_in_every_direction(self):
        from scripts import d118_momentum_calibration_runtime as frozen
        from scripts import d120_momentum_entry_calibration as calibration
        for direction in range(4):
            fixture = calibration.build_fixture(direction)
            origin = (30, 40)
            feet_y = 5
            manifest = probe.fabric_dependency_manifest(
                direction, origin=origin, feet_y=feet_y,
            )
            required = set(manifest["facts"])
            requested = set(probe.fabric_observation_positions(
                fixture.trial, direction_index=direction,
                origin=origin, feet_y=feet_y,
            ))
            placed = {
                tuple(position)
                for row in frozen.trial_fixture_rows(
                    fixture.trial, origin=origin, feet_y=feet_y,
                )
                for position in row["positions"]
            }
            self.assertEqual(manifest["unknown_canonical_facts"], ())
            self.assertGreater(manifest["legacy_request_missing_count"], 0)
            self.assertEqual(required - requested, set())
            self.assertEqual(required - placed, set())
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
            self.assertEqual(reference["production_semantics"]["file_count"], 31)
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
