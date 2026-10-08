"""F2-CV closed ordinary-ground candidate-family contracts."""
from __future__ import annotations

from dataclasses import replace
import json
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.ground_candidate_verifier import (
    CLOSED_GROUND_MOVEMENTS,
    GroundCandidateSafety,
    GroundCandidateVerifier,
)
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.motion_nav.fixed_route import FixedRouteConfig
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.ground_tracking_policy import (
    GroundTrackingContext, GroundTrackingLimits, GroundTrackingPolicy,
    GroundTrackingRoute,
)
from mc2p.motion_nav.safe_ground_control import (
    CachedGroundPhysicsWorld, verified_ground_route_candidate,
)
from mc2p.motion_nav.world_model import (
    BlockGeometry, CellKnowledge, ObservationStamp, WorldQueryCache, WorldView,
)
from scripts.f2_ground_route_evidence import MANIFEST, ROOT, _frame
from tests.sim.backend import CalculatorBackend
from tests.sim.product_cases import f2_ground_route_scene


def _case(family: str = "tangent"):
    return next(
        item for item in json.loads(MANIFEST.read_text("utf-8"))["tasks"]
        if item["id"] == f"f2/{family}/south/normal"
    )


def _fixture(family: str = "tangent"):
    item = _case(family)
    backend = CalculatorBackend([0], f2_ground_route_scene(item), tuple(item["start"]))
    frame = _frame(backend.state, backend.world._world, 1)
    profile = NavigationSessionProfiles.load(ROOT / "config/motion-navigation").ground
    return backend, frame, profile


def _context(frame, state, profile, cache):
    item = _case()
    points = tuple(tuple(value) for value in item["route_points"])
    return GroundTrackingContext(
        PlanarBodyState(
            state.position[0], state.position[2],
            state.velocity_blocks_per_tick[0] * 20,
            state.velocity_blocks_per_tick[2] * 20,
            state.yaw_radians,
        ),
        frame.body.body_box, frame.body.position[1], GroundTrackingRoute(points),
        0.0, 0, MovementV1(), (points[-1][0], points[-1][2]), False,
        profile, GroundTrackingLimits.from_fixed_route_config(FixedRouteConfig()),
        frame.world, cache,
    )


def _verify(frame, state, profile, **kwargs):
    cache = WorldQueryCache(frame.world)
    return GroundCandidateVerifier(**kwargs.pop("verifier_kwargs", {})).verify(
        frame, state, _context(frame, state, profile, cache),
        tail_ticks=30, minimum_support=.15, profile=profile,
        query_cache=cache, **kwargs,
    )


def _oracle(frame, state, profile):
    cache = WorldQueryCache(frame.world)
    context = _context(frame, state, profile, cache)
    neutral = verified_ground_route_candidate(
        frame, state, MovementV1(), control_ticks=0, tail_ticks=30,
        minimum_support=.15, profile=profile, query_cache=cache,
    )
    found = {}
    for movement in CLOSED_GROUND_MOVEMENTS:
        prefixes = [neutral]
        for control_ticks in (1, 2):
            prefixes.append(verified_ground_route_candidate(
                frame, state, movement, control_ticks=control_ticks,
                tail_ticks=30, minimum_support=.15, profile=profile,
                query_cache=cache,
            ))
            current = tuple(prefixes)
            if any(item.status is QueryStatus.BLOCKED for item in current):
                status = GroundCandidateSafety.UNSAFE
            elif any(item.status is QueryStatus.UNSUPPORTED for item in current):
                status = GroundCandidateSafety.UNSUPPORTED
            elif any(item.status is QueryStatus.NEEDS_INFORMATION for item in current):
                status = GroundCandidateSafety.NEEDS_INFORMATION
            elif all(item.status is QueryStatus.FEASIBLE for item in current):
                status = GroundCandidateSafety.SAFE
            else:
                status = GroundCandidateSafety.UNSAFE
            found[(movement.forward, movement.strafe, control_ticks)] = status
    return found


