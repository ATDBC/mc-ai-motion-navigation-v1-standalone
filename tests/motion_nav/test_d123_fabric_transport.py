"""D123-3 product harness stays shared across component and Fabric backends."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementTickWindowV1, MovementV1
from scripts import d122_fabric_timing as d122
from scripts import probe_fabric_deployment_observation as deployment
from scripts import d123_fabric_transport as probe


class D123FabricTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smoke = probe.component_preflight(full_matrix=False)
        cls.matrix = probe.component_preflight(full_matrix=True)

    def _fabric_rows(self, rows):
        result = deepcopy(list(rows))
        for row in result:
            row["backend"] = "fabric"
            row["dependency_precheck"] = {
                "required_count": 58, "known_count": 58,
                "unknown": [], "mismatched": [],
            }
            row["known_map"] = {
                "source": "test_oracle_fixture_map",
                "same_runtime_owner": True,
            }
            row["resource_close"] = {
                "session": {
                    "live_workers": 0, "live_receivers": 0,
                    "live_lease_returners": 0, "live_queue_feeders": 0,
                    "open_handles": 0, "unexpected_feeder_errors": 0,
                    "timed_out": False,
                },
                "worker": {
                    "live_workers": 0, "live_queue_feeders": 0,
                    "unexpected_feeder_errors": 0, "timed_out": False,
                },
            }
            if row["state"] == "remote_rejected_before_sampling":
                row["successor_expected_identity"] = {
                    "episode_id": row["successor_episode_id"],
                    "request_sequence_id": row["successor_request_sequence_id"],
                    "observation_sequence_id": (
                        row["successor_observation_sequence_id"]
                    ),
                    "first_effect_window": row["successor_window"],
                    "receipt_generation_id": row["successor_generation_id"],
                    "prepared_generation_id": "prepared-component",
                    "proof_token": "proof", "owner_id": "owner",
                    "ledger_id": "ledger", "world_session_id": "world",
                    "movement_intent_id": "movement",
                    "handoff_token": "handoff",
                    "movement": {"forward": 1, "strafe": 0,
                                 "jump": True, "sneak": False,
                                 "sprint": True},
                    "look": {"yaw_delta_degrees": 0.,
                             "pitch_delta_degrees": 0.},
                    "valid_for_ticks": 1, "safe_default": None,
                }
                row["transport_events"].append({
                    "kind": "remote_full_request_rejection_injected",
                    "label": "d123_windowed_operation_denial",
                    "expected_receipt_reason": "windowed_operation_unsupported",
                    "changed_fields": ["operation"],
                    "preserved_fields": {"all": True},
                    "action_identity": row["successor_expected_identity"],
                })
        return result

    @staticmethod
    def _arm_successor(backend, action, generation="prepared-successor-1"):
        backend.arm_d122_successor(SimpleNamespace(
            proof_token="proof", generation_id=generation, owner_id="owner",
            ledger_id="ledger", world_session_id="world",
            movement_intent_id="movement", expected_movement=action.movement,
            expected_yaw_radians=0., requested_window=action.first_effect_window,
            handoff_token="handoff",
        ))

    def test_smoke_and_matrix_use_the_exact_three_product_states(self):
        smoke = probe.build_trial_plan(full_matrix=False)
        matrix = probe.build_trial_plan(full_matrix=True)
        self.assertEqual(
            [(row.direction_index, row.repetition, row.state) for row in smoke],
            [
                (0, 0, "timely"),
                (0, 0, "late_window_rejected"),
                (0, 0, "remote_rejected_before_sampling"),
            ],
        )
        self.assertEqual(len(matrix), 36)
        self.assertEqual(
            {(row.direction_index, row.repetition) for row in matrix},
            {(direction, repetition) for direction in range(4) for repetition in range(3)},
        )
        for offset in range(0, 36, 3):
            self.assertEqual(
                [row.state for row in matrix[offset:offset + 3]],
                list(probe.STATES),
            )
        self.assertNotIn("local_not_dispatched", {row.state for row in matrix})

    def test_component_smoke_and_matrix_pass_the_shared_product_validator(self):
        self.assertEqual(self.smoke["result"], "PASS", self.smoke)
        self.assertEqual(len(self.smoke["trials"]), 3)
        self.assertEqual(self.matrix["result"], "PASS", self.matrix)
        self.assertEqual(len(self.matrix["trials"]), 36)
        for row in (*self.smoke["trials"], *self.matrix["trials"]):
            self.assertEqual(probe.validate_trial_result(row), ())
            self.assertTrue(row["source_released"])
            self.assertTrue(row["driver_terminal"])
            self.assertEqual(row["runtime_state"], "ready")

    def test_remote_rejection_binds_identity_window_generation_and_zero_inputs(self):
        row = deepcopy(next(
            row for row in self.smoke["trials"]
            if row["state"] == "remote_rejected_before_sampling"
        ))
        proof = row["not_applied_proof"]
        self.assertEqual(proof["kind"], "remote_rejected_before_sampling")
        self.assertEqual(proof["episode_id"], row["successor_episode_id"])
        self.assertEqual(proof["request_sequence_id"], row["successor_request_sequence_id"])
        self.assertEqual(proof["generation_id"], row["successor_generation_id"])
        self.assertEqual(
            [proof["earliest_effect_tick"], proof["latest_effect_tick"]],
            [row["successor_window"]["earliest_tick"],
             row["successor_window"]["latest_tick"]],
        )
        self.assertEqual(proof["input_applications"], 0)
        self.assertEqual(proof["reason"], "windowed_operation_unsupported")
        self.assertEqual(row["remote_receipt_reason"], "windowed_operation_unsupported")
        self.assertNotEqual(row["remote_receipt_status"], "operation_rejected")
        self.assertEqual(row["successor_jump_sample_ticks"], [])
        self.assertEqual(row["default_sample_count"], row["default_duration_ticks"])

        mutations = []
        operation_only = deepcopy(row)
        operation_only["remote_receipt_status"] = "operation_rejected"
        mutations.append((operation_only, "remote_rejection_not_full_request"))
        sampled = deepcopy(row)
        sampled["not_applied_proof"]["input_applications"] = 1
        mutations.append((sampled, "remote_rejection_has_input_applications"))
        wrong_generation = deepcopy(row)
        wrong_generation["not_applied_proof"]["generation_id"] += 1
        mutations.append((wrong_generation, "remote_rejection_generation_differs"))
        wrong_reason = deepcopy(row)
        wrong_reason["not_applied_proof"]["reason"] = "window_missed"
        mutations.append((wrong_reason, "remote_rejection_reason_not_independent"))
        old_observation = deepcopy(row)
        old_observation["final_observation_sequence_id"] = (
            old_observation["successor_observation_sequence_id"]
        )
        mutations.append((old_observation, "post_default_observation_not_fresh"))
        dropped = deepcopy(row)
        dropped["raw_successor_receipt"]["dropped_input_samples"] = 1
        mutations.append((dropped, "raw_receipt_dropped_inputs"))
        applied = deepcopy(row)
        applied["raw_successor_receipt"]["input_applications"].append({
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": row["successor_window"]["earliest_tick"],
            "episode_id": row["successor_episode_id"],
            "request_sequence_id": row["successor_request_sequence_id"],
            "sampled_at_jvm_ns": 1,
            "state": "leased", "forward": 1.0, "strafe": 0.0,
            "jump": True, "sneak": False, "sprint": True,
        })
        mutations.append((applied, "raw_receipt_has_target_application"))
        window_applied = deepcopy(row)
        window_applied_event = next(
            item for item in window_applied["raw_successor_receipt"]["timing_events"]
            if item["status"] == "not_applied"
            and item["request_sequence_id"] == row["successor_request_sequence_id"]
        )
        window_applied_event["status"] = "window_applied"
        mutations.append((window_applied, "raw_receipt_has_window_applied"))
        wrong_causal = deepcopy(row)
        wrong_causal["not_applied_proof"]["causal_movement_tick"] += 1
        mutations.append((wrong_causal, "raw_receipt_causal_tick_differs"))
        duplicate_not_applied = deepcopy(row)
        duplicate_event = next(
            item for item in duplicate_not_applied["raw_successor_receipt"]["timing_events"]
            if item["movement_tick_id"] == row["successor_window"]["earliest_tick"]
            and item["request_sequence_id"] != row["successor_request_sequence_id"]
        )
        duplicate_event.update(deepcopy(row["not_applied_event"]))
        mutations.append((duplicate_not_applied,
                          "raw_receipt_not_applied_count_differs"))
        for field in ("input_applications", "timing_events"):
            for malformed in (None, "malformed", {"bad": "batch"}, [{"bad": "item"}]):
                invalid_batch = deepcopy(row)
                invalid_batch["raw_successor_receipt"][field] = malformed
                mutations.append((invalid_batch, "raw_successor_receipt_invalid"))
        for changed, expected in mutations:
            with self.subTest(expected=expected):
                self.assertIn(expected, probe.validate_trial_result(changed))

        fabric = self._fabric_rows([row])[0]
        targeted_parent = deepcopy(fabric)
        injection = next(
            item for item in targeted_parent["transport_events"]
            if item.get("kind") == "remote_full_request_rejection_injected"
        )
        injection["action_identity"] = {
            **injection["action_identity"],
            "request_sequence_id": targeted_parent["parent_request_sequence_id"],
        }
        self.assertIn(
            "remote_rejection_injection_identity_differs",
            probe.validate_trial_result(targeted_parent),
        )

    def test_negative_states_require_complete_default_fresh_health_and_release(self):
        for state in ("late_window_rejected", "remote_rejected_before_sampling"):
            row = deepcopy(next(row for row in self.smoke["trials"] if row["state"] == state))
            self.assertEqual(row["default_sample_count"], row["default_duration_ticks"])
            self.assertGreater(
                row["final_observation_sequence_id"],
                row["successor_observation_sequence_id"],
            )
            self.assertFalse(row["health_evidence"]["decreased"])
            self.assertTrue(row["source_released"])
            for field, value, expected in (
                ("default_sample_count", 0, "default_duration_incomplete"),
                ("source_released", False, "source_not_released"),
            ):
                changed = deepcopy(row)
                changed[field] = value
                with self.subTest(state=state, expected=expected):
                    self.assertIn(expected, probe.validate_trial_result(changed))

    def test_explicit_rejection_injects_a_real_remote_denial_without_faking_receipt(self):
        backend = object.__new__(probe.D123FabricBehaviorBackend)
        backend._d123_state = "remote_rejected_before_sampling"
        backend.d123_injections = []
        backend.d122_transport_events = []
        backend.d122_steps = []
        action = ActionSnapshotV1(
            "episode", 7, 6, 9_000_000_000,
            movement=MovementV1(forward=1, jump=True),
            first_effect_window=MovementTickWindowV1(27, 27),
        )
        captured = {}
        self._arm_successor(backend, action)

        def parent_step(_self, sent, deadline, *, observation_request=None):
            captured["action"] = sent
            backend.d122_steps.append({
                "action_identity": {
                    **backend._action_identity(
                        sent, prepared=backend._d122_expected_successor,
                    ),
                    "receipt_generation_id": 8,
                },
            })
            return "remote-result"

        with patch.object(d122.D122FabricBehaviorBackend, "step", parent_step):
            result = probe.D123FabricBehaviorBackend.step(
                backend, action, 9_000_000_000,
            )
        self.assertEqual(result, "remote-result")
        sent = captured["action"]
        self.assertEqual(sent.episode_id, action.episode_id)
        self.assertEqual(sent.request_sequence_id, action.request_sequence_id)
        self.assertEqual(sent.observation_sequence_id, action.observation_sequence_id)
        self.assertEqual(sent.first_effect_window, action.first_effect_window)
        self.assertEqual(sent.operation.kind, "mine_block")
        injection = backend.d123_injections[0]
        self.assertEqual(injection["label"], "d123_windowed_operation_denial")
        self.assertEqual(injection["expected_receipt_reason"],
                         "windowed_operation_unsupported")
        self.assertEqual(injection["changed_fields"], ["operation"])
        self.assertTrue(all(injection["preserved_fields"].values()))
        source = inspect.getsource(probe.D123FabricBehaviorBackend.step)
        self.assertNotIn("ActionReceipt", source)
        self.assertNotIn("BackendStepResult", source)

    def test_remote_rejection_does_not_infer_successor_from_windowed_jump_shape(self):
        backend = object.__new__(probe.D123FabricBehaviorBackend)
        backend._d123_state = "remote_rejected_before_sampling"
        backend.d123_injections = []
        backend.d122_transport_events = []
        backend._d122_expected_successor = None
        action = ActionSnapshotV1(
            "episode", 7, 6, 9_000_000_000,
            movement=MovementV1(forward=1, jump=True),
            first_effect_window=MovementTickWindowV1(27, 27),
        )
        captured = {}

        def parent_step(_self, sent, deadline, *, observation_request=None):
            captured["action"] = sent
            return "parent-result"

        with patch.object(d122.D122FabricBehaviorBackend, "step", parent_step):
            result = probe.D123FabricBehaviorBackend.step(
                backend, action, 9_000_000_000,
            )
        self.assertEqual(result, "parent-result")
        self.assertIsNone(captured["action"].operation)
        self.assertEqual(backend.d123_injections, [])

    def test_fabric_assembler_joins_the_unique_explicit_successor_identity(self):
        def receipt(sequence, generation):
            return {
                "episode_id": "episode",
                "request_sequence_id": sequence,
                "generation_id": generation,
                "timing_events": [],
            }

        parent_identity = {
            "episode_id": "episode", "request_sequence_id": 7,
            "observation_sequence_id": 6,
            "first_effect_window": {"earliest_tick": 8, "latest_tick": 8},
            "prepared_generation_id": None,
            "receipt_generation_id": 8,
        }
        successor_identity = {
            "episode_id": "episode", "request_sequence_id": 19,
            "observation_sequence_id": 18,
            "first_effect_window": {"earliest_tick": 27, "latest_tick": 27},
            "prepared_generation_id": "prepared-successor-1",
            "receipt_generation_id": 20,
            "proof_token": "proof", "owner_id": "owner",
            "ledger_id": "ledger", "world_session_id": "world",
            "movement_intent_id": "movement", "handoff_token": "handoff",
            "movement": {"forward": 1, "strafe": 0, "jump": True,
                         "sneak": False, "sprint": True},
            "look": {"yaw_delta_degrees": 0., "pitch_delta_degrees": 0.},
            "valid_for_ticks": 1, "safe_default": None,
        }
        payload = {
            "backend_steps": [
                {
                    "action_role": "parent_command",
                    "action_identity": parent_identity,
                    **parent_identity,
                    "receipt": receipt(7, 8),
                },
                {
                    "action_role": "neutral",
                    "action_identity": {
                        **parent_identity, "request_sequence_id": 8,
                        "observation_sequence_id": 7,
                        "first_effect_window": None,
                    },
                    "episode_id": "episode", "request_sequence_id": 8,
                    "observation_sequence_id": 7,
                    "first_effect_window": None,
                    "receipt": receipt(8, 9),
                },
                {
                    "action_role": "successor",
                    "action_identity": successor_identity,
                    **successor_identity,
                    "receipt": receipt(19, 20),
                },
            ],
            "successor_expected_identity": deepcopy(successor_identity),
            "parent_request_sequence_id": 17,
            "default_sample_ticks": [],
            "frames": [],
        }
        trial = probe.D123FabricTrial(0, 0, "timely")
        with patch.object(
            probe, "assemble_trial_result",
            side_effect=lambda _trial, **values: dict(values["payload"]),
        ):
            row = probe._fabric_assembler(
                trial, backend="fabric", reset_succeeded=True,
                start_grounded_still=True, payload=payload,
            )
        self.assertEqual(row["successor_request_sequence_id"], 19)
        self.assertEqual(row["raw_successor_receipt"], receipt(19, 20))

        reordered = {
            **payload,
            "backend_steps": list(reversed(payload["backend_steps"])),
        }
        with patch.object(
            probe, "assemble_trial_result",
            side_effect=lambda _trial, **values: dict(values["payload"]),
        ):
            row = probe._fabric_assembler(
                trial, backend="fabric", reset_succeeded=True,
                start_grounded_still=True, payload=reordered,
            )
        self.assertEqual(row["successor_request_sequence_id"], 19)

        for changed in (
            {**payload, "successor_expected_identity": {
                **successor_identity, "request_sequence_id": 99,
            }},
            {**payload, "backend_steps": payload["backend_steps"] + [
                deepcopy(payload["backend_steps"][-1]),
            ]},
            {**payload, "backend_steps": payload["backend_steps"][:-1]},
            {**payload, "backend_steps": [
                *payload["backend_steps"],
                {
                    **deepcopy(payload["backend_steps"][1]),
                    "request_sequence_id": 19,
                    "action_identity": {
                        **deepcopy(payload["backend_steps"][1]["action_identity"]),
                        "request_sequence_id": 19,
                    },
                },
            ]},
            {**payload, "backend_steps": [
                *payload["backend_steps"][:-1],
                {
                    **deepcopy(payload["backend_steps"][-1]),
                    "action_identity": {
                        **successor_identity,
                        "prepared_generation_id": "wrong-generation",
                    },
                },
            ]},
            {**payload, "backend_steps": [
                *payload["backend_steps"][:-1],
                {
                    **deepcopy(payload["backend_steps"][-1]),
                    "receipt": receipt(99, 20),
                },
            ]},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    probe._fabric_assembler(
                        trial, backend="fabric", reset_succeeded=True,
                        start_grounded_still=True, payload=changed,
                    )

        for field, value in (
            ("episode_id", "other-episode"),
            ("request_sequence_id", 99),
            ("observation_sequence_id", 99),
            ("prepared_generation_id", "stale-generation"),
            ("proof_token", "stale-proof"),
            ("owner_id", "stale-owner"),
            ("ledger_id", "stale-ledger"),
            ("world_session_id", "stale-world"),
            ("movement_intent_id", "stale-intent"),
            ("handoff_token", "stale-handoff"),
        ):
            changed = deepcopy(payload)
            changed["successor_expected_identity"][field] = value
            with self.subTest(stale_expected_field=field):
                with self.assertRaises(ValueError):
                    probe._fabric_assembler(
                        trial, backend="fabric", reset_succeeded=True,
                        start_grounded_still=True, payload=changed,
                    )

        for field in successor_identity:
            changed = deepcopy(payload)
            del changed["successor_expected_identity"][field]
            with self.subTest(missing_expected_field=field):
                with self.assertRaisesRegex(ValueError, "identity is missing"):
                    probe._fabric_assembler(
                        trial, backend="fabric", reset_succeeded=True,
                        start_grounded_still=True, payload=changed,
                    )

        remote_trial = probe.D123FabricTrial(
            0, 0, "remote_rejected_before_sampling",
        )
        remote_payload = deepcopy(payload)
        remote_payload["transport_events"] = [{
            "kind": "remote_full_request_rejection_injected",
            "action_identity": deepcopy(successor_identity),
        }]
        with patch.object(
            probe, "assemble_trial_result",
            side_effect=lambda _trial, **values: dict(values["payload"]),
        ):
            probe._fabric_assembler(
                remote_trial, backend="fabric", reset_succeeded=True,
                start_grounded_still=True, payload=remote_payload,
            )
        for field, value in (
            ("request_sequence_id", 99),
            ("prepared_generation_id", "stale-generation"),
        ):
            changed = deepcopy(remote_payload)
            changed["transport_events"][0]["action_identity"][field] = value
            with self.subTest(stale_injection_field=field):
                with self.assertRaisesRegex(ValueError, "injection identity"):
                    probe._fabric_assembler(
                        remote_trial, backend="fabric", reset_succeeded=True,
                        start_grounded_still=True, payload=changed,
                    )

    def test_d123_runner_delegates_to_the_shared_d122_execution_loop(self):
        source = inspect.getsource(probe.run_fabric_runtime)
        self.assertIn("d122.run_fabric_runtime", source)
        self.assertNotIn("for _ in range(90)", source)

    def test_live_anchor_bind_and_start_has_no_synchronous_successor_wait(self):
        bind_and_start = getattr(d122._FabricExecutionCase, "bind_and_start", None)
        self.assertIsNotNone(bind_and_start)
        source = inspect.getsource(bind_and_start)
        self.assertIn("case.tick()", source)
        self.assertIn("case.submit_successor()", source)
        self.assertLess(source.index("case.tick()"), source.index("case.submit_successor()"))
        for forbidden in ("wait_for_receiver", "_execute_job", ".put("):
            self.assertNotIn(forbidden, source)
            self.assertNotIn(
                forbidden,
                inspect.getsource(d122._FabricExecutionCase.submit_successor),
            )
        prewarm_source = inspect.getsource(d122.FabricExecutionPrewarm.create)
        self.assertIn('dependencies["MotionSolverWorker"]', prewarm_source)
        runner_source = inspect.getsource(d122.run_fabric_runtime)
        self.assertLess(
            runner_source.index("FabricExecutionPrewarm.create"),
            runner_source.index("# This is the final observation"),
        )
        close_source = inspect.getsource(d122._FabricExecutionCase.close)
        self.assertIn("close_resources_only", close_source)
        self.assertIn("timeout_seconds=5.", close_source)

    def test_launcher_has_separate_d123_smoke_and_matrix_modes(self):
        smoke = probe.fabric_probe_command(
            server_port=25597, ipc_port=8140, full_matrix=False,
        )
        matrix = probe.fabric_probe_command(
            server_port=25597, ipc_port=8140, full_matrix=True,
        )
        self.assertIn("--d123-transport-probe", smoke)
        self.assertNotIn("--d123-full-matrix", smoke)
        self.assertIn("--d123-full-matrix", matrix)
        sources = deployment.frozen_deployment_sources(
            b03_fixed_route_probe=False, d123_transport_probe=True,
        )
        self.assertIn("scripts/d123_fabric_transport.py", sources)
        self.assertIn("scripts/d122_fabric_timing.py", sources)
        self.assertIn(
            "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java",
            sources,
        )
        self.assertEqual(
            set(probe.current_production_raw_hashes()) - set(sources),
            set(),
        )
        self.assertEqual(probe.current_harness_hashes(), sources)
        self.assertTrue(deployment._uses_single_client_session({"d123_transport"}))
        self.assertTrue(deployment._captures_close_diagnostics(
            {"d123_transport"}, time_diagnostics=True,
        ))

    def test_offline_preflight_binds_semantics_known_map_and_both_component_runs(self):
        result = probe.offline_preflight(run_java=False)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["transport_preflight"], {"passed": 3, "total": 3})
        self.assertEqual(result["component_smoke"], {"passed": 3, "total": 3})
        self.assertEqual(result["component_matrix"], {"passed": 36, "total": 36})
        self.assertEqual(len(result["production_semantics"]["files"]), 32)
        self.assertEqual(len(result["production_raw_hashes"]), 32)
        self.assertEqual(
            [(row["required_count"], row["known_count"], row["mismatch_count"])
             for row in result["known_map"]],
            [(58, 58, 0)] * 4,
        )
        self.assertTrue(all(row["same_runtime_owner"] for row in result["known_map"]))
        self.assertEqual(result["fabric_started"], False)
        self.assertEqual(result["environment"]["status"], "PASS")
        self.assertEqual(result["lazy_imports"]["status"], "PASS")
        self.assertEqual(result["standalone"]["status"], "PASS")
        self.assertEqual(result["known_map_mutations"]["status"], "PASS")

    def test_frame_limit_release_is_a_hard_failure(self):
        row = deepcopy(self.smoke["trials"][0])
        row["forced_frame_limit_release"] = True
        self.assertIn("forced_frame_limit_release",
                      probe.validate_trial_result(row))

    def test_preflight_reference_rechecks_every_bound_gate(self):
        payload = probe.offline_preflight(run_java=False)
        payload["java_overlay"] = {"status": "PASS"}
        self.assertTrue(probe.validate_offline_preflight(payload, require_java=True))
        mutations = []
        wrong_matrix = deepcopy(payload)
        wrong_matrix["component_matrix"]["passed"] = 35
        mutations.append(wrong_matrix)
        wrong_map = deepcopy(payload)
        wrong_map["known_map"][0]["known_count"] = 57
        mutations.append(wrong_map)
        wrong_transport = deepcopy(payload)
        wrong_transport["transport_preflight"]["passed"] = 2
        mutations.append(wrong_transport)
        wrong_production = deepcopy(payload)
        wrong_production["production_raw_hashes"].pop(next(iter(
            wrong_production["production_raw_hashes"]
        )))
        mutations.append(wrong_production)
        wrong_java = deepcopy(payload)
        wrong_java["java_tool_identity"]["canonical_java_sha256"] = "0" * 64
        mutations.append(wrong_java)
        for changed in mutations:
            with self.subTest(changed=changed):
                self.assertFalse(probe.validate_offline_preflight(
                    changed, require_java=True,
                ))

    def test_run_directory_validation_counts_real_sessions_and_trials(self):
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            calculator_result = {
                "status": "passed",
                "scenario": "d123-transport",
                "core_sources_before": probe.current_run_source_hashes(),
                "core_sources_after": probe.current_run_source_hashes(),
                "sessions": [{"d123_transport": {
                    "status": "passed", "trial_count": 3,
                    "direction_index": 0, "repetition": 0,
                    "trials": deepcopy(self.smoke["trials"]),
                }}],
            }
            (run_dir / "result.json").write_text(
                json.dumps(calculator_result), encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "backend"):
                probe.validate_run_directory(run_dir, mode="smoke")
            result = deepcopy(calculator_result)
            result["sessions"][0]["d123_transport"]["trials"] = (
                self._fabric_rows(self.smoke["trials"])
            )
            (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
            checked = probe.validate_run_directory(run_dir, mode="smoke")
            self.assertEqual(checked["trial_count"], 3)
            self.assertEqual(checked["session_count"], 1)
            for field, value, pattern in (
                ("trial_id", "fabric-looking-component", "identity"),
                ("policy", "never_delivered", "identity"),
                ("direction_index", 1, "identity"),
            ):
                changed_result = deepcopy(result)
                changed_result["sessions"][0]["d123_transport"]["trials"][0][field] = value
                (run_dir / "result.json").write_text(
                    json.dumps(changed_result), encoding="utf-8",
                )
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, pattern):
                    probe.validate_run_directory(run_dir, mode="smoke")
            wrong_map = deepcopy(result)
            wrong_map["sessions"][0]["d123_transport"]["trials"][0][
                "dependency_precheck"
            ]["known_count"] = 57
            (run_dir / "result.json").write_text(json.dumps(wrong_map), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "known-map"):
                probe.validate_run_directory(run_dir, mode="smoke")
            wrong_close = deepcopy(result)
            wrong_close["sessions"][0]["d123_transport"]["trials"][0][
                "resource_close"
            ]["worker"]["live_workers"] = 1
            (run_dir / "result.json").write_text(
                json.dumps(wrong_close), encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "current validator"):
                probe.validate_run_directory(run_dir, mode="smoke")
            (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
            launch = {
                "schema": "mc2p.d123-fabric-launch.v1",
                "status": "captured",
                "mode": "smoke",
                "source_run": str(run_dir),
                "run_validation": checked,
            }
            preflight = probe.offline_preflight(run_java=False)
            preflight["java_overlay"] = {"status": "PASS"}
            launch["preflight"] = preflight
            manifest = run_dir / "smoke-summary.json"
            manifest.write_text(json.dumps(launch), encoding="utf-8")
            self.assertEqual(probe.smoke_reference(manifest)["trial_count"], 3)
            changed_launch = deepcopy(launch)
            changed_launch["run_validation"]["harness_hashes"][
                "scripts/d123_fabric_transport.py"
            ] = "0" * 64
            manifest.write_text(json.dumps(changed_launch), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "smoke 3/3"):
                probe.smoke_reference(manifest)
            changed_java = deepcopy(launch)
            changed_java["preflight"]["java_tool_identity"][
                "canonical_java_sha256"
            ] = "0" * 64
            manifest.write_text(json.dumps(changed_java), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "smoke 3/3"):
                probe.smoke_reference(manifest)
            result["sessions"][0]["d123_transport"]["trials"][0]["passed"] = False
            (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "trial"):
                probe.validate_run_directory(run_dir, mode="smoke")

    def test_formal_launch_has_no_run_dir_or_external_preflight_bypass(self):
        source = inspect.getsource(probe.main)
        self.assertNotIn('"--run-dir"', source)
        self.assertNotIn('"--preflight-manifest"', source)
        self.assertIn("resolve_repository_java_tools(repository_root=ROOT)", source)
        self.assertIn("offline_preflight(", source)

    def test_launch_reexecutes_offline_preflight_before_probe_subprocess(self):
        preflight = probe.offline_preflight(run_java=False)
        preflight["java_overlay"] = {"status": "PASS"}
        java_tools = probe.resolve_repository_java_tools(repository_root=probe.ROOT)
        java_environment = probe.repository_java_environment(java_tools)
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "launch"
            with patch.object(probe, "resolve_repository_java_tools",
                              return_value=java_tools), \
                    patch.object(probe, "offline_preflight",
                                 return_value=preflight) as gate:
                with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(
                        stdout="", stderr="blocked-before-fabric", returncode=2,
                )) as launched:
                    result = probe.main([
                        "--launch", "smoke", "--output", str(output),
                    ])
            self.assertEqual(result, 2)
            gate.assert_called_once_with(
                java_tools=java_tools, java_environment=java_environment,
            )
            launched.assert_called_once()
            self.assertEqual(launched.call_args.kwargs["env"], java_environment)
            frozen = json.loads((output / "offline-preflight.json").read_text("utf-8"))
            self.assertEqual(frozen["production_raw_hashes"],
                             probe.current_production_raw_hashes())
            with self.assertRaises(SystemExit):
                probe.main([
                    "--launch", "smoke", "--output", str(Path(root) / "other"),
                    "--run-dir", str(Path(root)),
                ])


if __name__ == "__main__":
    unittest.main()
