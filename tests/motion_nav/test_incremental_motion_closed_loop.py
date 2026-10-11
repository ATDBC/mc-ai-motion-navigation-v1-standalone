from dataclasses import replace
import math
import unittest


class IncrementalMotionClosedLoopContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from experiments.motion_navigation.trajectory_proto.measurement import _set_a

        cls.case = tuple(_set_a(4))[1]  # jump_gap_1:A3 has a real commitment boundary
        _, _, cls.request, cls.world, cls.inputs, _, _ = cls.case

    def test_state_match_uses_absolute_1e_minus_9_and_exact_discrete_fields(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import compare_states

        base = self.request.anchor_state
        inside = replace(base, position=(base.position[0] + 1e-9, *base.position[1:]))
        outside = replace(base, position=(base.position[0] + 1.0001e-9, *base.position[1:]))
        discrete = replace(base, on_ground=not base.on_ground)
        same_numeric_value = replace(base, pitch_radians=int(base.pitch_radians))
        self.assertTrue(compare_states(base, inside).matches)
        self.assertTrue(compare_states(base, same_numeric_value).matches)
        self.assertFalse(compare_states(base, outside).matches)
        self.assertEqual(compare_states(base, outside).field, "position[0]")
        self.assertFalse(compare_states(base, discrete).matches)
        self.assertEqual(compare_states(base, discrete).field, "on_ground")

    def test_ledger_requires_typed_submit_and_actual_application_inside_window(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import (
            ApplicationStatus,
            ExperimentalInputLedger,
        )

        ledger = ExperimentalInputLedger(self.request.world_session, "p3-ledger")
        command = self.inputs[0]
        submitted = ledger.submit(100, 10, (11, 12), command)
        self.assertIs(ledger.status(100, 10).status, ApplicationStatus.PENDING)
        ledger.record_applied(100, 12, command)
        applied = ledger.status(100, 12)
        self.assertIs(applied.status, ApplicationStatus.APPLIED)
        self.assertEqual(applied.submission, submitted)
        with self.assertRaisesRegex(Exception, "already has actual application"):
            ledger.record_applied(100, 12, command)
        with self.assertRaisesRegex(Exception, "allowed effect window"):
            other = ExperimentalInputLedger(self.request.world_session, "late")
            other.submit(1, 10, (11, 12), command)
            other.record_applied(1, 13, command)

    def test_ledger_reports_missing_only_after_window_closes(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import (
            ApplicationStatus,
            ExperimentalInputLedger,
        )

        ledger = ExperimentalInputLedger(self.request.world_session, "missing")
        ledger.submit(100, 10, (11, 12), self.inputs[0])
        self.assertIs(ledger.status(100, 12).status, ApplicationStatus.PENDING)
        # Tick 12 is still pending because this check runs before the client
        # application receipt for tick 12 is ingested.  Tick 13 closes it.
        ledger.close_application_tick(12)
        self.assertIs(ledger.status(100, 12).status, ApplicationStatus.MISSING)

    def test_backend_rejects_a_bare_input_without_owner_and_ledger_authority(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import (
            AdvanceSource,
            AuthorizedAdvance,
            advance_backend,
        )

        with self.assertRaisesRegex(Exception, "authorized advance"):
            advance_backend(self.request.anchor_state, self.inputs[0], self.world)
        forged = AuthorizedAdvance(
            "forged-owner", "forged-ledger", 100, 11, self.inputs[0],
            AdvanceSource.APPLIED_COMMAND, 0, self.request.request_id,
        )
        with self.assertRaisesRegex(Exception, "owner-issued"):
            advance_backend(self.request.anchor_state, forged, self.world)


class IncrementalMotionClosedLoopIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from experiments.motion_navigation.trajectory_proto.measurement import _set_a

        cls.case = tuple(_set_a(4))[1]
        _, _, cls.request, cls.world, cls.inputs, _, _ = cls.case

    def _owner(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import FrozenInputPlan, RollingProofOwner

        return RollingProofOwner.start(
            self.request,
            self.world,
            FrozenInputPlan(self.inputs),
            runahead_steps=24,
        )

    def test_exact_on_time_step_is_authorized_and_advances_boundary(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus, advance_backend

        owner = self._owner()
        first = owner.decide(self.request.anchor_state, self.world, self.request)
        self.assertIs(first.status, LoopStatus.ACTIVE)
        self.assertIsNotNone(first.submission)
        authority = owner.authorize_application(first.submission, 11)
        next_state = advance_backend(self.request.anchor_state, authority, self.world)
        second = owner.decide(next_state, self.world, self.request)
        self.assertEqual(second.consumed_commands, 1)
        self.assertEqual(second.reproof_count, 0)
        self.assertEqual(second.safety_violations, ())

    def test_late_first_application_waits_without_claiming_a_neutral_command(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus, advance_backend

        owner = self._owner()
        first = owner.decide(self.request.anchor_state, self.world, self.request)
        hold = owner.authorize_pending_hold(11)
        self.assertEqual(hold.source.value, "timing_branch")
        waiting = advance_backend(self.request.anchor_state, hold, self.world)
        pending = owner.decide(waiting, self.world, self.request)
        self.assertIs(pending.status, LoopStatus.WAITING_APPLICATION)
        self.assertIsNone(pending.submission)
        authority = owner.authorize_application(first.submission, 12)
        applied = advance_backend(waiting, authority, self.world)
        resumed = owner.decide(applied, self.world, self.request)
        self.assertIs(resumed.status, LoopStatus.ACTIVE)
        self.assertEqual(resumed.consumed_commands, 1)

    def test_unrelated_world_change_keeps_current_permit(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import mutate_world_fact

        owner = self._owner()
        changed = mutate_world_fact(self.world, (50, 50, 50), unknown=True)
        decision = owner.decide(self.request.anchor_state, changed, self.request)
        self.assertFalse(decision.reproved)
        self.assertIsNotNone(decision.submission)

    def test_goal_revision_invalidates_old_permit_and_reproofs_in_same_decision(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus

        owner = self._owner()
        revised = replace(self.request, goal_revision=self.request.goal_revision + 1)
        decision = owner.decide(self.request.anchor_state, self.world, revised)
        self.assertIs(decision.status, LoopStatus.ACTIVE)
        self.assertTrue(decision.reproved)
        self.assertEqual(decision.reproof_count, 1)
        self.assertIsNotNone(decision.submission)
        self.assertGreater(decision.complete_decision_ns, 0)

    def test_goal_revision_rejects_old_inputs_that_do_not_reach_the_new_goal(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus

        owner = self._owner()
        moved_goal = replace(
            self.request.goal,
            region=self.request.goal.region.moved(1., 0., 0.),
        )
        revised = replace(
            self.request, goal_revision=self.request.goal_revision + 1,
            goal=moved_goal,
        )
        decision = owner.decide(self.request.anchor_state, self.world, revised)
        self.assertIs(decision.status, LoopStatus.BOUNDED_FAILURE)
        self.assertEqual(decision.reason, "candidate_terminal_goal_mismatch")
        self.assertIsNone(decision.submission)

    def test_small_goal_revision_can_reprove_when_both_timing_branches_still_reach_it(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus

        owner = self._owner()
        moved_goal = replace(
            self.request.goal,
            region=self.request.goal.region.moved(.01, 0., 0.),
        )
        revised = replace(
            self.request, goal_revision=self.request.goal_revision + 1,
            goal=moved_goal,
        )
        decision = owner.decide(self.request.anchor_state, self.world, revised)
        self.assertIs(decision.status, LoopStatus.ACTIVE)
        self.assertTrue(decision.reproved)
        self.assertIsNotNone(decision.submission)

    def test_initial_observed_position_drift_never_uses_the_old_permit(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus

        owner = self._owner()
        state = self.request.anchor_state
        drifted = replace(state, position=(state.position[0] + .01, *state.position[1:]))
        decision = owner.decide(drifted, self.world, self.request)
        self.assertTrue(decision.reproved or decision.status is LoopStatus.BOUNDED_FAILURE)
        self.assertTrue(decision.old_suffix_invalidated is False)  # still before commitment
        if decision.submission is not None:
            self.assertTrue(decision.reproved)

    def test_missing_application_is_reanchored_or_fails_in_bounded_time(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus, advance_backend

        owner = self._owner()
        first = owner.decide(self.request.anchor_state, self.world, self.request)
        state = self.request.anchor_state
        for _ in range(2):
            hold = owner.authorize_pending_hold(state.movement_tick_id + 1)
            state = advance_backend(state, hold, self.world)
            decision = owner.decide(state, self.world, self.request)
        # The tick-12 observation is evaluated before its receipt is ingested,
        # so it may still be pending.  Closing that client tick makes the same
        # missing receipt final without inventing an extra backend input.
        owner.ledger.close_application_tick(state.movement_tick_id)
        decision = owner.decide(state, self.world, self.request)
        self.assertIn(decision.status, (LoopStatus.ACTIVE, LoopStatus.BOUNDED_FAILURE))
        self.assertNotEqual(decision.status, LoopStatus.WAITING_APPLICATION)
        self.assertTrue(decision.reproved or decision.status is LoopStatus.BOUNDED_FAILURE)

    def test_unknown_dependency_never_becomes_air_and_fails_bounded(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus, mutate_world_fact

        owner = self._owner()
        dependency = owner.current_permit.dependency_facts[0][0]
        changed = mutate_world_fact(self.world, dependency, unknown=True)
        decision = owner.decide(self.request.anchor_state, changed, self.request)
        self.assertIs(decision.status, LoopStatus.BOUNDED_FAILURE)
        self.assertEqual(decision.reason, "unknown_world")
        self.assertIsNone(decision.submission)

    def test_post_commit_deviation_invalidates_suffix_but_keeps_owner(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import (
            ActionPhase,
            LoopStatus,
            advance_backend,
        )

        owner = self._owner()
        # Run to the real commitment command, then perturb the first post-commit observation.
        state = self.request.anchor_state
        request = self.request
        entered = False
        for _ in range(12):
            decision = owner.decide(state, self.world, request)
            self.assertIsNotNone(decision.submission)
            authority = owner.authorize_application(
                decision.submission, state.movement_tick_id + 1)
            state = advance_backend(state, authority, self.world)
            if decision.phase is ActionPhase.COMMIT_BOUNDARY:
                entered = True
                break
        self.assertTrue(entered)
        perturbed = replace(state, position=(state.position[0] + .01, *state.position[1:]))
        after = owner.decide(perturbed, self.world, request)
        self.assertTrue(after.old_suffix_invalidated)
        self.assertTrue(after.owner_retained)
        self.assertIn(after.status, (
            LoopStatus.ACTIVE, LoopStatus.BODY_RESPONSIBILITY_UNRESOLVED))
        if after.status is LoopStatus.ACTIVE:
            self.assertTrue(after.reproved)
            self.assertIsNotNone(after.submission)
        else:
            self.assertIsNone(after.submission)
            self.assertEqual(after.safety_violations,
                             ("body_responsibility_unresolved",))

    def test_applied_commit_command_makes_next_observation_post_commit(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import ActionPhase, advance_backend

        owner = self._owner()
        state = self.request.anchor_state
        for _ in range(12):
            decision = owner.decide(state, self.world, self.request)
            authority = owner.authorize_application(
                decision.submission, state.movement_tick_id + 1)
            state = advance_backend(state, authority, self.world)
            if decision.phase is ActionPhase.COMMIT_BOUNDARY:
                following = owner.decide(state, self.world, self.request)
                self.assertIs(following.phase, ActionPhase.POST_COMMIT)
                break
        else:
            self.fail("commitment command was never issued")

    def test_fourth_consecutive_reproof_without_progress_is_bounded(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import LoopStatus

        owner = self._owner()
        request = self.request
        state = request.anchor_state
        for revision in range(2, 5):
            request = replace(request, goal_revision=revision)
            decision = owner.decide(state, self.world, request)
            self.assertIs(decision.status, LoopStatus.ACTIVE)
            self.assertTrue(decision.reproved)
        request = replace(request, goal_revision=5)
        exhausted = owner.decide(state, self.world, request)
        self.assertIs(exhausted.status, LoopStatus.BOUNDED_FAILURE)
        self.assertEqual(exhausted.reason, "reproof_limit")
        self.assertEqual(exhausted.reproof_count, 3)

    def test_cancel_after_commit_keeps_owner_until_safe_bounded_result(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import ActionPhase, advance_backend

        owner = self._owner()
        state = self.request.anchor_state
        for _ in range(12):
            decision = owner.decide(state, self.world, self.request)
            authority = owner.authorize_application(
                decision.submission, state.movement_tick_id + 1)
            state = advance_backend(state, authority, self.world)
            if decision.phase is ActionPhase.COMMIT_BOUNDARY:
                break
        cancelled = owner.decide(state, self.world, self.request, cancel=True)
        self.assertTrue(cancelled.owner_retained)
        self.assertTrue(cancelled.old_suffix_invalidated)
        if cancelled.submission is not None:
            self.assertEqual(cancelled.status.value, "stopping")
            self.assertTrue(cancelled.reproved)
        else:
            self.assertEqual(cancelled.status.value, "body_responsibility_unresolved")

    def test_cancel_persists_until_body_responsibility_is_declared_unresolved(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop import ActionPhase, LoopStatus, advance_backend

        owner = self._owner()
        state = self.request.anchor_state
        for _ in range(12):
            decision = owner.decide(state, self.world, self.request)
            authority = owner.authorize_application(
                decision.submission, state.movement_tick_id + 1)
            state = advance_backend(state, authority, self.world)
            if decision.phase is ActionPhase.COMMIT_BOUNDARY:
                cancelled = owner.decide(state, self.world, self.request, cancel=True)
                self.assertIs(
                    cancelled.status, LoopStatus.BODY_RESPONSIBILITY_UNRESOLVED)
                self.assertTrue(owner.cancel_requested)
                self.assertTrue(cancelled.owner_retained)
                self.assertEqual(cancelled.safety_violations,
                                 ("body_responsibility_unresolved",))
                break
        else:
            self.fail("commitment boundary was never reached")


if __name__ == "__main__":
    unittest.main()
