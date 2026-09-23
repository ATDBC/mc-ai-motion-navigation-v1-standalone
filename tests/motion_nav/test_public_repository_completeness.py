from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PublicRepositoryCompletenessTests(unittest.TestCase):
    def test_motion_navigation_tests_keep_their_minimum_shared_dependencies(self):
        required = (
            "config/motion-navigation/reference-SHA256SUMS.txt",
            "config/motion-navigation/reference-scenes-v1.json",
            "config/motion-navigation/reference-versions-v1.json",
            "experiments/navigation-floating-wall-mazes-v1/layouts/maze_03_floating.json",
            "tests/observation_v2_fixtures.py",
            "tests/observation_v3_fixtures.py",
            "mc2p/contracts/action.py",
            "mc2p/contracts/action_receipt.py",
            "mc2p/contracts/action_v1.py",
            "mc2p/contracts/common.py",
            "mc2p/contracts/observation.py",
            "mc2p/contracts/observation_request_v3.py",
            "mc2p/contracts/observation_v2.py",
            "mc2p/contracts/observation_v3.py",
            "mc2p/backends/client_observation_payload.py",
            "mc2p/backends/client_observation_payload_v3.py",
        )

        missing = tuple(path for path in required if not (ROOT / path).is_file())

        self.assertEqual(missing, ())


if __name__ == "__main__":
    unittest.main()
