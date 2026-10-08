"""F2-WR keeps physical safety separate from admitted-route fit."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.ground_candidate_verifier import (
    GroundCandidateSafety,
    GroundCandidateVerifier,
)
from mc2p.motion_nav.fixed_route import FixedRouteController
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteState, RoutePoint
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.motion_nav.ground_tracking_policy import GroundTrackingPolicy
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET
from mc2p.motion_nav.world_model import WorldQueryCache
from scripts.f2_ground_route_evidence import ROOT, _frame, run_route
from tests.motion_nav.test_f2_non_center_ground_route import case
from tests.sim.backend import CalculatorBackend
from tests.sim.product_cases import f2_ground_route_scene
from tests.motion_nav.test_f2cv_ground_candidate_verifier import _context, _fixture


def _report(*, corridor: float):
    backend, frame, profile = _fixture()
    cache = WorldQueryCache(frame.world)
    context = _context(frame, backend.state, profile, cache)
    context = replace(
        context,
        limits=replace(
            context.limits,
            maximum_cross_track_blocks=corridor,
        ),
    )
    report = GroundCandidateVerifier().verify(
        frame,
        backend.state,
        context,
        tail_ticks=30,
        minimum_support=.15,
        profile=profile,
        query_cache=cache,
    )
    return backend, frame, profile, report


def _by_key(report, movement: MovementV1, control_ticks: int):
    return next(
        item for item in report.candidates
        if item.movement == movement and item.control_ticks == control_ticks
    )


def _quality(*, corridor: float):
    backend, frame, profile, report = _report(corridor=corridor)
    cache = WorldQueryCache(frame.world)
    context = _context(frame, backend.state, profile, cache)
    context = replace(
        context,
        limits=replace(
            context.limits,
            maximum_cross_track_blocks=corridor,
        ),
    )
    return report, GroundTrackingPolicy.verified_candidates(context, report)


class WorldSafetyRouteFitTests(unittest.TestCase):
    def _advance_until_tail_proof(self):
        item = case("tangent")
        backend = CalculatorBackend(
            [0], f2_ground_route_scene(item), tuple(item["start"]),
            item["yaw_degrees"],
        )
        profile = NavigationSessionProfiles.load(
            ROOT / "config/motion-navigation",
        ).ground
        controller = FixedRouteController(profile)
        state = backend.state
        controller.start(
            FixedRoute(
                "f2wr-tail-dependency",
                tuple(RoutePoint(*point) for point in item["route_points"]),
            ),
            _frame(state, backend.world._world, 0),
        )
        for sequence in range(1, 80):
            frame = _frame(state, backend.world._world, sequence)
            decision = controller.decide(frame, physics_state=state)
            if getattr(decision, "world_dependencies", ()):
                return controller, backend, state, frame, decision
            projected = project_movement_command(state, decision.movement)
            calculated = step(
                state, projected.tick_input, backend.world, JAVA_1_21_RULESET,
            )
            self.assertIs(calculated.status, CalculationStatus.OK)
            state = calculated.next_state
        self.fail("formal ground candidate never installed its tail proof")

    def test_safe_prefix_keeps_eligibility_when_neutral_tail_leaves_corridor(self):
        report, quality = _quality(corridor=.35)

        item = _by_key(report, MovementV1(forward=-1, strafe=-1), 2)
        route = next(value for value in quality
                     if value.movement == item.movement
                     and value.control_ticks == item.control_ticks)

        self.assertIs(item.status, GroundCandidateSafety.SAFE)
        self.assertTrue(route.control_prefix_inside)
        self.assertFalse(route.neutral_tail_inside)

    def test_prefix_outside_corridor_is_ineligible_without_rewriting_world_safety(self):
        wide, wide_quality = _quality(corridor=.45)
        narrow, narrow_quality = _quality(corridor=.05)
        movement = MovementV1(forward=-1, strafe=-1)
        wide_item = _by_key(wide, movement, 2)
        narrow_item = _by_key(narrow, movement, 2)

        self.assertIs(wide_item.status, GroundCandidateSafety.SAFE)
        self.assertIs(narrow_item.status, GroundCandidateSafety.SAFE)
        self.assertEqual(wide_item.dependencies, narrow_item.dependencies)
        narrow_route = next(value for value in narrow_quality
                            if value.movement == movement
                            and value.control_ticks == 2)
        wide_route = next(value for value in wide_quality
                          if value.movement == movement
                          and value.control_ticks == 2)
        self.assertTrue(wide_route.control_prefix_inside)
        self.assertFalse(narrow_route.control_prefix_inside)
        self.assertTrue(narrow_route.blocked)

    def test_corridor_width_does_not_change_world_safety_or_world_dependencies(self):
        _, _, _, wide = _report(corridor=.45)
        _, _, _, narrow = _report(corridor=.05)

        self.assertEqual(
            [(item.movement, item.control_ticks, item.status, item.dependencies)
             for item in wide.candidates],
            [(item.movement, item.control_ticks, item.status, item.dependencies)
             for item in narrow.candidates],
        )

    def test_tracking_end_precedes_the_neutral_tail_endpoint(self):
        _, _, _, report = _report(corridor=.35)
        item = _by_key(report, MovementV1(forward=-1, strafe=-1), 2)

        self.assertIsNotNone(item.result)
        self.assertEqual(item.result.tracking_end, item.result.trajectory[item.control_ticks])
        self.assertNotEqual(item.result.tracking_end, item.result.trajectory[-1])

    def test_observed_body_outside_corridor_requests_typed_replan(self):
        row = run_route(case("outside_corridor"))

        self.assertFalse(row["success"])
        self.assertEqual(row["reason"], "fixed_route_observed_outside_corridor")

    def test_formal_fixed_route_classifies_the_closed_candidate_family(self):
        decisions = []
        original = FixedRouteController.decide

        def capture(controller, *args, **kwargs):
            decision = original(controller, *args, **kwargs)
            decisions.append(decision)
            return decision

        with patch.object(FixedRouteController, "decide", capture):
            row = run_route(case("tangent"))

        self.assertTrue(row["success"], row)
        self.assertTrue(any(
            getattr(decision, "declared_candidates", 0) == 18
            and getattr(decision, "classified_candidates", 0) == 18
            for decision in decisions
        ))

    def test_selected_candidate_holds_tail_dependencies_through_its_stop_horizon(self):
        _, _, state, _, decision = self._advance_until_tail_proof()

        self.assertTrue(decision.world_dependencies)
        self.assertGreaterEqual(
            decision.dependency_valid_until_movement_tick,
            state.movement_tick_id + decision.input_lease_ticks,
        )

    def test_related_tail_change_requests_replan_but_unrelated_change_does_not(self):
        controller, backend, state, frame, decision = self._advance_until_tail_proof()
        related = decision.world_dependencies[0]
        unrelated = (related[0] + 100, related[1], related[2] + 100)

        unrelated_frame = replace(
            _frame(state, backend.world._world, frame.body.sequence_id + 1),
            changed_cells=(unrelated,),
        )
        kept = controller.decide(unrelated_frame, physics_state=state)
        self.assertIsNot(kept.state, FixedRouteState.NEEDS_REPLAN)

        related_frame = replace(
            _frame(state, backend.world._world, frame.body.sequence_id + 2),
            changed_cells=(related,),
        )
        stopped = controller.decide(related_frame, physics_state=state)
        self.assertIs(stopped.state, FixedRouteState.NEEDS_REPLAN)
        self.assertEqual(stopped.reason, "ground_tail_dependencies_changed")


if __name__ == "__main__":
    unittest.main()
