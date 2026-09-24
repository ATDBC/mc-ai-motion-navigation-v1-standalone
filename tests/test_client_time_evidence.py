from copy import deepcopy
from importlib.util import find_spec
import unittest


class ClientTimeEvidenceTests(unittest.TestCase):
    def test_lockstep_equal_timestamps_cannot_hide_extra_or_missing_world_ticks(self):
        import scripts.client_time_evidence as module
        evaluate = getattr(module, "evaluate_lockstep_world_ticks", None)
        self.assertTrue(callable(evaluate), "actual lockstep world tick evidence is missing")
        observations = [dict(sequence_id=i, world_time_ticks={"value": v}) for i, v in enumerate((73, 74))]
        for tick_count in (0, 1, 2):
            events = []
            def append(kind, counter, **fields):
                events.append(dict(schema_version="mc2p.client-time-event.v1", session_id="s", world_id="w",
                    event_sequence=len(events) + 1, event=kind, world_ticks=counter,
                    client_ticks=100 + len(events), **fields))
            append("observation", 10, generation_id=0, world_time=73)
            append("time_packet", 10, before=73, packet_time=74, after=74)
            for i in range(tick_count): append("world_tick", 11 + i, before=74 + i, after=75 + i)
            append("time_packet", 10 + tick_count, before=74 + tick_count, packet_time=74, after=74)
            append("observation", 10 + tick_count, generation_id=1, world_time=74)
            self.assertEqual(module.evaluate_time_events(events, observations)["status"], "passed")
            self.assertEqual(evaluate(events, observations)["passed"], tick_count == 1)
            if tick_count == 1:
                self.assertFalse(evaluate(events[:-1], observations)["passed"])
        self.assertFalse(evaluate([], observations)["passed"])

    def test_runtime_export_covers_formal_samples_and_independent_tick_counters(self):
        import json
        from dataclasses import asdict
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from tests.observation_v2_fixtures import valid_snapshot_v2
        import scripts.client_time_evidence as module
        self.assertTrue(callable(getattr(module, "export_runtime_time_evidence", None)), "Runtime time evidence export missing")
        events, _ = self.fixture()
        observations = [asdict(valid_snapshot_v2(sequence_id=i, request_sequence_id=i - 1 if i else None,
            world_time_ticks=world_time, source_backend="fabric")) for i, world_time in enumerate((285, 279))]
        records = [dict(record_type="reset", payload={"result": {"observation": observations[0]}}),
            dict(record_type="step", payload={"backend_result": {"observation": observations[1]}})]
        rows = [dict(episode_id=o["episode_id"], observation_sequence_id=i, diagnostics={"client_tick": 10 + i})
                for i, o in enumerate(observations)]
        with TemporaryDirectory(prefix="mc2p-runtime-time-") as directory:
            root = Path(directory)
            def write(name, values):
                (root / name).write_text("".join(json.dumps(x) + "\n" for x in values), encoding="utf-8")
            write("trace.jsonl", records)
            write("diagnostics.jsonl", rows)
            write("mc2p-client-time.jsonl", events)
            result = module.export_runtime_time_evidence(root)
            self.assertEqual(result["status"], "passed", result)
            self.assertEqual(result["intervals"][0]["packet_correction_sum"], -8)
            self.assertEqual((root / "time-events.jsonl").read_bytes(), (root / "mc2p-client-time.jsonl").read_bytes())
            rows[1]["diagnostics"]["client_tick"] = 12
            write("diagnostics.jsonl", rows)
            self.assertEqual(module.export_runtime_time_evidence(root)["status"], "failed")
            self.assertEqual(json.loads((root / "time-report.json").read_text())["status"], "failed")
            write("mc2p-client-time.jsonl", [])
            self.assertEqual(module.export_runtime_time_evidence(root)["status"], "failed")

    def test_runtime_export_reads_a_normally_sealed_segmented_trace(self):
        import json
        from dataclasses import asdict
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from mc2p.runtime.segmented_trace import SegmentedTraceWriter
        from tests.observation_v2_fixtures import valid_snapshot_v2
        import scripts.client_time_evidence as module

        events, _ = self.fixture()
        observations = [asdict(valid_snapshot_v2(
            sequence_id=i,
            request_sequence_id=i - 1 if i else None,
            world_time_ticks=world_time,
            source_backend="fabric",
        )) for i, world_time in enumerate((285, 279))]
        rows = [dict(
            episode_id=o["episode_id"], observation_sequence_id=i,
            diagnostics={"client_tick": 10 + i},
        ) for i, o in enumerate(observations)]
        with TemporaryDirectory(prefix="mc2p-runtime-segment-time-") as directory:
            root = Path(directory)
            trace = SegmentedTraceWriter(root / "runtime-trace" / "trace")
            trace.write("reset", {"result": {"observation": observations[0]}})
            trace.write("step", {"backend_result": {"observation": observations[1]}})
            trace.close()
            for name, values in (
                ("diagnostics.jsonl", rows),
                ("mc2p-client-time.jsonl", events),
            ):
                (root / name).write_text(
                    "".join(json.dumps(value) + "\n" for value in values), encoding="utf-8",
                )
            result = module.export_runtime_time_evidence(root)
        self.assertEqual(result["status"], "passed", result)

    def test_runtime_export_attributes_real_close_release_and_requires_its_diagnostics(self):
        import json
        from dataclasses import asdict
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from tests.observation_v2_fixtures import valid_snapshot_v2
        import scripts.client_time_evidence as module

        events, _ = self.fixture()
        events += [dict(schema_version="mc2p.client-time-event.v1", session_id="s", world_id="w",
                        event_sequence=6, event="world_tick", world_ticks=103, client_ticks=12,
                        before=279, after=280),
                   dict(schema_version="mc2p.client-time-event.v1", session_id="s", world_id="w",
                        event_sequence=7, event="observation", world_ticks=103, client_ticks=12,
                        generation_id=2, world_time=280)]
        observations = [asdict(valid_snapshot_v2(sequence_id=i, request_sequence_id=i - 1 if i else None,
            world_time_ticks=world_time, source_backend="fabric")) for i, world_time in enumerate((285, 279, 280))]
        records = [dict(record_type="reset", payload={"result": {"observation": observations[0]}}),
                   dict(record_type="step", payload={"backend_result": {"observation": observations[1]}}),
                   dict(record_type="close_release", payload={"backend_result": {"observation": observations[2]}})]
        rows = [dict(episode_id=o["episode_id"], observation_sequence_id=i,
                     diagnostics={"client_tick": 10 + i}) for i, o in enumerate(observations)]
        with TemporaryDirectory(prefix="mc2p-runtime-close-time-") as directory:
            root = Path(directory)

            def write(name, values):
                (root / name).write_text("".join(json.dumps(x) + "\n" for x in values), encoding="utf-8")

            write("trace.jsonl", records)
            write("diagnostics.jsonl", rows)
            write("mc2p-client-time.jsonl", events)
            result = module.export_runtime_time_evidence(root)
            self.assertEqual(result["status"], "passed", result)
            self.assertEqual(result["observation_count"], 3)
            self.assertEqual(result["intervals"][-1]["observation_sequence_id"], 2)

            write("diagnostics.jsonl", rows[:-1])
            self.assertEqual(module.export_runtime_time_evidence(root)["status"], "failed")

            rows[2]["observation_sequence_id"] = 1
            write("diagnostics.jsonl", rows)
            self.assertEqual(module.export_runtime_time_evidence(root)["status"], "failed")
            rows[2]["observation_sequence_id"] = 2
            rows[2]["diagnostics"]["client_tick"] = 13
            write("diagnostics.jsonl", rows)
            self.assertEqual(module.export_runtime_time_evidence(root)["status"], "failed")

    def test_export_preserves_raw_new_events_and_keeps_missing_diagnostics_failed(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import json
        import scripts.client_time_evidence as module
        self.assertTrue(hasattr(module, "export_time_evidence"), "time evidence artifact export missing")
        events, observations = self.fixture()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, run = root / "time.jsonl", root / "run"
            run.mkdir()
            prefix = b'{"previous_session": true}\n'
            raw = "".join(json.dumps(e) + "\n" for e in events).encode()
            source.write_bytes(prefix + raw)
            (run / "observations.jsonl").write_text("".join(json.dumps(o) + "\n" for o in observations), encoding="utf-8")
            result = module.export_time_evidence(source, len(prefix), run)
            self.assertEqual(result["status"], "passed")
            self.assertEqual((run / "time-events.jsonl").read_bytes(), raw)
            self.assertEqual(json.loads((run / "time-report.json").read_text())["regression_count"], 1)
            self.assertEqual(module.export_time_evidence(root / "missing", 0, run)["status"], "failed")

    def evaluate(self, events, observations):
        self.assertIsNotNone(find_spec("scripts.client_time_evidence"), "time attribution evaluator missing")
        from scripts.client_time_evidence import evaluate_time_events
        return evaluate_time_events(events, observations)

    def fixture(self):
        def row(seq, kind, **values):
            return dict(schema_version="mc2p.client-time-event.v1", session_id="s", event_sequence=seq,
                        event=kind, world_id="w", client_ticks=10 + (seq > 1), world_ticks=100 + (seq > 1) + (seq > 3),
                        **values)
        events = [row(1, "observation", generation_id=0, world_time=285),
                  row(2, "world_tick", before=285, after=286),
                  row(3, "time_packet", before=286, packet_time=278, after=278),
                  row(4, "world_tick", before=278, after=279),
                  row(5, "observation", generation_id=1, world_time=279)]
        observations = [dict(sequence_id=i, world_time_ticks={"value": time}) for i, time in enumerate((285, 279))]
        return events, observations

    def test_attributes_backward_observation_to_actual_packet_and_tick_events(self):
        result = self.evaluate(*self.fixture())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["intervals"], [{"observation_sequence_id": 1, "before": 285, "after": 279,
            "client_tick_calls": 1, "world_tick_calls": 2, "packet_correction_sum": -8,
            "packet_count": 1, "observed_delta": -6}])
        self.assertEqual(result["regression_count"], 1)
        self.assertEqual(result["packet_update_count"], 1)

    def test_rejects_world_identity_return_but_allows_new_world_followed_by_reset(self):
        events, observations = self.fixture()
        alien = {**events[2], "world_id": "other", "before": 12, "packet_time": 1, "after": 1}
        events.insert(2, alien)
        for i, e in enumerate(events): e["event_sequence"] = i + 1
        self.assertEqual(self.evaluate(events, observations)["status"], "failed")

        events, observations = self.fixture()
        base = {**events[-1], "world_id": "new"}
        events += [{k: v for k, v in {**base, "event_sequence": 6, "event": "time_packet",
                     "before": 12, "packet_time": 1, "after": 1}.items() if k not in ("generation_id", "world_time")},
                   {**base, "event_sequence": 7, "generation_id": 0, "world_time": 1},
                   {k: v for k, v in {**base, "event_sequence": 8, "event": "world_tick", "client_ticks": 12,
                     "world_ticks": 103, "before": 1, "after": 2}.items() if k not in ("generation_id", "world_time")},
                   {**base, "event_sequence": 9, "generation_id": 1, "world_time": 2, "client_ticks": 12, "world_ticks": 103}]
        observations += [{"sequence_id": 0, "world_time_ticks": {"value": 1}},
                         {"sequence_id": 1, "world_time_ticks": {"value": 2}}]
        result = self.evaluate(events, observations)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(len(result["intervals"]), 2)

    def test_missing_unknown_or_misassociated_events_never_become_causal_proof(self):
        for fault in ("missing_packet", "wrong_target", "unknown_write", "false_tick", "observation",
                      "generation", "session", "event_order", "client_counter", "world_counter", "empty",
                      "numeric_bool", "fractional_counter", "fractional_time", "no_initial_reset"):
            events, observations = deepcopy(self.fixture())
            if fault == "missing_packet":
                events.pop(2)
                for i, e in enumerate(events): e["event_sequence"] = i + 1
            if fault == "wrong_target": events[2]["packet_time"] = 277
            if fault == "unknown_write": events[3]["before"] = 280
            if fault == "false_tick": events[1]["after"] = 287
            if fault == "observation": observations[1]["world_time_ticks"]["value"] = 280
            if fault == "generation": events[-1]["generation_id"] = 2
            if fault == "session": events[2]["session_id"] = "other"
            if fault == "event_order": events[2]["event_sequence"] = 2
            if fault == "client_counter": events[-1]["client_ticks"] = 9
            if fault == "world_counter": events[3]["world_ticks"] = 999
            if fault == "empty": events = []
            if fault == "numeric_bool": events[0]["event_sequence"] = True
            if fault == "fractional_counter": events[0]["client_ticks"] = 10.5
            if fault == "fractional_time": events[2]["packet_time"] = 278.0
            if fault == "no_initial_reset":
                events[0]["generation_id"], events[-1]["generation_id"] = 1, 2
                observations[0]["sequence_id"], observations[1]["sequence_id"] = 1, 2
            with self.subTest(fault=fault):
                self.assertEqual(self.evaluate(events, observations)["status"], "failed")
