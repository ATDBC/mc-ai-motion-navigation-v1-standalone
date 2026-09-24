from pathlib import Path
from tempfile import TemporaryDirectory
import json
from types import SimpleNamespace
import unittest

from mc2p.contracts.common import FieldStatusV0


class C1FixedMeleeRuntimeTests(unittest.TestCase):
    def test_fixture_waits_until_player_is_grounded_before_starting_trial(self):
        from scripts.c1_fixed_melee_runtime import _refresh_navigation

        zombie = SimpleNamespace(
            hurt_animation_ticks=0, entity_type="minecraft:zombie", track_id="zombie-new",
        )

        def observation(on_ground):
            return SimpleNamespace(
                perception=SimpleNamespace(
                    status=FieldStatusV0.VALID,
                    value=SimpleNamespace(visible_entities=(zombie,)),
                ),
                self_state=SimpleNamespace(
                    status=FieldStatusV0.VALID,
                    value=SimpleNamespace(is_on_ground=on_ground),
                ),
                tracked_entity=SimpleNamespace(value=SimpleNamespace(
                    track_id="zombie-new", is_dead=False, health_points=20.0,
                )),
            )

        class Runtime:
            def __init__(self):
                self.observation = observation(False)
                self.steps = 0

            def step(self, *args, **kwargs):
                self.steps += 1
                self.observation = observation(self.steps >= 2)
                return SimpleNamespace(report=SimpleNamespace(failure=None))

        runtime = Runtime()
        self.assertIs(_refresh_navigation(runtime, {"trial_id": "timing"}, 10**18), zombie)
        self.assertEqual(runtime.steps, 3)

    def test_acceptance_controls_copy_only_the_sealed_j3_input_set(self):
        from scripts.c1_fixed_melee_runtime import c1_acceptance_control_capabilities
        controls = c1_acceptance_control_capabilities()
        document = json.loads(Path(
            "artifacts/normal-navigation/control-capability-v2.json"
        ).read_text("utf-8"))
        self.assertEqual(
            set(controls.allowed_controls),
            {tuple(item) for item in document["allowed_controls"]},
        )
        self.assertEqual(controls.source_fingerprints, ())

    def test_frozen_plan_has_four_success_directions_and_seven_declared_negatives(self):
        from scripts.c1_fixed_melee_runtime import c1_trial_plan
        plan = c1_trial_plan(21001)
        positives = [trial for trial in plan if trial["classification"] == "positive"]
        negatives = [trial for trial in plan if trial["classification"] == "negative"]
        self.assertEqual(len(plan), 47)
        self.assertEqual(len(positives), 40)
        self.assertEqual(
            {direction: sum(row["direction"] == direction for row in positives)
             for direction in {row["direction"] for row in positives}},
            {"north": 10, "east": 10, "south": 10, "west": 10},
        )
        self.assertEqual(
            {row["injection"] for row in negatives},
            {"approach_cancel", "aim_cancel", "after_submit_cancel",
             "target_revision", "wrong_crosshair", "known_wall",
             "confirmation_timeout"},
        )
        self.assertEqual(len({row["scenario_seed"] for row in plan}), 47)
        for trial in positives:
            start = trial["start_position"]
            target = trial["target_position"]
            center_distance = ((start["x"] - target["x"]) ** 2
                               + (start["z"] - target["z"]) ** 2) ** 0.5
            # A zombie is 0.6 blocks wide. Every positive starts outside
            # the three-block coarse melee surface range.
            self.assertGreater(center_distance - 0.3, 3.0)
            self.assertLess(center_distance, 4.0)
        by_injection = {row["injection"]: row for row in negatives}
        self.assertEqual(by_injection["aim_cancel"]["yaw_degrees"], 45.0)
        self.assertEqual(
            by_injection["aim_cancel"]["start_position"],
            {"x": 0.5, "y": 100.0, "z": -2.5},
        )
        self.assertEqual(
            by_injection["wrong_crosshair"]["start_position"],
            {"x": 0.5, "y": 100.0, "z": -2.5},
        )
        self.assertEqual(
            by_injection["known_wall"]["start_position"],
            {"x": 0.5, "y": 100.0, "z": -2.5},
        )
        self.assertEqual(
            by_injection["after_submit_cancel"]["start_position"],
            {"x": 0.5, "y": 100.0, "z": -2.5},
        )

    def test_guard_negative_waits_for_the_declared_crosshair_state(self):
        from scripts.c1_fixed_melee_runtime import _guard_target_ready

        wrong_entity = SimpleNamespace(hit_kind="entity", entity_ref="decoy")
        intended_entity = SimpleNamespace(hit_kind="entity", entity_ref="zombie")
        wall = SimpleNamespace(hit_kind="block", entity_ref=None)
        self.assertTrue(_guard_target_ready("wrong_crosshair", wrong_entity, "zombie"))
        self.assertFalse(_guard_target_ready("wrong_crosshair", intended_entity, "zombie"))
        self.assertTrue(_guard_target_ready("known_wall", wall, "zombie"))
        self.assertFalse(_guard_target_ready("known_wall", intended_entity, "zombie"))

    def test_crosshair_and_wall_negatives_require_exact_client_rejection(self):
        from scripts.c1_fixed_melee_runtime import _negative_passed
        report = SimpleNamespace(
            state="failed", reason="client_rejected/wrong_entity_target",
            attack_submissions=1, attack_submitted=False, hit_observed=False,
        )
        for injection, kind, reason in (
            ("wrong_crosshair", "entity", "wrong_entity_target"),
            ("known_wall", "block", "target_miss"),
        ):
            trial = {"injection": injection}
            evidence = {
                "selected_by_arbiter": True,
                "receipt_status": "rejected",
                "receipt_reason": reason,
                "targeting_hit_kind": kind,
            }
            report = SimpleNamespace(
                state="failed", reason="client_rejected/" + reason,
                attack_submissions=1, attack_submitted=False, hit_observed=False,
            )
            self.assertTrue(_negative_passed(trial, report, evidence))
            self.assertFalse(_negative_passed(
                trial, report, evidence | {"receipt_status": "pending_confirmation"},
            ))

    def test_manifest_freezes_identity_geometry_equipment_and_deadline(self):
        from scripts.c1_fixed_melee_runtime import write_c1_manifest
        with TemporaryDirectory() as tmp:
            path = write_c1_manifest(Path(tmp), world_seed=21001, code_hashes={"a.py": "abc"})
            value = json.loads(path.read_text("utf-8"))
        self.assertEqual(value["world_seed"], 21001)
        self.assertEqual(value["code_hashes"], {"a.py": "abc"})
        self.assertEqual(len(value["trials"]), 47)
        for trial in value["trials"]:
            self.assertEqual(set(trial["start_position"]), {"x", "y", "z"})
            self.assertEqual(set(trial["target_position"]), {"x", "y", "z"})
            self.assertEqual(trial["equipment"], "minecraft:stone_sword")
            self.assertEqual(trial["confirmation_timeout_ns"], 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