class ClosedGroundCandidateVerifierTests(unittest.TestCase):
    def test_calculator_shape_queries_share_one_immutable_frame_cache(self):
        _, frame, _ = _fixture()
        cache = WorldQueryCache(frame.world)
        world = CachedGroundPhysicsWorld(frame, cache)
        positions = ((0, 63, 0), (1, 63, 0), (0, 63, 0))
        first = world.shapes(positions)
        hits = cache.hits
        second = world.shapes(tuple(reversed(positions)))
        self.assertIs(second, first)
        self.assertEqual(cache.hits, hits)
        self.assertEqual(first.dependencies, tuple(sorted(set(first.dependencies))))

    def test_closed_domain_matches_naive_formal_oracle(self):
        backend, frame, profile = _fixture()

        report = _verify(frame, backend.state, profile)

        self.assertTrue(report.complete)
        self.assertEqual(len(report.candidates), 18)
        self.assertEqual(
            {(item.movement.forward, item.movement.strafe, item.control_ticks): item.status
             for item in report.candidates},
            _oracle(frame, backend.state, profile),
        )
        self.assertEqual(report.stats.prefix0_computations, 1)
        self.assertEqual(report.stats.first_tick_computations, 9)
        self.assertEqual(report.stats.second_tick_computations, 9)

    def test_candidate_enumeration_order_cannot_change_safety_set(self):
        backend, frame, profile = _fixture()
        verifier = GroundCandidateVerifier()
        del verifier
        normal = _verify(frame, backend.state, profile, movements=CLOSED_GROUND_MOVEMENTS)
        reversed_report = _verify(
            frame, backend.state, profile,
            movements=tuple(reversed(CLOSED_GROUND_MOVEMENTS)),
        )

        def signature(report):
            return {
                (item.movement.forward, item.movement.strafe, item.control_ticks):
                (item.status, item.dependencies, item.missing_cells)
                for item in report.candidates
            }

        self.assertEqual(signature(normal), signature(reversed_report))

    def test_shared_prefix_trajectory_matches_the_formal_calculator(self):
        backend, frame, profile = _fixture()
        report = _verify(frame, backend.state, profile)
        cache = WorldQueryCache(frame.world)
        for item in report.candidates:
            with self.subTest(
                forward=item.movement.forward,
                strafe=item.movement.strafe,
                ticks=item.control_ticks,
            ):
                expected = verified_ground_route_candidate(
                    frame, backend.state, item.movement,
                    control_ticks=item.control_ticks, tail_ticks=30,
                    minimum_support=.15, profile=profile, query_cache=cache,
                )
                self.assertIsNotNone(item.result)
                self.assertEqual(item.result.trajectory, expected.trajectory)
                self.assertEqual(item.result.tracking_end, expected.tracking_end)

    def test_unknown_fact_is_typed_and_is_not_treated_as_unsafe(self):
        backend, frame, profile = _fixture()
        baseline = _verify(frame, backend.state, profile)
        dependency_counts = {}
        for item in baseline.candidates:
            for position in item.dependencies:
                dependency_counts[position] = dependency_counts.get(position, 0) + 1
        # Remove exposed headroom rather than a solid support owner; an unknown
        # cell completely enclosed by a known collision owner is intentionally
        # not an information need.
        missing_position = max(dependency_counts, key=lambda value: (value[1], dependency_counts[value]))
        facts = {}
        for x in range(-8, 9):
            for y in range(60, 70):
                for z in range(-8, 12):
                    position = (x, y, z)
                    fact = frame.world.cell(position)
                    if (position != missing_position
                            and fact.knowledge is not CellKnowledge.UNKNOWN):
                        facts[position] = fact
        world = WorldView.detached(
            frame.session, frame.world.geometry_revision,
            frame.world.evidence_revision, facts,
        )
        changed = replace(frame, world=world)

        report = _verify(changed, backend.state, profile)

        self.assertTrue(any(
            item.status is GroundCandidateSafety.NEEDS_INFORMATION
            for item in report.candidates
        ))
        self.assertTrue(any(missing_position in item.missing_cells for item in report.candidates))

    def test_deadline_exhaustion_is_typed_and_never_returns_partial_complete_set(self):
        backend, frame, profile = _fixture()
        ticks = iter((0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20))
        report = _verify(
            frame, backend.state, profile, deadline_ns=5,
            verifier_kwargs={"clock_ns": lambda: next(ticks, 20)},
        )

        self.assertFalse(report.complete)
        self.assertTrue(any(
            item.status is GroundCandidateSafety.BUDGET_EXHAUSTED
            for item in report.candidates
        ))
        self.assertEqual(len(report.candidates), 18)
        self.assertEqual(report.safe_candidates, ())

    def test_material_change_is_in_the_bound_candidate_dependencies(self):
        backend, frame, profile = _fixture()
        report = _verify(frame, backend.state, profile)
        safe = next(item for item in report.candidates
                    if item.status is GroundCandidateSafety.SAFE
                    and item.movement != MovementV1())
        self.assertTrue(safe.dependencies)
        support = min(safe.dependencies, key=lambda cell: cell[1])
        stamp = ObservationStamp(frame.session, 3, 3, "f2cv-material", 150_000_000)
        backend.truth.observe_blocks(stamp, {
            support: BlockGeometry.full_cube("minecraft:ice"),
        })
        self.assertIn(support, safe.dependencies)


if __name__ == "__main__":
    unittest.main()
