"""R3 keeps ordinary safety while retiring the unproduced edge permission."""
import json
import unittest

from scripts.f2_ground_route_evidence import MANIFEST, ROOT, _frame, run_route
from tests.motion_nav.test_f2_non_center_ground_route import case
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav import ground_route_execution
from mc2p.motion_nav.fixed_route import FixedRouteController
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.motion_nav.safe_ground_control import verified_ground_route_candidate
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.world_model import WorldQueryCache
from tests.sim.backend import CalculatorBackend
from tests.sim.product_cases import f2_ground_route_scene


class R3CleanupTests(unittest.TestCase):
    def test_retired_permission_has_no_production_api(self):
        self.assertFalse(hasattr(ground_route_execution, "GroundRouteCapability"))
        self.assertFalse(hasattr(ground_route_execution, "GroundRouteGuardPhase"))
        self.assertFalse(hasattr(FixedRouteController, "_decide_edge_guard"))

    def test_undeclared_edges_stay_safely_rejected_all_directions_and_late1(self):
        rows = [c for c in json.loads(MANIFEST.read_text("utf-8"))["tasks"]
                if c["family"] == "undeclared_edge"]
        self.assertEqual(len(rows), 8)
        for item in rows:
            with self.subTest(case=item["id"]):
                row = run_route(item)
                self.assertFalse(row["success"], row)
                self.assertEqual(row["gate_violations"], [])
                self.assertEqual(row["lost_ground_frames"], 0)
                self.assertEqual(row["sneak_frames"], 0)

    def test_ordinary_replay_cannot_gain_sneak_permission(self):
        item = case("tangent")
        backend = CalculatorBackend([0], f2_ground_route_scene(item), tuple(item["start"]))
        state = backend.state
        frame = _frame(state, backend.world._world, 1)
        profile = NavigationSessionProfiles.load(ROOT / "config/motion-navigation").ground
        result = verified_ground_route_candidate(frame, state, MovementV1(forward=1, sneak=True),
            control_ticks=2, tail_ticks=30, minimum_support=.15, profile=profile,
            query_cache=WorldQueryCache(frame.world))
        self.assertIs(result.status, QueryStatus.UNSUPPORTED)
        self.assertEqual(result.physics_steps, 0)

    def test_ordinary_replay_retains_actual_support_threshold(self):
        item = case("declared_edge")
        backend = CalculatorBackend([0], f2_ground_route_scene(item), (2.1, 64., 6.5))
        state = backend.state
        frame = _frame(state, backend.world._world, 1)
        profile = NavigationSessionProfiles.load(ROOT / "config/motion-navigation").ground
        def replay(threshold):
            return verified_ground_route_candidate(
                frame, state, MovementV1(), control_ticks=2, tail_ticks=30,
                minimum_support=threshold, profile=profile,
                query_cache=WorldQueryCache(frame.world))
        allowed = replay(.15)
        rejected = replay(.5)
        self.assertIs(allowed.status, QueryStatus.FEASIBLE)
        self.assertAlmostEqual(allowed.minimum_support, 1/3)
        self.assertIs(rejected.status, QueryStatus.BLOCKED)
        self.assertEqual(rejected.reason, "insufficient_support")
        self.assertGreater(rejected.physics_steps, 0)


if __name__ == "__main__":
    unittest.main()
