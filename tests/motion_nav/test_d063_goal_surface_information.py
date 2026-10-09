"""D063 requests one complete bounded goal-surface fact set."""
from __future__ import annotations

import math
from pathlib import Path
import unittest
from dataclasses import replace
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.observation_v3 import AirQueryResultV3
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.world_model import Aabb
from scripts import f1_known_world_following_fabric as fabric_follow
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.motion_nav.test_navigation_session import (
    _InlinePlanner,
    _ground_anchor,
    _source,
)
from tests.sim.backend import CalculatorBackend, Perturbations
from tests.sim.runner import InlinePlannerWorker, TEST_ORACLE, lane
import tests.sim.known_world_following as following_sim


_FIRST_GOAL_MISSING = tuple(
    (x, -60, z)
    for x in range(-2, 3)
    for z in range(4, 9)
)
_SECOND_GOAL_MISSING = tuple(sorted({
    *((x, y, z)
      for x in (-3, 3)
      for y in (-60, -59)
      for z in range(3, 10)),
    *((x, -60, z)
      for x in range(-2, 3)
      for z in (3, 9)),
    *((x, -59, z)
      for x in range(-2, 3)
      for z in range(3, 10)),
}))
_COMPLETE_GOAL_MISSING = tuple(sorted(
    set(_FIRST_GOAL_MISSING) | set(_SECOND_GOAL_MISSING)
))


