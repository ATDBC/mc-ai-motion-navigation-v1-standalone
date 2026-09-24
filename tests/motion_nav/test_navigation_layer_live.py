import json
from pathlib import Path
import tempfile
import unittest

from scripts.navigation_layer_live_demo import force_creative_mode
from tools.navigation_layer_live.server import SnapshotContractError, SnapshotHub


def snapshot(sequence: int = 1) -> dict:
    return {
        "schema": "mc2p.navigation-layer-live.v1",
        "sequence": sequence,
        "world_tick": 42,
        "sample_ns": 123,
        "sample_duration_ns": 456,
        "camera": {"x": 1.5, "y": 65.62, "z": 2.5, "yaw": 0.0, "pitch": 0.0},
        "player": {"x": 1.5, "y": 64.0, "z": 2.5},
        "range": 16.0,
        "horizontal_fov": 120.0,
        "vertical_fov": 120.0,
        "complete": True,
        "queried_cells": 3,
        "unknown_cells": 0,
        "palette": [
            {"category": "air", "collision": "empty", "fluid": False, "boxes": []},
            {"category": "occupied", "collision": "full_cube", "fluid": False, "boxes": []},
        ],
        "runs": [[1, 64, 2, 4, 0], [1, 65, 3, 4, 1]],
    }


class SnapshotHubTests(unittest.TestCase):
    def test_latest_frame_replaces_backlog(self):
        hub = SnapshotHub()
        first = hub.publish(json.dumps(snapshot(1)))
        second = hub.publish(json.dumps(snapshot(2)))
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        version, payload = hub.wait_after(0, timeout=0)
        self.assertEqual(version, 2)
        self.assertEqual(json.loads(payload)["sequence"], 2)

    def test_identity_fields_are_rejected(self):
        value = snapshot()
        value["palette"][1]["block_id"] = "minecraft:diamond_ore"
        with self.assertRaises(SnapshotContractError):
            SnapshotHub().publish(json.dumps(value))

    def test_incomplete_coverage_keeps_unknown_count(self):
        value = snapshot()
        value["complete"] = False
        value["unknown_cells"] = 7
        hub = SnapshotHub()
        hub.publish(json.dumps(value))
        _, payload = hub.wait_after(0, timeout=0)
        self.assertEqual(json.loads(payload)["unknown_cells"], 7)

    def test_demo_server_is_forced_into_creative_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            properties = Path(directory) / "server.properties"
            properties.write_text(
                "gamemode=survival\nforce-gamemode=false\ndifficulty=peaceful\n",
                encoding="utf-8",
            )
            force_creative_mode(properties)
            values = dict(
                line.split("=", 1)
                for line in properties.read_text("utf-8").splitlines()
                if line
            )
            self.assertEqual(values["gamemode"], "creative")
            self.assertEqual(values["force-gamemode"], "true")

    def test_web_view_is_first_person_webgl_instead_of_map_slices(self):
        web = Path(__file__).parents[2] / "tools" / "navigation_layer_live" / "web"
        html = (web / "index.html").read_text("utf-8")
        script = (web / "app.js").read_text("utf-8")
        self.assertIn('id="viewport"', html)
        self.assertNotIn('id="layers"', html)
        self.assertIn("getContext('webgl2'", script)
        self.assertIn("frame.camera", script)


if __name__ == "__main__":
    unittest.main()
