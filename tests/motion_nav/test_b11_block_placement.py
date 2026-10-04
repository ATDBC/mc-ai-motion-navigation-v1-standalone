from __future__ import annotations

from mc2p.motion_nav.navigation_owners import GoalRequestLedger

from dataclasses import replace
from pathlib import Path
import unittest

from mc2p.contracts.action_v1 import InteractBlockV1
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, FieldStatusV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ItemStackV2
from mc2p.contracts.observation_v3 import TargetingStateV3
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.world_interaction import (
    BlockPlacementTransaction,
    InteractionKind,
    PlacementFailureKind,
    PlacementState,
    RequiredInteraction,
)
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.block_placement_driver import RuntimeBlockPlacementDriver
from tests.test_action_receipt import receipt_value
from tests.test_player_runtime import _RecordingTrace


SUPPORT = (0, 63, 0)
DESTINATION = (1, 63, 0)
WORK_POSITION = (0.5, 64.0, 0.5)


def _stack(count: int, item_id: str = "minecraft:dirt") -> ItemStackV2:
    if count == 0:
        return ItemStackV2(True, None, 0, 0, 0, False, None)
    return ItemStackV2(False, item_id, count, 0, 0, False, None)


def observation(
    sequence: int,
    *,
    destination: str = "air",
    count: int = 3,
    item_id: str = "minecraft:dirt",
    targeted: bool = True,
    position: tuple[float, float, float] = WORK_POSITION,
    profile: str = "interaction_v1",
    sneaking: bool = False,
    contact_air: bool = True,
):
    blocks = [observed_block(SUPPORT, "minecraft:stone", sources=("surface_depth",))]
    if contact_air:
        blocks.extend(observed_block((0, y, 0), "minecraft:air", kind="empty", sources=("body_contact",))
                      for y in (64, 65))
    if destination == "air":
        blocks.append(observed_block(
            DESTINATION, "minecraft:air", kind="empty", sources=("air_query",),
        ))
    else:
        blocks.append(observed_block(
            DESTINATION, destination, sources=("air_query",),
        ))
    snapshot = follow_snapshot(
        sequence=sequence,
        received=100_000_000 + sequence * 50_000_000,
        position=position,
        blocks=tuple(blocks),
        entities=[],
        profile=profile,
        self_changes={"is_sneaking": sneaking, "movement_tick_id": sequence + 1,
                      "velocity": {"x": 0., "y": -.0784, "z": 0.}},
    )
    held = _stack(count, item_id)
    empty = _stack(0)
    inventory = replace(
        snapshot.inventory.value,
        main=(held,) + (empty,) * 35,
        selected_hotbar_slot=0,
        main_hand=held,
    )
    target = TargetingStateV3(
        "block", SUPPORT, None, "east", Vec3V0(1.0, 63.5, 0.5), 2.0,
    ) if targeted else TargetingStateV3("miss", None, None, None, None, None)
    if targeted:
        current = replace(blocks[0], sources=("current_target", "surface_depth"))
        perceived = (current,) + tuple(blocks[1:])
    else:
        perceived = tuple(blocks)
    changes = dict(
        inventory=replace(snapshot.inventory, value=inventory),
        perception=replace(
            snapshot.perception,
            value=replace(snapshot.perception.value, blocks=perceived),
        ),
    )
    if profile == "interaction_v1":
        changes["targeting"] = replace(snapshot.targeting, value=target)
    return replace(snapshot, **changes)


def without_inventory(snapshot):
    return replace(
        snapshot,
        inventory=replace(
            snapshot.inventory,
            status=FieldStatusV0.MISSING,
            reason_code="inventory_not_sampled",
            value=None,
        ),
    )


def placement_scope(interaction):
    return GoalRequestLedger().bind_computation_scope(interaction.goal_id, interaction.world_session)


