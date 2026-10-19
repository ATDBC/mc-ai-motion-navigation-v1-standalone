from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.segmented_trace import (
    SegmentedJsonlWriter, SegmentedTraceWriter, iter_segmented_jsonl,
)
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.attack_evidence import AttackAttemptKeyV1, AttackAttemptOutcome
from mc2p.skills.attack_evidence_replay import replay_attack_attempt
from mc2p.skills.fixed_melee_driver import FixedMeleeDriver
from tests.navigation_session_fixtures import FakeNavigationSession
from tests.test_fixed_melee_driver import MeleeBackend, TRACK


class C1MeleeEvidenceTests(unittest.TestCase):
    def make_trace(self, root: Path) -> Path:
        trace_path = root / "trace"
        clock = [100_000_000]
        backend = MeleeBackend(clock)
        runtime = PlayerRuntimeV1(
            backend, SegmentedTraceWriter(trace_path), lambda: clock[0],
        )
        self.assertTrue(runtime.reset(
            ResetRequestV0("reset", "episode-1", "test", 1, 2_000_000_000)
        ).succeeded)
        driver = FixedMeleeDriver(
            runtime, FakeNavigationSession(),
            clock_ns=lambda: clock[0],
        )
        driver.start(
            CombatTargetV1("combat-task", "combat-goal", 1, "episode-1", TRACK),
            clock[0],
        )
        for _ in range(12):
            driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
            if driver.report.terminal:
                break
        self.assertEqual(driver.report.state, "complete")
        runtime.close()
        return trace_path

    @staticmethod
    def rewrite(root: Path, rows: list[dict]) -> Path:
        path = root / "trace"
        with closing(SegmentedJsonlWriter(path)) as writer:
            for row in rows:
                writer.write(row)
        return path

    def test_complete_driver_trace_replays_the_shared_policy(self):
        from scripts.c1_melee_evidence import replay_c1_melee
        with TemporaryDirectory() as tmp:
            trace = self.make_trace(Path(tmp))
            rows = list(iter_segmented_jsonl(trace))
            result = replay_c1_melee(trace)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["replayed_decisions"], 2)
        self.assertIsNone(result["earliest_failure_layer"])
        event = next(row["payload"] for row in rows
                     if row["record_type"] == "attack_attempt")
        key = AttackAttemptKeyV1(
            event["episode_id"], event["task_id"], event["goal_id"],
            event["target_revision"], event["track_id"],
            event["attempt_sequence"],
        )
        self.assertIs(
            replay_attack_attempt(rows, key).outcome,
            AttackAttemptOutcome.COMMAND_CORRELATED_HIT,
        )

    def test_each_tamper_is_attributed_to_its_earliest_layer(self):
        from scripts.c1_melee_evidence import replay_c1_melee
        mutations = {
            "situation_assessment": lambda rows: next(
                row for row in rows if row["record_type"] == "combat_assessment"
            )["payload"]["assessment"].update(horizontal_distance_blocks=99.0),
            "candidate_coverage": lambda rows: next(
                row for row in rows if row["record_type"] == "combat_candidates"
            )["payload"]["candidates"].pop(),
            "selection": lambda rows: next(
                row for row in rows if row["record_type"] == "combat_selection"
            )["payload"].update(selected_candidate_id="aim"),
            "skill_execution": lambda rows: next(
                row for row in rows if row["record_type"] == "combat_skill"
            )["payload"]["operation"].update(entity_ref="entity-other"),
            "arbitration": lambda rows: next(
                row for row in rows
                if row["record_type"] == "dispatch"
                and row["payload"]["decision"]["action"]["operation"] is not None
            )["payload"]["decision"]["action"]["operation"].update(entity_ref="entity-other"),
            "motion": lambda rows: next(
                row for row in rows
                if row["record_type"] == "step"
                and row["payload"]["backend_result"]["receipt"]["status"] == "pending_confirmation"
            )["payload"]["backend_result"]["receipt"].update(
                status="rejected", reason="wrong_entity_target"
            ),
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.make_trace(root / "original")
            original_rows = list(iter_segmented_jsonl(original))
            for layer, mutate in mutations.items():
                with self.subTest(layer=layer):
                    rows = deepcopy(original_rows)
                    mutate(rows)
                    result = replay_c1_melee(self.rewrite(root / layer, rows))
                    self.assertFalse(result["passed"])
                    self.assertEqual(result["earliest_failure_layer"], layer)

    def test_trace_delivery_gap_blocks_success_before_replay(self):
        from scripts.c1_melee_evidence import replay_c1_melee
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = list(iter_segmented_jsonl(self.make_trace(root / "original")))
            rows.append({
                "schema_version": "mc2p.trace-record.v0",
                "record_type": "trace_delivery_summary",
                "payload": {"dropped_records": 1},
            })
            result = replay_c1_melee(self.rewrite(root / "gapped", rows))
        self.assertFalse(result["passed"])
        self.assertEqual(result["earliest_failure_layer"], "incomplete_evidence")

    def test_multiple_task_identities_can_share_one_sealed_runtime_trace(self):
        from scripts.c1_melee_evidence import replay_c1_melee
        with TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace"
            clock = [100_000_000]
            backend = MeleeBackend(clock)
            runtime = PlayerRuntimeV1(
                backend, SegmentedTraceWriter(trace_path), lambda: clock[0],
            )
            self.assertTrue(runtime.reset(
                ResetRequestV0("reset", "episode-1", "test", 1, 2_000_000_000)
            ).succeeded)
            try:
                for index in range(2):
                    backend.hurt = 0
                    backend.track = f"entity-zombie-{index + 1}"
                    task = TaskIntentV0(
                        f"refresh-{index}", "test_refresh", "{}",
                        (SuccessCriterionV0(
                            "observation", ComparisonOperatorV0.GREATER_THAN, 0, "frames",
                        ),),
                        100, clock[0] + 2_000_000_000, True, 0.0,
                    )
                    runtime.step(
                        task, BehaviorProfileV0(), clock[0] + 2_000_000_000,
                    )
                    driver = FixedMeleeDriver(
                        runtime, FakeNavigationSession(),
                        clock_ns=lambda: clock[0],
                    )
                    driver.start(CombatTargetV1(
                        f"combat-task-{index}", f"combat-goal-{index}", 1,
                        "episode-1", backend.track,
                    ), clock[0])
                    for _ in range(12):
                        driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
                        if driver.report.terminal:
                            break
                    self.assertEqual(driver.report.state, "complete")
            finally:
                runtime.close()
            result = replay_c1_melee(trace_path)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["replayed_decisions"], 4)


if __name__ == "__main__":
    unittest.main()
