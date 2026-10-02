"""Admission must reuse proved starts, not re-solve on the control thread."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.motion_nav.motion_candidate import MotionCandidateAdmitter
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.motion_solver import GapSolveRequest, SolveStatus
from mc2p.motion_nav.motion_worker import GapMotionSolveJob, MotionJobOperation, _execute_job
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav.test_b10_motion_candidate import solved_candidate
from tests.motion_nav.test_b10_gap_solver import fixture


def admit(candidate, anchor, **overrides):
    context = candidate.context
    values = dict(
        planning_request_id=context.planning_request_id,
        planning_generation=context.planning_generation,
        goal_id=context.goal_id, goal_revision=context.goal_revision,
        route_id=context.route_id, route_revision=context.route_revision,
        action_index=context.action_index,
        candidate_revision=context.candidate_revision,
        damage_budget=TaskDamageBudget(),
        intended_start_tick=anchor.movement_tick_id + 1,
        changed_cells=(),
    )
    values.update(overrides)
    return MotionCandidateAdmitter().admit(candidate, anchor, **values)


def neutral_ledger(tick):
    ledger = InputApplicationLedger()
    ledger.observe_sample(ClientInputApplicationV1(
        "mc2p.input-application.v1", tick, None, None, tick,
        "expired", 0.0, 0.0, False, False, False,
    ))
    return ledger


class ActionContinuityAdmissionTests(unittest.TestCase):
    def test_worker_revalidates_old_commands_without_searching_a_new_solution(self):
        anchor, candidate = solved_candidate()
        _, world, _, _ = fixture()
        later = replace(
            anchor, observation_sequence_id=anchor.observation_sequence_id + 3,
            movement_tick_id=anchor.movement_tick_id + 3,
            physics_state=replace(anchor.physics_state, movement_tick_id=anchor.movement_tick_id + 3),
        )
        self.assertEqual(admit(candidate, later).status.value, "needs_revalidation")
        request = GapSolveRequest(candidate.proof.direction, candidate.proof.landing,
                                  CandidateExecutionWindow(14, 15))
        job = GapMotionSolveJob("edge", 2, later, world, request,
                                operation=MotionJobOperation.REVALIDATE, proof=candidate.proof)
        with patch("mc2p.motion_nav.motion_worker.solve_one_cell_gap", side_effect=AssertionError("new search")):
            result = _execute_job(job)
        self.assertIs(result.solve_result.status, SolveStatus.SOLVED)
        refreshed = replace(candidate, proof=result.solve_result.proof)
        self.assertEqual(admit(refreshed, later).status.value, "accepted")
        self.assertEqual(refreshed.proof.commands, candidate.proof.commands)
        self.assertEqual(refreshed.proof.recovery_horizon_ticks, candidate.proof.recovery_horizon_ticks)

    def test_new_observation_same_proved_state_does_not_expire_start(self):
        anchor, candidate = solved_candidate()
        newer = replace(anchor, observation_sequence_id=anchor.observation_sequence_id + 1)
        result = admit(candidate, newer)
        self.assertEqual(result.status.value, "accepted")
        self.assertIs(result.candidate.proof, candidate.proof)

    def test_delayed_proved_variant_needs_exact_observed_prelude(self):
        anchor, candidate = solved_candidate()
        variant = candidate.proof.start_variant(12)
        later = replace(
            anchor, observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=variant.entry_state.movement_tick_id,
            physics_state=variant.entry_state,
        )
        result = admit(candidate, later, input_ledger=neutral_ledger(11))
        self.assertEqual(result.status.value, "accepted")
        self.assertIs(result.candidate.proof, candidate.proof)
        missing = admit(candidate, later)
        self.assertEqual(missing.status.value, "needs_revalidation")

    def test_changed_physics_is_never_a_proved_start(self):
        anchor, candidate = solved_candidate()
        for change in (
            {"gravity_attribute": .16}, {"body_width": .7},
            {"jumping_cooldown_ticks": 3}, {"sprinting": True},
            {"submerged_in_water": True}, {"movement_speed_attribute": .2},
        ):
            with self.subTest(change=change):
                changed = replace(anchor, physics_state=replace(anchor.physics_state, **change))
                self.assertNotEqual(admit(candidate, changed).status.value, "accepted")


if __name__ == "__main__":
    unittest.main()