def requirement(**changes) -> RequiredInteraction:
    values = dict(
        interaction_id="placement-1",
        request_id="route-1",
        goal_id="goal-1",
        goal_revision=1,
        world_session="fixture:episode-1:jvm-test",
        kind=InteractionKind.PLACE_BLOCK,
        support=SUPPORT,
        face="east",
        destination=DESTINATION,
        expected_item_id="minecraft:dirt",
        expected_block_id="minecraft:dirt",
        work_position=WORK_POSITION,
        dependencies=(SUPPORT, DESTINATION),
        maximum_attempts=2,
        confirmation_timeout_ticks=4,
    )
    values.update(changes)
    return RequiredInteraction(**values)


class RequiredInteractionTests(unittest.TestCase):
    def test_destination_must_match_clicked_face(self):
        self.assertEqual(requirement().destination, DESTINATION)
        for destination in (SUPPORT, (0, 64, 0), (2, 63, 0)):
            with self.subTest(destination=destination), self.assertRaises(ContractViolation):
                requirement(destination=destination)

    def test_identity_limits_and_dependencies_are_strict(self):
        with self.assertRaises(ContractViolation):
            requirement(expected_item_id="")
        with self.assertRaises(ContractViolation):
            requirement(maximum_attempts=0)
        with self.assertRaises(ContractViolation):
            requirement(dependencies=(DESTINATION, SUPPORT))


class BlockPlacementTransactionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = NavigationObservationAdapter()
        self.clock = [150_000_000]
        self.transaction = BlockPlacementTransaction(requirement(), clock_ns=lambda: self.clock[0], computation_scope=placement_scope(requirement()))

    def _propose(self, snapshot):
        self.clock[0] = snapshot.received_at_monotonic_ns
        frame = self.adapter.ingest(snapshot)
        return self.transaction.propose(snapshot, frame)

    def test_ready_transaction_requires_current_target_and_safe_known_facts(self):
        proposal = self._propose(observation(1))
        self.assertEqual(proposal.state, PlacementState.READY)
        self.assertEqual(proposal.operation, InteractBlockV1(*SUPPORT, "east"))
        self.assertEqual(proposal.observation_request.air_positions, (DESTINATION,))

        other = BlockPlacementTransaction(requirement(interaction_id="placement-2"), computation_scope=placement_scope(requirement(interaction_id="placement-2")))
        snapshot = observation(2, targeted=False)
        frame = self.adapter.ingest(snapshot)
        rejected = other.propose(snapshot, frame)
        self.assertIsNone(rejected.operation)
        self.assertEqual(rejected.reason, "target_not_aligned")

        edge = BlockPlacementTransaction(requirement(
            interaction_id="placement-sneak",
            requires_sneak=True,
        ), computation_scope=placement_scope(requirement(
            interaction_id="placement-sneak",
            requires_sneak=True,
        )))
        not_sneaking = edge.propose(observation(3), self.adapter.ingest(observation(3)))
        self.assertIsNone(not_sneaking.operation)
        self.assertEqual(not_sneaking.reason, "sneak_not_confirmed")
        sneaking_snapshot = observation(4, sneaking=True)
        allowed = edge.propose(
            sneaking_snapshot, self.adapter.ingest(sneaking_snapshot),
        )
        self.assertIsNotNone(allowed.operation)

    def test_pending_receipt_does_not_complete_before_world_confirmation(self):
        proposal = self._propose(observation(1))
        self.transaction.register_dispatch(
            proposal,
            selected=True,
            receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched",
            control_sequence=7,
        )
        still_air = self._propose(observation(2))
        self.assertEqual(still_air.state, PlacementState.AWAITING_CONFIRMATION)
        self.assertIsNone(still_air.operation)

    def test_later_block_and_inventory_change_confirm_one_placement(self):
        proposal = self._propose(observation(1, count=3))
        self.transaction.register_dispatch(
            proposal,
            selected=True,
            receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched",
            control_sequence=7,
        )
        confirmed = self._propose(observation(
            2, destination="minecraft:dirt", count=2,
        ))
        self.assertEqual(confirmed.state, PlacementState.COMPLETE)
        self.assertEqual(confirmed.reason, "placement_confirmed")
        self.assertTrue(self.transaction.report.terminal)

    def test_wrong_result_fails_without_writing_a_hypothetical_block(self):
        proposal = self._propose(observation(1))
        self.transaction.register_dispatch(
            proposal,
            selected=True,
            receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched",
            control_sequence=7,
        )
        result = self._propose(observation(2, destination="minecraft:stone", count=2))
        self.assertEqual(result.state, PlacementState.FAILED)
        self.assertEqual(result.reason, "unexpected_destination_block")

    def test_item_body_and_world_identity_are_checked_before_submit(self):
        wrong_item = self._propose(observation(1, item_id="minecraft:stone"))
        self.assertEqual(wrong_item.reason, "expected_item_not_in_main_hand")
        self.assertIsNone(wrong_item.operation)
        self.assertTrue(self.transaction.report.terminal)

        overlapping = BlockPlacementTransaction(requirement(interaction_id="placement-3"), computation_scope=placement_scope(requirement(interaction_id="placement-3")))
        snapshot = observation(2, position=(1.5, 63.0, 0.5))
        frame = self.adapter.ingest(snapshot)
        blocked = overlapping.propose(snapshot, frame)
        self.assertEqual(blocked.reason, "destination_intersects_body")
        self.assertTrue(overlapping.report.terminal)

        occupied = BlockPlacementTransaction(requirement(
            interaction_id="placement-occupied",
        ), computation_scope=placement_scope(requirement(
            interaction_id="placement-occupied",
        )))
        snapshot = observation(3, destination="minecraft:stone")
        occupied_result = occupied.propose(snapshot, self.adapter.ingest(snapshot))
        self.assertEqual(occupied_result.reason, "destination_not_known_air")
        self.assertIs(
            occupied.report.failure_kind,
            PlacementFailureKind.WORLD_DEPENDENCY_CHANGED,
        )
        self.assertTrue(occupied.report.terminal)

        with self.assertRaises(ContractViolation):
            BlockPlacementTransaction(requirement(world_session="other/session"), computation_scope=placement_scope(requirement(world_session="other/session"))).propose(
                snapshot, frame,
            )

    def test_confirmation_timeout_retries_are_bounded(self):
        first = self._propose(observation(1))
        self.transaction.register_dispatch(
            first, selected=True, receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched", control_sequence=7,
        )
        first_identity = self.transaction.work_identity
        self.assertIsNotNone(first_identity)
        waiting = self._propose(observation(5))
        self.assertEqual(waiting.state, PlacementState.READY)
        self.assertEqual(waiting.reason, "confirmation_timeout_retry")
        self.assertIsNone(self.transaction.work_identity)
        second = self._propose(observation(6))
        self.transaction.register_dispatch(
            second, selected=True, receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched", control_sequence=8,
        )
        self.assertNotEqual(self.transaction.work_identity, first_identity)
        failed = self._propose(observation(10))
        self.assertEqual(failed.state, PlacementState.FAILED)
        self.assertEqual(failed.reason, "confirmation_timeout")
        self.assertIsNone(self.transaction.work_identity)
        self.assertEqual(
            tuple(record.disposition.value
                  for record in self.transaction.admission_records),
            ("recompute", "terminated"),
        )

    def test_world_confirmation_without_inventory_still_reaches_fixed_deadline(self):
        transaction = BlockPlacementTransaction(requirement(maximum_attempts=1), clock_ns=lambda: self.clock[0], computation_scope=placement_scope(requirement(maximum_attempts=1)))
        snapshot = observation(1)
        proposal = transaction.propose(snapshot, self.adapter.ingest(snapshot))
        transaction.register_dispatch(
            proposal, selected=True, receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched", control_sequence=7,
        )

        partial = without_inventory(observation(
            2, destination="minecraft:dirt", count=2,
        ))
        waiting = transaction.propose(partial, self.adapter.ingest(partial))
        self.assertEqual(waiting.state, PlacementState.AWAITING_CONFIRMATION)
        self.assertEqual(waiting.reason, "awaiting_inventory_confirmation")

        expired = without_inventory(observation(
            5, destination="minecraft:dirt", count=2,
        ))
        self.clock[0] = expired.received_at_monotonic_ns
        failed = transaction.propose(expired, self.adapter.ingest(expired))
        self.assertEqual(failed.state, PlacementState.FAILED)
        self.assertEqual(failed.reason, "confirmation_timeout")

        late = observation(6, destination="minecraft:dirt", count=2)
        unchanged = transaction.propose(late, self.adapter.ingest(late))
        self.assertEqual(unchanged.state, PlacementState.FAILED)
        self.assertEqual(unchanged.reason, "confirmation_timeout")
        self.assertIsNone(transaction.work_identity)
        self.assertEqual(
            transaction.last_admission.disposition.value, "terminated",
        )

    def test_inventory_evidence_can_arrive_before_world_evidence(self):
        proposal = self._propose(observation(1, count=3))
        self.transaction.register_dispatch(
            proposal, selected=True, receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched", control_sequence=7,
        )
        inventory_first = self._propose(observation(2, count=2))
        self.assertEqual(
            inventory_first.state, PlacementState.AWAITING_CONFIRMATION,
        )
        confirmed = self._propose(observation(
            3, destination="minecraft:dirt", count=2,
        ))
        self.assertEqual(confirmed.state, PlacementState.COMPLETE)

    def test_complete_evidence_at_deadline_cannot_revive_confirmation(self):
        transaction = BlockPlacementTransaction(requirement(maximum_attempts=1), clock_ns=lambda: self.clock[0], computation_scope=placement_scope(requirement(maximum_attempts=1)))
        snapshot = observation(1, count=3)
        proposal = transaction.propose(snapshot, self.adapter.ingest(snapshot))
        transaction.register_dispatch(
            proposal, selected=True, receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched", control_sequence=7,
        )

        late = observation(5, destination="minecraft:dirt", count=2)
        self.clock[0] = late.received_at_monotonic_ns
        result = transaction.propose(late, self.adapter.ingest(late))

        self.assertEqual(result.state, PlacementState.FAILED)
        self.assertEqual(result.reason, "confirmation_timeout")

    def test_cancel_is_terminal_and_never_proposes_an_operation(self):
        self.transaction.cancel("goal_cancelled")
        proposal = self._propose(observation(1))
        self.assertEqual(proposal.state, PlacementState.CANCELLED)
        self.assertIsNone(proposal.operation)


