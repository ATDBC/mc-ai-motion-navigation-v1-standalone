import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


REPLAY = Path(__file__).resolve().parents[2] / "tools" / "navigation_replay"
sys.path.insert(0, str(REPLAY))

from catalog import known_map_batches  # noqa: E402
from known_map import normalize_known_map_scenario  # noqa: E402


def scenario(name: str, positions: tuple[tuple[float, float, float], ...]) -> dict:
    return {
        "name": name,
        "planning_status": "complete",
        "planning_ms": 4.5,
        "waiting_polls": 1,
        "expanded_nodes": 8,
        "graph_path": [[0, 1, 0], [-1, 1, 0], [-1, 1, 2], [0, 1, 2]],
        "fixed_route": [
            {"x": positions[0][0], "y": positions[0][1], "z": positions[0][2]},
            {"x": -0.5, "y": 1.0, "z": 0.5},
            {"x": positions[-1][0], "y": positions[-1][1], "z": positions[-1][2]},
        ],
        "final_error_blocks": 0.02,
        "final_speed_blocks_per_second": 0.0,
        "decisions": [
            {
                "sequence": 10 + index,
                "state": "succeeded" if index == len(positions) - 1 else "running",
                "reason": "goal_reached_and_stopped" if index == len(positions) - 1 else "tracking_fixed_route",
                "progress": float(index),
                "control_time_ns": 100_000,
                "position": list(position),
                "movement": {"forward": int(index < len(positions) - 1), "strafe": 0,
                             "jump": False, "sneak": False, "sprint": False},
            }
            for index, position in enumerate(positions)
        ],
    }


class KnownMapReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = {
            "schema_version": "mc2p.b04-known-map-evidence.v1",
            "bounds": {"min_x": -2, "max_x": 2, "min_z": 0, "max_z": 4,
                       "feet_y": 1, "complete_scope": True},
            "start": [0, 1, 0],
            "end": [0, 1, 2],
            "scenarios": [
                scenario("open", ((0.5, 1.0, 0.5), (0.5, 1.0, 2.5))),
                scenario("wall", ((0.5, 1.0, 2.5), (-0.5, 1.0, 1.5), (0.5, 1.0, 0.5))),
                scenario("floating", ((0.5, 1.0, 0.5), (-0.5, 1.0, 1.5), (0.5, 1.0, 2.5))),
                scenario("pit", ((0.5, 1.0, 2.5), (-0.5, 1.0, 1.5), (0.5, 1.0, 0.5))),
            ],
        }

    def test_catalog_exposes_only_the_three_obstacle_runs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "artifacts" / "fabric-deployment" / "20260918T180000Z-example"
            client = run / "client-0"
            client.mkdir(parents=True)
            (run / "result.json").write_text(json.dumps({
                "scenario": "b04-known-map", "seed": 21_001,
                "core_sources_before": {"mc2p/example.py": "abc"},
            }), encoding="utf-8")
            (client / "b04-known-map.json").write_text(
                json.dumps(self.evidence), encoding="utf-8")

            batches = known_map_batches(root)

            self.assertEqual(len(batches), 1)
            self.assertEqual([run["case"] for run in batches[0]["runs"]],
                             ["wall", "floating", "pit"])
            self.assertTrue(all(run["kind"] == "known_map" for run in batches[0]["runs"]))

    def test_normalizer_preserves_route_and_marks_each_obstacle_type(self) -> None:
        expected = {
            "wall": (2, 0, "wall"),
            "floating": (1, 0, "floating"),
            "pit": (0, 1, None),
        }
        for name, (walls, pits, visual_kind) in expected.items():
            with self.subTest(name=name):
                original = next(item for item in self.evidence["scenarios"] if item["name"] == name)
                replay = normalize_known_map_scenario(self.evidence, original)
                self.assertEqual(replay["schema_version"], "mc2p.known-map-replay.v1")
                self.assertEqual(len(replay["layout"]["obstacles"]), walls)
                self.assertEqual(len(replay["layout"]["pit_cells"]), pits)
                if visual_kind:
                    self.assertEqual(replay["layout"]["obstacles"][0]["visual_kind"], visual_kind)
                self.assertEqual(replay["reference_path"], [
                    [point[axis] for axis in ("x", "y", "z")]
                    for point in original["fixed_route"]
                ])
                self.assertEqual(replay["samples"][0]["t"], 0.0)
                self.assertAlmostEqual(replay["samples"][-1]["t"], 0.1)
                self.assertEqual(replay["decisions"][-1]["target"], replay["goal"])
                self.assertEqual(replay["summary"]["outcome"], "success")


if __name__ == "__main__":
    unittest.main()
