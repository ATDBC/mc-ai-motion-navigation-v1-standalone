import unittest

from scripts.b12b_partial_combat_runtime import (
    B12B_RUNTIME_INJECTIONS,
    _PLAYER_START,
    _ROUTE_OFFSETS,
    _TARGET,
    _goal,
    _hidden_target_z,
    _latency_summary,
    b12b_trial_plan,
    evaluate_b12b_boundary_evidence,
    evaluate_b12b_active_target_evidence,
    evaluate_b12b_positive_evidence,
)


class B12BPartialCombatRuntimeTests(unittest.TestCase):
    def test_latency_summary_uses_nearest_rank_percentiles(self):
        summary = _latency_summary(range(1, 101))

        self.assertEqual(summary, {
            "count": 100,
            "p50": 50.0,
            "p95": 95.0,
            "p99": 99.0,
            "max": 100.0,
        })
        self.assertEqual(
            _latency_summary(()),
            {"count": 0, "p50": None, "p95": None,
             "p99": None, "max": None},
        )

    def test_fixture_goals_use_support_representatives_and_keep_melee_distance(self):
        self.assertEqual(
            ((_PLAYER_START["x"] - .5) % 1.0,
             (_PLAYER_START["z"] - .5) % 1.0),
            (0.0, 0.0),
        )
        self.assertEqual(
            ((_TARGET["x"] - _PLAYER_START["x"]) ** 2
             + (_TARGET["z"] - _PLAYER_START["z"]) ** 2) ** .5,
            2.25,
        )
        for direction, (dx, dz) in _ROUTE_OFFSETS.items():
            self.assertEqual(abs(dx) + abs(dz), 2.0, direction)
            goal = _goal(direction)
            center_x = (goal.region.min_x + goal.region.max_x) / 2.0
            center_z = (goal.region.min_z + goal.region.max_z) / 2.0
            self.assertEqual((center_x - .5) % 1.0, 0.0, direction)
            self.assertEqual((center_z - .5) % 1.0, 0.0, direction)

    def test_occlusion_boundaries_separate_navigation_from_reacquisition(self):
        self.assertGreater(
            _hidden_target_z("engaged_occlusion_navigation")
            - _PLAYER_START["z"],
            4.5,
        )
        self.assertLessEqual(
            _hidden_target_z("occluded_attack_reacquire")
            - _PLAYER_START["z"],
            4.5,
        )

    def test_plan_freezes_four_ground_axes_and_split_boundary_sources(self):
        rows = b12b_trial_plan(21001)
        positives = [row for row in rows if row["classification"] == "positive"]
        boundaries = [row for row in rows if row["classification"] == "boundary"]
        active = [row for row in rows if row["classification"] == "active_target"]

        self.assertEqual(len(positives), 24)
        self.assertEqual(
            {direction: sum(row["direction"] == direction for row in positives)
             for direction in {row["direction"] for row in positives}},
            {"forward": 8, "backward": 8, "left": 4, "right": 4},
        )
        self.assertEqual(len(boundaries), 12)
        self.assertEqual(len(active), 2)
        self.assertEqual(
            {(row["active_mode"], row["ai_seed"]) for row in active},
            {("induced_turn", 51001), ("sustained_chase", 51002)},
        )
        self.assertEqual(
            sum(row["evidence_source"] == "fabric" for row in boundaries), 8,
        )
        self.assertEqual(
            {row["injection"] for row in boundaries
             if row["evidence_source"] == "runtime"},
            set(B12B_RUNTIME_INJECTIONS),
        )
        self.assertEqual(len({row["trial_id"] for row in rows}), 38)

    def test_active_target_evaluator_requires_applied_turn_and_movement_same_tick(self):
        trial = next(
            row for row in b12b_trial_plan(21001)
            if row["classification"] == "active_target"
        )
        request = 31
        induced = {
            **trial,
            "target_track_id": "entity-active",
            "composed_request_sequences": [request],
            "seed_receipt_valid": True,
            "report": {"state": "complete", "confirmed_hits": 1},
            "runtime_ready": True,
        }
        sustained_trial = next(
            row for row in b12b_trial_plan(21001)
            if row.get("active_mode") == "sustained_chase"
        )
        sustained = {
            **sustained_trial,
            "target_track_id": "entity-active",
            "control_request_sequences": [request],
            "composed_request_sequences": [request],
            "seed_receipt_valid": True,
            "control_frame_count": 32,
            "elapsed_seconds": 2.1,
            "target_displacement_blocks": 1.25,
            "report": {"state": "complete", "confirmed_hits": 1},
            "runtime_ready": True,
        }
        trace = (
            {
                "record_type": "dispatch",
                "payload": {"decision": {"action": {
                    "request_sequence_id": request,
                    "movement": {"forward": 1, "strafe": 0, "jump": False,
                                 "sneak": False, "sprint": False},
                    "look": {"yaw_delta_degrees": 12.0,
                             "pitch_delta_degrees": 0.0},
                }}},
            },
            {
                "record_type": "navigation_route_decision",
                "payload": {
                    "session_id": "b12b-sustained-active-target-01",
                    "reason_code": "walk_tracking",
                    "submit_input": True,
                    "movement": {"forward": 1, "strafe": 0,
                                 "jump": False, "sneak": False,
                                 "sprint": False},
                },
            },
        )
        events = (
            {
                "event": "input_consumed", "request_sequence_id": request,
                "client_ticks": 51, "time_event_sequence": 44,
                "input_state": "leased",
                "actual_input": {"forward": 1.0, "strafe": 0.0,
                                 "jump": False, "sneak": False,
                                 "sprint": False},
            },
            {
                "event": "look_applied", "request_sequence_id": request,
                "client_ticks": 50, "time_event_sequence": 44,
                "actual_look": {"yaw": 12.0,
                                                        "pitch": 0.0},
            },
        )

        evaluated, checks = evaluate_b12b_active_target_evidence(
            (induced, sustained), trace, events,
        )

        self.assertTrue(all(row["passed"] for row in evaluated))
        sustained_result = next(
            row for row in evaluated
            if row["active_mode"] == "sustained_chase"
        )
        self.assertEqual(sustained_result["turn_frame_count"], 1)
        self.assertEqual(sustained_result["moving_turn_frame_count"], 1)
        self.assertEqual(
            sustained_result["navigation_reason_counts"],
            {"walk_tracking": 1},
        )
        self.assertTrue(all(check["passed"] for check in checks))
        failed, _ = evaluate_b12b_active_target_evidence(
            (induced, sustained), trace, (events[0],),
        )
        self.assertFalse(all(row["passed"] for row in failed))

    def test_positive_evaluator_requires_same_tick_attack_and_bounded_walk_intent(self):
        trial = next(
            row for row in b12b_trial_plan(21001)
            if row["classification"] == "positive"
        )
        movement_id = "navigation-intent-1"
        track_id = "entity-1"
        request_id = 17
        tick = {"world": 30, "player_movement": 41}
        row = {
            **trial,
            "target_track_id": track_id,
            "movement_intent_id": movement_id,
            "report": {
                "state": "complete",
                "reason": "hit_confirmed",
                "attack_submissions": 1,
                "hit_observed": True,
            },
            "runtime_ready": True,
        }
        trace = ({
            "record_type": "ordered_intent",
            "payload": {"envelope": {"intent": {
                "intent_id": movement_id,
                "movement": dict(trial["movement"], jump=False, sneak=False,
                                 sprint=False),
                "movement_observed_yaw_limit_degrees": 5.0,
                "valid_for_ticks": 1,
            }}},
        },)
        attack = {
            "event": "attack_dispatched",
            "episode_id": "episode-1",
            "request_sequence_id": request_id,
            "client_ticks": tick,
            "actual_operation": {"entity_ref": track_id},
        }
        movement = {
            "event": "input_consumed",
            "episode_id": "episode-1",
            "request_sequence_id": request_id,
            "client_ticks": tick,
            "input_state": "leased",
            "actual_input": dict(trial["movement"], jump=False, sneak=False,
                                 sprint=False),
        }

        boundary = {
            "classification": "boundary",
            "target_track_id": "boundary-entity",
        }
        evaluated, checks = evaluate_b12b_positive_evidence(
            (row, boundary), trace, (attack, movement),
        )

        self.assertEqual(len(evaluated), 1)
        self.assertTrue(evaluated[0]["passed"])
        self.assertTrue(all(check["passed"] for check in checks))

        for defect_trace, defect_events in (
            (({**trace[0], "payload": {"envelope": {"intent": {
                **trace[0]["payload"]["envelope"]["intent"],
                "movement_observed_yaw_limit_degrees": None,
            }}}},), (attack, movement)),
            (trace, (attack, {**movement, "client_ticks": {
                "world": 31, "player_movement": 42,
            }})),
        ):
            failed, _ = evaluate_b12b_positive_evidence(
                (row,), defect_trace, defect_events,
            )
            self.assertFalse(failed[0]["passed"])

    def test_boundary_evaluator_requires_each_declared_safety_effect(self):
        rows = (
            {
                "trial_id": "large", "injection": "large_combat_turn",
                "movement_suppressed_count": 0, "turn_selected_count": 1,
                "movement_during_turn_count": 1,
                "resumed_movement_count": 0, "runtime_ready": True,
            },
            {
                "trial_id": "hidden", "injection": "engaged_occlusion_navigation",
                "engagement_position_uses": 1, "hidden_attack_submissions": 0,
                "hidden_movement_events": 1, "runtime_ready": True,
            },
            {
                "trial_id": "reacquire", "injection": "occluded_attack_reacquire",
                "engagement_position_uses": 1, "hidden_attack_submissions": 0,
                "reacquire_look_count": 1, "post_reveal_confirmed_hit": True,
                "runtime_ready": True,
            },
            {
                "trial_id": "unengaged-hidden",
                "injection": "hidden_without_engagement",
                "hidden_observed": True,
                "engagement_position_uses": 0,
                "hidden_attack_submissions": 0,
                "hidden_movement_events": 0,
                "runtime_ready": True,
            },
        )

        evaluated, checks = evaluate_b12b_boundary_evidence(rows)

        self.assertTrue(all(row["passed"] for row in evaluated))
        self.assertTrue(all(check["passed"] for check in checks))
        defective = tuple(
            {**row, "hidden_attack_submissions": 1}
            if row["injection"] == "engaged_occlusion_navigation" else row
            for row in rows
        )
        evaluated, _ = evaluate_b12b_boundary_evidence(defective)
        self.assertFalse(next(
            row for row in evaluated
            if row["injection"] == "engaged_occlusion_navigation"
        )["passed"])


if __name__ == "__main__":
    unittest.main()
