"""D124 proof dependencies are the facts used by physics, not every broad read."""
from __future__ import annotations

import unittest
import json
import hashlib
from pathlib import Path
from dataclasses import replace

from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, CellFact, CellKnowledge, ObservationStamp,
    WorldKnowledge, WorldView,
)
from scripts import d123_dependency_probe as d123


class D124SourceQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = d123.fixed_case()
        cls.snapshot = cls.case[4]
        cls.stamp = next(
            fact.stamp for _, fact in cls.snapshot.facts if fact.stamp is not None)

    def _view(self, facts):
        return WorldView.detached(self.stamp.session, 0, 0, facts)

    def test_full_cube_shadow_removes_only_the_lower_broad_candidate(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        body = Aabb(.2, 2., .2, .8, 3.8, .8)
        world = self._view({
            cover: CellFact(
                CellKnowledge.BLOCK, stamp=self.stamp,
                block=BlockGeometry.full_cube("minecraft:stone"),
            ),
        })

        result = sweep(body, (0., -.08, 0.), world)

        self.assertIs(result.status, QueryStatus.BLOCKED)
        self.assertNotIn(hidden, result.dependencies)
        self.assertIn(cover, result.dependencies)
        self.assertNotIn(hidden, result.missing_cells)

    def test_air_block_and_unknown_below_full_cube_have_one_physics_result(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        body = Aabb(.2, 2., .2, .8, 3.8, .8)
        cover_fact = CellFact(
            CellKnowledge.BLOCK, stamp=self.stamp,
            block=BlockGeometry.full_cube("minecraft:stone"),
        )
        hidden_facts = (
            None,
            CellFact(CellKnowledge.AIR, stamp=self.stamp),
            CellFact(
                CellKnowledge.BLOCK, stamp=self.stamp,
                block=BlockGeometry(
                    "minecraft:honey_block", "boxes",
                    boxes=(Aabb(0., 0., 0., 1., 2., 1.),),
                ),
            ),
        )
        signatures = []
        for hidden_fact in hidden_facts:
            facts = {cover: cover_fact}
            if hidden_fact is not None:
                facts[hidden] = hidden_fact
            result = sweep(body, (0., -.08, 0.), self._view(facts))
            signatures.append((
                result.status, result.first_collision_fraction,
                result.dependencies, result.missing_cells,
            ))
        self.assertEqual(signatures, [signatures[0]] * len(signatures))
        self.assertNotIn(hidden, signatures[0][2])

    def test_known_special_lower_owner_is_shadowed_when_not_directly_occupied(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        body = Aabb(.2, 2., .2, .8, 3.8, .8)
        cover_fact = CellFact(
            CellKnowledge.BLOCK, stamp=self.stamp,
            block=BlockGeometry.full_cube("minecraft:stone"),
        )
        for geometry in (
                BlockGeometry.unsupported("test:synthetic_unmodeled_shape"),
                BlockGeometry.unsupported("test:synthetic_unmodeled_fluid", fluid=True)):
            with self.subTest(geometry=geometry):
                world = self._view({
                    cover: cover_fact,
                    hidden: CellFact(CellKnowledge.BLOCK, stamp=self.stamp, block=geometry),
                })
                swept = sweep(body, (0., -.08, 0.), world)
                support = query_support(body, world)
                self.assertIs(swept.status, QueryStatus.BLOCKED)
                self.assertIs(support.status, QueryStatus.FEASIBLE)
                self.assertNotIn(hidden, swept.dependencies)
                self.assertNotIn(hidden, support.dependencies)
                self.assertIn(cover, swept.dependencies)
                self.assertIn(cover, support.dependencies)

    def test_cross_section_hidden_owner_change_does_not_stale_shadow_cache(self):
        hidden = (0, 15, 0)
        cover = (0, 16, 0)
        body = Aabb(.2, 17., .2, .8, 18.8, .8)
        knowledge = WorldKnowledge(self.stamp.session)
        knowledge.observe_blocks(
            self.stamp, {cover: BlockGeometry.full_cube("minecraft:stone")})
        live_view = knowledge.view()
        cached_world = PhysicsWorldView(live_view, JAVA_1_21_RULESET)
        first = cached_world.shapes_for_sweep(body, (0., -.08, 0.))
        self.assertNotIn(hidden, first.dependencies)

        for sequence, geometry in enumerate((
                BlockGeometry.unsupported("test:synthetic_unmodeled_shape"),
                BlockGeometry.unsupported("test:synthetic_unmodeled_fluid", fluid=True),
        ), start=1):
            stamp = ObservationStamp(
                self.stamp.session, self.stamp.sequence_id + sequence,
                self.stamp.world_tick + sequence,
                self.stamp.controller_clock_id,
                self.stamp.received_monotonic_ns + sequence * 50_000_000,
            )
            knowledge.observe_blocks(stamp, {hidden: geometry})

            cached = cached_world.shapes_for_sweep(body, (0., -.08, 0.))
            fresh = PhysicsWorldView(
                knowledge.view(), JAVA_1_21_RULESET,
            ).shapes_for_sweep(body, (0., -.08, 0.))
            self.assertEqual(cached, first)
            self.assertEqual(fresh, first)

    def test_support_query_uses_cover_without_requiring_hidden_owner(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        body = Aabb(.2, 2., .2, .8, 3.8, .8)
        world = self._view({
            cover: CellFact(
                CellKnowledge.BLOCK, stamp=self.stamp,
                block=BlockGeometry.full_cube("minecraft:stone"),
            ),
        })

        result = query_support(body, world)

        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertNotIn(hidden, result.dependencies)
        self.assertIn(cover, result.dependencies)

    def test_shape_cache_keeps_conservative_and_sweep_queries_separate(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        body = Aabb(.2, 2., .2, .8, 3.8, .8)
        world = PhysicsWorldView(self._view({
            cover: CellFact(
                CellKnowledge.BLOCK, stamp=self.stamp,
                block=BlockGeometry.full_cube("minecraft:stone"),
            ),
        }), JAVA_1_21_RULESET)
        broad = ((0, 0, 0), (0, 1, 0), (0, 2, 0), (0, 3, 0))

        conservative = world.shapes(broad)
        swept = world.shapes_for_sweep(body, (0., -.08, 0.))

        self.assertIn(hidden, conservative.missing_cells)
        self.assertIn(hidden, conservative.dependencies)
        self.assertNotIn(hidden, swept.missing_cells)
        self.assertNotIn(hidden, swept.dependencies)

    def test_cover_change_invalidates_cached_shadow_and_restores_hidden_requirement(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        body = Aabb(.2, 2., .2, .8, 3.8, .8)
        knowledge = WorldKnowledge(self.stamp.session)
        knowledge.observe_blocks(
            self.stamp, {cover: BlockGeometry.full_cube("minecraft:stone")})
        world = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)
        first = world.shapes_for_sweep(body, (0., -.08, 0.))
        self.assertNotIn(hidden, first.dependencies)
        newer = ObservationStamp(
            self.stamp.session, self.stamp.sequence_id + 1,
            self.stamp.world_tick + 1, self.stamp.controller_clock_id,
            self.stamp.received_monotonic_ns + 50_000_000,
        )
        knowledge.confirm_air(newer, (cover,))

        with self.assertRaises(ValueError):
            world.shapes_for_sweep(body, (0., -.08, 0.))
        changed = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET).shapes_for_sweep(
            body, (0., -.08, 0.))
        self.assertIn(hidden, changed.dependencies)
        self.assertIn(hidden, changed.missing_cells)

    def test_partial_cover_does_not_hide_the_lower_owner(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        top_slab = BlockGeometry(
            "minecraft:stone_slab", "boxes",
            boxes=(Aabb(0., .5, 0., 1., 1., 1.),),
        )
        body = Aabb(.2, 2., .2, .8, 3.8, .8)
        world = self._view({
            cover: CellFact(CellKnowledge.BLOCK, stamp=self.stamp, block=top_slab),
        })

        result = sweep(body, (0., -.08, 0.), world)

        self.assertIs(result.status, QueryStatus.BLOCKED)
        self.assertIn(hidden, result.dependencies)
        self.assertIn(hidden, result.missing_cells)

    def test_entering_the_lower_cell_keeps_the_lower_owner_required(self):
        hidden = (0, 0, 0)
        cover = (0, 1, 0)
        body = Aabb(.2, .8, .2, .8, 1.8, .8)
        world = self._view({
            cover: CellFact(
                CellKnowledge.BLOCK, stamp=self.stamp,
                block=BlockGeometry.full_cube("minecraft:stone"),
            ),
        })

        result = sweep(body, (0., 0., 0.), world)

        self.assertIs(result.status, QueryStatus.BLOCKED)
        self.assertIn(hidden, result.dependencies)
        self.assertIn(hidden, result.missing_cells)

    def test_hidden_special_material_does_not_act_through_a_full_cube(self):
        position = (0, 62, -6)
        row = d123.replay(self.case, {
            position: d123.fact_for(self.snapshot, position, "tall_material_boundary"),
        })

        self.assertEqual(row["parent_status"], "solved")
        self.assertNotIn(position, row["dependencies"])

    def test_material_inside_the_body_remains_unsupported(self):
        state = replace(
            self.case[2].entry_state,
            position=(.5, 62., -5.5), on_ground=False,
            velocity_blocks_per_tick=(0., 0., 0.),
        )
        position = (0, 62, -6)
        world = d123.world_for(self.snapshot, {
            position: d123.fact_for(self.snapshot, position, "tall_material_boundary"),
        })

        result = step(
            state,
            TickInput(0., 0., False, False, False, state.yaw_radians),
            world,
            JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.UNSUPPORTED)
        self.assertIn(position, result.dependencies)
        self.assertIn("contact_material_motion_rule_not_supported", result.unsupported_reasons)


class D124WholeProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = d123.fixed_case()
        cls.snapshot = cls.case[4]
        cls.stamp = next(
            fact.stamp for _, fact in cls.snapshot.facts if fact.stamp is not None)

    def test_nine_shadowed_cells_do_not_become_actor_requirements(self):
        for z in (-6, -5, -4, -3, -2, -1, 1, 6, 7):
            position = (0, 62, z)
            row = d123.replay(self.case, {position: d123.UNKNOWN_CELL})
            self.assertNotIn(position, row["dependencies"], position)
            self.assertEqual(row["offline_dependency_gate"], "known", position)

    def test_three_real_dependencies_still_reject_unknown(self):
        for z in (0, 2, 5):
            position = (0, 62, z)
            row = d123.replay(self.case, {position: d123.UNKNOWN_CELL})
            self.assertIn(position, row["dependencies"], position)
            self.assertEqual(row["offline_dependency_gate"], "NEEDS_WORLD", position)

    def test_all_four_directions_keep_only_the_classified_lower_requirements(self):
        root = Path(__file__).resolve().parents[2]
        rows = json.loads((
            root / "evidence/motion_navigation/d123-proof-and-transport-v1/"
            "d123-0/classifications-final.json"
        ).read_text(encoding="utf-8"))["cells"]
        for direction in range(4):
            case = d123.fixed_case(direction)
            classified = [row for row in rows if row["direction"] == direction]
            shadowed = [tuple(row["position"]) for row in classified
                        if row["whole_chain_unknown_solved"]]
            required = [tuple(row["position"]) for row in classified
                        if not row["whole_chain_unknown_solved"]]
            self.assertEqual(len(shadowed), 9, direction)
            self.assertEqual(len(required), 3, direction)
            for position in shadowed:
                result = d123.replay(case, {position: d123.UNKNOWN_CELL})
                self.assertNotIn(position, result["dependencies"], (direction, position))
                self.assertEqual(result["offline_dependency_gate"], "known")
            for position in required:
                result = d123.replay(case, {position: d123.UNKNOWN_CELL})
                self.assertIn(position, result["dependencies"], (direction, position))
                self.assertEqual(result["offline_dependency_gate"], "NEEDS_WORLD")


class D124EvidenceTests(unittest.TestCase):
    def test_targeted_evidence_binds_rules_dependencies_and_source_boundaries(self):
        root = Path(__file__).resolve().parents[2]
        evidence = (
            root / "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
            "targeted/run-8"
        )
        manifest = json.loads((evidence / "manifest.json").read_text("utf-8"))
        for relative, expected in manifest["files"].items():
            self.assertEqual(
                hashlib.sha256((evidence / relative).read_bytes()).hexdigest(),
                expected,
                relative,
            )
        summary = json.loads((evidence / "summary.json").read_text("utf-8"))
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["dependency_counts"], [58, 58, 58, 58])
        self.assertTrue(summary["shadowed_removed"])
        self.assertTrue(summary["required_retained"])
        self.assertEqual(summary["largest_action_bytes"], 1102)
        self.assertTrue(summary["action_within_limit"])
        action = json.loads((evidence / "action-transport.json").read_text("utf-8"))
        self.assertEqual(action["largest_operation"], "ClickSlotV1")
        self.assertEqual(action["largest_bytes"], 1102)
        self.assertEqual(action["limit_bytes"], 16_384)
        self.assertTrue(action["within_limit"])
        rules = json.loads((evidence / "minecraft-1_21-rules.json").read_text("utf-8"))
        self.assertTrue(all(rules["checks"].values()))
        self.assertTrue(all(rules["block_collision_spliterator_checks"].values()))
        self.assertTrue(all(rules["representative_shape_checks"].values()))
        self.assertTrue(all(rules["dynamic_block_checks"].values()))
        self.assertEqual(
            rules["source_jar_sha256"],
            "4b56bc9b44de7e88f9fbb52e716c18240da34469fcbafa5ed2e75b04d60617e8",
        )
        self.assertEqual(
            rules["named_transformed_jar_sha256"],
            "f470e595e849846ecc74e4f029877d1e649e75ba9b5b40a5c9f26ba72e6b1fed",
        )
        boundaries = json.loads((evidence / "source-boundaries.json").read_text("utf-8"))
        self.assertFalse(boundaries["prepared_broad_dependency_update_present"])
        self.assertFalse(boundaries["support_surfaces_imported_by_prepared_proof"])
        self.assertFalse(boundaries["support_surfaces_imported_by_motion_solver"])


if __name__ == "__main__":
    unittest.main()
