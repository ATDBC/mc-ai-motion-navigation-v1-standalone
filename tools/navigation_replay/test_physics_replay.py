import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class PhysicsReplayTests(unittest.TestCase):
    def test_discovers_b08_ground_crawl_and_b09_air_evidence(self):
        import physics_replay

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "artifacts/fabric-deployment"
            b08 = base / "20260920T100000Z-b08"
            b09 = base / "20260920T110000Z-b09"
            for directory, scenario in (
                    (b08, "b08-ground-modes"), (b09, "b09-air-motion")):
                ticks = directory / "client-0/physics-tick-events"
                ticks.mkdir(parents=True)
                (ticks / "complete.json").write_text("{}", encoding="utf-8")
                (directory / "result.json").write_text(json.dumps({
                    "scenario": scenario, "status": "passed", "seed": 21001,
                }), encoding="utf-8")
            (b09 / "client-0/b09-fixture-commands.jsonl").write_text(
                json.dumps({"material": "minecraft:air", "positions": [[0, 64, 0]]}) + "\n",
                encoding="utf-8",
            )

            runs = physics_replay.discover(root)

            self.assertEqual([run["mode"] for run in runs], [
                "b09-air", "b08-ground", "b08-crawl",
            ])
            self.assertEqual(len({run["id"] for run in runs}), 3)
            self.assertTrue(all(run["complete"] for run in runs))

    def test_build_payload_keeps_actual_prediction_events_and_error(self):
        import physics_replay
        from mc2p.motion_nav.physics_adapter import PhysicsWorldView
        from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
        from mc2p.motion_nav.world_model import (
            BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
        )

        session = WorldSessionId("physics-replay-test")
        stamp = ObservationStamp(session, 0, 0, "fixture", 0)
        knowledge = WorldKnowledge(session)
        cells = tuple(
            (x, y, z) for x in range(-2, 3) for y in range(61, 68)
            for z in range(-2, 3)
        )
        knowledge.confirm_air(stamp, cells)
        knowledge.observe_blocks(stamp, {
            (x, 63, z): BlockGeometry.full_cube("minecraft:grass_block")
            for x in range(-2, 3) for z in range(-2, 3)
        })
        row = self._ground_row()

        payload = physics_replay.build_payload(
            (row,), PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET),
            title="冻结地面 tick", source="fixture",
            world_payload={"kind": "flat", "floor_y": 63, "blocks": []},
        )

        self.assertEqual(payload["schema_version"], "mc2p.physics-replay.v1")
        self.assertEqual(payload["summary"]["tick_count"], 1)
        self.assertEqual(payload["summary"]["sequence_count"], 1)
        samples = payload["sequences"][0]["samples"]
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[1]["input"]["forward"], 1.0)
        self.assertIn("vertical_collision", samples[1]["predicted_events"])
        self.assertIn("vertical_collision", samples[1]["actual_events"])
        self.assertLess(samples[1]["error"]["position_max"], 1e-4)

    def test_server_exposes_physics_catalog_payload_and_static_page(self):
        import server

        payload = {"schema_version": "mc2p.physics-replay.v1", "sequences": []}
        store = SimpleNamespace(
            catalog=lambda **kwargs: [],
            physics_catalog=lambda **kwargs: [{"id": "physics-run"}],
            get_physics=lambda run_id: payload if run_id == "physics-run" else None,
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler(store))
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            catalog = self._get_json(httpd.server_port, "/api/physics/catalog")
            self.assertEqual(catalog, {"runs": [{"id": "physics-run"}]})
            self.assertEqual(
                self._get_json(httpd.server_port, "/api/physics/run/physics-run"), payload)
            connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            try:
                connection.request("GET", "/physics.html", headers={"Host": "localhost"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("B09-R", response.read().decode("utf-8"))
            finally:
                connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    @staticmethod
    def _get_json(port, path):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path, headers={"Host": "localhost"})
            response = connection.getresponse()
            if response.status != 200:
                raise AssertionError((response.status, response.read()))
            return json.loads(response.read())
        finally:
            connection.close()

    @staticmethod
    def _ground_row():
        return {
            "schema_version": "mc2p.client-physics-tick.v1",
            "movement_tick_id": 1,
            "pre_state": {
                "position": {"x": .5, "y": 64., "z": .5},
                "velocity": {"x": 0., "y": -.0784000015258789, "z": 0.},
                "yaw": 0., "pitch": 0., "on_ground": True,
                "horizontal_collision": False, "vertical_collision": True,
                "actual_sprinting": False, "actual_sneaking": False,
                "pose": "standing",
            },
            "movement_yaw": 0.,
            "actual_input": {"forward": 1., "strafe": 0., "jump": False,
                             "sneak": False, "sprint": False},
            "post_state": {
                "position": {"x": .5, "y": 64., "z": .598000009074074},
                "velocity": {"x": 0., "y": -.0784000015258789,
                             "z": .05350801092592644},
                "yaw": 0., "pitch": 0., "on_ground": True,
                "horizontal_collision": False, "vertical_collision": True,
                "actual_sprinting": False, "actual_sneaking": False,
                "pose": "standing",
            },
            "contact_events": ["vertical_collision"],
        }


if __name__ == "__main__":
    unittest.main()
