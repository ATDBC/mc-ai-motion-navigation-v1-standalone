"""F2-TS pure bounded ordinary-ground terminal search."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.async_work import (
    AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind,
)
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.ground_terminal_search import (
    GroundTerminalSearchLayer,
    GroundTerminalSearchLimits,
    GroundTerminalSearchStatus,
    GroundTerminalSolveRequest,
    solve_ground_terminal_sequence,
)
import mc2p.motion_nav.ground_terminal_search as terminal_search
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, MotionTickPhase, StateAnchor,
)
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, WorldKnowledge
from scripts.f2_ground_route_evidence import _frame
from tests.motion_nav.test_f2cv_ground_candidate_verifier import _fixture


def _flat_fixture(*, yaw: float = 0.0, velocity=(0.0, -0.0784, 0.0),
                  scene: str = "open"):
    backend, _, profile = _fixture()
    state = replace(
        backend.state,
        position=(0.5, 64.0, 0.5),
        velocity_blocks_per_tick=velocity,
        yaw_radians=yaw,
        movement_tick_id=20,
        horizontal_collision=False,
    )
    world = WorldKnowledge(state.session)
    stamp = backend.world._world.cell((0, 63, 0)).stamp
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-5, 6) for y in range(63, 68) for z in range(-5, 6)
    ))
    floor = {
        (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
        for x in range(-5, 6) for z in range(-5, 6)
        if scene != "edge" or x <= 0
    }
    obstacles = {}
    if scene == "corridor":
        obstacles.update({
            (x, 64, z): BlockGeometry.full_cube("minecraft:stone")
            for x in (-1, 1) for z in range(-2, 4)
        })
    elif scene == "wall_corner":
        obstacles.update({
            (1, 64, z): BlockGeometry.full_cube("minecraft:stone")
            for z in range(1, 4)
        })
        obstacles.update({
            (x, 64, 1): BlockGeometry.full_cube("minecraft:stone")
            for x in range(1, 4)
        })
    elif scene == "clutter":
        obstacles.update({
            (-2, 64, 2): BlockGeometry.full_cube("minecraft:stone"),
            (2, 64, -2): BlockGeometry.full_cube("minecraft:stone"),
            (2, 64, 2): BlockGeometry.full_cube("minecraft:stone"),
        })
    world.observe_blocks(stamp, floor | obstacles)
    frame = _frame(state, world.view(), 20)
    anchor = StateAnchor(
        state.session, 20, 20, MotionTickPhase.AFTER_MOVEMENT,
        None, None, state.ruleset_id, state.state_schema,
        "client-input-projection-v1", state,
    )
    identity = AsyncWorkIdentity(
        AsyncComputationScope(state.session.value, "f2ts-holdout", 1),
        "ground-terminal-owner", AsyncWorkKind.MOTION_SOLVE,
        "ground-terminal", 1,
    )
    return frame, state, profile, anchor, identity


def _request(*, bounds: Aabb, yaw: float = 0.0,
             velocity=(0.0, -0.0784, 0.0),
             limits: GroundTerminalSearchLimits | None = None,
             lead_ticks: int = 1, scene: str = "open"):
    frame, state, profile, anchor, identity = _flat_fixture(
        yaw=yaw, velocity=velocity, scene=scene,
    )
    completion = GroundCompletionRegion(
        bounds, (
            (bounds.min_x + bounds.max_x) / 2,
            64.0,
            (bounds.min_z + bounds.max_z) / 2,
        ), 64.0, (0, 63, 0, 0), ((0, 63, 0),),
    )
    preparation = (MovementV1(),) * (lead_ticks - 1)
    return GroundTerminalSolveRequest(
        anchor=anchor,
        work_identity=identity,
        goal_id="holdout-goal",
        goal_revision=1,
        route_id="holdout-route",
        route_revision=1,
        action_index=0,
        execution_window=CandidateExecutionWindow(
            anchor.movement_tick_id + lead_ticks,
            anchor.movement_tick_id + lead_ticks + 1,
        ),
        frame=frame,
        completion=completion,
        profile=profile,
        limits=limits or GroundTerminalSearchLimits(
            maximum_ticks=6,
            neutral_tail_ticks=30,
            phase_candidate_budget=1024,
            beam_node_budget=4096,
        ),
        preparation_inputs=preparation,
    )


class GroundTerminalSearchContractTests(unittest.TestCase):
    def test_frozen_holdout_uses_only_relative_geometry_and_all_proofs_are_complete(self):
        manifest = json.loads((
            Path(__file__).with_name("fixtures") / "f2ts_holdout_v1.json"
        ).read_text("utf-8"))
        self.assertEqual(manifest["schema_version"], "f2ts-holdout-v1")
        self.assertNotIn("f2r/", json.dumps(manifest))
        for item in manifest["cases"]:
            with self.subTest(case=item["id"]):
                request = _request(
                    bounds=Aabb(*item["bounds"]), yaw=item["yaw"],
                    velocity=tuple(item["velocity"]), scene=item["scene"],
                    limits=GroundTerminalSearchLimits(
                        maximum_ticks=3 if item["expected_layer"] == "beam" else 6,
                        phase_candidate_budget=1000,
                        beam_node_budget=5000,
                    ),
                    lead_ticks=12 if item["expected_layer"] == "beam" else 1,
                )
                result = solve_ground_terminal_sequence(request)
                self.assertIs(result.status, GroundTerminalSearchStatus.SOLVED, result)
                assert result.sequence is not None
                self.assertEqual(result.sequence.layer.value, item["expected_layer"])
                self.assertTrue(all(
                    tail.safe and tail.dependencies
                    for branch in (result.sequence.normal, result.sequence.late1)
                    for tail in branch.prefix_tails
                ))

    def test_typed_request_rejects_an_unbounded_depth(self):
        with self.assertRaisesRegex(Exception, "maximum ticks"):
            _request(
                bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74),
                limits=GroundTerminalSearchLimits(maximum_ticks=65),
            )

    def test_phase_search_proves_normal_late1_and_every_prefix_tail(self):
        request = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))

        result = solve_ground_terminal_sequence(request)

        self.assertIs(result.status, GroundTerminalSearchStatus.SOLVED)
        self.assertIsNotNone(result.sequence)
        sequence = result.sequence
        assert sequence is not None
        self.assertIs(sequence.layer, GroundTerminalSearchLayer.PHASE)
        self.assertGreater(len(sequence.commands), 0)
        self.assertEqual(len(sequence.normal.prefix_tails), len(sequence.commands) + 1)
        self.assertEqual(len(sequence.late1.prefix_tails), len(sequence.commands) + 1)
        for branch in (sequence.normal, sequence.late1):
            self.assertTrue(request.completion.contains(branch.stopped_end.position))
            self.assertTrue(all(proof.safe for proof in branch.prefix_tails))
            self.assertTrue(branch.dependencies)
        self.assertEqual(sequence.work_identity, request.work_identity)
        self.assertEqual(sequence.goal_revision, request.goal_revision)
        self.assertEqual(sequence.route_revision, request.route_revision)
        self.assertEqual(sequence.cost_ticks, len(sequence.commands))
        self.assertTrue(sequence.sequence_id.startswith("ground-terminal:"))

    def test_same_request_has_a_stable_result_and_rotation_does_not_use_world_names(self):
        south = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))
        west = _request(
            bounds=Aabb(.26, 63.99, .45, .32, 64.01, .55),
            yaw=1.5707963267948966,
        )

        first = solve_ground_terminal_sequence(south)
        second = solve_ground_terminal_sequence(south)
        rotated = solve_ground_terminal_sequence(west)

        self.assertEqual(first.sequence, second.sequence)
        self.assertIs(rotated.status, GroundTerminalSearchStatus.SOLVED)
        self.assertFalse(any(
            word in (rotated.reasons or ()) for word in ("north", "south", "east", "west")
        ))

    def test_goal_route_and_work_revisions_change_the_stable_identity(self):
        request = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))
        baseline = solve_ground_terminal_sequence(request)
        assert baseline.sequence is not None
        changed = replace(
            request,
            goal_revision=2,
            route_revision=2,
            work_identity=replace(request.work_identity, revision=2),
        )

        revised = solve_ground_terminal_sequence(changed)

        assert revised.sequence is not None
        self.assertNotEqual(baseline.sequence.sequence_id, revised.sequence.sequence_id)
        self.assertEqual(revised.sequence.goal_revision, 2)
        self.assertEqual(revised.sequence.route_revision, 2)
        self.assertEqual(revised.sequence.work_identity.revision, 2)

    def test_solver_source_has_no_oracle_id_or_cardinal_case_switch(self):
        source = Path(terminal_search.__file__).read_text("utf-8")
        for forbidden in (
            "f2r/clutter", '"north"', '"south"', '"east"', '"west"', "oracle-",
        ):
            self.assertNotIn(forbidden, source.lower())

    def test_every_retained_phase_template_has_a_frozen_deletion_witness(self):
        manifest = json.loads((
            Path(__file__).with_name("fixtures") / "f2ts_holdout_v1.json"
        ).read_text("utf-8"))
        witnesses = {
            tuple(terminal_search.GroundTerminalPrimitive(value)
                  for value in item["witness_template"]): item
            for item in manifest["cases"] if "witness_template" in item
        }
        self.assertEqual(set(terminal_search._PHASE_TEMPLATES), set(witnesses))

        for template, item in witnesses.items():
            with self.subTest(template=tuple(value.value for value in template)):
                request = _request(
                    bounds=Aabb(*item["bounds"]), yaw=item["yaw"],
                    velocity=tuple(item["velocity"]), scene=item["scene"],
                    limits=GroundTerminalSearchLimits(
                        maximum_ticks=6, phase_candidate_budget=1000,
                        beam_node_budget=5000,
                    ),
                )
                baseline = solve_ground_terminal_sequence(request)
                self.assertIs(baseline.status, GroundTerminalSearchStatus.SOLVED)
                assert baseline.sequence is not None
                self.assertIs(baseline.sequence.layer, GroundTerminalSearchLayer.PHASE)
                self.assertEqual(baseline.sequence.primitive_ids, template)

                with patch.object(
                    terminal_search, "_PHASE_TEMPLATES",
                    tuple(value for value in terminal_search._PHASE_TEMPLATES
                          if value != template),
                ):
                    mutated = solve_ground_terminal_sequence(request)

                changed = mutated.status is not GroundTerminalSearchStatus.SOLVED
                if mutated.sequence is not None:
                    changed = changed or (
                        mutated.sequence.layer is GroundTerminalSearchLayer.BEAM
                        or mutated.sequence.cost_ticks > baseline.sequence.cost_ticks
                    )
                self.assertTrue(changed, mutated)

    def test_sequence_constructor_rejects_a_missing_late_or_prefix_proof(self):
        request = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))
        result = solve_ground_terminal_sequence(request)
        assert result.sequence is not None
        with self.assertRaisesRegex(Exception, "proof is incomplete"):
            replace(result.sequence, late1=result.sequence.normal)
        with self.assertRaisesRegex(Exception, "proof is incomplete"):
            replace(
                result.sequence,
                normal=replace(
                    result.sequence.normal,
                    prefix_tails=result.sequence.normal.prefix_tails[:-1],
                ),
            )

    def test_sequence_identity_includes_preparation_and_route_corridor(self):
        request = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))
        baseline = solve_ground_terminal_sequence(request)
        assert baseline.sequence is not None
        prepared = solve_ground_terminal_sequence(replace(
            request,
            preparation_inputs=(terminal_search._NEUTRAL,),
            execution_window=CandidateExecutionWindow(22, 23),
            route_corridor=(Aabb(-2, 63.9, -2, 2, 66, 2),),
        ))
        assert prepared.sequence is not None

        self.assertNotEqual(
            baseline.sequence.sequence_id, prepared.sequence.sequence_id,
        )

    def test_sequence_constructor_rejects_invalid_task_and_branch_fields(self):
        request = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))
        result = solve_ground_terminal_sequence(request)
        assert result.sequence is not None
        sequence = result.sequence
        invalid = (
            {"goal_id": ""},
            {"route_id": ""},
            {"goal_revision": 0},
            {"route_revision": 0},
            {"action_index": -1},
            {"normal": replace(
                sequence.normal,
                command_trajectory=sequence.normal.command_trajectory[:-1],
            )},
        )
        for mutation in invalid:
            with self.subTest(mutation=tuple(mutation)):
                with self.assertRaises(Exception):
                    replace(sequence, **mutation)

    def test_lead_is_derived_from_anchor_window_and_bounded_preparation(self):
        request = _request(
            bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74),
            lead_ticks=12,
        )
        self.assertFalse(hasattr(request, "lead_ticks"))
        self.assertEqual(
            request.execution_window.earliest_start_tick,
            request.anchor.movement_tick_id + 1 + len(request.preparation_inputs),
        )
        self.assertEqual(
            request.execution_window.latest_start_tick,
            request.execution_window.earliest_start_tick + 1,
        )
        with self.assertRaises(Exception):
            replace(
                request,
                execution_window=CandidateExecutionWindow(40, 41),
            )
        with self.assertRaises(Exception):
            replace(request, preparation_inputs=(MovementV1(),) * 33)

    def test_sequence_canonical_identity_rejects_replace_of_any_physics_basis(self):
        request = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))
        result = solve_ground_terminal_sequence(request)
        assert result.sequence is not None
        sequence = result.sequence
        changed_entry = replace(
            sequence.normal.entry_state,
            horizontal_collision=not sequence.normal.entry_state.horizontal_collision,
        )
        mutations = (
            {"anchor": replace(
                sequence.anchor, input_projection_version="changed-projection-v2",
            )},
            {"commands": (MovementV1(-1, 0),) * len(sequence.commands)},
            {"primitive_ids": (terminal_search.GroundTerminalPrimitive.NEUTRAL,)},
            {"execution_window": CandidateExecutionWindow(
                sequence.execution_window.earliest_start_tick + 1,
                sequence.execution_window.latest_start_tick + 1,
            )},
            {"normal": replace(
                sequence.normal,
                entry_state=changed_entry,
                command_trajectory=(changed_entry,)
                    + sequence.normal.command_trajectory[1:],
            )},
        )
        for mutation in mutations:
            with self.subTest(mutation=tuple(mutation)):
                with self.assertRaisesRegex(Exception, "identity"):
                    replace(sequence, **mutation)

    def test_phase_missing_precedes_beam_lead_rejection(self):
        request = _request(
            bounds=Aabb(.45, 63.99, 1.10, .55, 64.01, 1.16),
            lead_ticks=10,
        )
        facts = {
            pos: request.frame.world.cell(pos)
            for x in range(-5, 6) for y in range(63, 68) for z in range(-5, 6)
            if (pos := (x, y, z)) != (0, 64, 1)
            and request.frame.world.cell(pos).knowledge.value != "unknown"
        }
        changed = replace(
            request,
            frame=replace(request.frame, world=request.frame.world.detached(
                request.frame.session,
                request.frame.world.geometry_revision,
                request.frame.world.evidence_revision,
                facts,
            )),
        )

        result = solve_ground_terminal_sequence(changed)

        self.assertIs(result.status, GroundTerminalSearchStatus.NEEDS_INFORMATION)
        self.assertIn((0, 64, 1), result.missing_cells)

    def test_template_order_cannot_publish_a_partial_cost_tier_at_budget_boundary(self):
        request = _request(
            bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74),
            limits=GroundTerminalSearchLimits(
                maximum_ticks=3, phase_candidate_budget=1,
                beam_node_budget=1,
            ),
        )
        normal = solve_ground_terminal_sequence(request)
        with patch.object(
            terminal_search, "_PHASE_TEMPLATES",
            tuple(reversed(terminal_search._PHASE_TEMPLATES)),
        ):
            reversed_result = solve_ground_terminal_sequence(request)
        for result in (normal, reversed_result):
            self.assertIs(result.status, GroundTerminalSearchStatus.BUDGET_EXHAUSTED)
            self.assertIsNone(result.sequence)

    def test_real_wall_beam_deduplicates_and_reaches_the_512_capacity(self):
        request = _request(
            bounds=Aabb(.87, 63.99, .87, .93, 64.01, .93),
            scene="wall_corner", lead_ticks=12,
            limits=GroundTerminalSearchLimits(
                maximum_ticks=4, phase_candidate_budget=1000,
                beam_node_budget=900,
            ),
        )

        result = solve_ground_terminal_sequence(request)

        self.assertIs(result.status, GroundTerminalSearchStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.stats.beam_maximum_width, 512)
        self.assertGreater(result.stats.beam_deduplicated, 0)
        self.assertEqual(result.stats.beam_generated, 900)

    def test_beam_reports_stable_predicted_state_deduplication(self):
        request = _request(
            bounds=Aabb(.17, 63.99, .32, .23, 64.01, .38),
            velocity=(-.15, -0.0784, -.15),
            lead_ticks=12,
            limits=GroundTerminalSearchLimits(
                maximum_ticks=3, phase_candidate_budget=1000,
                beam_node_budget=5000,
            ),
        )

        result = solve_ground_terminal_sequence(request)

        self.assertIs(result.status, GroundTerminalSearchStatus.SOLVED)
        self.assertEqual(result.stats.beam_nodes, result.stats.beam_generated)
        self.assertGreater(result.stats.beam_kept, 0)
        self.assertLessEqual(result.stats.beam_maximum_width, 512)

        with patch.object(
            terminal_search, "_beam_state_key", return_value=("same-state",),
        ):
            collapsed = solve_ground_terminal_sequence(request)
        self.assertGreater(collapsed.stats.beam_deduplicated, 0)
        self.assertLessEqual(collapsed.stats.beam_maximum_width, 1)
        self.assertEqual(
            collapsed.stats.beam_nodes, collapsed.stats.beam_generated,
        )

    def test_budget_exhaustion_is_not_reported_as_no_sequence(self):
        request = _request(
            bounds=Aabb(.45, 63.99, 1.2, .55, 64.01, 1.24),
            limits=GroundTerminalSearchLimits(
                maximum_ticks=6, neutral_tail_ticks=30,
                phase_candidate_budget=1, beam_node_budget=1,
            ),
        )

        result = solve_ground_terminal_sequence(request)

        self.assertIs(result.status, GroundTerminalSearchStatus.BUDGET_EXHAUSTED)
        self.assertIsNone(result.sequence)

    def test_phase_failure_does_not_run_beam_without_eleven_ticks_of_lead(self):
        request = _request(
            bounds=Aabb(.45, 63.99, 2.0, .55, 64.01, 2.05),
            lead_ticks=10,
        )

        result = solve_ground_terminal_sequence(request)

        self.assertIs(result.status, GroundTerminalSearchStatus.INSUFFICIENT_LEAD)
        self.assertEqual(result.stats.beam_nodes, 0)

    def test_beam_fallback_is_bounded_and_reverified(self):
        request = _request(
            bounds=Aabb(.17, 63.99, .32, .23, 64.01, .38),
            velocity=(-.15, -0.0784, -.15),
            lead_ticks=12,
            limits=GroundTerminalSearchLimits(
                maximum_ticks=3, neutral_tail_ticks=30,
                phase_candidate_budget=1000, beam_node_budget=5000,
            ),
        )

        result = solve_ground_terminal_sequence(request)

        self.assertIs(result.status, GroundTerminalSearchStatus.SOLVED)
        assert result.sequence is not None
        self.assertIs(result.sequence.layer, GroundTerminalSearchLayer.BEAM)
        self.assertLessEqual(result.stats.beam_maximum_width, 512)
        self.assertGreater(result.stats.beam_nodes, 0)
        self.assertTrue(all(
            tail.safe
            for branch in (result.sequence.normal, result.sequence.late1)
            for tail in branch.prefix_tails
        ))

    def test_missing_world_is_typed_and_never_published_as_solved(self):
        request = _request(bounds=Aabb(.45, 63.99, .68, .55, 64.01, .74))
        facts = {
            pos: request.frame.world.cell(pos)
            for x in range(-5, 6) for y in range(63, 68) for z in range(-5, 6)
            if (pos := (x, y, z)) != (0, 64, 0)
            and request.frame.world.cell(pos).knowledge.value != "unknown"
        }
        changed = replace(
            request,
            frame=replace(request.frame, world=request.frame.world.detached(
                request.frame.session,
                request.frame.world.geometry_revision,
                request.frame.world.evidence_revision,
                facts,
            )),
        )

        result = solve_ground_terminal_sequence(changed)

        self.assertIs(result.status, GroundTerminalSearchStatus.NEEDS_INFORMATION)
        self.assertIsNone(result.sequence)
        self.assertIn((0, 64, 0), result.missing_cells)

    def test_typed_stop_callback_preempts_search_without_publishing_candidate(self):
        request = _request(
            bounds=Aabb(.17, 63.99, .32, .23, 64.01, .38),
            velocity=(-.15, -0.0784, -.15), lead_ticks=12,
        )
        for status in (
            GroundTerminalSearchStatus.CANCELLED,
            GroundTerminalSearchStatus.STALE,
            GroundTerminalSearchStatus.TIMEOUT,
        ):
            with self.subTest(status=status):
                result = solve_ground_terminal_sequence(
                    request, stop_check=lambda status=status: status,
                )
                self.assertIs(result.status, status)
                self.assertIsNone(result.sequence)
                self.assertEqual(result.stats.beam_generated, 0)

    def test_cancellation_during_beam_stops_at_next_incremental_checkpoint(self):
        request = _request(
            bounds=Aabb(.87, 63.99, .87, .93, 64.01, .93),
            scene="wall_corner", lead_ticks=12,
            limits=GroundTerminalSearchLimits(
                maximum_ticks=4, phase_candidate_budget=1000,
                beam_node_budget=5000,
            ),
        )
        checks = 0

        def stop_check():
            nonlocal checks
            checks += 1
            return (GroundTerminalSearchStatus.CANCELLED
                    if checks >= 2_000 else None)

        result = solve_ground_terminal_sequence(
            request, stop_check=stop_check,
        )

        self.assertIs(result.status, GroundTerminalSearchStatus.CANCELLED)
        self.assertIsNone(result.sequence)
        self.assertGreater(result.stats.beam_generated, 0)
        self.assertLess(result.stats.beam_generated, 5000)

    def test_beam_incremental_expansion_defers_full_replay_to_completion_neighborhood(self):
        request = _request(
            bounds=Aabb(.17, 63.99, .32, .23, 64.01, .38),
            velocity=(-.15, -0.0784, -.15), lead_ticks=12,
            limits=GroundTerminalSearchLimits(
                maximum_ticks=3, phase_candidate_budget=1000,
                beam_node_budget=5000,
            ),
        )

        result = solve_ground_terminal_sequence(request)

        self.assertIs(result.status, GroundTerminalSearchStatus.SOLVED)
        self.assertGreater(result.stats.beam_generated, 0)
        self.assertGreater(result.stats.beam_physics_steps, 0)
        self.assertGreater(result.stats.replay_candidates, 0)
        self.assertLess(
            result.stats.replay_candidates, result.stats.beam_generated,
        )
        self.assertEqual(result.stats.canonical_proofs, 1)
        self.assertLessEqual(result.stats.beam_maximum_width, 512)


if __name__ == "__main__":
    unittest.main()
