"""S0 formal-path matrix and intentional invariant failures."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import time
import unittest

from mc2p.contracts.action_v1 import ActionSnapshotV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode, ResourceState
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.body_control import (
    BodyControlActivity, BodyControlPhase, BodyControlProgress,
    HandoffDisposition, HandoffEvidence,
)
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.navigation_handoff import StopCause
from mc2p.motion_nav.navigation_lifecycle import NavigationSessionState
from mc2p.motion_nav.navigation_session import PendingPlanningRecovery
from mc2p.motion_nav.motion_risk import (
    RiskCommitEvidence, RiskCommitKind, RiskReservationStatus,
    TaskDamageBudget, TaskRiskLedger,
)
from mc2p.motion_nav.retry_ledger import (
    RecoveryBudgetKind, RecoveryLimitStatus, RetryCause, WaitVerdict,
)
from mc2p.motion_nav.world_model import WorldSessionId
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter, TEST_ORACLE
from mc2p.motion_nav.world_model import Aabb
from tests.sim.backend import CalculatorBackend, Perturbations, Scene
from tests.sim.monitor import InvariantMonitor, TickEvidence
from tests.sim.runner import Event, Scenario, _goal, lane, late_ticks, run
from tests.sim.scenarios import SCENARIOS, columns
from tests.sim.scenarios import airborne_in_drop, revise_goal_back
from tests.sim.run_navigation_matrix import read_manifest, run_matrix


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/sim/manifests/navigation-coordination-smoke.json"


def evidence(**changes) -> TickEvidence:
    goal = GoalState(Aabb(.3, 63.92, .3, .7, 64.08, .7), GoalSupport.SOLID,
                     frozenset({MovementMode.WALK}), frozenset({"standing"}), .6)
    base = TickEvidence(
        tick=2, now_ns=150_000_000, position=(.5, 64.0, .5), on_ground=True,
        controller_ids=("route_executor",), source_bound=True, state="executing",
        reason="tracking", damage=0.0, damage_limit=0.0, committed_damage=0.0,
        request_generation=1, goal_revision=1, wait_frames=0,
        applied_request=None, applied_movement=MovementV1(), issued_commands=(),
        proposed_movement_intents=(), selected_intents=(), action_movement=None,
        goal_position=(.5, 64.0, .5), goal_state=goal,
        velocity=(0.0, 0.0, 0.0), pose="standing", yaw_radians=0.0,
        risk_policy_id="no_expected_damage",
    )
    return replace(base, **changes)


class InvariantNegativeTests(unittest.TestCase):
    def check_code(self, code: str, *samples: TickEvidence) -> None:
        monitor = InvariantMonitor()
        for sample in samples:
            monitor.check(sample)
        self.assertIn(code, {item[1] for item in monitor.violations})

    def test_i1_owner_lost_in_air(self):
        self.check_code("I1", evidence(on_ground=False, controller_ids=()))

    def test_i1_i6_transfer_requires_selected_successor_and_sequence(self):
        handoff = HandoffEvidence(
            "route/old", WorldSessionId("test-world"),
            HandoffDisposition.TRANSFERABLE, 1, None,
            MovementV1(forward=1), "selected_successor_route_command",
            "route/new", 9, 1, 0,
        )
        self.check_code(
            "I1", evidence(
                observation_sequence=2, route_id="route/new",
                submitted_request=10, handoff=handoff,
                proposed_movement_intents=("nav-candidate",),
                selected_intents=(("movement", "other-intent"),),
                action_movement=MovementV1(forward=1),
            ),
        )
        self.check_code(
            "I6", evidence(
                observation_sequence=2, route_id="route/new",
                submitted_request=10, handoff=handoff,
                proposed_movement_intents=("nav-candidate",),
                selected_intents=(("movement", "other-intent"),),
                action_movement=MovementV1(forward=1),
            ),
        )

    def test_i2_unowned_fall_after_release(self):
        self.check_code("I2", evidence(),
                        evidence(tick=3, position=(.5, 63.5, .5), on_ground=False,
                                 controller_ids=(), source_bound=False),
                        evidence(tick=4, position=(.5, 62.0, .5)))

    def test_i3_damage_exceeds_authority(self):
        self.check_code("I3", evidence(damage=1.0))

    def test_i3_policy_cut_keeps_prior_commit_and_allows_zero_risk(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("old", 2, policy_revision=0)
        ledger.commit("old", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=2,
        ))
        ledger.update_policy(TaskDamageBudget("zero", 0), revision=1)
        ledger.reserve("zero", 0, policy_revision=1)
        ledger.commit("zero", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=3,
        ))
        monitor = InvariantMonitor()
        monitor.check(evidence(damage=2, damage_limit=0,
                               risk_actions=ledger.snapshot_actions()))
        self.assertNotIn("I3", {item[1] for item in monitor.violations})
        invalid = replace(ledger.action("zero"),
                          expected_damage_points=1)
        self.check_code("I3", evidence(
            damage=2, damage_limit=0,
            risk_actions=(ledger.action("old"), invalid),
        ))

    def test_i4_wait_has_no_bound(self):
        self.check_code("I4", evidence(wait_frames=41, state="needs_information"))

    def test_i4_explicit_unresolved_recovery_is_not_an_authorized_wait(self):
        monitor = InvariantMonitor()
        for tick in range(2, 140):
            monitor.check(evidence(
                tick=tick,
                state="stopping",
                reason="recovery_unresolved",
            ))
        self.assertIn("I4", {item[1] for item in monitor.violations})

    def test_i4_detects_motion_without_body_control_progress(self):
        monitor = InvariantMonitor()
        progress = BodyControlProgress(
            "route/test-route", "recovering_grounded_verified_entry",
            3, 1, 20, 40,
        )
        for tick in range(2, 45):
            offset = .04 if tick % 2 else -.04
            monitor.check(evidence(
                tick=tick,
                position=(.5 + offset, 64.0, .5 - offset),
                body_control_progress=progress,
            ))
        self.assertIn("I4", {item[1] for item in monitor.violations})

    def test_i4_keep_active_satisfied_idle_is_not_waiting(self):
        monitor = InvariantMonitor()
        for tick in range(2, 802):
            monitor.check(evidence(
                tick=tick,
                controller_ids=(),
                reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
                observed_goal_status=ObservedGoalStatus.SATISFIED,
            ))

        self.assertNotIn("I4", {item[1] for item in monitor.violations})

    def test_i4_keep_active_restarts_when_goal_becomes_unmet(self):
        monitor = InvariantMonitor()
        for tick in range(2, 202):
            monitor.check(evidence(
                tick=tick,
                controller_ids=(),
                reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
                observed_goal_status=ObservedGoalStatus.SATISFIED,
            ))
        for tick in range(202, 301):
            monitor.check(evidence(
                tick=tick,
                controller_ids=(),
                reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
                observed_goal_status=ObservedGoalStatus.NOT_SATISFIED,
            ))
        self.assertNotIn("I4", {item[1] for item in monitor.violations})

        monitor.check(evidence(
            tick=301,
            controller_ids=(),
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            observed_goal_status=ObservedGoalStatus.NOT_SATISFIED,
        ))
        self.assertIn("I4", {item[1] for item in monitor.violations})

    def test_i4_keep_active_satisfied_does_not_exempt_active_recovery(self):
        monitor = InvariantMonitor()
        for tick in range(2, 103):
            monitor.check(evidence(
                tick=tick,
                controller_ids=(),
                reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
                observed_goal_status=ObservedGoalStatus.SATISFIED,
                active_waits=(("recovery", "navigation-session/test"),),
                recovery_wait_status=WaitVerdict.WAITING,
            ))

        self.assertIn("I4", {item[1] for item in monitor.violations})

    def test_i4_keep_active_satisfied_only_exempts_executing_session(self):
        for session_state in (
            NavigationSessionState.STOPPING,
            NavigationSessionState.PLANNING,
            NavigationSessionState.NEEDS_INFORMATION,
        ):
            with self.subTest(session_state=session_state):
                monitor = InvariantMonitor()
                for tick in range(2, 103):
                    monitor.check(evidence(
                        tick=tick,
                        controller_ids=(),
                        reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
                        observed_goal_status=ObservedGoalStatus.SATISFIED,
                        session_state=session_state,
                    ))

                self.assertIn("I4", {item[1] for item in monitor.violations})

    def test_i4_keep_active_satisfied_does_not_exempt_strict_or_stopping_body(self):
        for phase in (BodyControlPhase.STRICT_EXECUTION, BodyControlPhase.STOPPING):
            with self.subTest(phase=phase):
                monitor = InvariantMonitor()
                activity = BodyControlActivity(
                    WorldSessionId("test-world"), 2, "route/test", "route", 1, 0,
                    phase,
                )
                for tick in range(2, 103):
                    monitor.check(evidence(
                        tick=tick,
                        reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
                        observed_goal_status=ObservedGoalStatus.SATISFIED,
                        body_control_activities=(activity,),
                    ))

                self.assertIn("I4", {item[1] for item in monitor.violations})

    def test_i10_terminal_source_release_has_a_deadline(self):
        monitor = InvariantMonitor()
        for tick in range(2, 24):
            monitor.check(evidence(
                tick=tick,
                state="failed",
                reason="bounded_failure",
                source_bound=True,
            ))
        self.assertIn("I10", {item[1] for item in monitor.violations})

    def test_i11_source_release_requires_stable_support(self):
        monitor = InvariantMonitor()
        monitor.check(evidence(tick=2, source_bound=True))
        monitor.check(evidence(
            tick=3,
            source_bound=False,
            controller_ids=(),
            support_fraction=0.0,
        ))
        self.assertIn("I11", {item[1] for item in monitor.violations})

    def test_i12_active_risk_records_have_a_capacity(self):
        ledger = TaskRiskLedger("capacity", TaskDamageBudget("many", 100))
        reservation = ledger.reserve("one", 0, policy_revision=0)
        self.assertIsNotNone(reservation.record)
        record = reservation.record
        assert record is not None
        self.check_code("I12", evidence(risk_actions=(record,) * 65))

    def test_i13_illegal_lifecycle_transition_is_observable(self):
        self.check_code("I13", evidence(illegal_transition_count=1))

    def test_i5_distinct_failures_without_progress(self):
        samples = [evidence(tick=i + 2, retry_attempt_id=f"attempt-{i}")
                   for i in range(7)]
        self.check_code("I5", *samples)

    def test_i5_finite_recoveries_ignore_diagnostic_cause_counts(self):
        monitor = InvariantMonitor()
        monitor.check(evidence(
            recovery_budget_kind=RecoveryBudgetKind.FINITE,
            recovery_maximum_starts=12,
            recovery_window_starts=3,
            recovery_total_starts=3,
            recovery_limit_status=RecoveryLimitStatus.ALLOWED,
            retry_cause_counts=(("execution", 3),),
        ))

        self.assertNotIn("I5", {item[1] for item in monitor.violations})

    def test_i5_persistent_total_can_exceed_twelve_outside_window(self):
        monitor = InvariantMonitor()
        monitor.check(evidence(
            recovery_budget_kind=RecoveryBudgetKind.PERSISTENT,
            recovery_maximum_starts=12,
            recovery_window_starts=1,
            recovery_total_starts=13,
            recovery_limit_status=RecoveryLimitStatus.ALLOWED,
            retry_cause_counts=(("execution", 13),),
        ))

        self.assertNotIn("I5", {item[1] for item in monitor.violations})

    def test_i5_persistent_thirteenth_in_window_must_be_typed_exhausted(self):
        monitor = InvariantMonitor()
        monitor.check(evidence(
            recovery_budget_kind=RecoveryBudgetKind.PERSISTENT,
            recovery_maximum_starts=12,
            recovery_window_starts=12,
            recovery_total_starts=12,
            recovery_limit_status=RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED,
            recovery_limit_window_starts=12,
        ))
        self.assertNotIn("I5", {item[1] for item in monitor.violations})

        invalid = InvariantMonitor()
        invalid.check(evidence(
            recovery_budget_kind=RecoveryBudgetKind.PERSISTENT,
            recovery_maximum_starts=12,
            recovery_window_starts=13,
            recovery_total_starts=13,
            recovery_limit_status=RecoveryLimitStatus.ALLOWED,
        ))
        self.assertIn("I5", {item[1] for item in invalid.violations})

    def test_i5_rate_exhaustion_uses_decision_count_after_window_slides(self):
        monitor = InvariantMonitor()
        monitor.check(evidence(
            recovery_budget_kind=RecoveryBudgetKind.PERSISTENT,
            recovery_maximum_starts=12,
            recovery_window_starts=0,
            recovery_total_starts=12,
            recovery_limit_status=RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED,
            recovery_limit_window_starts=12,
        ))

        self.assertNotIn("I5", {item[1] for item in monitor.violations})

    def test_i6_applied_identity_must_match_issued_command(self):
        self.check_code("I6", evidence(applied_request=9,
                         applied_movement=MovementV1(forward=1),
                         issued_commands=((9, MovementV1(strafe=1), 2, 500_000_000),)))
        self.check_code("I6", evidence(
            proposed_movement_intents=("navigation-intent",),
            selected_intents=(), action_movement=MovementV1(forward=1)))

    def test_i7_proof_must_be_current(self):
        self.check_code("I7", evidence(entry_proof_valid=False))

    def test_i8_no_route_requires_exhaustive_evidence(self):
        self.check_code("I8", evidence(planning_failure_kind="no_route",
                                       search_exhaustive=False))

    def test_i9_complete_requires_full_goal_state(self):
        self.check_code("I9", evidence(state="complete", position=(1.1, 64.0, .5)))
        self.check_code("I9", evidence(state="complete", velocity=(.1, 0.0, 0.0)))
        self.check_code("I9", evidence(state="complete", pose="crouching"))

    def test_i14_terminal_session_cannot_keep_active_waits(self):
        self.check_code(
            "I14",
            evidence(
                state="failed",
                controller_ids=(),
                source_bound=False,
                active_waits=(("information", "navigation-session/old/information"),),
            ),
        )

    def test_i15_planning_state_requires_owned_work(self):
        self.check_code(
            "I15",
            evidence(
                state="planning",
                reason="route_dependencies_changed",
                planning_work_owned=False,
            ),
        )

    def test_i15_planning_work_requires_matching_identity_and_deadline(self):
        self.check_code(
            "I15",
            evidence(
                state="planning",
                planning_work_owned=True,
                planning_work_identity_valid=False,
            ),
        )

    def test_i16_attempt_requires_matching_one_shot_permit(self):
        self.check_code(
            "I16",
            evidence(
                state="planning",
                planning_permit_identity_valid=False,
            ),
        )

    def test_i17_information_need_requires_frontier_identity(self):
        self.check_code(
            "I17",
            evidence(
                state="needs_information",
                planning_information_identity_valid=False,
            ),
        )

    def test_goal_monitor_rejects_unsupported_resource_contract(self):
        goal = replace(evidence().goal_state,
                       minimum_resources=ResourceState((("blocks", 1.0),)))
        with self.assertRaises(ValueError):
            InvariantMonitor().check(evidence(goal_state=goal))


class ClosedLoopToolTests(unittest.TestCase):
    def test_executing_recovery_exposes_typed_monitor_activity(self):
        scenario = replace(
            next(item for item in SCENARIOS if item.name == "flat_walk"),
            name="executing_recovery_monitor_activity",
            perturbations=Perturbations(),
            max_ticks=180,
        )
        injected: list[int] = []
        observed = []

        def step(context):
            session = context.session
            if (not injected and session.active_route is not None
                    and session.has_owned_body_control):
                session._pending_planning_recovery = PendingPlanningRecovery(
                    "active_route_dependency_changed",
                    RetryCause.DEPENDENCY,
                )
                session._supervisor.route.request_stop(
                    StopCause.DEPENDENCY_CHANGED,
                )
                injected.append(context.tick)
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )
            diagnostics = session.diagnostics
            if (session._retry_ledger.active_recovery_id is not None
                    and diagnostics.state is NavigationSessionState.EXECUTING):
                observed.append(diagnostics)

        run(
            scenario,
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
        )

        self.assertTrue(injected)
        self.assertTrue(observed)
        self.assertTrue(all(
            item.active_waits
            or item.recovery_wait_status is not None
            or item.body_control_activities
            for item in observed
        ))

    def test_keep_active_satisfied_idle_does_not_trigger_i4(self):
        scenario = replace(
            next(item for item in SCENARIOS if item.name == "flat_walk"),
            name="keep_active_satisfied_idle",
            perturbations=Perturbations(),
            max_ticks=900,
        )

        result = run(
            scenario,
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
        )

        satisfied_idle = [
            row for row in result.trace
            if row["goal_satisfied"] and not row["body_control_activities"]
        ]
        self.assertEqual(result.outcome, "executing", result.reason)
        self.assertGreaterEqual(len(satisfied_idle), 800)
        self.assertNotIn("I4", {item[1] for item in result.violations})

    def test_initial_live_observation_precedes_height_route_planning(self):
        scenario = next(
            item for item in SCENARIOS
            if item.name == "half_steps_up_down"
        )

        result = run(scenario)

        self.assertEqual(result.verdict, "PASS", result.reason)
        self.assertEqual(result.trace[0]["recovery_total_starts"], 0)
        self.assertIsNotNone(result.trace[0]["route_id"])

    def test_constant_late_input_has_a_bounded_typed_result(self):
        configured = Scenario(
            "down1_after_approach_constant_late",
            lane(columns([65] * 3 + [64] * 3), width=3),
            (.5, 65.0, .5),
            (.5, 64.0, 4.5),
            max_ticks=120,
            perturbations=Perturbations(
                late_ticks=frozenset(range(2, 800)),
            ),
        )

        result = run(configured)

        self.assertIn(result.outcome, {"success", "failed", "cancelled"})
        self.assertLess(result.ticks, configured.max_ticks)

    def test_exhausted_recovery_moves_back_onto_support_and_terminates(self):
        scenario = next(item for item in SCENARIOS
                        if item.name == "direct_drop_2")
        configured = replace(
            scenario,
            perturbations=replace(
                scenario.perturbations,
                late_ticks=late_ticks(.2, 280067),
            ),
        )

        result = run(configured)

        self.assertNotEqual(result.outcome, "stopping")
        self.assertNotIn("I4", {item[1] for item in result.violations})
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(result.trace[-1]["on_ground"])

    def test_probe_release_stage_keeps_its_acquisition_deadline(self):
        configured = replace(
            next(item for item in SCENARIOS if item.name == "direct_drop_2"),
            name="direct_drop_probe_release_deadline",
            perturbations=replace(
                next(item for item in SCENARIOS if item.name == "direct_drop_2").perturbations,
                late_ticks=late_ticks(.2, 280001),
            ),
            max_ticks=120,
        )

        result = run(configured)

        self.assertNotEqual(
            (result.outcome, result.reason),
            ("needs_information", "landing_lower_evidence_required"),
        )
        self.assertNotIn("I4", {item[1] for item in result.violations})

    def test_sneak_samples_float_axes_and_moves_slower_than_walk(self):
        scene = Scene({(x, 63, z): "minecraft:stone"
                       for x in range(-1, 2) for z in range(5)},
                      ((-2, 2), (60, 68), (-2, 6))).with_floor()
        velocities = []
        for sneak, expected in ((False, 1.0), (True, .3)):
            backend = CalculatorBackend([100_000_000], scene, (.5, 64.0, .5))
            action = ActionSnapshotV1(
                "episode-sim", 1, 0, 600_000_000,
                movement=MovementV1(forward=1, sneak=sneak),
                valid_for_ticks=1,
            )
            result = backend.step(action, 600_000_000)
            self.assertEqual(backend.applied[-1].forward, 1)
            self.assertAlmostEqual(result.receipt.input_applications[0].forward,
                                   expected)
            self.assertAlmostEqual(backend.sampled_inputs[-1].forward, expected)
            velocities.append(backend.state.velocity_blocks_per_tick[2])
        self.assertLess(velocities[1], velocities[0])

    def test_exhausted_lease_records_neutral_samples_in_ledger(self):
        scene = Scene({(0, 63, z): "minecraft:stone" for z in range(5)},
                      ((-2, 2), (60, 68), (-2, 6))).with_floor()
        backend = CalculatorBackend([100_000_000], scene, (.5, 64.0, .5))
        first = ActionSnapshotV1(
            "episode-sim", 1, 0, 600_000_000,
            movement=MovementV1(forward=1), valid_for_ticks=1,
        )
        ledger = InputApplicationLedger()
        ledger.submit(WorldSessionId("sim-truth"), first, requested_first_tick=2)
        first_receipt = backend.step(first, 600_000_000).receipt
        ledger.observe_receipt(first_receipt)
        backend.free_tick()
        backend.free_tick()
        second = ActionSnapshotV1(
            "episode-sim", 2, 0, 900_000_000,
            movement=MovementV1(), valid_for_ticks=1,
        )
        ledger.submit(WorldSessionId("sim-truth"), second, requested_first_tick=5)
        second_receipt = backend.step(second, 900_000_000).receipt
        ledger.observe_receipt(second_receipt)
        self.assertEqual(ledger.sample(3).state, "lease_exhausted")
        self.assertEqual(ledger.sample(4).state, "lease_exhausted")
        self.assertEqual(ledger.sample(3).request_sequence_id, 1)
        self.assertEqual(ledger.sample(3).forward, 0.0)
        self.assertIsNone(backend.applied_commands[1][1])

    def test_same_support_different_position_uses_local_walk(self):
        scenario = replace(SCENARIOS[2], name="same_support_position_offset",
                           start=(.15, 64.0, .5), goal=(.6, 64.0, .5))
        result = run(scenario)
        self.assertEqual(result.verdict, "PASS", result.reason)
        self.assertTrue(any(row["action_kind"] == "WalkSegment"
                            for row in result.trace))

    def test_selected_drop_reserves_then_commits_only_after_application(self):
        scenario = replace(
            SCENARIOS[7], name="near_edge_risk_receipt",
            start=(.5, 64.0, 2.5),
        )
        result = run(scenario)
        self.assertEqual(result.verdict, "PASS", result.reason)
        records = [row["risk_actions"][0] for row in result.trace
                   if row["risk_actions"]]
        self.assertEqual({record["action_id"] for record in records},
                         {records[0]["action_id"]})
        self.assertEqual(records[0]["state"], "reserved")
        self.assertTrue(records[0]["submitted_sequences"])
        self.assertIsNone(records[0]["commit_kind"])
        self.assertTrue(any(record["state"] == "committed"
                            and record["commit_kind"] == "applied_command"
                            for record in records))
        self.assertEqual(result.damage, 2.0)

    def test_grounded_drop_handles_one_late_active_command_locally(self):
        base = next(
            scenario for scenario in SCENARIOS
            if scenario.name == "direct_drop_2_20pct_late"
        )
        scenario = replace(
            base,
            name="direct_drop_grounded_active_command_late_once",
            perturbations=replace(
                base.perturbations,
                late_ticks=frozenset({37}),
            ),
        )

        result = run(scenario)

        self.assertEqual(result.violations, [])
        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        delayed = next(row for row in result.trace if row["tick"] == 37)
        delayed_request = delayed["submitted_request"]
        delivered = next(
            row for row in result.trace
            if row["tick"] > delayed["tick"]
            and delayed_request in row["command_event"]["ready"]
        )
        self.assertIsNone(delayed["applied_request"])
        self.assertGreater(delivered["tick"], delayed["tick"])
        self.assertTrue(delayed["on_ground"])
        self.assertTrue(delivered["on_ground"])
        self.assertTrue(any(
            row["session_reason"] in {
                "resubmit_verified_command_within_window",
                "repreparing_grounded_verified_motion",
            }
            for row in result.trace
            if row["tick"] >= delayed["tick"]
        ))
        self.assertEqual(
            max(row["recovery_total_starts"] for row in result.trace), 0,
            "grounded-entry wait is local and must not buy task recovery",
        )
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_late_neutral_confirmation_after_landing_completes_drop(self):
        base = next(
            scenario for scenario in SCENARIOS
            if scenario.name == "direct_drop_2"
        )
        scenario = replace(
            base,
            name="direct_drop_late_neutral_after_landing",
            perturbations=replace(
                base.perturbations,
                late_ticks=late_ticks(.2, 280033),
            ),
        )

        result = run(scenario)

        self.assertEqual(result.violations, [])
        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        self.assertFalse(any(
            row["session_reason"]
                == "repreparing_grounded_verified_motion"
            for row in result.trace
        ))

    def test_moving_off_edge_after_input_loss_keeps_landing_owner(self):
        base = next(
            scenario for scenario in SCENARIOS
            if scenario.name == "direct_drop_2"
        )
        scenario = replace(
            base,
            name="direct_drop_input_loss_at_departure",
            perturbations=replace(
                base.perturbations,
                late_ticks=late_ticks(.2, 280078),
            ),
        )

        result = run(scenario)

        self.assertEqual(result.violations, [])
        self.assertEqual((result.outcome, result.reason),
                         ("success", "goal_state_satisfied"))
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(any(
            row["session_reason"]
                == "expired_pending_submission_retain_responsibility"
            for row in result.trace
        ))
        self.assertTrue(all(
            row["source_bound"] and row["controller_ids"]
            for row in result.trace if not row["on_ground"]
        ))
        self.assertFalse(any(
            row["session_reason"].startswith("motion_unsolvable:")
            for row in result.trace
        ))

    def test_old_air_input_loss_during_revised_goal_keeps_landing_owner(self):
        scenario = replace(
            SCENARIOS[7], name="air_input_loss_during_goal_revision",
            start=(.5, 64.0, 2.5),
            perturbations=replace(
                SCENARIOS[7].perturbations,
                omitted_receipt_ticks=frozenset({29}),
            ),
            events=[Event("revise_in_air", airborne_in_drop,
                          revise_goal_back)],
        )
        result = run(scenario)
        self.assertEqual(result.events, ["revise_in_air@31"])
        self.assertEqual(result.violations, [])
        self.assertEqual((result.outcome, result.reason),
                         ("failed", "input_lost"))
        airborne = [row for row in result.trace
                    if row["goal_revision"] == 2 and not row["on_ground"]]
        self.assertTrue(airborne)
        self.assertTrue(all(row["source_bound"] and
                            row["driver_state"] not in {"failed", "cancelled"}
                            for row in airborne))
        self.assertTrue(any(
            row["recovery_wait_status"] == "waiting"
            and row["source_bound"]
            for row in result.trace
        ))
        self.assertTrue(any(
            row["body_handoff"] is not None
            and row["body_handoff"]["reason"] == "current_body_still_moving"
            and row["source_bound"]
            for row in result.trace
        ))

    def test_goal_revision_after_risk_commit_finishes_drop_before_replanning(self):
        submitted_before_revision = []

        def risk_action_was_submitted(context):
            return any(
                action.submitted_sequences
                for action in context.diagnostics.risk_actions
            )

        def revise_after_first_submission(context):
            submitted_before_revision.extend(
                sequence
                for action in context.diagnostics.risk_actions
                for sequence in action.submitted_sequences
            )
            goal = _goal((1.5, 59.0, 4.5), context.risk_policy_id)
            context.driver.replace_goal(
                "goal", 2, goal, context.clock[0],
                damage_budget=TaskDamageBudget(
                    context.risk_policy_id, context.damage_points,
                ),
            )
            context.goal_state = goal
            context.goal_position = (1.5, 59.0, 4.5)

        base = next(
            item for item in SCENARIOS
            if item.name == "direct_drop_5_budget_2"
        )
        scenario = replace(
            base,
            name="drop_revision_after_risk_commit",
            events=[Event(
                "revise",
                risk_action_was_submitted,
                revise_after_first_submission,
            )],
            max_ticks=300,
        )
        result = run(scenario)
        self.assertEqual(len(result.events), 1)
        self.assertTrue(submitted_before_revision)
        self.assertEqual(result.violations, [])
        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        self.assertEqual(result.damage, 2.0)
        revised_airborne = [
            row for row in result.trace
            if row["goal_revision"] == 2 and not row["on_ground"]
        ]
        self.assertTrue(revised_airborne)
        self.assertTrue(all(
            row["source_bound"] and row["controller_ids"]
            for row in revised_airborne
        ))

    def test_goal_revision_before_strict_submission_revokes_old_drop(self):
        old_route_ids = []

        def awaiting_unsubmitted_strict(context):
            control = context.session._supervisor.incumbent_route
            probe = context.session._edge_probe
            return (
                control is not None
                and control.executor.action_index == 1
                and not control.executor.current_verified_action_started()
                and probe is not None
                and probe.ready
                and all(
                    not action.submitted_sequences
                    for action in context.diagnostics.risk_actions
                )
            )

        def revise_before_submission(context):
            control = context.session._supervisor.incumbent_route
            self.assertIsNotNone(control)
            old_route_ids.append(control.route.route_id)
            revise_goal_back(context)

        base = next(
            item for item in SCENARIOS
            if item.name == "direct_drop_5_budget_2"
        )
        scenario = replace(
            base,
            name="drop_revision_before_strict_submission",
            events=[Event(
                "revise_before_submission",
                awaiting_unsubmitted_strict,
                revise_before_submission,
            )],
            max_ticks=300,
        )

        result = run(scenario)

        self.assertEqual(len(result.events), 1)
        self.assertEqual(len(old_route_ids), 1)
        self.assertEqual(result.violations, [])
        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        old_route_rows = [
            row for row in result.trace
            if row["route_id"] == old_route_ids[0]
        ]
        self.assertTrue(old_route_rows)
        self.assertTrue(all(
            not any(
                action["submitted_sequences"]
                for action in row["risk_actions"]
            )
            for row in old_route_rows
        ))
        self.assertTrue(any(
            row["goal_revision"] == 2
            and row["route_id"] not in {None, old_route_ids[0]}
            for row in result.trace
        ))
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_observed_health_overrun_locks_future_risk_on_formal_path(self):
        ledger = TaskRiskLedger("goal", TaskDamageBudget("sim-budget", 4))
        injected = False

        def controlled_hit(context):
            nonlocal injected
            if (not injected and ledger.committed_points >= 2
                    and not context.backend.state.on_ground):
                # The game side applies a concurrent, unattributed hit. The
                # navigator sees only formal health samples and charges the
                # full observed decline conservatively to the active drop.
                context.backend.health -= 3
                context.backend.damage_taken += 3
                injected = True
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        scenario = replace(
            SCENARIOS[7], name="observed_health_overrun",
            start=(.5, 64.0, 2.5), damage_points=4,
        )
        result = run(scenario, control_step=controlled_hit,
                     risk_ledger=ledger)
        self.assertTrue(injected)
        self.assertTrue(ledger.risk_overrun)
        self.assertGreaterEqual(
            ledger.snapshot_actions()[0].observed_damage_lower_bound_points,
            3,
        )
        self.assertIs(ledger.reserve("later", 1, policy_revision=0).status,
                      RiskReservationStatus.RISK_OVERRUN)
        self.assertIn("I3", {code for _, code, _ in result.violations})

    def test_same_support_inside_region_but_moving_must_brake(self):
        scenario = replace(
            SCENARIOS[2], name="same_support_moving_in_region",
            start_velocity_blocks_per_tick=(.04, 0.0, 0.0),
        )
        result = run(scenario)
        self.assertEqual(result.verdict, "PASS", result.reason)
        self.assertTrue(any(row["session_reason"] == "same_support_local_route_started"
                            or row["action_kind"] == "WalkSegment"
                            for row in result.trace))

    def test_same_support_heading_must_align_before_complete(self):
        scenario = replace(SCENARIOS[2], name="same_support_heading",
                           goal_yaw_degrees=90.0)
        result = run(scenario)
        self.assertEqual(result.verdict, "PASS", result.reason)
        self.assertTrue(any(row["session_reason"] == "aligning_goal_heading"
                            for row in result.trace))
        self.assertAlmostEqual(result.trace[-1]["yaw_radians"], math.pi / 2,
                               delta=math.radians(2.0))

    def test_legal_event_exception_keeps_prior_tick_trace(self):
        def fail(_context):
            raise RuntimeError("injected_event_failure")

        scenario = replace(SCENARIOS[0], events=[Event(
            "injected_failure", lambda context: context.tick == 4, fail)])
        rows = []
        with self.assertRaisesRegex(RuntimeError, "injected_event_failure"):
            run(scenario, trace_sink=rows.append)
        self.assertEqual([row["loop_tick"] for row in rows], [1, 2, 3])
        self.assertTrue(all("applied_request" in row for row in rows))

    def test_test_oracle_is_explicit_and_absent_from_formal_actor(self):
        with self.assertRaises(ContractViolation):
            NavigationObservationAdapter().seed_test_oracle_memory(object(), {}, ())
        for directory in (ROOT / "mc2p/runtime", ROOT / "mc2p/skills"):
            for path in directory.rglob("*.py"):
                self.assertNotIn("seed_test_oracle_memory", path.read_text(encoding="utf-8"))
                self.assertNotIn("TEST_ORACLE", path.read_text(encoding="utf-8"))
        self.assertIsNotNone(TEST_ORACLE)

    def test_late_one_sample_lease_applies_once_then_exhausts(self):
        scene = Scene({(0, 63, 0): "minecraft:stone",
                       (0, 63, 1): "minecraft:stone"}, ((-2, 2), (60, 68), (-2, 3))).with_floor()
        from tests.sim.backend import Perturbations
        backend = CalculatorBackend([100_000_000], scene, (.5, 64.0, .5),
                                    perturbations=Perturbations(late_ticks=frozenset({2})))
        action = ActionSnapshotV1("episode-sim", 1, 0, 600_000_000,
                                  movement=MovementV1(forward=1), valid_for_ticks=1)
        backend.step(action, 600_000_000)
        self.assertEqual(backend.command_events[-1]["sample_state"], "no_accepted_command")
        self.assertIsNone(backend.command_events[-1]["applied_request"])
        backend.free_tick()
        self.assertEqual(backend.command_events[-1]["applied_request"], 1)
        self.assertEqual(backend.command_events[-1]["sample_state"], "leased")
        backend.free_tick()
        self.assertEqual(backend.command_events[-1]["sample_state"], "lease_exhausted")
        self.assertIsNone(backend.command_events[-1]["applied_request"])

    def test_multi_tick_lease_does_not_repeat_look_delta(self):
        scene = Scene({(0, 63, z): "minecraft:stone" for z in range(4)},
                      ((-2, 2), (60, 68), (-2, 5))).with_floor()
        backend = CalculatorBackend([100_000_000], scene, (.5, 64.0, .5))
        action = ActionSnapshotV1("episode-sim", 1, 0, 600_000_000,
                                  movement=MovementV1(forward=1),
                                  look=LookV1(15.0, 0.0), valid_for_ticks=3)
        backend.step(action, 600_000_000)
        backend.free_tick()
        backend.free_tick()
        self.assertAlmostEqual(backend.state.yaw_radians, math.radians(15), places=6)
        self.assertEqual([event["sample_state"] for event in backend.command_events],
                         ["leased", "leased", "leased"])

    def test_manifest_is_discovered_and_matches_frozen_fourteen(self):
        document, _ = read_manifest(MANIFEST)
        self.assertEqual(len(document["cases"]), 14)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "matrix"
            summary = run_matrix(MANIFEST, output)
            self.assertEqual(
                summary["schema_version"],
                "mc2p.navigation-sim-result.v2",
            )
            self.assertEqual(summary["counts"], {
                "positive_pass": 14, "known_failure": 0,
                "formal_receipt_new_failure": 0,
                "calibrated_input_new_failure": 0, "unexpected": 0,
            })
            self.assertEqual(summary["outcome_counts"], {
                "task_success": 13,
                "bounded_safe_failure": 1,
                "unexpected_result": 0,
            })
            identity = summary["source_identity"]
            self.assertGreater(identity["python_files"], 0)
            self.assertEqual(len(identity["sha256"]), 64)
            self.assertTrue(all(char in "0123456789abcdef"
                                for char in identity["sha256"]))
            self.assertIn(identity["git_dirty"], (True, False, None))
            self.assertTrue((output / "summary.json").is_file())
            self.assertEqual(len(json.loads((output / "summary.json").read_text())[
                "cases"]), 14)
            with self.assertRaises(FileExistsError):
                run_matrix(MANIFEST, output)


if __name__ == "__main__":
    unittest.main()