class _PlacementBackend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(self, clock):
        self.clock = clock
        self.actions = []
        self.sequence = 0

    def _observation(self, *, placed: bool, request_sequence_id, profile="interaction_v1"):
        value = observation(
            self.sequence,
            destination="minecraft:dirt" if placed else "air",
            count=2 if placed else 3,
            targeted=profile == "interaction_v1",
            profile=profile,
        )
        return replace(
            value,
            episode_id="episode-1",
            request_sequence_id=request_sequence_id,
            received_at_monotonic_ns=self.clock[0],
        )

    def reset(self, request):
        return ResetResultV0(
            request.request_id,
            request.episode_id,
            True,
            self._observation(
                placed=False, request_sequence_id=None, profile="navigation_v1",
            ),
        )

    def step(self, action, deadline, *, observation_request=None):
        self.actions.append(action)
        self.sequence += 1
        self.clock[0] += 50_000_000
        placed = action.operation == InteractBlockV1(*SUPPORT, "east")
        observed = self._observation(
            placed=placed,
            request_sequence_id=action.request_sequence_id,
        )
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id,
                generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observed.world_time_ticks.value,
                input_samples=self.sequence + 1,
                status="pending_confirmation" if placed else "executed",
                reason="block_use_dispatched" if placed else "neutral",
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": 0,
            "oldest_retained_input_tick": self.sequence + 1,
            "input_applications": [{
                "schema_version": "mc2p.input-application.v1",
                "movement_tick_id": self.sequence + 1,
                "episode_id": action.episode_id,
                "request_sequence_id": action.request_sequence_id,
                "sampled_at_jvm_ns": self.sequence + 1,
                "state": "leased",
                "forward": float(action.movement.forward),
                "strafe": float(action.movement.strafe),
                "jump": action.movement.jump,
                "sneak": action.movement.sneak,
                "sprint": action.movement.sprint,
            }],
        })
        return BackendStepResultV1(observed, 0.0, False, False, receipt)

    def close(self):
        pass


