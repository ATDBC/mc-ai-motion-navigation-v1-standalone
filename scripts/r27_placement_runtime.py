"""Narrow real-Fabric R27 cases using public prepare/adopt and real observations."""
from dataclasses import asdict, replace
import math
import time

from mc2p.contracts.common import FieldStatusV0
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.world_interaction import BlockPlacementTransaction, PlacementState
from mc2p.motion_nav.world_model import CellKnowledge
from mc2p.skills.block_placement_driver import RuntimeBlockPlacementDriver
from scripts.control_probe_core import append_jsonl


CASES = ("moving_cancel", "air_cancel", "late_confirmation", "partial_revoked")


class InventoryDelayTestBackend:
    """Test-only field delivery fault; retain the original sampled inventory."""
    def __init__(self, backend):
        self._backend = backend
        self.delay_inventory = False
        self.raw_inventory_evidence = None
        self.delay_inventory_after_dispatch = False
        self.wrong_destination = None

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def step(self, *args, **kwargs):
        result = self._backend.step(*args, **kwargs)
        if self.delay_inventory_after_dispatch and args[0].operation is not None:
            self.delay_inventory = True
        self.raw_inventory_evidence = {"sequence": result.observation.sequence_id,
                                       "inventory": asdict(result.observation.inventory),
                                       "delivery_delayed": self.delay_inventory}
        observation = result.observation
        if self.wrong_destination is not None:
            observation = replace(observation, perception=replace(observation.perception,
                value=replace(observation.perception.value, blocks=tuple(
                    replace(block, block_id="minecraft:stone")
                    if block.position == self.wrong_destination and block.block_id == "minecraft:dirt" else block
                    for block in observation.perception.value.blocks))))
        if self.delay_inventory:
            observation = replace(observation, inventory=replace(observation.inventory,
                status=FieldStatusV0.MISSING, reason_code="r27_test_inventory_delivery_delayed", value=None))
        return result if observation is result.observation else replace(result, observation=observation)


def run_r27_placement_cases(runtime, backend, directory, deadline_ns, profiles, diagnostic, fixture_writer):
    from scripts.b11_world_change_runtime import _fixture_commands, _ready_fixture, _fixed_requirement, FEET_Y
    rows = []
    for case in CASES:
        trial = {"trial_id": "r27-" + case, "kind": "fixed_placement", "gap_count": 1, "initial_items": 1}
        fixture_writer(_fixture_commands(trial), trial)
        task, profile, frame = _ready_fixture(runtime, trial, deadline_ns, diagnostic, fixture_writer)
        requirement = replace(_fixed_requirement(trial, frame, edge=True), maximum_attempts=1)
        transaction = BlockPlacementTransaction(requirement)
        driver = RuntimeBlockPlacementDriver(runtime, transaction,
            approach_mode=profiles.ground_modes.require(MovementMode.CROUCH))
        driver.start()
        events, commands = [], 0
        phase_triggered = False
        partial_observed = False
        released_with_evidence = False
        if case == "air_cancel":
            fixture_writer((f"tp MC2PProbe 0.5 {FEET_Y + .35} 0.5 -90.0 70.0",), trial)
        try:
            for tick in range(1, 81):
                if transaction.report.terminal:
                    result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
                else:
                    limit = min(deadline_ns, time.perf_counter_ns() + 400_000_000)
                    proposal = driver.prepare_proposal(limit)
                    # The client always samples inventory. Only this explicit test
                    # wrapper delays field delivery; raw inventory remains in evidence.
                    if case == "partial_revoked" and (driver.prepared_placement.operation is not None
                                                     or transaction.report.state is PlacementState.AWAITING_CONFIRMATION):
                        latest = runtime.navigation_observation_adapter.latest_frame
                        use_inventory = phase_triggered and latest.world.cell(requirement.destination).knowledge is CellKnowledge.AIR
                        backend.delay_inventory = not use_inventory
                    result = runtime.control_frame(replace(task, deadline_monotonic_ns=limit), profile, limit,
                                                   proposals=(proposal,))
                    driver.adopt_result(result)
                if result is not None:
                    if result.report.failure is not None:
                        raise RuntimeError(f"R27 placement control failed: {case}/{result.report.failure.code}/{result.report.failure.message}")
                    diagnostic()
                    commands += int(result.decision is not None and result.decision.action.operation is not None)
                frame = runtime.navigation_observation_adapter.latest_frame
                speed = math.hypot(frame.body.velocity_blocks_per_second[0], frame.body.velocity_blocks_per_second[2])
                if not phase_triggered:
                    if case == "moving_cancel" and speed > .10:
                        driver.cancel("r27_moving_cancel")
                        phase_triggered = True
                    elif case == "air_cancel" and not frame.body.is_on_ground:
                        driver.cancel("r27_air_cancel")
                        phase_triggered = True
                    elif case == "late_confirmation" and transaction.report.state is PlacementState.AWAITING_CONFIRMATION:
                        phase_triggered = True
                        # Processing delay is deliberately injected, not algorithm time.
                        time.sleep(requirement.confirmation_timeout_ticks * .05 + .06)
                    elif case == "partial_revoked" and transaction.report.state is PlacementState.AWAITING_CONFIRMATION:
                        partial_observed = frame.world.cell(requirement.destination).knowledge is CellKnowledge.BLOCK
                        if partial_observed:
                            phase_triggered = True
                            fixture_writer((f"setblock 1 {FEET_Y - 1} 0 minecraft:air",), trial)
                event = {"tick": tick, "sequence": runtime.observation.sequence_id,
                         "movement_tick": frame.body.movement_tick_id,
                         "position": frame.body.position, "velocity": frame.body.velocity_blocks_per_second,
                         "on_ground": frame.body.is_on_ground, "source_owned": driver.source is not None,
                         "phase_triggered": phase_triggered, "placement": asdict(transaction.report),
                         "raw_inventory": backend.raw_inventory_evidence,
                         "async": asdict(transaction.async_diagnostics)}
                events.append(event)
                append_jsonl(directory / "r27-placement-controls.jsonl", {"case": case, **event})
                if driver.source is None:
                    released_with_evidence = frame.body.is_on_ground and speed <= .10
                    break
            expected = PlacementState.CANCELLED if case in {"moving_cancel", "air_cancel"} else PlacementState.FAILED
            passed = (phase_triggered and released_with_evidence and transaction.report.state is expected
                      and commands == (0 if expected is PlacementState.CANCELLED else 1))
            if case == "partial_revoked":
                passed = passed and partial_observed and frame.world.cell(requirement.destination).knowledge is CellKnowledge.AIR
            row = {"case": case, "passed": passed, "controls": len(events), "operations": commands,
                   "state": transaction.report.state.value, "reason": transaction.report.reason,
                   "partial_observed": partial_observed, "released_with_evidence": released_with_evidence}
            rows.append(row)
            append_jsonl(directory / "r27-placement-trials.jsonl", row)
            if not passed:
                raise RuntimeError(f"R27 placement case failed: {row}")
        finally:
            backend.delay_inventory = False
            if driver.has_prepared_frame:
                driver.discard_prepared()
            if not transaction.report.terminal:
                driver.cancel("r27_case_cleanup")
    return rows
