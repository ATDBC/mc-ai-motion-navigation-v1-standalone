from dataclasses import replace
import unittest

from mc2p.contracts import action_v1 as values
from mc2p.contracts.action import ActionPriorityV0, ActionSnapshotV0
from mc2p.contracts.common import ContractViolation


class FormalArbiterTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(values, "ActionIntentV1"), "formal intent contract is missing")
        from mc2p.runtime.arbiter_v1 import ActionArbiterV1
        self.arbiter = ActionArbiterV1()

    def intent(self, name, **kwargs):
        return values.ActionIntentV1(name, kwargs.pop("source_id", name), "episode", 0,
            kwargs.pop("priority", ActionPriorityV0.TASK), 1, kwargs.pop("expires_at_monotonic_ns", 100), **kwargs)

    def resolve(self, now=10, observation=0, **kwargs):
        return self.arbiter.resolve(now, "episode", observation, observation, 100, **kwargs)

    def test_movement_persists_but_relative_look_is_consumed_once(self):
        self.arbiter.submit(self.intent("move", movement=values.MovementV1(forward=1)))
        self.arbiter.submit(self.intent("look", look=values.LookV1(12, 3)))
        first = self.resolve()
        second = self.resolve(observation=1)
        self.assertEqual(first.action.look, values.LookV1(12, 3))
        self.assertEqual(second.action.look, values.LookV1())
        self.assertEqual(second.action.movement, values.MovementV1(forward=1))
        self.assertEqual(first.selected_intents, (("movement", "move"), ("look", "look")))
        self.assertEqual(first.action.valid_for_ticks, 1)

    def test_verified_movement_window_follows_winner_and_is_consumed_once(self):
        window = values.MovementTickWindowV1(11, 12)
        self.arbiter.submit(self.intent(
            "verified",
            movement=values.MovementV1(forward=1, jump=True),
            movement_tick_window=window,
        ))

        first = self.resolve()
        second = self.resolve(observation=1)

        self.assertEqual(first.movement_tick_window, window)
        self.assertEqual(first.action.movement.jump, True)
        self.assertIsNone(second.movement_tick_window)
        self.assertEqual(second.action.movement, values.MovementV1())

    def test_heading_bound_movement_is_once_only_and_requires_own_look_winner(self):
        for forbidden, other in (((), False), (("look",), False), ((), True)):
            self.arbiter.clear()
            self.arbiter.submit(self.intent("coupled", movement=values.MovementV1(forward=1),
                look=values.LookV1(), movement_requires_look=True))
            if other:
                self.arbiter.submit(self.intent("human", look=values.LookV1(90,0), priority=ActionPriorityV0.PLAYER))
            result = self.resolve(forbidden_actions=forbidden)
            self.assertEqual(result.action.movement.forward, 0 if forbidden or other else 1)
            if forbidden or other:
                self.assertIn(("coupled", "required_look_not_selected"), result.suppressed_intents)
            self.assertEqual(self.resolve(observation=1).action.movement, values.MovementV1())

    def test_heading_bound_ground_movement_accepts_only_a_nearby_same_frame_look(self):
        for combat_yaw, expected_forward in ((14.9, 1), (15.1, 0)):
            with self.subTest(combat_yaw=combat_yaw):
                self.arbiter.clear()
                self.arbiter.submit(self.intent(
                    "navigation", movement=values.MovementV1(forward=1),
                    look=values.LookV1(10, 0), movement_requires_look=True,
                    movement_look_tolerance_degrees=5.0,
                ))
                self.arbiter.submit(self.intent(
                    "combat", look=values.LookV1(combat_yaw, -3),
                    operation=values.AttackEntityV1("entity-session-7"),
                    priority=ActionPriorityV0.PLAYER,
                ))
                result = self.resolve()
                self.assertEqual(result.action.movement.forward, expected_forward)
                self.assertEqual(result.action.look, values.LookV1(combat_yaw, -3))
                self.assertIsInstance(result.action.operation, values.AttackEntityV1)

    def test_ground_movement_can_keep_observed_heading_under_an_alternate_look(self):
        movements = (
            values.MovementV1(forward=1),
            values.MovementV1(strafe=1),
            values.MovementV1(strafe=-1),
            values.MovementV1(forward=-1),
        )
        for movement in movements:
            with self.subTest(movement=movement):
                self.arbiter.clear()
                self.arbiter.submit(self.intent(
                    "navigation", movement=movement,
                    movement_observed_yaw_limit_degrees=5.0,
                ))
                self.arbiter.submit(self.intent(
                    "combat", look=values.LookV1(2, -3),
                    operation=values.AttackEntityV1("entity-session-7"),
                    priority=ActionPriorityV0.PLAYER,
                ))

                result = self.resolve()

                self.assertEqual(result.action.movement, movement)
                self.assertEqual(result.action.look, values.LookV1(2, -3))
                self.assertIsInstance(result.action.operation, values.AttackEntityV1)

    def test_large_alternate_turn_still_suppresses_heading_bound_movement(self):
        window = values.MovementTickWindowV1(11, 12)
        self.arbiter.submit(self.intent(
            "navigation", movement=values.MovementV1(strafe=1),
            movement_observed_yaw_limit_degrees=5.0,
            movement_tick_window=window,
        ))
        self.arbiter.submit(self.intent(
            "combat", look=values.LookV1(5.1, 0),
            priority=ActionPriorityV0.PLAYER,
        ))

        result = self.resolve()

        self.assertEqual(result.action.movement, values.MovementV1())
        self.assertEqual(result.action.look, values.LookV1(5.1, 0))
        self.assertIsNone(result.movement_tick_window)
        self.assertIn(
            ("navigation", "observed_yaw_limit_exceeded"),
            result.suppressed_intents,
        )

    def test_observed_yaw_bound_requires_fresh_one_tick_movement(self):
        for kwargs in (
            {"look": values.LookV1(), "movement_observed_yaw_limit_degrees": 5.0},
            {"movement": values.MovementV1(forward=1), "valid_for_ticks": 2,
             "movement_observed_yaw_limit_degrees": 5.0},
            {"movement": values.MovementV1(forward=1),
             "movement_observed_yaw_limit_degrees": 5.1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractViolation):
                self.intent("invalid-yaw-bound", **kwargs)

        self.arbiter.submit(self.intent(
            "stale-navigation", movement=values.MovementV1(forward=1),
            movement_observed_yaw_limit_degrees=5.0,
        ))
        stale = self.resolve(observation=1)
        self.assertEqual(stale.action.movement, values.MovementV1())
        self.assertIn(
            ("stale-navigation", "movement_observation_stale"),
            stale.suppressed_intents,
        )

    def test_heading_binding_requires_both_groups_and_one_tick(self):
        for kwargs in ({"movement": values.MovementV1()}, {"look": values.LookV1()},
                       {"movement": values.MovementV1(), "look": values.LookV1(), "valid_for_ticks": 2}):
            with self.assertRaises(ContractViolation):
                self.intent("invalid", movement_requires_look=True, **kwargs)

    def test_operation_and_duplicate_id_are_not_replayed(self):
        intent = self.intent("click", operation=values.ClickSlotV1("gui", 1, 0, 0, 0, "pickup"))
        self.arbiter.submit(intent)
        self.assertIsNotNone(self.resolve().action.operation)
        self.assertIsNone(self.resolve(observation=1).action.operation)
        with self.assertRaises(ContractViolation): self.arbiter.submit(intent)
        with self.assertRaises(ContractViolation): self.arbiter.submit(replace(intent, observation_sequence_id=1))

    def test_targeted_attack_is_identity_bound_once_only_and_forbiddable(self):
        operation = values.AttackEntityV1("entity-session-7")
        intent = values.ActionIntentV1(
            "attack-1", "combat", "episode", 0, ActionPriorityV0.TASK,
            1, 100, operation=operation,
        )
        self.arbiter.submit(intent)
        decision = self.resolve()
        self.assertEqual(decision.action.operation, operation)
        self.assertEqual(dict(decision.selected_intents)["operation"], "attack-1")
        self.assertIsNone(self.resolve(observation=1).action.operation)

        self.arbiter.submit(self.intent("blocked", operation=operation))
        blocked = self.resolve(observation=1, forbidden_actions=("attack_entity",))
        self.assertIsNone(blocked.action.operation)
        self.assertIn(("blocked", "task_forbidden_attack_entity"), blocked.suppressed_intents)
        self.assertIsNone(self.resolve(observation=2).action.operation)

    def test_targeted_attack_can_share_one_snapshot_with_movement_and_look(self):
        intents = (
            self.intent("move", movement=values.MovementV1(forward=1)),
            self.intent("look", look=values.LookV1(12, -3)),
            self.intent("attack", operation=values.AttackEntityV1("entity-session-7")),
        )
        decisions = []
        for order in (intents, tuple(reversed(intents))):
            self.arbiter.clear()
            for intent in order:
                self.arbiter.submit(intent)
            decisions.append(self.resolve())
        for decision in decisions:
            self.assertEqual(decision.action.movement, values.MovementV1(forward=1))
            self.assertEqual(decision.action.look, values.LookV1(12, -3))
            self.assertEqual(
                decision.action.operation,
                values.AttackEntityV1("entity-session-7"),
            )
            self.assertEqual(
                dict(decision.selected_intents),
                {"movement": "move", "look": "look", "operation": "attack"},
            )

    def test_targeted_attack_declares_bounded_cooldown_requirement(self):
        self.assertEqual(
            values.AttackEntityV1("entity-session-7").minimum_cooldown_progress,
            1.0,
        )
        self.assertEqual(
            values.AttackEntityV1("entity-session-7", 0.65).minimum_cooldown_progress,
            0.65,
        )
        for invalid in (-0.01, 1.01, float("nan"), float("inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ContractViolation):
                values.AttackEntityV1("entity-session-7", invalid)

    def test_targeted_attack_rejects_invalid_identity_and_unknown_operation(self):
        for entity_ref in ("", "x" * 129, True):
            with self.subTest(entity_ref=entity_ref), self.assertRaises(ContractViolation):
                values.AttackEntityV1(entity_ref)
        with self.assertRaises(ContractViolation):
            self.intent("unknown-operation", operation=object())

    def test_source_cancel_keeps_other_movement_and_expiry_releases(self):
        self.arbiter.submit(self.intent("move", movement=values.MovementV1(forward=1), expires_at_monotonic_ns=12))
        self.arbiter.submit(self.intent("look", look=values.LookV1(12, 0)))
        self.assertEqual(self.arbiter.cancel_source("look"), ("look",))
        first = self.resolve()
        self.assertEqual(first.action.look, values.LookV1())
        self.assertEqual(first.action.deadline_monotonic_ns, 12)
        ended = self.resolve(now=12, observation=1)
        self.assertEqual(ended.action.movement, values.MovementV1())
        self.assertIn(("move", "expired"), ended.suppressed_intents)

    def test_operation_control_conflict_respects_priority_without_deferred_click(self):
        self.arbiter.submit(self.intent("move", movement=values.MovementV1(forward=1), priority=ActionPriorityV0.PLAYER))
        self.arbiter.submit(self.intent("open", operation=values.OpenInventoryV1()))
        first = self.resolve()
        self.assertIsNone(first.action.operation)
        self.assertEqual(first.action.movement.forward, 1)
        self.assertIn(("open", "operation_control_conflict"), first.suppressed_intents)
        self.arbiter.cancel_source("move")
        self.assertIsNone(self.resolve(observation=1).action.operation)

    def test_operation_winner_suppresses_controls_and_gui_blocks_resumption(self):
        self.arbiter.submit(self.intent("move", movement=values.MovementV1(forward=1)))
        self.arbiter.submit(self.intent("open", operation=values.OpenInventoryV1(), priority=ActionPriorityV0.PLAYER))
        first = self.resolve()
        self.assertEqual(first.action.movement, values.MovementV1())
        self.assertEqual(first.action.operation, values.OpenInventoryV1())
        blocked = self.resolve(observation=1, controls_blocked=True)
        self.assertEqual(blocked.action.movement, values.MovementV1())
        self.assertIn(("move", "gui_controls_blocked"), blocked.suppressed_intents)
        self.assertEqual(self.resolve(observation=2).action.movement.forward, 1)

    def test_stale_or_wrong_episode_one_shots_cannot_be_retargeted(self):
        self.arbiter.submit(self.intent("stale", operation=values.OpenInventoryV1()))
        self.arbiter.submit(replace(self.intent("foreign", movement=values.MovementV1(forward=1)), episode_id="old"))
        decision = self.resolve(observation=1)
        self.assertIsNone(decision.action.operation)
        self.assertEqual(decision.action.movement, values.MovementV1())
        self.assertIn(("stale", "stale_observation"), decision.suppressed_intents)
        self.assertIn(("foreign", "wrong_episode"), decision.suppressed_intents)

    def test_ties_and_losing_look_are_deterministic_and_not_replayed(self):
        for name, angle in (("a", 2), ("b", 3)):
            self.arbiter.submit(self.intent(name, look=values.LookV1(angle, 0)))
        self.assertEqual(self.resolve().action.look.yaw_delta_degrees, 3)
        self.assertEqual(self.resolve(observation=1).action.look, values.LookV1())

    def test_bounds_and_clear_do_not_silently_evict(self):
        for index in range(128):
            self.arbiter.submit(self.intent(f"move-{index}", movement=values.MovementV1(forward=1)))
        with self.assertRaises(ContractViolation):
            self.arbiter.submit(self.intent("overflow", operation=values.OpenInventoryV1()))
        self.assertEqual(len(self.resolve().candidate_intent_ids), 128)
        self.arbiter.clear()
        for index in range(4096):
            self.arbiter.submit(self.intent(f"once-{index}", look=values.LookV1()))
            self.resolve()
        with self.assertRaises(ContractViolation):
            self.arbiter.submit(self.intent("ledger-overflow", look=values.LookV1()))
        self.arbiter.clear()
        self.arbiter.submit(self.intent("fresh", look=values.LookV1(1, 0)))
        self.assertEqual(self.resolve().action.look.yaw_delta_degrees, 1)

    def test_invalid_contracts_and_expired_resolve_cannot_consume_intents(self):
        for kwargs in ({}, {"movement": ActionSnapshotV0.neutral(0)}, {"priority": 200, "look": values.LookV1()},
                       {"expires_at_monotonic_ns": 1, "look": values.LookV1()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractViolation): self.intent("invalid", **kwargs)
        with self.assertRaises(ContractViolation): self.arbiter.submit(ActionSnapshotV0.neutral(0))
        self.arbiter.submit(self.intent("once", operation=values.OpenInventoryV1()))
        with self.assertRaises(TimeoutError): self.resolve(now=100)
        self.assertIsNotNone(self.resolve().action.operation)
