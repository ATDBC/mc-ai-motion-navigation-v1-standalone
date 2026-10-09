"""Real calculator closed loops for ordinary non-center route execution."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from scripts.f2_ground_route_evidence import MANIFEST, ROOT, _frame, run_route
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteConfig, FixedRouteController, RoutePoint
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.safe_ground_control import ground_route_state_matches, verified_ground_route_candidate
from mc2p.motion_nav.world_model import WorldQueryCache
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from tests.sim.backend import CalculatorBackend
from tests.sim.product_cases import f2_ground_route_scene


def case(family):
    return next(c for c in json.loads(MANIFEST.read_text("utf-8"))["tasks"]
                if c["id"] == f"f2/{family}/south/normal")


class NonCenterGroundRouteTests(unittest.TestCase):
    def test_safe_lease_with_escaping_neutral_tail_is_rejected(self):
        item = case("tangent")
        backend = CalculatorBackend([0], f2_ground_route_scene(item), tuple(item["start"]))
        state = replace(backend.state, velocity_blocks_per_tick=(0., -.0784, .14))
        frame = _frame(state, backend.world._world, 1)
        profile = NavigationSessionProfiles.load(ROOT / "config/motion-navigation").ground
        controller = FixedRouteController(profile, FixedRouteConfig(maximum_cross_track_blocks=.15))
        controller.start(FixedRoute("tail-escape", (RoutePoint(1.7, 64., .5), RoutePoint(1.7, 64., 1.05))), frame)
        cache = WorldQueryCache(frame.world)
        body = controller._planar(frame)
        command = MovementV1(forward=1)
        candidate = controller._evaluate_candidate(frame, body, command, (1.7, 1.05), braking=False, query_cache=cache)
        result = verified_ground_route_candidate(frame, state, command, control_ticks=2, tail_ticks=30,
                                                minimum_support=.15, profile=profile, query_cache=cache)
        self.assertIs(result.status, QueryStatus.FEASIBLE)
        errors = [max(0., s.position[2] - 1.05) for s in result.trajectory]
        self.assertLessEqual(max(errors[:3]), .15)
        self.assertGreater(max(errors[3:]), .15)
        # Use the real rejected fast candidate and real calculator result. Only
        # the trajectory after the full lease invalidates this input.
        reviewed = controller._replay_rejected_candidates([candidate], frame, body, (1.7, 1.05), state, cache)
        self.assertTrue(reviewed[0].blocked)

    def test_ground_replay_preserves_full_stop_and_rejects_vertical_motion(self):
        item = case("tangent")
        backend = CalculatorBackend([0], f2_ground_route_scene(item), tuple(item["start"]))
        profile = NavigationSessionProfiles.load(ROOT / "config/motion-navigation").ground
        frame = _frame(backend.state, backend.world._world, 1)
        result = verified_ground_route_candidate(frame, backend.state, MovementV1(forward=1), control_ticks=2,
                                                tail_ticks=30, minimum_support=.15, profile=profile,
                                                query_cache=WorldQueryCache(frame.world))
        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertGreater(len(result.trajectory), 3)
        self.assertEqual(result.trajectory[-1].velocity_blocks_per_tick[::2], (0., 0.))
        airborne = replace(backend.state, velocity_blocks_per_tick=(0., .2, 0.))
        frame = _frame(airborne, backend.world._world, 1)
        result = verified_ground_route_candidate(frame, airborne, MovementV1(forward=1), control_ticks=2,
                                                tail_ticks=30, minimum_support=.15, profile=profile,
                                                query_cache=WorldQueryCache(frame.world))
        self.assertIsNot(result.status, QueryStatus.FEASIBLE)

    def test_contact_routes_complete_actual_closed_loop(self):
        for family in ("tangent", "corner"):
            with self.subTest(family=family):
                row = run_route(case(family))
                self.assertTrue(row["success"], row)
                self.assertEqual(row["gate_violations"], [])
                self.assertGreater(row["physics_steps_total"], 0)
                self.assertLessEqual(row["full_candidates_max"], 3)

    def test_open_routes_preserve_fast_path(self):
        for family in ("offset", "diagonal", "centre_to_offset"):
            with self.subTest(family=family):
                row = run_route(case(family))
                self.assertTrue(row["success"], row)
                self.assertEqual(row["full_candidates_max"], 0)
                self.assertEqual(row["physics_steps_total"], 0)

    def test_negative_routes_reject_without_actual_safety_violation(self):
        for family in ("head_wall", "hazard_wall", "unknown_wall", "fluid_wall",
                       "unsupported_wall", "outside_corridor", "undeclared_edge"):
            with self.subTest(family=family):
                row = run_route(case(family))
                self.assertFalse(row["success"], row)
                self.assertEqual(row["gate_violations"], [])
                self.assertLessEqual(row["full_candidates_max"], 3)

    def test_absent_or_mismatched_state_does_not_relax_contact(self):
        item = case("tangent")
        backend = CalculatorBackend([0], f2_ground_route_scene(item), tuple(item["start"]), item["yaw_degrees"])
        profile = NavigationSessionProfiles.load(ROOT / "config/motion-navigation").ground
        for state in (None, replace(backend.state, position=(1., 64., 1.)),
                      replace(backend.state, velocity_blocks_per_tick=(.1, -.0784, 0.)),
                      replace(backend.state, yaw_radians=.1),
                      replace(backend.state, movement_tick_id=1),
                      replace(backend.state, horizontal_collision=True)):
            with self.subTest(state=state):
                frame = _frame(backend.state, backend.world._world, 1)
                controller = FixedRouteController(profile)
                controller.start(FixedRoute("absent-state", tuple(RoutePoint(*p) for p in item["route_points"])), frame)
                decision = controller.decide(frame, physics_state=state)
                baseline = FixedRouteController(profile)
                baseline.start(FixedRoute("fast-state", tuple(RoutePoint(*p) for p in item["route_points"])), frame)
                self.assertEqual(decision.movement, baseline.decide(frame).movement)
                self.assertEqual(decision.full_candidates, 0)
                self.assertEqual(decision.physics_steps, 0)

        flag_pairs = (
            ("on_ground", "is_on_ground"),
            ("horizontal_collision", "horizontal_collision"),
            ("vertical_collision", "vertical_collision"),
            ("sprinting", "is_sprinting"), ("sneaking", "is_sneaking"),
            ("swimming", "is_swimming"), ("submerged_in_water", "is_submerged_in_water"),
            ("climbing", "is_climbing"), ("fall_flying", "is_fall_flying"),
            ("flying", "is_flying"), ("allow_flying", "allow_flying"),
            ("is_using_item", "is_using_item"),
        )
        for state_flag, body_flag in flag_pairs:
            for observed in (False, True):
                with self.subTest(flag=body_flag, observed=observed, physics=not observed):
                    frame = _frame(backend.state, backend.world._world, 1)
                    frame = replace(frame, body=replace(frame.body, **{body_flag: observed}))
                    mismatched = replace(backend.state, **{state_flag: not observed})
                    self.assertFalse(ground_route_state_matches(frame, mismatched))
                    rejected = verified_ground_route_candidate(
                        frame, mismatched, MovementV1(forward=1), control_ticks=2,
                        tail_ticks=30, minimum_support=.15, profile=profile,
                        query_cache=WorldQueryCache(frame.world),
                    )
                    self.assertIs(rejected.status, QueryStatus.UNSUPPORTED)
                    self.assertEqual(rejected.reason, "state_unavailable_or_mismatched")
                    self.assertEqual(rejected.physics_steps, 0)
                    controller = FixedRouteController(profile)
                    route = FixedRoute("flag-mismatch", tuple(RoutePoint(*p) for p in item["route_points"]))
                    controller.start(route, frame)
                    decision = controller.decide(frame, physics_state=mismatched)
                    baseline = FixedRouteController(profile)
                    baseline.start(route, frame)
                    original = baseline.decide(frame)
                    self.assertEqual((decision.state, decision.movement, decision.reason),
                                     (original.state, original.movement, original.reason))
                    self.assertEqual(decision.full_candidates, 0)
                    self.assertEqual(decision.physics_steps, 0)

        # Flight permission alone can coexist with ordinary ground movement.
        # Equality is required; matching true permission is not a new prohibition.
        allowed = replace(backend.state, allow_flying=True)
        frame = _frame(backend.state, backend.world._world, 1)
        frame = replace(frame, body=replace(frame.body, allow_flying=True))
        self.assertTrue(ground_route_state_matches(frame, allowed))


if __name__ == "__main__":
    unittest.main()
