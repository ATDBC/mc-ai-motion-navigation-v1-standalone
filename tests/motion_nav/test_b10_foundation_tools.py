import unittest


class B10FoundationToolTests(unittest.TestCase):
    def test_complete_check_benchmark_covers_single_and_small_batch_horizons(self):
        from scripts.benchmark_b10_motion_foundation import run_benchmark
        result = run_benchmark(repetitions=1, batch_sizes=(2,))
        self.assertEqual(result["schema_version"], "mc2p.b10-foundation-benchmark.v1")
        self.assertEqual(
            {(row["ticks"], row["branches"]) for row in result["results"]},
            {(1, 1), (20, 1), (60, 1), (20, 2)},
        )
        self.assertTrue(all(row["wall_p99_ms"] >= 0 for row in result["results"]))
        self.assertTrue(all(row["cpu_p99_ms"] >= 0 for row in result["results"]))

    def test_input_chain_validator_joins_command_application_and_physics_tick(self):
        from scripts.validate_b10_input_chain import validate_rows
        trace = ({
            "record_type": "step",
            "payload": {
                "decision": {"action": {
                    "request_sequence_id": 3,
                    "movement": {"forward": 1, "strafe": 0, "jump": True,
                                 "sneak": False, "sprint": True},
                }},
                "backend_result": {"receipt": {
                    "schema_version": "mc2p.client_action_receipt.v3",
                    "input_applications": [{
                        "schema_version": "mc2p.input-application.v1",
                        "movement_tick_id": 7, "episode_id": "ep",
                        "request_sequence_id": 3, "sampled_at_jvm_ns": 10,
                        "state": "leased", "forward": 1.0, "strafe": 0.0,
                        "jump": True, "sneak": False, "sprint": True,
                    }],
                }},
            },
        },)
        physics = ({
            "movement_tick_id": 7,
            "request_sequence_id": 3,
            "actual_input": {"forward": 1.0, "strafe": 0.0, "jump": True,
                             "sneak": False, "sprint": True},
        },)
        report = validate_rows(trace, physics)
        self.assertEqual(report["application_count"], 1)
        self.assertEqual(report["owned_application_count"], 1)
        self.assertEqual(report["command_application_delay_ticks"]["samples"], 0)
        self.assertEqual(report["mismatches"], [])

        next_trace = ({
            "record_type": "dispatch",
            "payload": {"decision": {"action": {
                "request_sequence_id": 4,
                "movement": {"forward": 0, "strafe": 0, "jump": False,
                             "sneak": False, "sprint": False},
            }}},
        }, {
            "record_type": "step",
            "payload": {
                "decision": {"action": {
                    "request_sequence_id": 4,
                    "movement": {"forward": 0, "strafe": 0, "jump": False,
                                 "sneak": False, "sprint": False},
                }},
                "backend_result": {"receipt": {
                    "schema_version": "mc2p.client_action_receipt.v3",
                    "input_applications": [{
                        "schema_version": "mc2p.input-application.v1",
                        "movement_tick_id": 8, "episode_id": "ep",
                        "request_sequence_id": 4, "sampled_at_jvm_ns": 11,
                        "state": "neutral", "forward": 0.0, "strafe": 0.0,
                        "jump": False, "sneak": False, "sprint": False,
                    }],
                }},
            },
        })
        next_physics = ({
            "movement_tick_id": 8, "request_sequence_id": 4,
            "actual_input": {"forward": 0.0, "strafe": 0.0, "jump": False,
                             "sneak": False, "sprint": False},
        },)
        delayed = validate_rows(trace + next_trace, physics + next_physics)
        self.assertEqual(delayed["command_application_delay_ticks"], {
            "samples": 1, "p50": 1, "p95": 1, "p99": 1, "maximum": 1,
        })


if __name__ == "__main__":
    unittest.main()