class RuntimeBlockPlacementDriverTests(unittest.TestCase):
    def test_edge_approach_requests_and_looks_at_missing_headroom(self):
        class _EdgeBackend(_PlacementBackend):
            def _observation(
                self, *, placed: bool, request_sequence_id,
                profile="interaction_v1",
            ):
                value = observation(
                    self.sequence,
                    destination="minecraft:dirt" if placed else "air",
                    count=2 if placed else 3,
                    targeted=profile == "interaction_v1",
                    position=(0.625, 64.0, 0.5),
                    profile=profile,
                    sneaking=True,
                    contact_air=False,
                )
                value = replace(
                    value,
                    self_state=replace(
                        value.self_state,
                        value=replace(value.self_state.value, pose="crouching"),
                    ),
                    perception=replace(
                        value.perception,
                        value=replace(
                            value.perception.value,
                            blocks=tuple(sorted((
                                *value.perception.value.blocks,
                                observed_block(
                                    (0, 64, 0), "minecraft:air", kind="empty",
                                    sources=("body_contact",),
                                ),
                                observed_block(
                                    (0, 65, 0), "minecraft:air", kind="empty",
                                    sources=("body_contact",),
                                ),
                            ), key=lambda block: block.position)),
                        ),
                    ),
                )
                return replace(
                    value,
                    episode_id="episode-1",
                    request_sequence_id=request_sequence_id,
                    received_at_monotonic_ns=self.clock[0],
                )

        clock = [200_000_000]
        backend = _EdgeBackend(clock)
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        reset = runtime.reset(ResetRequestV0(
            "reset-edge-information", "episode-1", "test", 1,
            5_000_000_000,
        ))
        self.assertTrue(reset.succeeded, reset)
        self.addCleanup(runtime.close)
        edge = requirement(
            work_position=(1.12, 64.0, 0.5),
            work_position_tolerance=0.08,
            requires_sneak=True,
        )
        profiles = NavigationSessionProfiles.load(
            Path(__file__).resolve().parents[2] / "config" / "motion-navigation",
        )
        driver = RuntimeBlockPlacementDriver(
            runtime,
            BlockPlacementTransaction(edge, computation_scope=placement_scope(edge)),
            approach_mode=profiles.ground_modes.require(MovementMode.CROUCH),
            clock_ns=lambda: clock[0],
        )
        driver.start()

        proposal = driver.prepare_proposal(clock[0] + 500_000_000)

        self.assertIn(
            (1, 65, 0),
            proposal.observation_request.air_positions,
            "edge approach must preserve the missing headroom returned by its route",
        )
        self.assertEqual(len(proposal.intents), 1)
        self.assertIsNotNone(proposal.intents[0].intent.look)

    def test_runtime_dispatches_once_and_only_later_observation_completes(self):
        clock = [200_000_000]
        backend = _PlacementBackend(clock)
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        reset = runtime.reset(ResetRequestV0(
            "reset-placement", "episode-1", "test", 1, 5_000_000_000,
        ))
        self.assertTrue(reset.succeeded)
        self.addCleanup(runtime.close)
        transaction = BlockPlacementTransaction(requirement(), computation_scope=placement_scope(requirement()))
        driver = RuntimeBlockPlacementDriver(
            runtime, transaction, clock_ns=lambda: clock[0],
        )
        driver.start()

        refreshed = driver.tick(
            BehaviorProfileV0(), clock[0] + 500_000_000,
        )
        self.assertIsNotNone(refreshed)
        self.assertEqual(len(backend.actions), 1)
        self.assertIsNone(backend.actions[0].operation)

        dispatched = driver.tick(
            BehaviorProfileV0(), clock[0] + 500_000_000,
        )
        self.assertIsNotNone(dispatched)
        self.assertEqual(len(backend.actions), 2)
        self.assertEqual(
            backend.actions[1].operation,
            InteractBlockV1(*SUPPORT, "east"),
        )
        self.assertEqual(
            transaction.report.state,
            PlacementState.AWAITING_CONFIRMATION,
        )

        completed = driver.tick(
            BehaviorProfileV0(), clock[0] + 500_000_000,
        )
        self.assertIsNone(completed)
        self.assertEqual(transaction.report.state, PlacementState.COMPLETE)
        self.assertEqual(len(backend.actions), 2)
        self.assertIsNone(driver.source)

    def test_preparation_is_bounded_by_new_observations(self):
        class _NeverAlignedBackend(_PlacementBackend):
            def _observation(
                self, *, placed: bool, request_sequence_id,
                profile="interaction_v1",
            ):
                value = observation(
                    self.sequence,
                    destination="minecraft:dirt" if placed else "air",
                    count=2 if placed else 3,
                    targeted=False,
                    profile=profile,
                )
                return replace(
                    value,
                    episode_id="episode-1",
                    request_sequence_id=request_sequence_id,
                    received_at_monotonic_ns=self.clock[0],
                )

        clock = [200_000_000]
        backend = _NeverAlignedBackend(clock)
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        reset = runtime.reset(ResetRequestV0(
            "reset-placement-timeout", "episode-1", "test", 1,
            5_000_000_000,
        ))
        self.assertTrue(reset.succeeded)
        self.addCleanup(runtime.close)
        transaction = BlockPlacementTransaction(requirement(), computation_scope=placement_scope(requirement()))
        driver = RuntimeBlockPlacementDriver(
            runtime,
            transaction,
            preparation_timeout_observations=3,
            clock_ns=lambda: clock[0],
        )
        driver.start()

        for _ in range(6):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if transaction.report.terminal:
                break

        self.assertEqual(transaction.report.state, PlacementState.FAILED)
        self.assertEqual(transaction.report.reason, "preparation_timeout")
        self.assertLessEqual(len(backend.actions), 3)

    def test_preparation_timeout_stops_after_dispatch(self):
        class _DelayedConfirmationBackend(_PlacementBackend):
            def __init__(self, clock):
                super().__init__(clock)
                self.dispatched_at = None

            def step(self, action, deadline, *, observation_request=None):
                self.actions.append(action)
                self.sequence += 1
                self.clock[0] += 50_000_000
                clicked = action.operation == InteractBlockV1(*SUPPORT, "east")
                if clicked and self.dispatched_at is None:
                    self.dispatched_at = self.sequence
                placed = (
                    self.dispatched_at is not None
                    and self.sequence >= self.dispatched_at + 2
                )
                observed = self._observation(
                    placed=placed,
                    request_sequence_id=action.request_sequence_id,
                )
                receipt = behavior_receipt_from_mapping({
                    **receipt_value(
                        episode_id=action.episode_id,
                        generation_id=self.sequence,
                        request_sequence_id=action.request_sequence_id,
                        world_tick=observed.world_time_ticks.value,
                        status="pending_confirmation" if clicked else "executed",
                        reason="block_use_dispatched" if clicked else "neutral",
                    ),
                    "schema_version": "mc2p.client_action_receipt.v3",
                    "dropped_input_samples": 0,
                    "oldest_retained_input_tick": self.sequence,
                    "input_applications": [],
                })
                return BackendStepResultV1(observed, 0.0, False, False, receipt)

        clock = [200_000_000]
        backend = _DelayedConfirmationBackend(clock)
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        reset = runtime.reset(ResetRequestV0(
            "reset-delayed-placement", "episode-1", "test", 1,
            5_000_000_000,
        ))
        self.assertTrue(reset.succeeded)
        self.addCleanup(runtime.close)
        transaction = BlockPlacementTransaction(requirement(), computation_scope=placement_scope(requirement()))
        driver = RuntimeBlockPlacementDriver(
            runtime,
            transaction,
            preparation_timeout_observations=3,
            clock_ns=lambda: clock[0],
        )
        driver.start()

        for _ in range(8):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if transaction.report.terminal:
                break

        self.assertEqual(transaction.report.state, PlacementState.COMPLETE)
        self.assertEqual(transaction.report.reason, "placement_confirmed")


if __name__ == "__main__":
    unittest.main()
