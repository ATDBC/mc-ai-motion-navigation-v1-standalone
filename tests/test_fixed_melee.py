from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from tests.test_targeting import targeted_entity_snapshot


NOW = 101_000_000
TRACK = "entity-zombie-1"


class FixedMeleePolicyTests(unittest.TestCase):
    def target(self):
        from mc2p.skills.fixed_melee import CombatTargetV1
        return CombatTargetV1("combat-task-1", "goal-1", 1, "episode-1", TRACK)

    def decide(self, observation, phase, **kwargs):
        from mc2p.skills.fixed_melee import decide_fixed_melee
        values = dict(
            target=self.target(), phase=phase, generation=7, now_ns=NOW,
            controller_clock_id="controller-test",
        )
        values.update(kwargs)
        return decide_fixed_melee(observation, **values)

    def test_frozen_decision_table_has_one_feasible_choice_and_complete_candidates(self):
        from mc2p.skills.fixed_melee import (
            CANDIDATE_IDS, CandidateStatus, FixedMeleePhase,
        )
        cases = (
            (targeted_entity_snapshot(relative=(0, 0, 3.01)), FixedMeleePhase.APPROACHING,
             {}, "approach"),
            (targeted_entity_snapshot(targeted=False), FixedMeleePhase.ALIGNING,
             {}, "aim"),
            (targeted_entity_snapshot(hurt=3), FixedMeleePhase.WAITING_HURT_CLEAR,
             {}, "wait_hurt_clear"),
            (targeted_entity_snapshot(cooldown=.5), FixedMeleePhase.WAITING_COOLDOWN,
             {}, "wait_cooldown"),
            (targeted_entity_snapshot(), FixedMeleePhase.READY_TO_ATTACK,
             {}, "attack"),
            (targeted_entity_snapshot(sequence=2), FixedMeleePhase.CONFIRMING_HIT,
             dict(attack_observation_sequence_id=1, pre_attack_hurt_animation_ticks=0,
                  confirmation_deadline_ns=200_000_000), "wait_hit"),
            (targeted_entity_snapshot(sequence=2, hurt=10), FixedMeleePhase.CONFIRMING_HIT,
             dict(attack_observation_sequence_id=1, pre_attack_hurt_animation_ticks=0,
                  confirmation_deadline_ns=200_000_000), "complete"),
            (targeted_entity_snapshot(sequence=2), FixedMeleePhase.CONFIRMING_HIT,
             dict(now_ns=201_000_000, attack_observation_sequence_id=1,
                  pre_attack_hurt_animation_ticks=0,
                  confirmation_deadline_ns=200_000_000), "fail_confirmation_timeout"),
        )
        for observation, phase, changes, selected in cases:
            with self.subTest(selected=selected):
                decision = self.decide(observation, phase, **changes)
                self.assertEqual(decision.selected_candidate_id, selected)
                self.assertEqual(tuple(item.candidate_id for item in decision.candidates), CANDIDATE_IDS)
                self.assertEqual(
                    [item.status for item in decision.candidates].count(CandidateStatus.FEASIBLE), 1
                )
                self.assertEqual(
                    next(item for item in decision.candidates if item.candidate_id == selected).status,
                    CandidateStatus.FEASIBLE,
                )
                self.assertTrue(all(item.reason for item in decision.candidates))

    def test_existing_hurt_animation_must_clear_before_attack(self):
        from mc2p.skills.fixed_melee import FixedMeleePhase
        decision = self.decide(
            targeted_entity_snapshot(hurt=4), FixedMeleePhase.READY_TO_ATTACK,
        )
        self.assertEqual(decision.selected_candidate_id, "wait_hurt_clear")
        self.assertNotEqual(decision.selected_candidate_id, "attack")

    def test_three_block_coarse_range_defers_to_exact_client_reach_guard(self):
        from mc2p.skills.fixed_melee import FixedMeleePhase
        inside = self.decide(
            targeted_entity_snapshot(relative=(0, 0, 3.0)),
            FixedMeleePhase.READY_TO_ATTACK,
        )
        outside = self.decide(
            targeted_entity_snapshot(relative=(0, 0, 3.01)),
            FixedMeleePhase.READY_TO_ATTACK,
        )
        self.assertEqual(inside.selected_candidate_id, "attack")
        self.assertEqual(outside.selected_candidate_id, "approach")

    def test_reach_uses_crosshair_hit_surface_instead_of_entity_center(self):
        from mc2p.skills.fixed_melee import FixedMeleePhase
        observation = targeted_entity_snapshot(relative=(0, 0, 3.17))
        observation = replace(
            observation,
            targeting=replace(
                observation.targeting,
                value=replace(observation.targeting.value, distance_blocks=2.91),
            ),
        )
        decision = self.decide(observation, FixedMeleePhase.READY_TO_ATTACK)
        self.assertEqual(decision.assessment.horizontal_distance_blocks, 3.17)
        self.assertEqual(decision.assessment.target_hit_distance_blocks, 2.91)
        self.assertEqual(decision.selected_candidate_id, "attack")

    def test_close_entity_surface_is_aimed_before_requesting_more_approach(self):
        from mc2p.skills.fixed_melee import FixedMeleePhase
        decision = self.decide(
            targeted_entity_snapshot(relative=(0, 0, 3.13), targeted=False),
            FixedMeleePhase.ALIGNING,
        )
        self.assertAlmostEqual(
            decision.assessment.coarse_surface_distance_blocks, 2.83, places=6,
        )
        self.assertEqual(decision.selected_candidate_id, "aim")

    def test_missing_new_hurt_observation_fails_closed(self):
        from mc2p.skills.fixed_melee import FixedMeleePhase
        observation = targeted_entity_snapshot()
        entity = replace(observation.perception.value.visible_entities[0],
                         hurt_animation_ticks=None)
        observation = replace(
            observation,
            perception=replace(observation.perception,
                value=replace(observation.perception.value, visible_entities=(entity,))),
        )
        decision = self.decide(observation, FixedMeleePhase.READY_TO_ATTACK)
        self.assertEqual(decision.selected_candidate_id, "fail_missing_hurt_observation")

    def test_hit_confirmation_requires_later_sequence_and_zero_baseline(self):
        from mc2p.skills.fixed_melee import FixedMeleePhase
        for sequence, baseline, expected in (
            (1, 0, "wait_hit"),
            (2, 4, "fail_invalid_attack_baseline"),
            (2, 0, "complete"),
        ):
            with self.subTest(sequence=sequence, baseline=baseline):
                decision = self.decide(
                    targeted_entity_snapshot(sequence=sequence, hurt=8),
                    FixedMeleePhase.CONFIRMING_HIT,
                    attack_observation_sequence_id=1,
                    pre_attack_hurt_animation_ticks=baseline,
                    confirmation_deadline_ns=200_000_000,
                )
                self.assertEqual(decision.selected_candidate_id, expected)

    def test_stable_attack_position_uses_target_direction_and_rejects_zero_vector(self):
        from mc2p.skills.fixed_melee import stable_attack_position
        self.assertEqual(
            stable_attack_position(Vec3V0(.5, 64, .5), Vec3V0(0, 0, 5)),
            Vec3V0(.5, 64, 3.0),
        )
        with self.assertRaises(ContractViolation):
            stable_attack_position(Vec3V0(.5, 64, .5), Vec3V0(0, 0, 0))

    def test_target_and_decision_inputs_are_strict(self):
        from mc2p.skills.fixed_melee import CombatTargetV1, FixedMeleePhase
        for changes in ({"revision": -1}, {"track_id": ""}, {"episode_id": True}):
            with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                CombatTargetV1("task", "goal", 1, "episode", TRACK).__class__(
                    **({"task_id": "task", "goal_id": "goal", "revision": 1,
                        "episode_id": "episode", "track_id": TRACK} | changes)
                )
        with self.assertRaises(ContractViolation):
            self.decide(targeted_entity_snapshot(), FixedMeleePhase.READY_TO_ATTACK,
                        controller_clock_id="")


if __name__ == "__main__":
    unittest.main()
