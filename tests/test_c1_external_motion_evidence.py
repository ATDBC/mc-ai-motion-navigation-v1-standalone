"""C1-C replay reruns the recovery state machine over formal observations."""
from copy import deepcopy
import unittest

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import trace_projection
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.moving_melee_driver import MovingMeleeDriver
from scripts.c1_external_motion_evidence import _replay_rows
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
from tests.navigation_session_fixtures import FakeNavigationSession
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
        runtime, FakeNavigationSession(),
        clock_ns=lambda: clock[0],
    )
    driver.start(CombatTargetV1(
        "combat-task", "combat-goal", 1, "episode-1", TRACK,
    ), clock[0])
    backend.own_hurt, backend.own_health = 10, 18.0
    backend.own_ground, backend.own_velocity = False, (.10, .20, 0.0)
    driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
    backend.own_ground, backend.own_velocity = True, (.02, 0.0, 0.0)
    for _ in range(5):
        if driver.report.external_recoveries_completed:
            break
        driver.tick(BehaviorProfileV0(), clock[0] + 2_000_000_000)
    rows = [{
        "schema_version": "mc2p.trace-record.v0",
        "record_type": kind,
        "payload": trace_projection(payload),
    } for kind, payload in trace.records]
    runtime.close()
    return rows


class C1ExternalMotionEvidenceTests(unittest.TestCase):
    def test_actual_recovery_trace_replays(self):
        result = _replay_rows(actual_rows())
        self.assertTrue(result["passed"], result)
        self.assertGreaterEqual(result["replayed_records"], 3)
        self.assertFalse(result["simulated_server_physics"])

    def test_parallel_owner_is_rejected(self):
        rows = deepcopy(actual_rows())
        event = next(row for row in rows
                     if row["record_type"] == "external_motion_recovery"
                     and row["payload"].get("stage") == "decision")
        event["payload"]["movement_owner_source_ids"].append("second-owner")
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "movement_ownership")

    def test_tampered_recovery_decision_is_rejected(self):
        rows = deepcopy(actual_rows())
        event = next(row for row in rows
                     if row["record_type"] == "external_motion_recovery"
                     and row["payload"].get("stage") == "decision")
        event["payload"]["decision"]["reason"] = "tampered"
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "stability")

    def test_missing_parent_handoff_is_rejected(self):
        rows = [row for row in actual_rows()
                if not (row["record_type"] == "external_motion_recovery"
                        and row["payload"].get("stage") == "parent_handoff")]
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "incomplete_evidence")

    def test_tampered_detected_event_is_rejected(self):
        rows = deepcopy(actual_rows())
        event = next(row for row in rows
                     if row["record_type"] == "external_motion_recovery"
                     and row["payload"].get("stage") == "started")
        event["payload"]["event"]["health_delta_points"] = -19.0
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "event_identity")

    def test_missing_recovery_decisions_are_rejected(self):
        rows = [row for row in actual_rows()
                if not (row["record_type"] == "external_motion_recovery"
                        and row["payload"].get("stage") == "decision")]
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "incomplete_evidence")

    def test_reanchor_requires_completed_controller_decision(self):
        rows = deepcopy(actual_rows())
        for row in rows:
            if (row["record_type"] == "external_motion_recovery"
                    and row["payload"].get("stage") == "decision"
                    and row["payload"].get("decision", {}).get("complete") is True):
                row["payload"]["decision"]["complete"] = False
                break
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "stability")

    def test_logged_generation_cannot_override_detector_generation(self):
        rows = deepcopy(actual_rows())
        for row in rows:
            if row["record_type"] != "external_motion_recovery":
                continue
            payload = row["payload"]
            if payload.get("event") is not None:
                payload["event"]["generation"] = 99
                payload["event"]["event_id"] = payload["event"]["event_id"].rsplit("/", 1)[0] + "/99"
            if payload.get("decision") is not None:
                payload["decision"]["generation"] = 99
            if payload.get("completed_generation") is not None:
                payload["completed_generation"] = 99
        result = _replay_rows(rows)
        self.assertEqual(result["earliest_failure_layer"], "event_identity")


if __name__ == "__main__":
    unittest.main()