def _goal(*, center_z: float = 6.5) -> GoalState:
    half = 2.5 / math.sqrt(2.0)
    return GoalState(
        Aabb(
            .5 - half,
            -60.1,
            center_z - half,
            .5 + half,
            -59.9,
            center_z + half,
        ),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _wide_goal() -> GoalState:
    return GoalState(
        Aabb(-2.5, -60.1, 3.5, 3.5, -59.9, 9.5),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _initial_blocks():
    ground = tuple(
        observed_block((x, -61, z), "minecraft:grass_block")
        for x in range(-14, 15)
        for z in range(-5, 17)
    )
    body_contact = (
        observed_block(
            (0, -60, 0), "minecraft:air", kind="empty",
            sources=("body_contact",),
        ),
        observed_block(
            (0, -59, 0), "minecraft:air", kind="empty",
            sources=("body_contact",),
        ),
    )
    return ground + body_contact


def _snapshot(
    *, sequence: int = 1,
    results: tuple[AirQueryResultV3, ...] = (),
    additional_blocks=(),
):
    visible_air = tuple(
        observed_block(
            result.position,
            "minecraft:air",
            kind="empty",
            sources=("air_query",),
        )
        for result in results
        if result.status == "visible_air"
    )
    snapshot = follow_snapshot(
        sequence=sequence,
        received=100_000_000 + sequence * 50_000_000,
        position=(.5, -60.0, .5),
        blocks=_initial_blocks() + visible_air + tuple(additional_blocks),
        entities=(),
        episode="d063-goal-surface",
        self_changes={"movement_tick_id": sequence},
    )
    if not results:
        return snapshot
    perception = snapshot.perception.value
    assert perception is not None
    return replace(
        snapshot,
        perception=replace(
            snapshot.perception,
            value=replace(perception, air_query_results=results),
        ),
    )


def _visible_air_results(positions):
    return tuple(
        AirQueryResultV3(position, "visible_air", 6.0, True)
        for position in tuple(sorted(positions))
    )


def _fabric_target_positions():
    """Replay the frozen Fabric target input cadence through the 1.21 model."""
    scenario = fabric_follow.SCENARIO_BY_ID["straight_2_0"]
    clock = [100_000_000]
    scene = lane([[63]] * 72, width=25).with_floor()
    backend = CalculatorBackend(
        clock,
        scene,
        (.5, 64.0, 6.5),
        0.0,
        perturbations=Perturbations(),
    )
    positions = [backend.state.position]
    pattern = fabric_follow.TARGET_MOVEMENT_PATTERN
    for tick in range(scenario.move_ticks):
        backend.advance(MovementV1(forward=pattern[tick % len(pattern)]))
        positions.append(backend.state.position)
    return tuple(positions)


def _seed_known_support_only(runtime, scene):
    """Match D060-F: floor is known, target clearance still needs observation."""
    runtime.navigation_observation_adapter.seed_test_oracle_memory(
        TEST_ORACLE,
        {
            position: scene.geometry(block)
            for position, block in scene.solids.items()
        },
        (),
    )


class D063GoalSurfaceInformationTests(unittest.TestCase):
    def _waiting_session(self, goal: GoalState | None = None):
        planner = _InlinePlanner()
        session = NavigationSession(
            "d063-goal-surface",
            NavigationSessionProfiles.load(Path("config/motion-navigation")),
            planner_worker=planner,
            clock_ns=lambda: 100_000_000,
        )
        initial = session.ingest(_snapshot())
        session.bind_source(_source())
        session.start_goal(
            "d063-follow-goal",
            1,
            _goal() if goal is None else goal,
            initial,
        )
        return session, planner, initial

    def test_first_formal_goal_surface_request_contains_all_98_missing_cells(self):
        self.assertEqual(len(_FIRST_GOAL_MISSING), 25)
        self.assertEqual(len(_SECOND_GOAL_MISSING), 73)
        self.assertEqual(len(_COMPLETE_GOAL_MISSING), 98)
        session, planner, initial = self._waiting_session()
        try:
            proposal = session.propose(
                initial,
                _ground_anchor(initial),
                500_000_000,
                input_ledger=InputApplicationLedger(),
            )

            self.assertIs(
                proposal.report.state,
                NavigationSessionState.NEEDS_INFORMATION,
            )
            self.assertEqual(
                proposal.report.reason,
                "goal_surface_requires_information",
            )
            self.assertEqual(
                proposal.report.missing_cells,
                _COMPLETE_GOAL_MISSING,
            )
            self.assertEqual(
                proposal.control_frame.observation_request.air_positions,
                _COMPLETE_GOAL_MISSING,
            )
            self.assertEqual(len(set(proposal.report.missing_cells)), 98)
            self.assertEqual(planner.jobs, [])
            self.assertFalse(session.diagnostics.planning_work_owned)
            self.assertIsNone(session.active_route)
            self.assertTrue(all(
                item.intent.movement in (None, MovementV1())
                for item in proposal.control_frame.intents
            ))
        finally:
            session.close()

    def test_one_98_cell_reply_enters_planning_without_a_second_goal_round(self):
        session, planner, initial = self._waiting_session()
        ledger = InputApplicationLedger()
        try:
            first = session.propose(
                initial,
                _ground_anchor(initial),
                500_000_000,
                input_ledger=ledger,
            )
            self.assertEqual(
                first.control_frame.observation_request.air_positions,
                _COMPLETE_GOAL_MISSING,
            )

            answered = session.ingest(_snapshot(
                sequence=2,
                results=_visible_air_results(_COMPLETE_GOAL_MISSING),
            ))
            advanced = session.propose(
                answered,
                _ground_anchor(answered),
                500_000_000,
                input_ledger=ledger,
            )

            self.assertNotEqual(
                advanced.report.reason,
                "goal_surface_requires_information",
            )
            self.assertTrue(session.diagnostics.planning_work_owned)
            self.assertTrue(
                set(advanced.report.missing_cells).isdisjoint(
                    _COMPLETE_GOAL_MISSING,
                )
            )
            self.assertTrue(
                set(advanced.control_frame.observation_request.air_positions)
                .isdisjoint(_COMPLETE_GOAL_MISSING)
            )
            self.assertLessEqual(
                len(advanced.control_frame.observation_request.air_positions),
                5,
            )
            self.assertIsNone(session.active_route)
            self.assertTrue(all(
                item.intent.movement in (None, MovementV1())
                for item in advanced.control_frame.intents
            ))
            self.assertLessEqual(planner.polls, 2)
        finally:
            session.close()

    def test_large_goal_missing_is_kept_and_paged_at_128(self):
        session, planner, current = self._waiting_session(_wide_goal())
        ledger = InputApplicationLedger()
        requested = []
        expected = None
        try:
            for sequence in range(1, 8):
                node, remaining = session._surface_for_goal(current, _wide_goal())
                proposal = session.propose(
                    current,
                    _ground_anchor(current),
                    500_000_000,
                    input_ledger=ledger,
                )
                if node is not None and not remaining:
                    break
                self.assertIs(proposal.report.state, NavigationSessionState.NEEDS_INFORMATION)
                if expected is None:
                    expected = proposal.report.missing_cells
                page = proposal.control_frame.observation_request.air_positions
                self.assertTrue(page)
                self.assertLessEqual(len(page), 128)
                self.assertTrue(set(page).isdisjoint(requested))
                requested.extend(page)
                self.assertEqual(planner.jobs, [])
                self.assertFalse(session.diagnostics.planning_work_owned)
                # Revealing air everywhere would make the first page enough
                # for a safe subregion.  Instead all early candidates are
                # obstacles, with one clear corner completed on the last page.
                clear = tuple(cell for cell in page
                              if cell[0] in (3, 4) and cell[2] in (9, 10))
                current = session.ingest(_snapshot(
                    sequence=sequence + 1,
                    results=_visible_air_results(clear),
                    additional_blocks=tuple(observed_block(cell)
                                            for cell in page if cell not in clear),
                ))
            else:
                self.fail("bounded goal missing did not finish paging")

            self.assertGreater(len(requested), 128)
            self.assertEqual(len(requested), len(set(requested)))
            self.assertTrue(set(requested).issubset(expected))
            # Discarded blocked candidates no longer require their far halo.
            # The selected corner must nevertheless have no missing facts.
            self.assertIsNotNone(node)
            self.assertEqual(remaining, ())
            self.assertEqual((node.column_x, node.column_z), (3, 9))
            selected, missing = session._surface_for_goal(current, _wide_goal())
            self.assertEqual(selected, node)
            self.assertEqual(missing, ())
        finally:
            session.close()

    def test_residual_fact_keeps_priority_over_a_large_goal_page(self):
        session, planner, current = self._waiting_session(_wide_goal())
        urgent = (20, -60, 20)
        session._residual_missing = (urgent,)
        try:
            proposal = session.propose(
                current,
                _ground_anchor(current),
                500_000_000,
                input_ledger=InputApplicationLedger(),
            )
            requested = proposal.control_frame.observation_request.air_positions

            self.assertEqual(len(requested), 128)
            self.assertIn(urgent, requested)
            self.assertEqual(
                len(set(requested).intersection(
                    set(proposal.report.missing_cells) - {urgent}
                )),
                127,
            )
            self.assertEqual(planner.jobs, [])
            self.assertFalse(session.diagnostics.planning_work_owned)
        finally:
            session.close()

    def test_failed_air_queries_remain_unknown_and_retry_after_five_ticks(self):
        for status in ("occluded", "outside_view"):
            with self.subTest(status=status):
                session, planner, current = self._waiting_session()
                ledger = InputApplicationLedger()
                try:
                    first = session.propose(
                        current,
                        _ground_anchor(current),
                        500_000_000,
                        input_ledger=ledger,
                    )
                    positions = (
                        first.control_frame.observation_request.air_positions
                    )
                    self.assertEqual(positions, _COMPLETE_GOAL_MISSING)
                    for sequence in range(2, 7):
                        results = (
                            tuple(AirQueryResultV3(position, status)
                                  for position in positions)
                            if sequence == 2 else ()
                        )
                        current = session.ingest(_snapshot(
                            sequence=sequence,
                            results=results,
                        ))
                        proposal = session.propose(
                            current,
                            _ground_anchor(current),
                            500_000_000,
                            input_ledger=ledger,
                        )
                        requested = (
                            proposal.control_frame.observation_request
                            .air_positions
                        )
                        self.assertEqual(
                            requested,
                            positions if sequence == 6 else (),
                        )
                        self.assertIs(
                            proposal.report.state,
                            NavigationSessionState.NEEDS_INFORMATION,
                        )
                        self.assertEqual(planner.jobs, [])
                        self.assertTrue(all(
                            current.world.cell(position).knowledge.value
                            == "unknown"
                            for position in positions
                        ))
                finally:
                    session.close()

    def test_goal_revision_replaces_missing_and_old_reply_only_updates_world(self):
        session, planner, initial = self._waiting_session()
        ledger = InputApplicationLedger()
        try:
            old = session.propose(
                initial,
                _ground_anchor(initial),
                500_000_000,
                input_ledger=ledger,
            ).control_frame.observation_request.air_positions
            self.assertEqual(old, _COMPLETE_GOAL_MISSING)

            self.assertTrue(session.update_goal(
                "d063-follow-goal",
                2,
                _goal(center_z=13.5),
            ))
            revised = session.propose(
                initial,
                _ground_anchor(initial),
                500_000_000,
                input_ledger=ledger,
            )
            new = revised.report.missing_cells
            self.assertTrue(new)
            self.assertTrue(set(new).isdisjoint(old))
            self.assertEqual(revised.report.goal_revision, 2)

            late = session.ingest(_snapshot(
                sequence=2,
                results=_visible_air_results(old),
            ))
            after = session.propose(
                late,
                _ground_anchor(late),
                500_000_000,
                input_ledger=ledger,
            )
            self.assertEqual(after.report.goal_revision, 2)
            self.assertTrue(set(after.report.missing_cells).isdisjoint(old))
            self.assertTrue(set(new).issubset(after.report.missing_cells))
            self.assertEqual(planner.jobs, [])
            self.assertTrue(all(
                late.world.cell(position).knowledge.value == "air"
                for position in old
            ))
            self.assertTrue(all(
                late.world.cell(position).knowledge.value == "unknown"
                for position in new
            ))
        finally:
            session.close()

    def test_cancelled_goal_does_not_restart_from_a_late_reply(self):
        session, planner, initial = self._waiting_session()
        ledger = InputApplicationLedger()
        try:
            request = session.propose(
                initial,
                _ground_anchor(initial),
                500_000_000,
                input_ledger=ledger,
            ).control_frame.observation_request.air_positions
            session.cancel("d063-cancelled")
            self.assertTrue(session.report.terminal)

            late = session.ingest(_snapshot(
                sequence=2,
                results=_visible_air_results(request),
            ))
            terminal = session.propose(
                late,
                _ground_anchor(late),
                500_000_000,
                input_ledger=ledger,
            )
            self.assertTrue(terminal.report.terminal)
            self.assertEqual(
                terminal.control_frame.observation_request.air_positions,
                (),
            )
            self.assertEqual(planner.jobs, [])
            self.assertIsNone(session.active_route)
        finally:
            session.close()

    def test_formal_runtime_chain_matches_d060_cold_start_contract(self):
        target_positions = _fabric_target_positions()
        fabric_scenario = fabric_follow.SCENARIO_BY_ID["straight_2_0"]
        scenario = replace(
            following_sim.SCENARIO_BY_NAME["straight_2_0"],
            name="d063-partial-observation-runtime",
            move_ticks=fabric_scenario.move_ticks,
            final_hold_ticks=fabric_scenario.final_hold_ticks,
        )
        trace = following_sim._RecordingTrace()
        planners = []
        frames = []

        class RecordingPlanner(InlinePlannerWorker):
            def __init__(self):
                super().__init__()
                planners.append(self)

        original_propose = NavigationSession.propose

        def capture_proposal(session, *args, **kwargs):
            proposal = original_propose(session, *args, **kwargs)
            planner = planners[0]
            frames.append({
                "goal_revision": proposal.report.goal_revision,
                "reason": proposal.report.reason,
                "air_positions": (
                    proposal.control_frame.observation_request.air_positions
                ),
                "planning_submissions": sum(
                    item.operation == "submit" for item in planner.activity
                ),
            })
            return proposal

        def target_position(_scenario, tick):
            bounded = min(max(tick, 0), fabric_scenario.move_ticks)
            return target_positions[bounded]

        with patch.object(
            following_sim, "_TARGET_START", (.5, 64.0, 6.5),
        ), patch.object(
            following_sim, "INITIAL_TARGET_DISTANCE_BLOCKS", 6.0,
        ), patch.object(
            following_sim, "seed_memory", _seed_known_support_only,
        ), patch.object(
            following_sim, "InlinePlannerWorker", RecordingPlanner,
        ), patch.object(
            following_sim.FollowScenario, "target_position", target_position,
        ), patch.object(
            NavigationSession, "propose", capture_proposal,
        ):
            result = following_sim._run_scenario(scenario, trace)

        first_runtime_step = next(
            payload["report"].runtime_step
            for kind, payload in trace.records
            if kind == "step"
            and any((
                payload["decision"].action.movement.forward,
                payload["decision"].action.movement.strafe,
                payload["decision"].action.movement.jump,
                payload["decision"].action.movement.sneak,
                payload["decision"].action.movement.sprint,
            ))
        )
        first_scenario_tick = first_runtime_step - 1  # bootstrap control frame
        before_walk = frames[:first_scenario_tick]
        goal_surface_requests = tuple(
            frame for frame in before_walk
            if frame["reason"] == "goal_surface_requires_information"
            and frame["air_positions"]
        )
        graph_information_requests = tuple(
            frame for frame in before_walk
            if frame["reason"] == "no_known_route_requires_information"
            and frame["air_positions"]
        )

        self.assertLessEqual(first_scenario_tick, 11)
        self.assertEqual(len(goal_surface_requests), 1)
        self.assertEqual(goal_surface_requests[0]["goal_revision"], 1)
        self.assertEqual(
            len(goal_surface_requests[0]["air_positions"]),
            98,
        )
        self.assertLessEqual(len(graph_information_requests), 2)
        self.assertLessEqual(
            frames[first_scenario_tick - 1]["planning_submissions"],
            5,
        )
        self.assertEqual(
            result["distance_sampling_windows"]["stable"]["sample_count"],
            27,
        )
        self.assertLessEqual(result["planning_submissions_per_accepted_revision"], 1.25)
        self.assertLessEqual(result["revision_response_p95_ticks"], 5)


if __name__ == "__main__":
    unittest.main()
