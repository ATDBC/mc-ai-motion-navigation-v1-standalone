"""C1-B replay uses actual runtime records, not a post-hoc trial summary."""
from copy import deepcopy
import unittest

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import trace_projection
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.moving_melee_driver import MovingMeleeDriver
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.point_goal_policy import PointGoalPolicy
from scripts.c1_moving_melee_evidence import _replay_rows
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
from tests.test_navigation_joint_policy import TestCapabilities
from tests.test_player_runtime import _RecordingTrace


def actual_rows():
    clock = [100_000_000]
    backend = MeleeBackend(clock, distance=5.0)
    trace = _RecordingTrace()
    runtime = PlayerRuntimeV1(backend, trace, lambda: clock[0])
    assert runtime.reset(ResetRequestV0(
        "reset", "episode-1", "test", 1, 5_000_000_000,
    )).succeeded
    driver = MovingMeleeDriver(
        runtime, NavigationState("scope"),
        PointGoalPolicy("D", control_capabilities=TestCapabilities()),
        clock_ns=lambda: clock[0],
    )
    driver.start(CombatTargetV1(
        "combat-task", "combat-goal", 1, "episode-1", TRACK,
    ), clock[0])
    driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
    rows = [{
        "schema_version": "mc2p.trace-record.v0",
        "record_type": kind,
        "payload": trace_projection(payload),
    } for kind, payload in trace.records]
    runtime.close()
    return rows


class C1MovingMeleeEvidenceTests(unittest.TestCase):
    def test_actual_frame_decisions_replay(self):
        result = _replay_rows(actual_rows())
        self.assertTrue(result["passed"], result)
        self.assertGreaterEqual(result["replayed_decisions"], 3)
        self.assertFalse(result["simulated_entity_ai"])

    def test_tampered_moving_decision_is_attributed(self):
        rows = deepcopy(actual_rows())
        event = next(row for row in rows
                     if row["record_type"] == "moving_melee_decision")
        event["payload"]["decision"]["reason"] = "tampered"
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "moving_decision")

    def test_tampered_goal_predecessor_is_attributed(self):
        rows = deepcopy(actual_rows())
        event = next(row for row in rows
                     if row["record_type"] == "moving_goal_decision")
        event["payload"]["previous"] = event["payload"]["decision"]
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "moving_goal")

    def test_missing_dispatch_step_is_attributed(self):
        rows = deepcopy(actual_rows())
        dispatch = next(row for row in rows if row["record_type"] == "dispatch")
        dispatch["payload"]["decision"]["action"]["request_sequence_id"] = 999
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "arbitration_output")

    def test_decision_with_unknown_observation_is_rejected(self):
        rows = deepcopy(actual_rows())
        for row in rows:
            if row["record_type"] in {"moving_melee_decision", "moving_goal_decision"}:
                row["payload"]["observation_sequence_id"] = 999999
        result = _replay_rows(rows)
        self.assertIn(result["earliest_failure_layer"], {"moving_decision", "moving_goal"})

    def test_missing_engagement_chain_is_rejected(self):
        rows = [row for row in actual_rows() if row["record_type"] != "moving_engagement"]
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "incomplete_evidence")

    def test_goal_fact_must_come_from_replayed_engagement(self):
        rows = deepcopy(actual_rows())
        event = next(row for row in rows if row["record_type"] == "moving_goal_decision")
        event["payload"]["fact"]["relative_position"]["z"] += 1.0
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "moving_goal")


if __name__ == "__main__":
    unittest.main()
