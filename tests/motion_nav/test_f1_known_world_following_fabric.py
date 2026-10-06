"""F1-D independent-Fabric plan and target-control gates."""
from __future__ import annotations

import ast
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.intent_source import IntentSourceV1
from mc2p.motion_nav.execution_supervisor import BodyRouteValidation
from mc2p.motion_nav.navigation_session import (
    NavigationDiagnostics,
    NavigationSessionState,
)
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidation,
    ActiveRouteValidationDisposition,
    ActiveRouteValidationIdentity,
    ActiveRouteValidationReason,
)
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.route_admission import (
    AdmissionReason,
    AdmissionStatus,
    LocalDirectAdmissionEvidence,
    LocalDirectAdmissionPhase,
)
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.world_model import Aabb


ROOT = Path(__file__).resolve().parents[2]


class F1KnownWorldFollowingFabricTests(unittest.TestCase):
    def api(self):
        from scripts import f1_known_world_following_fabric as api
        return api

    def test_f1_runner_diagnostics_json_round_trips_typed_route_validation(self):
        self.assertIn(
            "observation_sequence_id", BodyRouteValidation.__dataclass_fields__,
        )
        self.assertIn("route_validation", NavigationDiagnostics.__dataclass_fields__)
        identity = ActiveRouteValidationIdentity(
            "world", "route", 1, "request", "goal", 2, 3, None, 0,
        )
        incumbent = ActiveRouteValidation(
            ActiveRouteValidationDisposition.UNAFFECTED,
            ActiveRouteValidationReason.NO_INTERSECTION,
            identity,
            (),
        )
        route_validation = BodyRouteValidation(17, incumbent, None)
        local_admission = LocalDirectAdmissionEvidence(
            "local-request",
            4,
            SurfaceNodeId(0, 1, 64, 0),
            SurfaceNodeId(0, 1, 64, 0),
            Aabb(0.0, 64.0, 0.0, 1.0, 64.2, 1.0),
            (.5, 64.0, .5),
            LocalDirectAdmissionPhase.EXACT_CONNECTION,
            QueryStatus.BLOCKED,
            AdmissionStatus.REJECTED,
            AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
            SurfaceNodeId(0, 1, 64, 0),
            (.8, 64.0, .8),
            QueryStatus.FEASIBLE,
            QueryStatus.BLOCKED,
            0,
            0,
        )
        diagnostics = NavigationDiagnostics(
            NavigationSessionState.EXECUTING,
            ("route_executor",),
            "running",
            "WalkSegment",
            0,
            3,
            2,
            "route",
            True,
            None,
            "running",
            None,
            0.0,
            0.0,
            0,
            0,
            "tracking_fixed_route",
            route_validation=route_validation,
            local_direct_admission=local_admission,
        )

        source = (ROOT / "scripts/f1_known_world_following_fabric.py").read_text(
            encoding="utf-8",
        )
        self.assertIn('"diagnostics": asdict(diagnostics)', source)
        self.assertIn('"submitted_target_position"', source)
        persisted = json.loads(json.dumps(asdict(diagnostics)))
        self.assertEqual(persisted["route_validation"], {
            "observation_sequence_id": 17,
            "incumbent": {
                "disposition": "unaffected",
                "reason": "no_intersection",
                "identity": {
                    "world_session": "world",
                    "route_id": "route",
                    "route_revision": 1,
                    "source_request_id": "request",
                    "goal_id": "goal",
                    "goal_revision": 2,
                    "planning_generation": 3,
                    "work_identity": None,
                    "action_index": 0,
                },
                "affected_cells": [],
                "refreshed_dependencies": [],
                "missing_cells": [],
                "queries_used": 0,
            },
            "pending": None,
        })
        self.assertEqual(persisted["local_direct_admission"], {
            "request_id": "local-request",
            "goal_revision": 4,
            "start": {
                "column_x": 0, "column_z": 1,
                "vertical_band": 64, "surface_index": 0,
            },
            "goal": {
                "column_x": 0, "column_z": 1,
                "vertical_band": 64, "surface_index": 0,
            },
            "goal_region": {
                "min_x": 0.0, "min_y": 64.0, "min_z": 0.0,
                "max_x": 1.0, "max_y": 64.2, "max_z": 1.0,
            },
            "body_position": [0.5, 64.0, 0.5],
            "phase": "exact_connection",
            "phase_query_status": "blocked",
            "final_status": "rejected",
            "final_reason": "current_body_cannot_connect",
            "body_surface_node": {
                "column_x": 0, "column_z": 1,
                "vertical_band": 64, "surface_index": 0,
            },
            "selected_position": [0.8, 64.0, 0.8],
            "selector_status": "feasible",
            "exact_status": "blocked",
            "missing_count": 0,
            "exact_dependency_count": 0,
        })

    def test_frozen_plan_uses_two_real_players_and_representative_scope(self):
        plan = self.api().frozen_plan()

        self.assertEqual(plan["seed"], 21001)
        self.assertEqual(plan["fixture_scenario"], "static")
        self.assertEqual(plan["roles"], {
            "robot": "MC2PFollower",
            "target_player": "MC2PLeader",
        })
        self.assertEqual(plan["starts"], {
            "robot": [.5, -60.0, .5],
            "target_player": [.5, -60.0, 6.5],
        })
        self.assertEqual(plan["target_speed_blocks_per_second"], 2.0)
        self.assertEqual(plan["sample_claim"], "representative_only")
        self.assertFalse(plan["permits_teleport_or_world_write"])
        self.assertEqual(
            [item["id"] for item in plan["scenarios"]],
            [
                "straight_2_0",
                "lateral_2_0",
                "move_stop_800_resume_2_0",
                "normal_cancel_2_0",
                "first_input_late_one_tick_2_0",
            ],
        )
        stopped = plan["scenarios"][2]
        self.assertEqual(stopped["pause_ticks"], 800)
        self.assertEqual(stopped["pause_seconds"], 40.0)

    def test_every_quality_scenario_has_a_measurable_stable_window(self):
        api = self.api()

        counts = {
            scenario.identifier: sum(
                scenario.stable_tick(completed_tick)
                for completed_tick in range(1, scenario.total_ticks + 1)
            )
            for scenario in api.SCENARIOS
            if scenario.cancel_tick is None
        }

        self.assertTrue(all(count > 0 for count in counts.values()), counts)
        self.assertEqual(counts["move_stop_800_resume_2_0"], 27)

    def test_target_controls_are_bounded_real_player_inputs(self):
        api = self.api()
        pattern = tuple(api.TARGET_MOVEMENT_PATTERN)
        self.assertEqual(len(pattern), 11)
        self.assertEqual(sum(pattern), 5)

        straight = api.SCENARIO_BY_ID["straight_2_0"]
        lateral = api.SCENARIO_BY_ID["lateral_2_0"]
        stopped = api.SCENARIO_BY_ID["move_stop_800_resume_2_0"]
        for tick in range(straight.total_ticks):
            movement = api.target_movement(straight, tick)
            self.assertIs(type(movement), MovementV1)
            self.assertFalse(movement.jump)
            self.assertFalse(movement.sneak)
            self.assertFalse(movement.sprint)
            self.assertEqual(movement.strafe, 0)
        self.assertTrue(any(
            api.target_movement(straight, tick).forward == 1
            for tick in range(straight.move_ticks)
        ))
        self.assertTrue(all(
            api.target_movement(lateral, tick).forward == 0
            for tick in range(lateral.total_ticks)
        ))
        self.assertTrue(any(
            api.target_movement(lateral, tick).strafe == 1
            for tick in range(lateral.move_ticks)
        ))
        self.assertTrue(all(
            api.target_movement(stopped, tick) == MovementV1()
            for tick in range(stopped.move_ticks,
                              stopped.move_ticks + stopped.pause_ticks)
        ))
        self.assertTrue(any(
            api.target_movement(stopped, tick) != MovementV1()
            for tick in range(stopped.move_ticks + stopped.pause_ticks,
                              stopped.motion_end_tick)
        ))

    def test_static_gate_has_no_legacy_follower_or_world_mutation(self):
        path = ROOT / "scripts/f1_known_world_following_fabric.py"
        source = path.read_text("utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(any("follow_scenarios" in name for name in imported))
        for forbidden in (
            "RuleFollower", "PlaygroundFollower", "navigation_controller",
            "fixture_writer", '"tp ', "setblock", "fill ",
        ):
            self.assertNotIn(forbidden, source)

    def test_target_controller_uses_one_ordered_runtime_source(self):
        api = self.api()

        class Runtime:
            def __init__(self):
                self.observation = SimpleNamespace(
                    episode_id="f1-episode", sequence_id=7,
                )
                self.cancelled = []
                self.unregistered = []

            def register_ordered_source(self, label):
                self.label = label
                return IntentSourceV1(
                    "0" * 32, "f1-episode", 0, 1,
                    "ordered/" + "0" * 32 + "/0/1",
                )

            def cancel_source(self, source_id):
                self.cancelled.append(source_id)
                return ()

            def unregister_ordered_source(self, source):
                self.unregistered.append(source)
                return ()

        runtime = Runtime()
        controller = api.TargetPlayerController(runtime)
        moving = controller.proposal(
            MovementV1(forward=1), api.time.perf_counter_ns() + 1_000_000_000,
        )
        neutral = controller.proposal(
            MovementV1(), api.time.perf_counter_ns() + 1_000_000_000,
        )
        controller.close()

        self.assertEqual(runtime.label, "f1-target-player")
        self.assertEqual(len(moving.intents), 1)
        self.assertEqual(moving.intents[0].intent.movement,
                         MovementV1(forward=1))
        self.assertEqual(neutral.intents, ())
        self.assertEqual(len(runtime.cancelled), 2)
        self.assertEqual(runtime.unregistered, [controller.source])

    def test_preflight_smoke_writes_plan_without_starting_host(self):
        api = self.api()
        with TemporaryDirectory() as directory:
            output = Path(directory) / "preflight.json"
            report = api.preflight(output, check_assets=False)

            self.assertTrue(report["passed"], report)
            self.assertTrue(output.is_file())
            self.assertFalse((Path(directory) / "server").exists())
            self.assertEqual(report["processes_started"], 0)
            self.assertEqual(report["formal_chain"], (
                "PlayerRuntimeV1->RuntimeNavigationDriver->"
                "KnownWorldFollowDriver"
            ))
            self.assertEqual(report["trace_format"], "segmented_jsonl")
            quality = next(
                item for item in report["checks"]
                if item["name"] == "quality_scenarios_have_stable_samples"
            )
            self.assertTrue(quality["passed"])
            self.assertEqual(
                quality["sample_counts"]["move_stop_800_resume_2_0"], 27,
            )

    def test_smoke_summary_requires_every_host_check_and_clean_close(self):
        api = self.api()
        host = SimpleNamespace(
            cleanup_failures=[],
            checks=[
                {"name": "owned_ports_released", "passed": True},
                {"name": "MC2PFollower:time_attribution", "passed": False},
            ],
        )

        checks, failures = api.smoke_evidence_gates(host)

        self.assertFalse(all(item["passed"] for item in checks))
        self.assertEqual(failures, [{
            "kind": "host_check_failed",
            "name": "MC2PFollower:time_attribution",
        }])

        host.cleanup_failures = [{"client": "MC2PLeader", "error": "close"}]
        checks, failures = api.smoke_evidence_gates(host)
        self.assertFalse(checks[-1]["passed"])
        self.assertEqual(failures[-1], {
            "kind": "cleanup_failure",
            "detail": {"client": "MC2PLeader", "error": "close"},
        })

    def _passing_scenario_data(self):
        return {
            "samples": [
                {
                    "tick": 1,
                    "robot_position": [0.0, 64.0, 0.0],
                    "target_position": [0.0, 64.0, 2.5],
                    "stable": True,
                    "target_speed_blocks_per_second": 2.0,
                },
                {
                    "tick": 2,
                    "robot_position": [0.0, 64.0, .1],
                    "target_position": [0.0, 64.0, 2.6],
                    "stable": True,
                    "target_speed_blocks_per_second": 2.0,
                },
            ],
            "response_frames": [{
                "movement_tick": 1,
                "position": [0.0, 64.0, .1],
                "driver_state": "running",
                "source_bound": True,
                "goal_satisfied": True,
                "goal_revision": 2,
                "goal_revision_requests": [{
                    "revision": 2, "movement_tick": 0,
                }],
                "goal_position": [0.0, 64.0, 2.5],
                "applied_movement": {"forward": 0, "strafe": 0},
            }],
            "response_start_tick": 0,
            "response_start_position": [0.0, 64.0, 0.0],
            "initial_raw_distance_blocks": 2.5,
            "accepted_revisions": 2,
            "rejected_revisions": 0,
            "planning_submissions": 2,
            "target_actual_input_ticks": 2,
            "robot_actual_input_ticks": 1,
            "task_recoveries": 0,
            "safety_violations": [],
            "terminal_session_state": "cancelled",
            "terminal_driver_state": "cancelled",
            "source_released": True,
            "cancel_requested": True,
            "cancel_tail_ticks": 1,
            "late_input_evidence": None,
            "update_status_counts": {"revised": 1},
        }

    def test_scenario_summary_uses_matching_satisfaction_and_f1_gates(self):
        api = self.api()
        data = self._passing_scenario_data()
        report = api.summarize_scenario(
            api.SCENARIO_BY_ID["straight_2_0"], data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )

        self.assertTrue(report["passed"], report)
        self.assertEqual(report["revision_response_details"], [{
            "revision": 2,
            "response_ticks": 1,
            "end": "effective",
            "evidence": "satisfied",
        }])
        self.assertEqual(report["revision_response_p95_ticks"], 1)
        self.assertEqual(report["planning_submissions_per_accepted_revision"], 1.0)
        self.assertEqual(report["stable_excess_lag"]["p95_blocks"], 0.0)
        self.assertEqual(report["gates"]["target_speed"], True)
        self.assertIsNone(report["gates"]["late_input"])

    def test_scenario_summary_rejects_quiescent_satisfied_stopping_hold(self):
        api = self.api()
        data = self._passing_scenario_data()
        data["samples"][0].update({
            "session": {
                "state": "stopping",
                "observed_goal_status": "satisfied",
                "reach_policy": "keep_active_on_reach",
                "route_id": None,
            },
            "diagnostics": {
                "handoff": {"disposition": "quiescent"},
                "controller_ids": [],
                "active_waits": [],
                "body_control_activities": [],
                "pending_route_id": None,
            },
        })

        report = api.summarize_scenario(
            api.SCENARIO_BY_ID["straight_2_0"], data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )

        self.assertFalse(report["passed"])
        self.assertFalse(report["gates"]["satisfied_idle_lifecycle"])
        self.assertEqual(report["satisfied_idle_stopping_ticks"], [1])

        data["samples"][0]["cancellation_requested"] = True
        cancelled = api.summarize_scenario(
            api.SCENARIO_BY_ID["normal_cancel_2_0"], data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )
        self.assertTrue(cancelled["gates"]["satisfied_idle_lifecycle"])
        self.assertEqual(cancelled["satisfied_idle_stopping_ticks"], [])
        self.assertTrue(cancelled["passed"], cancelled)

    def test_cancel_scope_ignores_capability_metrics_but_not_cleanup(self):
        api = self.api()
        data = self._passing_scenario_data()
        data.update(
            samples=[], planning_submissions=99,
            accepted_revisions=1, response_frames=[],
        )
        report = api.summarize_scenario(
            api.SCENARIO_BY_ID["normal_cancel_2_0"], data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )
        self.assertTrue(report["passed"], report)
        for name in ("target_speed", "stable_lag", "final_within_hold",
                     "planning_ratio", "revision_response", "late_input"):
            self.assertIsNone(report["gates"][name])

        failed = api.summarize_scenario(
            api.SCENARIO_BY_ID["normal_cancel_2_0"], data,
            host_checks=[{"name": "time", "passed": False}],
            cleanup_failures=[],
        )
        self.assertFalse(failed["passed"])
        self.assertFalse(failed["gates"]["host_evidence"])

    def test_first_late_requires_actual_one_tick_extra_application(self):
        api = self.api()
        scenario = api.SCENARIO_BY_ID["first_input_late_one_tick_2_0"]
        data = self._passing_scenario_data()
        data["late_input_evidence"] = {
            "injection_requested": True,
            "input_application_status": "applied_outside_window",
            "valid_for_ticks": 1,
            "requested_first_tick": 9,
            "requested_last_tick": 9,
            "latest_allowed_first_tick": 9,
            "actual_application_ticks": [10],
            "normal_application_offset_ticks": 1,
            "actual_application_offset_ticks": 2,
        }
        failed = api.summarize_scenario(
            scenario, data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )
        self.assertFalse(failed["gates"]["late_input"])

        data["late_input_evidence"]["input_application_status"] = "applied"
        data["late_input_evidence"]["latest_allowed_first_tick"] = 10
        passed = api.summarize_scenario(
            scenario, data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )
        self.assertTrue(passed["gates"]["late_input"])
        self.assertTrue(passed["passed"], passed)

        data["late_input_evidence"]["latest_allowed_first_tick"] = 11
        widened = api.summarize_scenario(
            scenario, data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )
        self.assertFalse(widened["gates"]["late_input"])

        data["late_input_evidence"]["latest_allowed_first_tick"] = 10
        data["late_input_evidence"]["actual_application_ticks"] = [9, 10]
        repeated = api.summarize_scenario(
            scenario, data,
            host_checks=[{"name": "time", "passed": True}],
            cleanup_failures=[],
        )
        self.assertFalse(repeated["gates"]["late_input"])

    def test_scenario_cli_requires_one_frozen_id_and_has_no_all_mode(self):
        api = self.api()
        parsed = api.parse_arguments([
            "scenario", "--id", "straight_2_0",
            "--output", "artifacts/f1-known-world-following/new-case",
        ])
        self.assertEqual(parsed.scenario_id, "straight_2_0")
        with self.assertRaises(SystemExit):
            api.parse_arguments([
                "scenario", "--id", "all", "--output", "unused",
            ])
        with self.assertRaises(SystemExit):
            api.parse_arguments(["scenario", "--output", "unused"])

    def test_late_injection_uses_public_prepare_control_adopt_and_real_ledger(self):
        api = self.api()
        calls = []
        runtime = SimpleNamespace(
            observation=SimpleNamespace(
                self_state=SimpleNamespace(
                    value=SimpleNamespace(movement_tick_id=10),
                ),
            ),
            input_ledger=SimpleNamespace(record=lambda sequence: SimpleNamespace(
                status=SimpleNamespace(value="applied"),
                action=SimpleNamespace(valid_for_ticks=1),
                requested_first_tick=11,
                requested_last_tick=11,
                latest_allowed_first_tick=12,
                applied_ticks=(12,),
            )),
        )
        runtime.control_frame = Mock(side_effect=lambda *args, **kwargs: (
            calls.append("control") or SimpleNamespace()
        ))
        intent = SimpleNamespace(movement=MovementV1(forward=1))
        driver = SimpleNamespace(
            runtime=runtime,
            prepare_proposals=Mock(side_effect=lambda deadline: (
                calls.append("prepare") or
                (SimpleNamespace(intents=(SimpleNamespace(intent=intent),)),)
            )),
            adopt_result=Mock(side_effect=lambda result: calls.append("adopt")),
            last_frame_diagnostics=SimpleNamespace(action_request_sequence=7),
        )
        state = {"enabled": True, "attempted": False, "evidence": None}
        with patch.object(api.time, "sleep") as sleep:
            _, evidence = api._driver_step(
                driver, api.time.perf_counter_ns() + 1_000_000_000, state,
            )

        self.assertEqual(calls, ["prepare", "control", "adopt"])
        sleep.assert_called_once_with(.055)
        self.assertEqual(evidence["normal_application_offset_ticks"], 1)
        self.assertEqual(evidence["actual_application_offset_ticks"], 2)
        self.assertEqual(evidence["input_application_status"], "applied")
        self.assertEqual(evidence["latest_allowed_first_tick"], 12)
        self.assertIs(state["evidence"], evidence)

    def test_smoke_and_scenario_use_the_same_formal_case_runner(self):
        source = (ROOT / "scripts/f1_known_world_following_fabric.py").read_text(
            "utf-8"
        )
        tree = ast.parse(source)
        calls = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls[node.name] = {
                    child.func.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                }
        self.assertIn("_run_formal_case", calls["run_smoke"])
        self.assertIn("_run_formal_case", calls["run_scenario"])

    def test_scenario_sample_persists_current_submitted_target_position(self):
        source = (ROOT / "scripts/f1_known_world_following_fabric.py").read_text(
            "utf-8"
        )
        tree = ast.parse(source)
        formal = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_run_formal_case"
        )
        sample = next(
            node for node in ast.walk(formal)
            if isinstance(node, ast.Dict)
            and any(
                isinstance(key, ast.Constant) and key.value == "diagnostics"
                for key in node.keys
            )
            and any(
                isinstance(key, ast.Constant) and key.value == "tick"
                for key in node.keys
            )
        )
        values = {
            key.value: value
            for key, value in zip(sample.keys, sample.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        self.assertIn("submitted_target_position", values)
        self.assertIn(
            "latest_follow_result.submitted_target_position",
            ast.unparse(values["submitted_target_position"]),
        )

    def test_planning_identity_safety_matches_formal_i15_i16_i17_states(self):
        api = self.api()

        def diagnostics(*, owned=False, work=False, permit=True,
                        information=True):
            return SimpleNamespace(
                planning_work_owned=owned,
                planning_work_identity_valid=work,
                planning_permit_identity_valid=permit,
                planning_information_identity_valid=information,
            )

        self.assertEqual(
            api.planning_identity_violations(
                "executing", diagnostics(owned=False, work=False),
            ),
            (),
        )
        self.assertEqual(
            api.planning_identity_violations(
                "planning", diagnostics(owned=False, work=False),
            ),
            ("I15:planning_work_identity_invalid",),
        )
        self.assertEqual(
            api.planning_identity_violations(
                "planning", diagnostics(owned=True, work=False, permit=False),
            ),
            (
                "I15:planning_work_identity_invalid",
                "I16:planning_permit_identity_invalid",
            ),
        )
        self.assertEqual(
            api.planning_identity_violations(
                "needs_information",
                diagnostics(information=False),
            ),
            ("I17:planning_information_identity_invalid",),
        )


if __name__ == "__main__":
    unittest.main()
