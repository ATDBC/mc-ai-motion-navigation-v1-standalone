"""D116-1 prepared handoff trusts the live owner's semantic dependency history."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.action_preconditions import (
    ActionPreconditionStatus,
    check_action_precondition,
)
from mc2p.motion_nav.prepared_motion import PreparedQueryStatus
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    CellKnowledge,
    ObservationStamp,
    VisualAirEvidence,
    WorldKnowledge,
    WorldView,
)
from mc2p.motion_nav.world_snapshot import DependencyCheck, DependencyStatus
from tests.motion_nav.test_action_preconditions import _drop_route
from tests.motion_nav.test_continuous_descent import SESSION, world_and_anchor
from tests.motion_nav import test_d102_action_handoff as handoff_fixtures
from tests.motion_nav import test_b10_motion_candidate as candidate_fixtures


class SemanticDependencyHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handoff_fixtures.ActionHandoffTests.setUpClass()

    def handoff_case(self):
        case = handoff_fixtures.ActionHandoffTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        handle = case.ready_next()
        case.observe_first()
        return case, handle

    @staticmethod
    def stamp(case, sequence=20):
        return ObservationStamp(
            case.current.session, sequence, sequence, "d116-semantic", sequence * 50_000_000,
        )

    def test_stamp_only_refresh_uses_semantic_versions_and_stays_ready(self):
        case, handle = self.handoff_case()
        stamp = self.stamp(case)
        facts = handle.hot_view.dependency_facts
        blocks = {position: fact.block for position, fact in facts
                  if fact.knowledge is CellKnowledge.BLOCK}
        air = tuple(position for position, fact in facts if fact.knowledge is CellKnowledge.AIR)
        self.assertTrue(blocks)
        self.assertTrue(air)

        case.world.observe_blocks(stamp, blocks)
        case.world.confirm_air(stamp, air)
        self.assertIs(
            case.world.dependency_changes(handle.hot_view.dependency_versions).status,
            DependencyStatus.UNCHANGED,
        )

        with patch.object(
            case.world, "dependency_changes", wraps=case.world.dependency_changes,
        ) as checked:
            proposal = case.executor.stage_prepared_action(case.frame(case.current), case.current)

        checked.assert_called_once_with(handle.hot_view.dependency_versions)
        self.assertIs(proposal.entry_result, PreparedQueryStatus.READY_MATCH)

    def test_missing_live_owner_cannot_fall_back_to_equal_facts(self):
        case, handle = self.handoff_case()
        live = case.world.view()
        facts = {position: live.cell(position) for position, _ in handle.hot_view.dependency_facts}
        detached = WorldView.detached(
            live.session, live.geometry_revision, live.evidence_revision, facts,
        )
        frame = replace(case.frame(case.current), world=detached)

        proposal = case.executor.stage_prepared_action(frame, case.current)

        self.assertIs(proposal.entry_result, PreparedQueryStatus.DEPENDENCY_CHANGED)

    def test_history_incomplete_is_not_treated_as_unchanged(self):
        case, handle = self.handoff_case()
        history_world = WorldKnowledge(case.current.session)
        old = history_world.dependency_versions(((30, 0, 0),))
        geometry = BlockGeometry.full_cube("minecraft:stone")
        for sequence in range(1, 4100):
            material = "minecraft:stone" if sequence % 2 else "minecraft:dirt"
            history_world.observe_blocks(
                ObservationStamp(case.current.session, sequence, sequence, "history", sequence),
                {(0, 0, 0): BlockGeometry.full_cube(material)},
            )
        real_check = history_world.dependency_changes(old)
        self.assertIs(real_check.status, DependencyStatus.HISTORY_INCOMPLETE)

        with patch.object(case.world, "dependency_changes", return_value=real_check) as checked:
            proposal = case.executor.stage_prepared_action(case.frame(case.current), case.current)

        checked.assert_called_once_with(handle.hot_view.dependency_versions)
        self.assertIs(proposal.entry_result, PreparedQueryStatus.DEPENDENCY_CHANGED)

    def test_every_physical_semantic_change_rejects_the_prepared_entry(self):
        mutations = (
            "known_to_unknown",
            "air_to_block",
            "block_to_air",
            "material",
            "fluid",
            "collision_kind",
            "boxes",
        )
        for offset, mutation in enumerate(mutations, 30):
            with self.subTest(mutation=mutation):
                case, handle = self.handoff_case()
                facts = handle.hot_view.dependency_facts
                air = next((item for item in facts if item[1].knowledge is CellKnowledge.AIR), None)
                block = next((item for item in facts if item[1].knowledge is CellKnowledge.BLOCK), None)
                self.assertIsNotNone(air)
                self.assertIsNotNone(block)
                stamp = self.stamp(case, offset)

                if mutation == "air_to_block":
                    position, _ = air
                    case.world.observe_blocks(
                        stamp, {position: BlockGeometry.full_cube("minecraft:stone")},
                    )
                else:
                    position, fact = block
                    if mutation == "known_to_unknown":
                        case.world.invalidate(stamp, (position,))
                    elif mutation == "block_to_air":
                        case.world.confirm_air(stamp, (position,))
                    elif mutation == "material":
                        case.world.observe_blocks(
                            stamp,
                            {position: replace(fact.block, material_key="minecraft:dirt")},
                        )
                    elif mutation == "fluid":
                        case.world.observe_blocks(
                            stamp, {position: replace(fact.block, fluid=not fact.block.fluid)},
                        )
                    elif mutation == "collision_kind":
                        case.world.observe_blocks(
                            stamp,
                            {position: BlockGeometry.empty(fact.block.material_key,
                                                           fluid=fact.block.fluid)},
                        )
                    else:
                        case.world.observe_blocks(
                            stamp,
                            {position: BlockGeometry(
                                fact.block.material_key,
                                "boxes",
                                (Aabb(0, 0, 0, 1, .5, 1),),
                                fact.block.fluid,
                            )},
                        )

                current = case.world.dependency_versions((position,))[0][1]
                saved = dict(handle.hot_view.dependency_versions)[position]
                self.assertNotEqual(current, saved)
                self.assertIs(
                    case.world.dependency_changes(handle.hot_view.dependency_versions).status,
                    DependencyStatus.CHANGED,
                )
                proposal = case.executor.stage_prepared_action(
                    case.frame(case.current), case.current,
                )
                self.assertIs(
                    proposal.entry_result, PreparedQueryStatus.DEPENDENCY_CHANGED,
                )

    def test_visual_air_metadata_refresh_is_not_a_physical_change(self):
        case, handle = self.handoff_case()
        position, _ = next(
            item for item in handle.hot_view.dependency_facts
            if item[1].knowledge is CellKnowledge.AIR
        )
        stamp = self.stamp(case, 50)
        case.world.confirm_air(
            stamp,
            (position,),
            {position: VisualAirEvidence(stamp, 3.5, True)},
        )
        self.assertIs(
            case.world.dependency_changes(handle.hot_view.dependency_versions).status,
            DependencyStatus.UNCHANGED,
        )

        proposal = case.executor.stage_prepared_action(case.frame(case.current), case.current)

        self.assertIs(proposal.entry_result, PreparedQueryStatus.READY_MATCH)

    def test_controlled_drop_still_rejects_missing_stale_far_or_upper_only_evidence(self):
        cases = (
            ("missing", None, 2),
            ("far", (9.0, True), 2),
            ("upper_only", (4.0, False), 2),
            ("stale", (4.0, True), 10),
        )
        for name, evidence, frame_sequence in cases:
            with self.subTest(case=name):
                anchor, physics_world = world_and_anchor(direct_height=6, speed=0.0)
                owner = physics_world._world._owner
                self.assertIsNotNone(owner)
                if evidence is not None:
                    evidence_stamp = ObservationStamp(SESSION, 2, 2, "d116-drop", 100_000_000)
                    owner.confirm_air(
                        evidence_stamp,
                        ((0, 58, 1),),
                        {(0, 58, 1): VisualAirEvidence(
                            evidence_stamp, evidence[0], evidence[1],
                        )},
                    )
                frame = candidate_fixtures.VerifiedMotionRouteIntegrationTests.frame(
                    owner.view(),
                    replace(anchor.physics_state, movement_tick_id=frame_sequence),
                    frame_sequence,
                )

                result = check_action_precondition(
                    _drop_route(), 0, frame, task_id="d116-drop-task",
                )

                self.assertIn(
                    result.status,
                    {ActionPreconditionStatus.NEEDS_ACQUISITION,
                     ActionPreconditionStatus.NEEDS_INFORMATION},
                )


if __name__ == "__main__":
    unittest.main()
