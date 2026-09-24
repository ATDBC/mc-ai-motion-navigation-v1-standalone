"""Frozen C1-C real-damage plan and result accounting."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.contracts.behavior import BehaviorProfileV0
from scripts.c1_external_motion_runtime import (
    C1C_NEGATIVE_INJECTIONS,
    _append_motion_observation,
    _fixture_commands,
    _cleanup_trial,
    controlled_attack_due,
    c1c_trial_plan,
    evaluate_c1c_positive,
    recovery_application_latency_ms,
    selected_retired_route,
    summarize_c1c_trials,
    validate_c1c_seed_receipt,
    wait_after_fast_poll,
)


class C1ExternalMotionRuntimeTests(unittest.TestCase):
    def test_route_becomes_stale_only_after_transition_frame_finishes(self):
        source_id = "navigation-session/test/7"
        selected = source_id + "/movement/41"
        retired_sources: set[str] = set()

        # This input was selected before the returned observation revealed
        # damage, so the transition frame itself is still causally valid.
        self.assertFalse(selected_retired_route(selected, retired_sources))

        retired_sources.add(source_id)
        self.assertTrue(selected_retired_route(selected, retired_sources))

    def test_fast_poll_yields_but_completed_control_does_not(self):
        with patch('scripts.c1_external_motion_runtime.time.sleep') as sleep:
            wait_after_fast_poll(None)
            sleep.assert_called_once_with(.01)
            wait_after_fast_poll(SimpleNamespace())
            sleep.assert_called_once_with(.01)

    def test_recovery_latency_uses_client_application_clock(self):
        result = SimpleNamespace(
            decision=SimpleNamespace(action=SimpleNamespace(request_sequence_id=7)),
            backend_result=SimpleNamespace(receipt=SimpleNamespace(
                input_applications=(SimpleNamespace(
                    request_sequence_id=7, sampled_at_jvm_ns=1_075_000_000,
                ),),
            )),
        )

        self.assertEqual(recovery_application_latency_ms(1_000_000_000, result), 75.0)
        self.assertIsNone(recovery_application_latency_ms(
            1_100_000_000, result,
        ))

    def test_child_event_observation_is_inserted_in_causal_order_once(self):
        def snapshot(sequence, tick, hurt, health):
            return SimpleNamespace(
                sequence_id=sequence,
                self_state=SimpleNamespace(value=SimpleNamespace(
                    movement_tick_id=tick,
                    hurt_animation_ticks=hurt,
                    health_points=health,
                )),
            )

        rows = []
        _append_motion_observation(rows, snapshot(10, 10, 0, 20.0))
        _append_motion_observation(rows, snapshot(12, 12, 8, 17.0))
        _append_motion_observation(rows, snapshot(11, 11, 9, 20.0))
        _append_motion_observation(rows, snapshot(11, 11, 9, 20.0))

        self.assertEqual([row["sequence_id"] for row in rows], [10, 11, 12])

    def test_fixture_reset_heals_player_without_modifying_player_nbt(self):
        commands = _fixture_commands(c1c_trial_plan(21001)[0])

        self.assertIn(
            "effect give MC2PProbe minecraft:instant_health 1 10 true",
            commands,
        )
        self.assertIn(
            "item replace entity MC2PProbe weapon.mainhand with minecraft:diamond_sword",
            commands,
        )
        self.assertFalse(any("data modify entity MC2PProbe Health" in command
                             for command in commands))

    def test_trial_cleanup_releases_nonterminal_driver_and_always_removes_fixture(self):
        events = []

        class Driver:
            report = SimpleNamespace(terminal=False)

            def cancel(self, profile, reason):
                events.append(("cancel", profile, reason))

        profile = BehaviorProfileV0()
        trial = {"trial_id": "negative-target-death-during-recovery"}
        _cleanup_trial(
            Driver(), profile, trial,
            lambda commands, current: events.append(("fixture", commands, current)),
        )

        self.assertEqual(events[0], ("cancel", profile, "c1c_trial_end"))
        self.assertEqual(events[1], (
            "fixture",
            ("mc2p_c1_remove negative-target-death-during-recovery",),
            trial,
        ))

    def test_plan_freezes_twenty_staged_positives_and_ten_negatives(self):
        rows = c1c_trial_plan(21001)
        positives = [row for row in rows if row["classification"] == "positive"]
        negatives = [row for row in rows if row["classification"] == "negative"]
        self.assertEqual(len(positives), 20)
        self.assertEqual(len(negatives), 10)
        self.assertEqual({row["injection"] for row in negatives},
                         set(C1C_NEGATIVE_INJECTIONS))
        self.assertEqual({row["direction"] for row in positives},
                         {"north", "east", "south", "west"})
        counts = {}
        for row in positives:
            counts[row["damage_stage"]] = counts.get(row["damage_stage"], 0) + 1
            self.assertEqual(row["damage_mode"], "real")
            self.assertEqual(row["maximum_recovery_ticks"], 40)
            self.assertEqual(row["maximum_external_motion_events"], 4)
        self.assertEqual(counts, {
            "pursuing": 8,
            "aim_or_cooldown": 4,
            "attack_submitted": 4,
            "post_recovery_rehit": 4,
        })
        self.assertEqual({row["attack_trigger"] for row in positives},
                         {"controlled_vanilla_attack"})

    def test_controlled_attack_is_due_only_in_the_declared_phase(self):
        own = SimpleNamespace(hurt_animation_ticks=0)
        pursuing = SimpleNamespace(
            state="pursuing", attack_submissions=0,
            external_recoveries_completed=0,
        )
        aiming = SimpleNamespace(
            state="striking", attack_submissions=0,
            external_recoveries_completed=0,
        )
        submitted = SimpleNamespace(
            state="striking", attack_submissions=1,
            external_recoveries_completed=0,
        )
        recovered = SimpleNamespace(
            state="pursuing", attack_submissions=1,
            external_recoveries_completed=1,
        )

        self.assertTrue(controlled_attack_due("pursuing", pursuing, own, 0))
        self.assertFalse(controlled_attack_due("pursuing", aiming, own, 0))
        self.assertTrue(controlled_attack_due("aim_or_cooldown", aiming, own, 0))
        self.assertTrue(controlled_attack_due("attack_submitted", submitted, own, 0))
        self.assertTrue(controlled_attack_due("post_recovery_rehit", pursuing, own, 0))
        self.assertTrue(controlled_attack_due("post_recovery_rehit", recovered, own, 1))
        self.assertFalse(controlled_attack_due(
            "post_recovery_rehit", recovered,
            SimpleNamespace(hurt_animation_ticks=3), 1,
        ))
        self.assertFalse(controlled_attack_due("post_recovery_rehit", recovered, own, 2))

    def test_real_damage_seed_receipt_is_required(self):
        trial = c1c_trial_plan(21001)[0]
        receipt = {
            "event": "spawned", "scenario_id": trial["trial_id"],
            "seed": trial["ai_seed"], "spawn_tick": 12,
            "seed_applied_tick": 12,
            "seed_applied_before_first_ai_tick": True,
            "damage_mode": "real",
        }
        self.assertTrue(validate_c1c_seed_receipt(trial, receipt))
        self.assertFalse(validate_c1c_seed_receipt(
            trial, dict(receipt, damage_mode="isolated")
        ))

    def test_positive_requires_real_recovery_and_declared_stage(self):
        trial = next(row for row in c1c_trial_plan(21001)
                     if row["damage_stage"] == "post_recovery_rehit")
        evidence = {
            "fixture_valid": True,
            "observed_damage_stage": "post_recovery_rehit",
            "controlled_attack_requests": 2,
            "controlled_attack_successes": 2,
            "real_damage_events": 2,
            "damage_transitions": 2,
            "external_motion_events": 2,
            "external_recoveries_completed": 1,
            "maximum_recovery_elapsed_ticks": 20,
            "parallel_recovery_owners": 0,
            "stale_route_takeovers": 0,
            "duplicate_attacks": 0,
            "reanchors": 1,
            "explicit_death": True,
            "terminal_state": "complete",
        }
        self.assertEqual(evaluate_c1c_positive(trial, evidence), ())
        for field, bad in (
            ("observed_damage_stage", "pursuing"),
            ("controlled_attack_requests", 1),
            ("controlled_attack_successes", 1),
            ("real_damage_events", 1),
            ("damage_transitions", 1),
            ("external_motion_events", 1),
            ("external_recoveries_completed", 0),
            ("maximum_recovery_elapsed_ticks", 41),
            ("parallel_recovery_owners", 1),
            ("stale_route_takeovers", 1),
            ("duplicate_attacks", 1),
            ("reanchors", 0),
            ("explicit_death", False),
            ("terminal_state", "timeout"),
        ):
            self.assertTrue(evaluate_c1c_positive(trial, dict(evidence, **{field: bad})), field)

    def test_reachable_timeout_remains_in_positive_denominator(self):
        summary = summarize_c1c_trials((
            {"classification": "positive", "fixture_valid": True, "passed": True},
            {"classification": "positive", "fixture_valid": True, "passed": False,
             "terminal_state": "timeout"},
            {"classification": "positive", "fixture_valid": False, "passed": False},
            {"classification": "negative", "fixture_valid": True, "passed": True},
        ))
        self.assertEqual((summary["positive_passed"], summary["positive_total"]), (1, 2))
        self.assertEqual(summary["fixture_invalid"], 1)


if __name__ == "__main__":
    unittest.main()
