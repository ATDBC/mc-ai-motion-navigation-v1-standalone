"""Typed, observation-confirmed world interactions used by B11."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.action_v1 import InteractBlockV1
from mc2p.contracts.common import (
    ContractViolation,
    FieldStatusV0,
    require_identifier,
    require_nonnegative_int,
)
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v3 import ObservationSnapshotV3, TargetingStateV3
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import Aabb, BlockPos, CellKnowledge


_FACE_OFFSETS: dict[str, BlockPos] = {
    "down": (0, -1, 0),
    "up": (0, 1, 0),
    "north": (0, 0, -1),
    "south": (0, 0, 1),
    "west": (-1, 0, 0),
    "east": (1, 0, 0),
}

def _block_position(value: BlockPos, name: str) -> None:
    if (type(value) is not tuple or len(value) != 3
            or any(type(axis) is not int or not -30_000_000 <= axis <= 30_000_000
                   for axis in value)):
        raise ContractViolation(f"{name} must be a bounded integer block position")


def _finite_position(value: tuple[float, float, float], name: str) -> None:
    if (type(value) is not tuple or len(value) != 3
            or any(type(axis) not in (int, float) or not math.isfinite(float(axis))
                   for axis in value)):
        raise ContractViolation(f"{name} must be a finite position")


def _overlaps(left: Aabb, right: Aabb) -> bool:
    return (
        left.min_x < right.max_x and left.max_x > right.min_x
        and left.min_y < right.max_y and left.max_y > right.min_y
        and left.min_z < right.max_z and left.max_z > right.min_z
    )


class InteractionKind(StrEnum):
    PLACE_BLOCK = "place_block"


class PlacementState(StrEnum):
    READY = "ready"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlacementFailureKind(StrEnum):
    NONE = "none"
    WORLD_DEPENDENCY_CHANGED = "world_dependency_changed"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class RequiredInteraction:
    interaction_id: str
    request_id: str
    goal_id: str
    goal_revision: int
    world_session: str
    kind: InteractionKind
    support: BlockPos
    face: str
    destination: BlockPos
    expected_item_id: str
    expected_block_id: str
    work_position: tuple[float, float, float]
    dependencies: tuple[BlockPos, ...]
    requires_sneak: bool = False
    maximum_attempts: int = 2
    confirmation_timeout_ticks: int = 8
    work_position_tolerance: float = 0.4

    def __post_init__(self) -> None:
        for value, name in (
            (self.interaction_id, "interaction id"),
            (self.request_id, "planning request id"),
            (self.goal_id, "goal id"),
            (self.world_session, "world session"),
            (self.expected_item_id, "expected item id"),
            (self.expected_block_id, "expected block id"),
        ):
            require_identifier(value, name)
        require_nonnegative_int(self.goal_revision, "goal revision")
        if type(self.kind) is not InteractionKind:
            raise ContractViolation("world interaction kind must be typed")
        _block_position(self.support, "interaction support")
        _block_position(self.destination, "interaction destination")
        offset = _FACE_OFFSETS.get(self.face)
        if offset is None:
            raise ContractViolation("invalid interaction face")
        expected_destination = tuple(
            self.support[index] + offset[index] for index in range(3)
        )
        if self.destination != expected_destination:
            raise ContractViolation("interaction destination does not match clicked face")
        _finite_position(self.work_position, "interaction work position")
        if type(self.requires_sneak) is not bool:
            raise ContractViolation("interaction sneak requirement must be explicit")
        if (type(self.dependencies) is not tuple
                or self.dependencies != tuple(sorted(set(self.dependencies)))):
            raise ContractViolation("interaction dependencies must be sorted and unique")
        for dependency in self.dependencies:
            _block_position(dependency, "interaction dependency")
        if self.support not in self.dependencies or self.destination not in self.dependencies:
            raise ContractViolation("interaction dependencies omit support or destination")
        if type(self.maximum_attempts) is not int or not 1 <= self.maximum_attempts <= 4:
            raise ContractViolation("interaction attempt limit must be within 1..4")
        if (type(self.confirmation_timeout_ticks) is not int
                or not 1 <= self.confirmation_timeout_ticks <= 40):
            raise ContractViolation("interaction confirmation timeout must be within 1..40 ticks")
        if (type(self.work_position_tolerance) not in (int, float)
                or not math.isfinite(float(self.work_position_tolerance))
                or not 0.0 < self.work_position_tolerance <= 1.0):
            raise ContractViolation("interaction work-position tolerance is invalid")


@dataclass(frozen=True, slots=True)
class PlacementReport:
    interaction_id: str
    state: PlacementState
    reason: str
    attempts: int
    submitted_observation_sequence: int | None
    submitted_control_sequence: int | None
    terminal: bool
    failure_kind: PlacementFailureKind


@dataclass(frozen=True, slots=True)
class PlacementProposal:
    interaction_id: str
    observation_sequence: int
    state: PlacementState
    reason: str
    operation: InteractBlockV1 | None
    observation_request: ObservationRequestV3


class BlockPlacementTransaction:
    """One bounded block placement; success comes only from later observation."""

    def __init__(self, requirement: RequiredInteraction) -> None:
        if type(requirement) is not RequiredInteraction:
            raise ContractViolation("block placement requires a typed interaction")
        if requirement.kind is not InteractionKind.PLACE_BLOCK:
            raise ContractViolation("block placement received another interaction kind")
        self.requirement = requirement
        self._state = PlacementState.READY
        self._reason = "not_started"
        self._failure_kind = PlacementFailureKind.NONE
        self._attempts = 0
        self._last_proposed_sequence: int | None = None
        self._submitted_observation_sequence: int | None = None
        self._submitted_world_tick: int | None = None
        self._submitted_item_count: int | None = None
        self._submitted_slot: int | None = None
        self._submitted_control_sequence: int | None = None

    @property
    def report(self) -> PlacementReport:
        return PlacementReport(
            self.requirement.interaction_id,
            self._state,
            self._reason,
            self._attempts,
            self._submitted_observation_sequence,
            self._submitted_control_sequence,
            self._state in {
                PlacementState.COMPLETE,
                PlacementState.FAILED,
                PlacementState.CANCELLED,
            },
            self._failure_kind,
        )

    def cancel(self, reason: str) -> None:
        require_identifier(reason, "placement cancellation reason")
        if self.report.terminal:
            raise ContractViolation("terminal placement cannot be cancelled")
        self._state = PlacementState.CANCELLED
        self._reason = reason
        self._failure_kind = PlacementFailureKind.NONE
        self._last_proposed_sequence = None

    def fail(self, reason: str) -> None:
        require_identifier(reason, "placement failure reason")
        if self.report.terminal:
            raise ContractViolation("terminal placement cannot fail again")
        self._fail(reason)

    def propose(
        self,
        observation: ObservationSnapshotV3,
        frame: NavigationFrame,
    ) -> PlacementProposal:
        if type(observation) is not ObservationSnapshotV3 or type(frame) is not NavigationFrame:
            raise ContractViolation("placement proposal requires formal observation and frame")
        if frame.session.value != self.requirement.world_session:
            raise ContractViolation("placement world session changed")
        if frame.body.sequence_id != observation.sequence_id:
            raise ContractViolation("placement observation and body frame differ")
        request = ObservationRequestV3("interaction_v1", (self.requirement.destination,))
        if self.report.terminal:
            return self._proposal(observation.sequence_id, None, request)
        if self._state is PlacementState.AWAITING_CONFIRMATION:
            self._observe_confirmation(observation, frame)
            return self._proposal(observation.sequence_id, None, request)
        precondition = self._precondition_failure(observation, frame)
        if precondition is not None:
            reason, failure_kind = precondition
            if failure_kind is not None:
                self._fail(reason, failure_kind=failure_kind)
            else:
                self._reason = reason
                self._last_proposed_sequence = None
            return self._proposal(observation.sequence_id, None, request)
        operation = InteractBlockV1(*self.requirement.support, self.requirement.face)
        self._reason = "ready_to_place"
        self._last_proposed_sequence = observation.sequence_id
        return self._proposal(observation.sequence_id, operation, request)

    def register_dispatch(
        self,
        proposal: PlacementProposal,
        *,
        selected: bool,
        receipt_status: str,
        receipt_reason: str,
        control_sequence: int,
    ) -> None:
        if type(proposal) is not PlacementProposal or proposal.interaction_id != self.requirement.interaction_id:
            raise ContractViolation("placement dispatch uses another proposal")
        if (proposal.operation is None
                or self._state is not PlacementState.READY
                or proposal.observation_sequence != self._last_proposed_sequence):
            raise ContractViolation("placement dispatch has no current operation")
        if type(selected) is not bool:
            raise ContractViolation("placement selection must be explicit")
        require_identifier(receipt_status, "placement receipt status")
        require_identifier(receipt_reason, "placement receipt reason")
        require_nonnegative_int(control_sequence, "placement control sequence")
        self._last_proposed_sequence = None
        if not selected:
            self._reason = "operation_not_selected"
            return
        if receipt_status != "pending_confirmation":
            self._reason = f"operation_{receipt_status}"
            if receipt_status in {"rejected", "operation_rejected", "timed_out"}:
                self._record_failed_attempt()
            return
        self._attempts += 1
        self._state = PlacementState.AWAITING_CONFIRMATION
        self._reason = "awaiting_world_confirmation"
        self._submitted_observation_sequence = proposal.observation_sequence
        self._submitted_control_sequence = control_sequence
        # Item, slot and world-tick evidence was frozen while producing this
        # exact proposal.  Dispatch only records which control sequence used it.

    def _proposal(
        self,
        sequence: int,
        operation: InteractBlockV1 | None,
        request: ObservationRequestV3,
    ) -> PlacementProposal:
        return PlacementProposal(
            self.requirement.interaction_id,
            sequence,
            self._state,
            self._reason,
            operation,
            request,
        )

    def _precondition_failure(
        self,
        observation: ObservationSnapshotV3,
        frame: NavigationFrame,
    ) -> tuple[str, PlacementFailureKind | None] | None:
        body = frame.body
        destination_box = Aabb(
            self.requirement.destination[0], self.requirement.destination[1],
            self.requirement.destination[2], self.requirement.destination[0] + 1,
            self.requirement.destination[1] + 1, self.requirement.destination[2] + 1,
        )
        if _overlaps(body.body_box, destination_box):
            return "destination_intersects_body", PlacementFailureKind.TERMINAL
        horizontal_error = math.hypot(
            body.position[0] - self.requirement.work_position[0],
            body.position[2] - self.requirement.work_position[2],
        )
        if (horizontal_error > self.requirement.work_position_tolerance
                or abs(body.position[1] - self.requirement.work_position[1]) > 0.1):
            return "work_position_not_reached", None
        if not body.is_on_ground:
            return "body_not_grounded", None
        if self.requirement.requires_sneak and not body.is_sneaking:
            return "sneak_not_confirmed", None
        if math.hypot(body.velocity_blocks_per_second[0], body.velocity_blocks_per_second[2]) > 0.08:
            return "body_not_settled", None
        support = frame.world.cell(self.requirement.support)
        destination = frame.world.cell(self.requirement.destination)
        if support.knowledge is not CellKnowledge.BLOCK:
            return (
                "support_not_known_block",
                PlacementFailureKind.WORLD_DEPENDENCY_CHANGED,
            )
        if destination.knowledge is not CellKnowledge.AIR:
            return (
                "destination_not_known_air",
                PlacementFailureKind.WORLD_DEPENDENCY_CHANGED,
            )
        if observation.inventory.status is not FieldStatusV0.VALID or observation.inventory.value is None:
            return "inventory_unavailable", None
        inventory = observation.inventory.value
        held = inventory.main_hand
        if held.empty or held.item_id != self.requirement.expected_item_id:
            return "expected_item_not_in_main_hand", PlacementFailureKind.TERMINAL
        if observation.self_state.value is None or observation.self_state.value.game_mode != "survival":
            return "unsupported_game_mode", PlacementFailureKind.TERMINAL
        targeting = observation.targeting
        if (targeting.status is not FieldStatusV0.VALID
                or type(targeting.value) is not TargetingStateV3
                or targeting.value.hit_kind != "block"
                or targeting.value.block_position != self.requirement.support
                or targeting.value.face != self.requirement.face):
            return "target_not_aligned", None
        self._submitted_item_count = held.count
        self._submitted_slot = inventory.selected_hotbar_slot
        self._submitted_world_tick = frame.body.stamp.world_tick
        return None

    def _observe_confirmation(
        self,
        observation: ObservationSnapshotV3,
        frame: NavigationFrame,
    ) -> None:
        if (self._submitted_observation_sequence is None
                or self._submitted_world_tick is None
                or self._submitted_item_count is None
                or self._submitted_slot is None):
            raise ContractViolation("placement confirmation lost its submission evidence")
        if observation.sequence_id <= self._submitted_observation_sequence:
            self._reason = "awaiting_new_observation"
            return
        destination = frame.world.cell(self.requirement.destination)
        if destination.knowledge is CellKnowledge.BLOCK:
            assert destination.block is not None
            if destination.block.material_key != self.requirement.expected_block_id:
                self._fail("unexpected_destination_block")
                return
            if observation.inventory.status is not FieldStatusV0.VALID or observation.inventory.value is None:
                self._reason = "awaiting_inventory_confirmation"
                return
            inventory = observation.inventory.value
            if inventory.selected_hotbar_slot != self._submitted_slot:
                self._fail("selected_slot_changed")
                return
            held = inventory.main_hand
            actual_count = 0 if held.empty else held.count
            actual_id = None if held.empty else held.item_id
            if actual_count != self._submitted_item_count - 1:
                self._fail("inventory_count_mismatch")
                return
            if actual_count > 0 and actual_id != self.requirement.expected_item_id:
                self._fail("inventory_item_changed")
                return
            self._state = PlacementState.COMPLETE
            self._reason = "placement_confirmed"
            return
        elapsed = frame.body.stamp.world_tick - self._submitted_world_tick
        if elapsed >= self.requirement.confirmation_timeout_ticks:
            if self._attempts < self.requirement.maximum_attempts:
                self._state = PlacementState.READY
                self._reason = "confirmation_timeout_retry"
                self._clear_submission()
            else:
                self._fail("confirmation_timeout")
        else:
            self._reason = "awaiting_world_confirmation"

    def _record_failed_attempt(self) -> None:
        self._attempts += 1
        if self._attempts >= self.requirement.maximum_attempts:
            self._fail("operation_attempts_exhausted")
        else:
            self._state = PlacementState.READY

    def _clear_submission(self) -> None:
        self._submitted_observation_sequence = None
        self._submitted_world_tick = None
        self._submitted_item_count = None
        self._submitted_slot = None
        self._submitted_control_sequence = None

    def _fail(
        self,
        reason: str,
        *,
        failure_kind: PlacementFailureKind = PlacementFailureKind.TERMINAL,
    ) -> None:
        if type(failure_kind) is not PlacementFailureKind:
            raise ContractViolation("placement failure kind must be typed")
        self._state = PlacementState.FAILED
        self._reason = reason
        self._failure_kind = failure_kind
        self._last_proposed_sequence = None
