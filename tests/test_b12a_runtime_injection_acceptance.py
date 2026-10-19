"""Frozen B12-A control-fault matrix; every case starts from a fresh Runtime."""
from dataclasses import replace
import unittest

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, AttackEntityV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import trace_projection
from mc2p.skills.attack_evidence import (
    AttackAttemptKeyV1, AttackAttemptOutcome, AttackAttemptPhase,
    AttackAttemptReportV1, AttackEvidenceGrade, AttackRetryLedgerV1,
    AttackTaskOutcome, advance_attack_retry,
)
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver
from mc2p.skills.attack_evidence_replay import replay_attack_attempt
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
from tests.test_player_runtime import _RecordingTrace


class B12ARuntimeInjectionAcceptanceTests(unittest.TestCase):
    def fresh(self):
        clock = [100_000_000]
        backend = MeleeBackend(clock)
        trace = _RecordingTrace()
        runtime = PlayerRuntimeV1(backend, trace, lambda: clock[0])
        self.assertTrue(runtime.reset(ResetRequestV0(
            "reset", "episode-1", "test", 1, 2_000_000_000,
        )).succeeded)
        target = CombatTargetV1(
            "combat-task", "combat-goal", 1, "episode-1", TRACK,
        )
        return clock, backend, runtime, target, trace

    @staticmethod
    def tick_until_submitted(driver, profile, clock):
        while not driver.report.attack_submitted:
            driver.tick(profile, clock[0] + 2_000_000_000)

    def assert_replay_matches(self, trace, driver):
        rows = [
            {"record_type": kind, "payload": trace_projection(payload)}
            for kind, payload in trace.records
        ]
        replayed = replay_attack_attempt(rows, driver.attempt_report.key)
        self.assertIs(replayed.outcome, driver.attempt_report.outcome)

    def test_arbitration_deferred_twice_without_consuming_runtime(self):
        for repeat in (1, 2):
            with self.subTest(repeat=repeat):
                clock, backend, runtime, target, trace = self.fresh()
                try:
                    backend.confirm_hit = False
                    driver = MeleeStrikeDriver(runtime, clock_ns=lambda: clock[0])
                    driver.start(target, clock[0])
                    driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
                    source = runtime.register_ordered_source("competing-operation")
                    observation = runtime.observation
                    intent = ActionIntentV1(
                        ordered_intent_id(source, 1), source.source_id,
                        source.episode_id, observation.sequence_id,
                        ActionPriorityV0.PLAYER, clock[0],
                        clock[0] + 250_000_000,
                        operation=AttackEntityV1("entity-zombie-other"),
                    )
                    driver.tick(
                        BehaviorProfileV0(), clock[0] + 2_000_000_000,
                        additional_proposals=(ControlFrameProposalV1((
                            OrderedIntentV1(source, 1, intent),
                        )),),
                    )
                    self.assertIs(
                        driver.attempt_report.outcome,
                        AttackAttemptOutcome.DEFERRED_BY_ARBITRATION,
                    )
                    self.assert_replay_matches(trace, driver)
                    self.assertEqual(runtime.state.value, "ready")
                finally:
                    runtime.close()

    def test_input_failure_twice_without_sealing_runtime(self):
        for repeat in (1, 2):
            with self.subTest(repeat=repeat):
                clock, backend, runtime, target, trace = self.fresh()
                try:
                    backend.reject_reason = "backend_rejected"
                    driver = MeleeStrikeDriver(runtime, clock_ns=lambda: clock[0])
                    driver.start(target, clock[0])
                    for _ in range(12):
                        if driver.report.terminal:
                            break
                        driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
                    self.assertIs(
                        driver.attempt_report.outcome,
                        AttackAttemptOutcome.INPUT_FAILED,
                    )
                    self.assert_replay_matches(trace, driver)
                    self.assertEqual(runtime.state.value, "ready")
                finally:
                    runtime.close()

    def test_observation_interruption_twice_is_typed(self):
        for repeat in (1, 2):
            with self.subTest(repeat=repeat):
                clock, backend, runtime, target, trace = self.fresh()
                try:
                    backend.confirm_hit = False
                    driver = MeleeStrikeDriver(runtime, clock_ns=lambda: clock[0])
                    driver.start(target, clock[0])
                    profile = BehaviorProfileV0()
                    self.tick_until_submitted(driver, profile, clock)
                    driver.interrupt_for_external_motion(profile, "test_gap")
                    driver.observe_after_external_motion(replace(
                        runtime.observation,
                        controller_clock_id="replacement-clock",
                    ))
                    self.assertIs(
                        driver.attempt_report.outcome,
                        AttackAttemptOutcome.OBSERVATION_INTERRUPTED,
                    )
                    self.assert_replay_matches(trace, driver)
                    self.assertEqual(runtime.state.value, "ready")
                finally:
                    runtime.close()

    def test_world_session_handoff_cancels_old_attempt_twice(self):
        for repeat in (1, 2):
            with self.subTest(repeat=repeat):
                clock, backend, runtime, target, trace = self.fresh()
                try:
                    backend.confirm_hit = False
                    driver = MeleeStrikeDriver(runtime, clock_ns=lambda: clock[0])
                    driver.start(target, clock[0])
                    profile = BehaviorProfileV0()
                    self.tick_until_submitted(driver, profile, clock)
                    driver.cancel(profile, "world_session_changed")
                    self.assertIs(
                        driver.attempt_report.outcome,
                        AttackAttemptOutcome.CANCELLED,
                    )
                    self.assert_replay_matches(trace, driver)
                    self.assertEqual(runtime.state.value, "ready")
                finally:
                    runtime.close()

    def test_each_retry_budget_exhausts_only_its_own_class_twice(self):
        cases = (
            (AttackAttemptOutcome.GATE_REJECTED,
             AttackTaskOutcome.GATE_RETRY_EXHAUSTED),
            (AttackAttemptOutcome.INPUT_FAILED,
             AttackTaskOutcome.INPUT_RETRY_EXHAUSTED),
            (AttackAttemptOutcome.CONFIRMATION_TIMEOUT,
             AttackTaskOutcome.CONFIRMATION_RETRY_EXHAUSTED),
        )
        for outcome, expected in cases:
            for repeat in (1, 2):
                with self.subTest(outcome=outcome.value, repeat=repeat):
                    ledger = AttackRetryLedgerV1()
                    report = self._terminal(outcome)
                    ledger, first = advance_attack_retry(
                        ledger, report, gate_limit=2, input_limit=2,
                        confirmation_limit=2,
                    )
                    ledger, second = advance_attack_retry(
                        ledger, report, gate_limit=2, input_limit=2,
                        confirmation_limit=2,
                    )
                    self.assertIsNone(first)
                    self.assertIs(second, expected)

    @staticmethod
    def _terminal(outcome: AttackAttemptOutcome) -> AttackAttemptReportV1:
        submitted = outcome is AttackAttemptOutcome.CONFIRMATION_TIMEOUT
        return AttackAttemptReportV1(
            AttackAttemptKeyV1(
                "episode-1", "combat-task", "combat-goal", 1, TRACK, 1,
            ),
            AttackAttemptPhase.TERMINAL,
            outcome,
            AttackEvidenceGrade.NONE,
            intent_id="intent-1",
            action_request_sequence_id=2,
            attack_observation_sequence_id=3 if submitted else None,
            confirmation_deadline_ns=4 if submitted else None,
            receipt_status=(
                "pending_confirmation" if submitted
                else "operation_rejected" if outcome is AttackAttemptOutcome.GATE_REJECTED
                else "rejected"
            ),
        )


if __name__ == "__main__":
    unittest.main()
