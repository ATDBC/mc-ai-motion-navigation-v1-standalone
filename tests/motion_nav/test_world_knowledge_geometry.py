import math
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_v3 import CollisionShapeV3, ObservedBlockV3
from mc2p.motion_nav.legacy.air_confirmation import (
    AirConfirmationBatch, AirConfirmationService,
)
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.observed_block_adapter import apply_observed_blocks
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, CellKnowledge, ObservationStamp, WorldKnowledge,
    WorldSessionId, WorldUpdateStatus, elapsed_seconds,
)


def stamp(session, sequence, tick, received_ns=None):
    return ObservationStamp(
        session=session,
        sequence_id=sequence,
        world_tick=tick,
        controller_clock_id="controller-a",
        received_monotonic_ns=tick * 50_000_000 if received_ns is None else received_ns,
    )


class PositiveAirProbe:
    def __init__(self, air_positions):
        self.air_positions = frozenset(air_positions)
        self.calls = []

    def confirm_air(self, session, positions, request_stamp):
        self.calls.append(positions)
        confirmed = tuple(position for position in positions if position in self.air_positions)
        return AirConfirmationBatch(stamp(session, request_stamp.sequence_id + 1,
                                           request_stamp.world_tick), confirmed)


class WorldKnowledgeTests(unittest.TestCase):
    def test_live_view_expires_only_when_its_queried_section_changes(self):
        session = WorldSessionId("partitioned-world")
        world = WorldKnowledge(session)
        world.observe_blocks(stamp(session, 1, 1), {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        local_view = world.view()

        world.observe_blocks(stamp(session, 2, 2), {
            (32, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        self.assertIs(local_view.cell((0, 0, 0)).knowledge, CellKnowledge.BLOCK)

        world.observe_blocks(stamp(session, 3, 3), {
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        with self.assertRaisesRegex(ContractViolation, "section changed"):
            local_view.cell((0, 0, 0))

    def test_capacity_never_evicts_current_area_or_route_dependencies(self):
        session = WorldSessionId("bounded-world")
        world = WorldKnowledge(session, max_known_cells=2)
        world.set_protection((0.5, 0.5, 0.5), ((32, 0, 0),))
        world.observe_blocks(stamp(session, 1, 1), {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (32, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })

        full = world.observe_blocks(stamp(session, 2, 2), {
            (64, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })

        self.assertIs(full.status, WorldUpdateStatus.CAPACITY_EXHAUSTED)
        self.assertEqual(full.rejected_positions, ((64, 0, 0),))
        self.assertIs(world.view().cell((0, 0, 0)).knowledge, CellKnowledge.BLOCK)
        self.assertIs(world.view().cell((32, 0, 0)).knowledge, CellKnowledge.BLOCK)

        world.set_protection((128.5, 0.5, 0.5), ())
        admitted = world.observe_blocks(stamp(session, 3, 3), {
            (64, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        self.assertIs(admitted.status, WorldUpdateStatus.APPLIED)
        self.assertEqual(admitted.applied_count, 1)
        self.assertGreaterEqual(len(admitted.evicted_positions), 1)
        self.assertEqual(world.known_cell_count, 2)

    def test_section_recency_never_moves_backward_for_an_older_cell_fact(self):
        session = WorldSessionId("section-recency")
        world = WorldKnowledge(session, max_known_cells=3)
        stone = BlockGeometry.full_cube("minecraft:stone")
        world.observe_blocks(stamp(session, 100, 100), {(0, 0, 0): stone})
        world.observe_blocks(stamp(session, 50, 50), {(1, 0, 0): stone})
        world.observe_blocks(stamp(session, 75, 75), {(32, 0, 0): stone})

        result = world.observe_blocks(
            stamp(session, 101, 101), {(64, 0, 0): stone},
        )

        self.assertEqual(result.evicted_positions, ((32, 0, 0),))
        view = world.view()
        self.assertIs(view.cell((0, 0, 0)).knowledge, CellKnowledge.BLOCK)
        self.assertIs(view.cell((1, 0, 0)).knowledge, CellKnowledge.BLOCK)
        self.assertIs(view.cell((32, 0, 0)).knowledge, CellKnowledge.UNKNOWN)

    def test_evicted_then_reobserved_section_never_revives_an_old_view(self):
        session = WorldSessionId("section-reobserved")
        world = WorldKnowledge(session, max_known_cells=1)
        stone = BlockGeometry.full_cube("minecraft:stone")
        world.observe_blocks(stamp(session, 1, 1), {(0, 0, 0): stone})
        old_view = world.view()
        self.assertIs(old_view.cell((0, 0, 0)).knowledge, CellKnowledge.BLOCK)

        world.observe_blocks(stamp(session, 2, 2), {(32, 0, 0): stone})
        world.observe_blocks(stamp(session, 3, 3), {(0, 0, 0): stone})

        with self.assertRaisesRegex(ContractViolation, "section changed"):
            old_view.cell((0, 0, 0))

    def test_thirty_minute_equivalent_updates_keep_world_state_bounded(self):
        session = WorldSessionId("thirty-minute-capacity")
        world = WorldKnowledge(session, max_known_cells=32)
        for tick in range(1, 36_001):
            position = (tick * 16, 0, 0)
            world.confirm_air(stamp(session, tick, tick), (position,))
            world.invalidate(stamp(session, tick + 40_000, tick + 40_000), (
                (tick * 16, 32, 0),
            ))

        self.assertLessEqual(world.known_cell_count, 32)
        self.assertLessEqual(world.tombstone_count, 32)
        self.assertLessEqual(world.section_count, 32)
        self.assertLessEqual(world.metadata_section_count, 64)

    def test_elapsed_time_requires_same_world_session_and_clock(self):
        first_session = WorldSessionId("world-a")
        later = stamp(first_session, 2, 11, 1_150_000_000)
        earlier = stamp(first_session, 1, 10, 1_000_000_000)
        self.assertAlmostEqual(elapsed_seconds(later, earlier), 0.15)
        with self.assertRaises(ContractViolation):
            elapsed_seconds(stamp(WorldSessionId("world-b"), 1, 1), earlier)
        with self.assertRaises(ContractViolation):
            ObservationStamp(first_session, 3, 12, "other-clock", 1_200_000_000).elapsed_seconds(earlier)

    def test_visible_block_air_and_unknown_remain_distinct(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        world.observe_blocks(stamp(session, 1, 1), {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        world.confirm_air(stamp(session, 2, 2), ((0, 1, 0),))
        view = world.view()
        self.assertIs(view.cell((0, 0, 0)).knowledge, CellKnowledge.BLOCK)
        self.assertIs(view.cell((0, 1, 0)).knowledge, CellKnowledge.AIR)
        self.assertIs(view.cell((0, 2, 0)).knowledge, CellKnowledge.UNKNOWN)
        with self.assertRaises(ContractViolation):
            world.confirm_air(stamp(WorldSessionId("world-b"), 3, 3), ((0, 2, 0),))

    def test_air_confirmation_is_batched_positive_only_and_cached(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        world.observe_blocks(stamp(session, 1, 1), {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        probe = PositiveAirProbe({(0, 1, 0)})
        service = AirConfirmationService(world, probe, retry_after_ticks=5)
        requested = ((0, 0, 0), (0, 1, 0), (1, 1, 0), (0, 1, 0))
        first = service.ensure_air(requested, stamp(session, 2, 2))
        self.assertEqual(probe.calls, [((0, 1, 0), (1, 1, 0))])
        self.assertEqual(first.confirmed_air, ((0, 1, 0),))
        self.assertEqual(first.unresolved, ((1, 1, 0),))
        self.assertIs(world.view().cell((1, 1, 0)).knowledge, CellKnowledge.UNKNOWN)
        second = service.ensure_air(requested, stamp(session, 4, 3))
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(second.cached_air, ((0, 1, 0),))
        service.ensure_air(requested, stamp(session, 5, 7))
        self.assertEqual(probe.calls[-1], ((1, 1, 0),))

    def test_large_air_volume_uses_one_probe_call_and_world_change_invalidates_it(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        positions = tuple((x, 1, z) for x in range(20) for z in range(20))
        probe = PositiveAirProbe(positions)
        service = AirConfirmationService(world, probe)
        service.ensure_air(positions, stamp(session, 1, 1))
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(len(probe.calls[0]), 400)
        service.ensure_air(positions, stamp(session, 3, 2))
        self.assertEqual(len(probe.calls), 1)

        changed = ((3, 1, 4),)
        world.invalidate(stamp(session, 4, 3), changed)
        service.invalidate_attempts(changed)
        self.assertIs(world.view().cell(changed[0]).knowledge, CellKnowledge.UNKNOWN)
        service.ensure_air(changed, stamp(session, 5, 3))
        self.assertEqual(probe.calls[-1], changed)

        world.invalidate(stamp(session, 8, 4), changed)
        world.confirm_air(stamp(session, 7, 3), changed)
        self.assertIs(world.view().cell(changed[0]).knowledge, CellKnowledge.UNKNOWN)

    def test_observed_block_adapter_preserves_visible_collision_geometry(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        block = ObservedBlockV3(
            (3, 4, 5), "minecraft:stone", CollisionShapeV3("full_cube"), None,
            ("surface_depth",),
        )
        apply_observed_blocks(world, stamp(session, 1, 1), (block,))
        fact = world.view().cell((3, 4, 5))
        self.assertIs(fact.knowledge, CellKnowledge.BLOCK)
        self.assertEqual(fact.block.material_key, "minecraft:stone")
        self.assertEqual(fact.block.collision_kind, "full_cube")


class GeometryTests(unittest.TestCase):
    def test_cross_cell_collision_box_is_owned_by_and_depends_on_source_cell(self):
        session = WorldSessionId("cross-cell-owner")
        world = WorldKnowledge(session)
        body = Aabb(.2, 1.2, .2, .8, 3.0, .8)
        initial = sweep(body, (0.0, 0.0, 0.0), world.view())
        world.confirm_air(stamp(session, 1, 1), initial.missing_cells)
        world.observe_blocks(stamp(session, 2, 2), {
            (0, 0, 0): BlockGeometry(
                "minecraft:cobblestone_wall", "boxes",
                (Aabb(.25, 0.0, .25, .75, 1.5, .75),),
            ),
        })

        result = sweep(body, (0.0, 0.0, 0.0), world.view())

        self.assertIs(result.status, QueryStatus.BLOCKED)
        self.assertIn((0, 0, 0), result.dependencies)

    def test_cross_cell_owner_lookup_works_across_negative_coordinate_boundary(self):
        session = WorldSessionId("negative-cross-cell-owner")
        world = WorldKnowledge(session)
        body = Aabb(-.75, 1.2, .2, -.25, 3.0, .8)
        initial = sweep(body, (0.0, 0.0, 0.0), world.view())
        world.confirm_air(stamp(session, 1, 1), initial.missing_cells)
        world.observe_blocks(stamp(session, 2, 2), {
            (-1, 0, 0): BlockGeometry(
                "minecraft:cobblestone_wall", "boxes",
                (Aabb(.25, 0.0, .25, .75, 1.5, .75),),
            ),
        })

        result = sweep(body, (0.0, 0.0, 0.0), world.view())

        self.assertIs(result.status, QueryStatus.BLOCKED)
        self.assertIn((-1, 0, 0), result.dependencies)

    def test_cross_cell_support_uses_actual_top_and_owner_dependency(self):
        session = WorldSessionId("cross-cell-support")
        world = WorldKnowledge(session)
        body = Aabb(.2, 1.5, .2, .8, 3.3, .8)
        initial = query_support(body, world.view())
        world.confirm_air(stamp(session, 1, 1), initial.missing_cells)
        world.observe_blocks(stamp(session, 2, 2), {
            (0, 0, 0): BlockGeometry(
                "minecraft:cobblestone_wall", "boxes",
                (Aabb(0.0, 0.0, 0.0, 1.0, 1.5, 1.0),),
            ),
        })

        result = query_support(body, world.view())

        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertAlmostEqual(result.support_fraction, 1.0)
        self.assertIn((0, 0, 0), result.dependencies)

    def test_collision_box_extent_is_finite_and_bounded_to_neighbor_cells(self):
        BlockGeometry(
            "minecraft:cobblestone_wall", "boxes",
            (Aabb(0.0, 0.0, 0.0, 1.0, 2.0, 1.0),),
        )
        with self.assertRaisesRegex(ContractViolation, "vertical neighbor extent"):
            BlockGeometry(
                "minecraft:custom", "boxes",
                (Aabb(-.01, 0.0, 0.0, 1.0, 1.0, 1.0),),
            )
        with self.assertRaisesRegex(ContractViolation, "vertical neighbor extent"):
            BlockGeometry(
                "minecraft:custom", "boxes",
                (Aabb(0.0, 0.0, 0.0, 1.0, 2.01, 1.0),),
            )

    def test_support_tolerance_checks_the_block_below_an_integer_boundary(self) -> None:
        session = WorldSessionId("support-tolerance")
        world = WorldKnowledge(session)
        observed = stamp(session, 1, 1)
        world.observe_blocks(observed, {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        world.confirm_air(observed, ((0, 1, 0),))
        result = query_support(Aabb(.2, 1.02, .2, .8, 2.82, .8), world.view())
        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertAlmostEqual(result.support_fraction, 1.0)
        self.assertIn((0, 0, 0), result.dependencies)

    def test_sweep_requests_unknown_volume_then_reuses_confirmed_air(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        body = Aabb(.2, 1.0, .2, .8, 2.8, .8)
        first = sweep(body, (2.0, 0.0, 0.0), world.view())
        self.assertIs(first.status, QueryStatus.NEEDS_INFORMATION)
        self.assertTrue(first.missing_cells)
        world.confirm_air(stamp(session, 1, 1), first.missing_cells)
        second = sweep(body, (2.0, 0.0, 0.0), world.view())
        self.assertIs(second.status, QueryStatus.FEASIBLE)
        self.assertFalse(second.missing_cells)

    def test_continuous_sweep_detects_middle_obstacle_with_clear_endpoints(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        body = Aabb(.2, 1.0, .2, .8, 2.8, .8)
        initial = sweep(body, (2.0, 0.0, 0.0), world.view())
        world.confirm_air(stamp(session, 1, 1), initial.missing_cells)
        world.observe_blocks(stamp(session, 2, 2), {
            (1, 1, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        result = sweep(body, (2.0, 0.0, 0.0), world.view())
        self.assertIs(result.status, QueryStatus.BLOCKED)
        self.assertGreaterEqual(result.first_collision_fraction, 0.0)
        self.assertLess(result.first_collision_fraction, 1.0)

    def test_sweep_allows_touching_at_end_and_moving_away_from_contact(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        world.observe_blocks(stamp(session, 1, 1), {
            (1, 1, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        toward = Aabb(.2, 1.0, .2, .8, 1.8, .8)
        cells = sweep(toward, (.2, 0.0, 0.0), world.view()).missing_cells
        world.confirm_air(stamp(session, 2, 2), cells)
        self.assertIs(sweep(toward, (.2, 0.0, 0.0), world.view()).status,
                      QueryStatus.FEASIBLE)

        touching = Aabb(.4, 1.0, .2, 1.0, 1.8, .8)
        cells = sweep(touching, (-.1, 0.0, 0.0), world.view()).missing_cells
        world.confirm_air(stamp(session, 3, 3), cells)
        self.assertIs(sweep(touching, (-.1, 0.0, 0.0), world.view()).status,
                      QueryStatus.FEASIBLE)

    def test_sweep_treats_negative_integer_contact_as_contact_when_moving_up(self):
        session = WorldSessionId("negative-contact")
        world = WorldKnowledge(session)
        world.observe_blocks(stamp(session, 1, 1), {
            (19, -61, -8): BlockGeometry.full_cube("minecraft:stone"),
        })
        body = Aabb(
            19.19, -60.00000000000001, -7.88,
            19.81, -58.20000000000001, -7.26,
        )
        movement = (0.0, 1.0e-6, 0.0)
        missing = sweep(body, movement, world.view()).missing_cells
        world.confirm_air(stamp(session, 2, 2), missing)

        self.assertIs(sweep(body, movement, world.view()).status,
                      QueryStatus.FEASIBLE)

    def test_partial_support_is_reported_without_turning_unknown_into_air(self):
        session = WorldSessionId("world-a")
        world = WorldKnowledge(session)
        world.observe_blocks(stamp(session, 1, 1), {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        body = Aabb(.7, 1.0, .2, 1.3, 2.8, .8)
        result = query_support(body, world.view())
        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertGreater(result.support_fraction, 0.0)
        self.assertLess(result.support_fraction, 1.0)

        empty = WorldKnowledge(WorldSessionId("world-b"))
        unknown = query_support(body, empty.view())
        self.assertIs(unknown.status, QueryStatus.NEEDS_INFORMATION)
        empty.confirm_air(stamp(empty.session, 1, 1), unknown.missing_cells)
        unsupported = query_support(body, empty.view())
        self.assertIs(unsupported.status, QueryStatus.BLOCKED)
        self.assertEqual(unsupported.support_fraction, 0.0)


if __name__ == "__main__":
    unittest.main()
