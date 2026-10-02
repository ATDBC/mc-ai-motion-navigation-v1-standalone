"""Continuous action checks through Runtime, the real driver and navigation.

Only the game and worker transport are substituted.  CalculatorBackend advances
the real 1.21 calculator; worker jobs run motion_worker._execute_job unchanged.
This is component closed-loop evidence, not Fabric evidence.

The pre-change south-facing audit completed this exact non-centred scene in
68 movement ticks, jumped on tick 12 and ended at (.55, 64, 8.48), with zero
invariant violations.  These facts are retained for comparison, not overwritten
or treated as a required tick-for-tick signature of the behaviour change.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import unittest

from mc2p.motion_nav import motion_worker
from mc2p.motion_nav.motion_solver import SolveStatus
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from tests.sim.async_monitor import ObservedAsyncActivity
from tests.sim.backend import Scene
from tests.sim.runner import Event, InlineMotionWorker, Result, Scenario, run
from tests.test_player_runtime import _task


PRE_CHANGE_SOUTH = {
    "outcome": "success", "ticks": 68, "jump_tick": 12,
    "final_position": (.55, 64.0, 8.48), "violations": (),
}


class _DeliveryWorker(InlineMotionWorker):
    """Hold each real worker result for a fixed number of observation polls."""

    def __init__(self, delivery_polls: int = 1, *, first_delivery_polls=None) -> None:
        super().__init__()
        self.delivery_polls = delivery_polls
        self.first_delivery_polls = first_delivery_polls
        self.poll_count = 0
        self.jobs = []
        self.results = []
        self.deliveries = []

    def submit(self, job) -> bool:
        wait = (self.first_delivery_polls if not self.jobs
                and self.first_delivery_polls is not None else self.delivery_polls)
        self.jobs.append(job)
        self._pending.append((self.poll_count + wait, job))
        self.activity.append(ObservedAsyncActivity(job.work_identity, "submit"))
        return True

    def poll_available(self):
        self.poll_count += 1
        ready = []
        pending = []
        for due, job in self._pending:
            if due <= self.poll_count:
                result = motion_worker._execute_job(job)
                self.results.append(result)
                self.deliveries.append((self.poll_count, job))
                self.activity.append(ObservedAsyncActivity(job.work_identity, "poll"))
                ready.append(result)
            else:
                pending.append((due, job))
        self._pending = pending
        return tuple(ready)


class _LoseFirstJump:
    """One competing safety intent, submitted through the actual arbiter."""

    def __init__(self, *, steal_look=False):
        self.source = None
        self.lost_at_tick = None
        self.navigation_intents = ()
        self.steal_look = steal_look

    def __call__(self, context):
        driver = context.driver
        runtime = driver.runtime
        deadline = context.clock[0] + 500_000_000
        if self.source is not None:
            runtime.cancel_source(self.source.source_id)
        navigation = driver.prepare_proposals(deadline)
        proposed_jumps = tuple(
            envelope.intent.intent_id
            for proposal in navigation for envelope in proposal.intents
            if envelope.intent.movement is not None
            and envelope.intent.movement.jump)
        external = ()
        if self.lost_at_tick is None and proposed_jumps:
            self.source = runtime.register_ordered_source("continuity-first-jump-loss")
            intent = ActionIntentV1(
                ordered_intent_id(self.source, 1), self.source.source_id,
                self.source.episode_id, runtime.observation.sequence_id,
                ActionPriorityV0.SAFETY, context.clock[0], deadline,
                movement=None if self.steal_look else MovementV1(),
                look=LookV1(15.0, 0.0) if self.steal_look else None,
            )
            envelope = OrderedIntentV1(self.source, 1, intent)
            navigation += (ControlFrameProposalV1((envelope,)),)
            external = (intent.intent_id,)
            self.lost_at_tick = context.backend.movement_tick + 1
            self.navigation_intents = proposed_jumps
        result = runtime.control_frame(_task(deadline), BehaviorProfileV0(),
                                       deadline, proposals=navigation)
        driver.adopt_result(result)
        return external


def _rotate(x: float, z: float, turns: int) -> tuple[float, float]:
    for _ in range(turns):
        x, z = z, -x
    return x, z


def _gap_case(turns: int = 0) -> Scenario:
    """Four rotations of one known ordinary platform and one-cell gap."""
    solids = {}
    for x in range(-2, 3):
        for z in range(-2, 10):
            if z == 3:
                continue
            rx, rz = _rotate(x, z, turns)
            solids[(int(rx), 63, int(rz))] = "minecraft:grass_block"
    corners = [_rotate(x, z, turns) for x in (-2, 2) for z in (-2, 9)]
    volume = (
        (int(min(p[0] for p in corners)), int(max(p[0] for p in corners))),
        (60, 70),
        (int(min(p[1] for p in corners)), int(max(p[1] for p in corners))),
    )
    sx, sz = _rotate(.05, .15, turns)
    gx, gz = _rotate(0.0, 8.0, turns)
    return Scenario(
        f"action-continuity-gap-{turns}", Scene(solids, volume),
        (.5 + sx, 64.0, .5 + sz), (.5 + gx, 64.0, .5 + gz),
        yaw_degrees=-90.0 * turns, max_ticks=200,
    )


@dataclass(frozen=True)
class _Metrics:
    jump_ticks: tuple[int, ...]
    jump_entry_speeds: tuple[float, ...]
    movement_gap_before_jump: int | None
    landing_to_next_movement: int | None
    unowned_active_input_ticks: tuple[int, ...]
    worker_calls: int
    prefix_calls: int
    revalidation_calls: int


def _active(row: dict) -> bool:
    return any(row["applied_movement"].values())


def _metrics(result: Result, worker: _DeliveryWorker) -> _Metrics:
    rows = result.trace
    jump_indices = [i for i, row in enumerate(rows)
                    if row["applied_movement"]["jump"]]
    jump_ticks = tuple(rows[i]["movement_tick"] for i in jump_indices)
    speeds = tuple(
        20.0 * math.hypot(rows[i - 1]["velocity"][0], rows[i - 1]["velocity"][2])
        for i in jump_indices if i > 0
    )
    approach_gap = None
    landing_gap = None
    if jump_indices:
        first = jump_indices[0]
        approach = [row["movement_tick"] for row in rows[:first]
                    if _active(row) and row["on_ground"]]
        if approach:
            approach_gap = rows[first]["movement_tick"] - approach[-1] - 1
        landing = next((i for i in range(first + 1, len(rows))
                        if rows[i]["on_ground"]), None)
        if landing is not None:
            resumed = next((row["movement_tick"] for row in rows[landing:]
                            if _active(row)), None)
            if resumed is not None:
                landing_gap = resumed - rows[landing]["movement_tick"]
    return _Metrics(
        jump_ticks, speeds, approach_gap, landing_gap,
        tuple(row["movement_tick"] for row in rows
              if _active(row) and row["applied_body_activity"] is None),
        len(worker.jobs), sum(bool(job.entry_prefix) for job in worker.jobs),
        sum(job.operation is motion_worker.MotionJobOperation.REVALIDATE
            for job in worker.jobs),
    )


class ActionContinuityFormalTests(unittest.TestCase):
    def _run(self, case: Scenario, delivery_polls: int = 1,
             first_delivery_polls=None, **kwargs):
        worker = _DeliveryWorker(delivery_polls,
                                 first_delivery_polls=first_delivery_polls)
        samples = {}
        records = {}
        original_control = kwargs.pop("control_step", None)

        def recorded_control(context):
            first = context.backend.movement_tick + 1
            if original_control is None:
                context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
                external = ()
            else:
                external = original_control(context) or ()
            ledger = context.driver.runtime.input_ledger
            for sample in ledger.samples_between(first, context.backend.movement_tick):
                samples[sample.movement_tick_id] = sample
            for record in ledger.snapshot():
                records[record.control_sequence] = record
            return external

        result = run(case, motion_factory=lambda: worker,
                     control_step=recorded_control, **kwargs)
        worker.actual_samples = samples
        worker.actual_records = records
        return result, worker, _metrics(result, worker)

    def _assert_complete(self, result: Result, metrics: _Metrics) -> None:
        self.assertEqual(result.outcome, "success", (result.reason, metrics,
            [(row["movement_tick"], row["position"], row["session_state"],
              row["session_reason"], row["applied_movement"])
             for row in result.trace[-12:]]))
        self.assertEqual(result.violations, [], metrics)
        self.assertEqual(metrics.unowned_active_input_ticks, (), metrics)
        self.assertTrue(metrics.jump_ticks, metrics)
        self.assertTrue(all(speed <= 3.0 + 1.0e-6
                            for speed in metrics.jump_entry_speeds), metrics)
        self.assertTrue(all(row["runtime_failure"] is None
                            for row in result.trace), metrics)

    def _assert_executed_preparation(self, result: Result, worker: _DeliveryWorker) -> None:
        """A delivered proof must match actual samples, not only submitted keys."""
        rows = {row["movement_tick"]: row for row in result.trace}
        used = []
        for delivered in worker.results:
            proof = delivered.solve_result.proof
            if proof is None or proof.preparation is None:
                continue
            preparation = proof.preparation
            first_tick = preparation.source_anchor.movement_tick_id + 1
            prefix_end = first_tick + len(preparation.commands) - 1
            if not all(tick in worker.actual_samples
                       for tick in range(first_tick, prefix_end + 1)):
                continue
            expected_samples = tuple(
                (value.forward, value.strafe, value.jump, value.sneak, value.sprint)
                for value in preparation.tick_inputs)
            actual_samples = tuple(
                (sample.forward, sample.strafe, sample.jump, sample.sneak, sample.sprint)
                for tick in range(first_tick, prefix_end + 1)
                for sample in (worker.actual_samples[tick],))
            if actual_samples != expected_samples:
                continue
            for variant in (proof.start_variant(tick) for tick in range(
                    proof.execution_window.earliest_start_tick,
                    proof.execution_window.latest_start_tick + 1)):
                entry = rows.get(variant.start_tick - 1)
                if entry is None:
                    continue
                if any(abs(a - b) > 1.0e-7 for a, b in zip(
                        entry["position"], variant.entry_state.position)):
                    continue
                if any(abs(a - b) > 1.0e-7 for a, b in zip(
                        entry["velocity"], variant.entry_state.velocity_blocks_per_tick)):
                    continue
                first = rows.get(variant.start_tick)
                if first is None:
                    continue
                command = proof.commands[0].movement
                actual = first["applied_movement"]
                if actual != dict(forward=command.forward, strafe=command.strafe,
                                  jump=command.jump, sneak=command.sneak,
                                  sprint=command.sprint):
                    continue
                for tick in range(first_tick, prefix_end + 1):
                    sample = worker.actual_samples[tick]
                    record = worker.actual_records[sample.request_sequence_id]
                    self.assertEqual(record.action.look, LookV1())
                self.assertEqual(preparation.trajectory[0],
                                 preparation.source_anchor.physics_state)
                self.assertEqual(preparation.source_anchor.observation_sequence_id,
                                 proof.anchor_observation_sequence_id)
                self.assertEqual(preparation.trajectory[-1], proof.entry_state)
                self.assertTrue(set(preparation.world_dependencies).issubset(
                    proof.world_dependencies))
                used.append(proof)
        self.assertTrue(used, "No executed proof matched actual prefix ledger and start variant")

    def test_non_centred_approach_gap_and_following_walk_complete(self):
        result, worker, metrics = self._run(_gap_case())
        self._assert_complete(result, metrics)
        self.assertGreater(metrics.prefix_calls, 0, metrics)
        self.assertTrue(any(result.solve_result.status is SolveStatus.SOLVED
                            for result in worker.results))
        self._assert_executed_preparation(result, worker)

    def test_non_centred_continuity_is_cardinally_symmetric(self):
        for turns in (1, 2, 3):
            with self.subTest(turns=turns):
                result, worker, metrics = self._run(_gap_case(turns))
                self._assert_complete(result, metrics)
                self._assert_executed_preparation(result, worker)

    def test_two_observation_delivery_keeps_continuity(self):
        result, worker, metrics = self._run(_gap_case(), delivery_polls=2)
        self._assert_complete(result, metrics)
        self._assert_executed_preparation(result, worker)

    def test_three_observation_delivery_keeps_continuity(self):
        result, worker, metrics = self._run(_gap_case(), delivery_polls=3)
        self._assert_complete(result, metrics)
        self._assert_executed_preparation(result, worker)

    def test_cold_first_result_then_hot_revalidation_completes(self):
        result, worker, metrics = self._run(
            _gap_case(), first_delivery_polls=7,
        )
        self._assert_complete(result, metrics)
        self.assertEqual(metrics.worker_calls, 2, metrics)
        self.assertEqual(metrics.prefix_calls, 1, metrics)
        self.assertEqual(metrics.revalidation_calls, 1, metrics)
        first_job, revalidation = worker.jobs
        self.assertIs(revalidation.operation, motion_worker.MotionJobOperation.REVALIDATE)
        self.assertGreater(revalidation.anchor.movement_tick_id + 1,
                           first_job.request.execution_window.latest_start_tick)
        end_prefix_tick = first_job.anchor.movement_tick_id + len(first_job.entry_prefix)
        for tick in range(end_prefix_tick + 1, revalidation.anchor.movement_tick_id + 1):
            sample = worker.actual_samples[tick]
            self.assertEqual((sample.forward, sample.strafe, sample.jump,
                              sample.sneak, sample.sprint),
                             (0., 0., False, False, False))
        refreshed = worker.results[1].solve_result.proof
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.entry_state, revalidation.anchor.physics_state)
        self.assertIsNone(refreshed.preparation)

    def test_first_jump_loses_arbitration_without_false_application(self):
        arbitration = _LoseFirstJump()
        result, worker, metrics = self._run(_gap_case(), control_step=arbitration)
        self._assert_complete(result, metrics)
        self.assertIsNotNone(arbitration.lost_at_tick)
        lost = next(row for row in result.trace
                    if row["movement_tick"] == arbitration.lost_at_tick)
        self.assertFalse(lost["applied_movement"]["jump"])
        self.assertTrue(set(arbitration.navigation_intents).intersection(
            intent for intent, _reason in lost["suppressed_intents"]))
        self.assertTrue(lost["controller_ids"])
        self.assertTrue(lost["source_bound"])
        self.assertEqual(lost["damage_spent"], 0.0)
        self.assertTrue(all(tick > arbitration.lost_at_tick
                            for tick in metrics.jump_ticks))

    def test_airborne_cancel_keeps_owner_until_observed_safe_landing(self):
        case = _gap_case()
        case.events.append(Event(
            "cancel-in-gap-air", lambda context: not context.backend.state.on_ground,
            lambda context: context.driver.release("continuity-test-cancel"),
        ))
        case.expect = "cancelled"
        result, worker, metrics = self._run(case)
        self.assertEqual(result.outcome, "cancelled", (result.reason, metrics))
        self.assertTrue(any(event.startswith("cancel-in-gap-air@")
                            for event in result.events))
        self.assertEqual(result.violations, [])
        self.assertEqual(metrics.unowned_active_input_ticks, ())
        airborne = [row for row in result.trace if not row["on_ground"]]
        self.assertTrue(airborne)
        self.assertTrue(all(row["controller_ids"] and row["source_bound"]
                            for row in airborne))
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(all(row["runtime_failure"] is None
                            for row in result.trace))

    def test_first_jump_cannot_use_a_losing_required_look(self):
        arbitration = _LoseFirstJump(steal_look=True)
        result, worker, metrics = self._run(_gap_case(), control_step=arbitration)
        self.assertIsNotNone(arbitration.lost_at_tick)
        lost = next(row for row in result.trace
                    if row["movement_tick"] == arbitration.lost_at_tick)
        self.assertFalse(lost["applied_movement"]["jump"], lost)
        self.assertTrue(set(arbitration.navigation_intents).intersection(
            intent for intent, _reason in lost["suppressed_intents"]))
        self.assertTrue(lost["controller_ids"])
        self.assertTrue(lost["source_bound"])
        self.assertIn(result.outcome, {"success", "failed", "cancelled"},
                      (result.reason, metrics))
        self.assertEqual(result.violations, [], (result.reason, metrics))
        self.assertEqual(metrics.unowned_active_input_ticks, ())
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(all(row["runtime_failure"] is None
                            for row in result.trace))

    def test_known_landing_change_rejects_prepared_jump_and_releases_safely(self):
        case = _gap_case()
        worker = _DeliveryWorker(2)
        changed = []

        def block_landing(context):
            tick = context.backend.movement_tick + 1
            context.backend.perturbations.world_edits[tick] = {
                (x, 64, 4): "minecraft:stone" for x in range(-2, 3)
            }
            changed.append(tick)

        case.events.append(Event(
            "landing-blocked-before-delivery",
            lambda context: bool(worker.jobs) and context.backend.state.on_ground,
            block_landing,
        ))
        result = run(case, motion_factory=lambda: worker)
        metrics = _metrics(result, worker)
        self.assertEqual(len(changed), 1)
        self.assertIn(result.outcome, {"failed", "cancelled"}, (result.reason, metrics))
        self.assertEqual(result.violations, [], (result.reason, metrics))
        self.assertFalse(any(row["applied_movement"]["jump"]
                             for row in result.trace
                             if row["movement_tick"] > changed[0]), metrics)
        self.assertEqual(metrics.unowned_active_input_ticks, (), metrics)
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(all(row["runtime_failure"] is None
                            for row in result.trace))


if __name__ == "__main__":
    unittest.main()
