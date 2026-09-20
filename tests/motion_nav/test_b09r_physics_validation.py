import unittest
from copy import deepcopy

from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.physics_validation import compare_open_loop_rows, compare_tick_rows
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)


class B09RPhysicsValidationTests(unittest.TestCase):
    def test_frozen_fabric_ground_tick_matches_within_predeclared_tolerance(self):
        session = WorldSessionId("fabric-ground-validation")
        stamp = ObservationStamp(session, 0, 0, "frozen-fabric", 0)
        knowledge = WorldKnowledge(session)
        positions = tuple(
            (x, y, z) for x in range(-2, 7) for y in range(-64, -56)
            for z in range(3, 11)
        )
        knowledge.confirm_air(stamp, positions)
        knowledge.observe_blocks(stamp, {
            (x, -61, z): BlockGeometry.full_cube("minecraft:grass_block")
            for x in range(-2, 7) for z in range(3, 11)
        })
        row = {
            "schema_version": "mc2p.client-physics-tick.v1",
            "movement_tick_id": 1,
            "pre_state": {
                "position": {"x": 2.5, "y": -60., "z": 6.5},
                "velocity": {"x": 0., "y": -.0784000015258789, "z": 0.},
                "yaw": 12., "pitch": 0., "on_ground": True,
                "horizontal_collision": False, "vertical_collision": True,
                "actual_sprinting": False, "actual_sneaking": False,
                "pose": "standing",
            },
            "movement_yaw": 12.,
            "actual_input": {"forward": 1., "strafe": 0., "jump": False,
                             "sneak": False, "sprint": False},
            "post_state": {
                "position": {"x": 2.4796295549723992, "y": -60.,
                             "z": 6.595859510377999},
                "velocity": {"x": -.011122264276950266,
                             "y": -.0784000015258789,
                             "z": .05233929874573485},
                "yaw": 12., "pitch": 0., "on_ground": True,
                "horizontal_collision": False, "vertical_collision": True,
                "actual_sprinting": False, "actual_sneaking": False,
                "pose": "standing",
            },
            "contact_events": ["vertical_collision"],
        }
        report = compare_tick_rows(
            (row,), PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET),
            JAVA_1_21_RULESET,
        )
        self.assertEqual(report.complete_rows, 1)
        self.assertEqual(report.incomplete_rows, 0)
        self.assertLess(report.position_error_max, 1e-4)
        self.assertLess(report.velocity_error_max, 1e-4)
        self.assertEqual(report.event_mismatch_rows, ())

    def test_invalid_or_incomplete_evidence_is_not_counted_as_a_match(self):
        session = WorldSessionId("invalid-evidence")
        world = PhysicsWorldView(WorldKnowledge(session).view(), JAVA_1_21_RULESET)
        report = compare_tick_rows(({"schema_version": "wrong"},), world,
                                   JAVA_1_21_RULESET)
        self.assertEqual(report.complete_rows, 0)
        self.assertEqual(report.incomplete_rows, 1)
        self.assertIn("row_0:invalid_schema", report.issues)

    def test_open_loop_report_explains_pose_transition_sequence_break(self):
        session = WorldSessionId("fabric-boundary-validation")
        stamp = ObservationStamp(session, 0, 0, "frozen-fabric", 0)
        knowledge = WorldKnowledge(session)
        positions = tuple(
            (x, y, z) for x in range(-2, 3) for y in range(62, 68)
            for z in range(-2, 3)
        )
        knowledge.confirm_air(stamp, positions)
        knowledge.observe_blocks(stamp, {
            (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-2, 3) for z in range(-2, 3)
        })
        base = {
            "schema_version": "mc2p.client-physics-tick.v1",
            "movement_tick_id": 1,
            "pre_state": {
                "position": {"x": .5, "y": 64., "z": .5},
                "velocity": {"x": 0., "y": -.0784000015258789, "z": 0.},
                "yaw": 0., "pitch": 0., "on_ground": True,
                "horizontal_collision": False, "vertical_collision": True,
                "actual_sprinting": False, "actual_sneaking": False,
                "pose": "crouching",
            },
            "movement_yaw": 0.,
            "actual_input": {"forward": 0., "strafe": 0., "jump": False,
                             "sneak": False, "sprint": False},
            "post_state": {
                "position": {"x": .5, "y": 64., "z": .5},
                "velocity": {"x": 0., "y": -.0784000015258789, "z": 0.},
                "yaw": 0., "pitch": 0., "on_ground": True,
                "horizontal_collision": False, "vertical_collision": True,
                "actual_sprinting": False, "actual_sneaking": False,
                "pose": "crouching",
            },
            "contact_events": ["vertical_collision"],
        }
        next_row = deepcopy(base)
        next_row["movement_tick_id"] = 2
        next_row["pre_state"]["pose"] = "swimming"
        next_row["post_state"]["pose"] = "swimming"

        report = compare_open_loop_rows(
            (base, next_row),
            PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET),
            JAVA_1_21_RULESET,
        )

        self.assertEqual(report.sequence_count, 2)
        self.assertEqual(report.sequence_breaks, ((1, ("pose",)),))


if __name__ == "__main__":
    unittest.main()
