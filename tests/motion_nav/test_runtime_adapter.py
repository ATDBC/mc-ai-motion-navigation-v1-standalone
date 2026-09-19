import math
import unittest
from dataclasses import replace

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v3 import CollisionShapeV3, ObservedBlockV3
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import CellKnowledge
from tests.observation_v3_fixtures import valid_snapshot_v3


class B02RuntimeAdapterTests(unittest.TestCase):
    def test_fabric_and_craftground_share_body_and_world_contract(self):
        base = valid_snapshot_v3(sequence=3)
        fabric = replace(base, episode_id="same-episode", source_backend="fabric")
        craftground = replace(base, episode_id="same-episode", source_backend="craftground")

        fabric_frame = NavigationObservationAdapter().ingest(fabric)
        craftground_frame = NavigationObservationAdapter().ingest(craftground)

        for frame in (fabric_frame, craftground_frame):
            self.assertEqual(frame.body.position, (0.5, 64.0, 0.5))
            self.assertEqual(frame.body.body_box.as_tuple(), (0.2, 64.0, 0.2, 0.8, 65.8, 0.8))
            self.assertAlmostEqual(frame.body.yaw_radians, math.radians(base.self_state.value.yaw_degrees))
            self.assertEqual(frame.body.velocity_blocks_per_second, tuple(
                value * 20.0 for value in (
                    base.self_state.value.velocity.x,
                    base.self_state.value.velocity.y,
                    base.self_state.value.velocity.z,
                )))
        self.assertNotEqual(fabric_frame.session, craftground_frame.session)

    def test_new_episode_replaces_world_owner_and_rejects_old_snapshot(self):
        adapter = NavigationObservationAdapter()
        first = adapter.ingest(replace(valid_snapshot_v3(sequence=1), episode_id="one"))
        second = adapter.ingest(replace(valid_snapshot_v3(sequence=0), episode_id="two"))
        self.assertNotEqual(first.session, second.session)
        with self.assertRaisesRegex(ContractViolation, "retired world session"):
            adapter.ingest(replace(valid_snapshot_v3(sequence=2), episode_id="one"))

    def test_air_request_is_bounded_sorted_and_positive_only(self):
        positions = tuple((x, 64, 0) for x in range(3))
        request = ObservationRequestV3("navigation_v1", tuple(reversed(positions)))
        self.assertEqual(request.air_positions, positions)
        with self.assertRaises(ContractViolation):
            ObservationRequestV3("navigation_v1", tuple((x, 64, 0) for x in range(513)))

        air = ObservedBlockV3((1, 64, 0), "minecraft:air", CollisionShapeV3("empty"),
                              None, ("air_query",))
        occupied_air = ObservedBlockV3((0, 64, 0), "minecraft:air", CollisionShapeV3("empty"),
                                       None, ("body_contact",))
        wall = ObservedBlockV3((2, 64, 0), "minecraft:stone", CollisionShapeV3("full_cube"),
                               None, ("first_hit_ray",))
        adapter = NavigationObservationAdapter()
        frame = adapter.ingest(valid_snapshot_v3(blocks=(occupied_air, air, wall), sequence=4))
        self.assertIs(frame.world.cell((0, 64, 0)).knowledge, CellKnowledge.AIR)
        self.assertIs(frame.world.cell((1, 64, 0)).knowledge, CellKnowledge.AIR)
        self.assertIs(frame.world.cell((2, 64, 0)).knowledge, CellKnowledge.BLOCK)
        self.assertIs(frame.world.cell((0, 65, 0)).knowledge, CellKnowledge.UNKNOWN)

    def test_request_builder_preserves_unknown_and_limits_one_frame(self):
        cells = tuple((index, 64, 0) for index in range(700))
        adapter = NavigationObservationAdapter()
        adapter.ingest(valid_snapshot_v3(sequence=1))
        request, deferred = adapter.air_request(cells, max_positions=128)
        self.assertEqual(len(request.air_positions), 128)
        self.assertEqual(len(deferred), 572)
        self.assertEqual(request.air_positions + deferred, cells)

    def test_unknown_air_request_is_suppressed_until_retry_tick(self):
        adapter = NavigationObservationAdapter()
        first = valid_snapshot_v3(sequence=1)
        adapter.ingest(first)
        position = (10, 64, 0)
        request, deferred = adapter.air_request((position,))
        self.assertEqual(request.air_positions, (position,))
        self.assertEqual(deferred, ())
        request, _ = adapter.air_request((position,))
        self.assertEqual(request.air_positions, ())
        for sequence in range(2, 6):
            adapter.ingest(valid_snapshot_v3(sequence=sequence))
        request, _ = adapter.air_request((position,))
        self.assertEqual(request.air_positions, ())
        adapter.ingest(valid_snapshot_v3(sequence=6))
        request, _ = adapter.air_request((position,))
        self.assertEqual(request.air_positions, (position,))

    def test_frame_reports_only_cells_whose_navigation_geometry_changed(self):
        position=(4,64,0)
        air=ObservedBlockV3(position,"minecraft:air",CollisionShapeV3("empty"),
                            None,("air_query",))
        wall=ObservedBlockV3(position,"minecraft:stone",CollisionShapeV3("full_cube"),
                             None,("first_hit_ray",))
        adapter=NavigationObservationAdapter()
        first=adapter.ingest(valid_snapshot_v3(blocks=(air,),sequence=1))
        second=adapter.ingest(valid_snapshot_v3(blocks=(wall,),sequence=2))
        third=adapter.ingest(valid_snapshot_v3(blocks=(wall,),sequence=3))
        self.assertIn(position,first.changed_cells)
        self.assertEqual(second.changed_cells,(position,))
        self.assertEqual(third.changed_cells,())


if __name__ == "__main__":
    unittest.main()
