"""Formal client behavior values. V0 key actions are deliberately not convertible here.

Movement is a complete input snapshot, look is a one-shot delta in degrees, and
one discrete operation may be dispatched per cycle. A lease never repeats it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

from mc2p.contracts.action import ActionPriorityV0

from mc2p.contracts.common import (
    ContractViolation, require_finite, require_identifier, require_nonnegative_int,
)


@dataclass(frozen=True, slots=True)
class MovementV1:
    forward: int = 0  # -1 back, 0 neutral, +1 forward
    strafe: int = 0  # -1 right, 0 neutral, +1 left
    jump: bool = False
    sneak: bool = False
    sprint: bool = False

    def __post_init__(self) -> None:
        for name in ("forward", "strafe"):
            value = getattr(self, name)
            if type(value) is not int or value not in (-1, 0, 1):
                raise ContractViolation(f"{name} must be -1, 0 or 1")
        for name in ("jump", "sneak", "sprint"):
            if type(getattr(self, name)) is not bool:
                raise ContractViolation(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class LookV1:
    yaw_delta_degrees: float = 0.0
    pitch_delta_degrees: float = 0.0

    def __post_init__(self) -> None:
        for name in ("yaw_delta_degrees", "pitch_delta_degrees"):
            value = getattr(self, name)
            require_finite(value, name)
            if not -180 <= value <= 180:
                raise ContractViolation(f"{name} outside [-180,180]")


@dataclass(frozen=True, slots=True)
class OpenInventoryV1:
    kind: str = field(default="open_inventory", init=False)


@dataclass(frozen=True, slots=True)
class CloseScreenV1:
    kind: str = field(default="close_screen", init=False)


@dataclass(frozen=True, slots=True)
class SelectHotbarV1:
    slot: int
    kind: str = field(default="select_hotbar", init=False)

    def __post_init__(self) -> None:
        if type(self.slot) is not int or not 0 <= self.slot <= 8:
            raise ContractViolation("hotbar slot must be 0 through 8")


@dataclass(frozen=True, slots=True)
class ClickSlotV1:
    gui_session_id: str
    sync_id: int
    expected_revision: int
    slot: int
    button: int
    click_type: str
    kind: str = field(default="click_slot", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.gui_session_id, "gui_session_id")
        if len(self.gui_session_id) > 128:
            raise ContractViolation("GUI session identifier exceeds 128 characters")
        for name in ("sync_id", "expected_revision", "slot", "button"):
            require_nonnegative_int(getattr(self, name), name)
        for name in ("sync_id", "expected_revision"):
            if getattr(self, name) > 2**31 - 1:
                raise ContractViolation(f"{name} exceeds signed 32-bit range")
        # Drag/crafting automation and creative clone are not implemented primitives.
        buttons = {"pickup": (0, 1), "quick_move": (0, 1), "swap": tuple(range(9)),
                   "throw": (0, 1), "pickup_all": (0, 1)}
        if self.click_type not in buttons or self.button not in buttons[self.click_type]:
            raise ContractViolation("unsupported click type/button combination")
        if self.slot > 1023:
            raise ContractViolation("slot exceeds bounded handler size")


@dataclass(frozen=True, slots=True)
class InteractBlockV1:
    # Expected world-grid target, not a permission to query/act at arbitrary coordinates.
    # The actual hit point and hand dispatch come only from normal client raycast/use logic.
    block_x: int
    block_y: int
    block_z: int
    face: str
    kind: str = field(default="interact_block", init=False)

    def __post_init__(self) -> None:
        for name in ("block_x", "block_y", "block_z"):
            value = getattr(self, name)
            if type(value) is not int or not -30_000_000 <= value <= 30_000_000:
                raise ContractViolation(f"{name} exceeds bounded world-grid coordinates")
        if type(self.face) is not str or self.face not in {"down", "up", "north", "south", "west", "east"}:
            raise ContractViolation("invalid block interaction face")


@dataclass(frozen=True, slots=True)
class MineBlockV1(InteractBlockV1):
    """Install/renew bounded mining of this observed target; never an instant-success macro."""
    kind: str = field(default="mine_block", init=False)


@dataclass(frozen=True, slots=True)
class AttackEntityV1:
    entity_ref: str
    minimum_cooldown_progress: float = 1.0
    kind: str = field(default="attack_entity", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.entity_ref, "attack entity reference")
        if len(self.entity_ref) > 128:
            raise ContractViolation("attack entity reference exceeds 128 characters")
        require_finite(self.minimum_cooldown_progress, "minimum attack cooldown progress")
        if type(self.minimum_cooldown_progress) not in (int, float):
            raise ContractViolation("minimum attack cooldown progress must be numeric")
        if not 0.0 <= self.minimum_cooldown_progress <= 1.0:
            raise ContractViolation("minimum attack cooldown progress must be in [0,1]")


BehaviorOperationV1: TypeAlias = (
    OpenInventoryV1 | CloseScreenV1 | SelectHotbarV1 | ClickSlotV1
    | InteractBlockV1 | MineBlockV1 | AttackEntityV1
)
_OPERATION_TYPES = (
    OpenInventoryV1, CloseScreenV1, SelectHotbarV1, ClickSlotV1,
    InteractBlockV1, MineBlockV1, AttackEntityV1,
)


@dataclass(frozen=True, slots=True)
class ActionIntentV1:
    """Movement may persist; look deltas and operations are one-shot proposals."""

    intent_id: str
    source_id: str
    episode_id: str
    observation_sequence_id: int
    priority: ActionPriorityV0
    submitted_at_monotonic_ns: int
    expires_at_monotonic_ns: int
    movement: MovementV1 | None = None
    look: LookV1 | None = None
    operation: BehaviorOperationV1 | None = None
    valid_for_ticks: int = 1
    movement_requires_look: bool = False  # Internal arbitration dependency; not a client wire field.
    movement_look_tolerance_degrees: float = 0.0
    schema_version: str = field(default="mc2p.action-intent.v1", init=False)

    def __post_init__(self) -> None:
        for name in ("intent_id", "source_id", "episode_id"):
            require_identifier(getattr(self, name), name)
            if len(getattr(self, name)) > 128:
                raise ContractViolation(f"{name} exceeds 128 characters")
        for name in ("observation_sequence_id", "submitted_at_monotonic_ns", "expires_at_monotonic_ns"):
            value = getattr(self, name)
            require_nonnegative_int(value, name)
            if value > 2**63 - 1:
                raise ContractViolation(f"{name} exceeds signed 64-bit range")
        if self.expires_at_monotonic_ns <= self.submitted_at_monotonic_ns:
            raise ContractViolation("intent expiry must follow submission")
        if type(self.priority) is not ActionPriorityV0:
            raise ContractViolation("intent requires the four-level ActionPriorityV0 policy")
        for name, expected in (("movement", MovementV1), ("look", LookV1)):
            if getattr(self, name) is not None and type(getattr(self, name)) is not expected:
                raise ContractViolation(f"intent {name} requires formal V1 controls")
        if self.operation is not None and type(self.operation) not in _OPERATION_TYPES:
            raise ContractViolation("unsupported formal operation type")
        if self.movement is None and self.look is None and self.operation is None:
            raise ContractViolation("intent must claim a control group")
        if type(self.valid_for_ticks) is not int or not 1 <= self.valid_for_ticks <= 20:
            raise ContractViolation("valid_for_ticks must be 1 through 20")
        if type(self.movement_requires_look) is not bool:
            raise ContractViolation("movement_requires_look must be bool")
        if self.movement_requires_look and (self.movement is None or self.look is None or self.valid_for_ticks != 1):
            raise ContractViolation("heading-bound movement requires both controls and one tick")
        require_finite(
            self.movement_look_tolerance_degrees,
            "movement look tolerance",
        )
        if not 0.0 <= self.movement_look_tolerance_degrees <= 5.0:
            raise ContractViolation("movement look tolerance must be in [0,5] degrees")
        if not self.movement_requires_look and self.movement_look_tolerance_degrees != 0.0:
            raise ContractViolation("only heading-bound movement may tolerate another look")


@dataclass(frozen=True, slots=True)
class ActionSnapshotV1:
    episode_id: str
    request_sequence_id: int
    observation_sequence_id: int
    deadline_monotonic_ns: int
    movement: MovementV1 = field(default_factory=MovementV1)
    look: LookV1 = field(default_factory=LookV1)
    operation: BehaviorOperationV1 | None = None
    valid_for_ticks: int = 1
    cancel_request_sequence_id: int | None = None
    schema_version: str = field(default="mc2p.action-snapshot.v1", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.episode_id, "episode_id")
        if len(self.episode_id) > 128:
            raise ContractViolation("episode identifier exceeds 128 characters")
        for name in ("request_sequence_id", "observation_sequence_id", "deadline_monotonic_ns"):
            value = getattr(self, name)
            require_nonnegative_int(value, name)
            if value > 2**63 - 1:
                raise ContractViolation(f"{name} exceeds signed 64-bit range")
        if type(self.valid_for_ticks) is not int or not 1 <= self.valid_for_ticks <= 20:
            raise ContractViolation("valid_for_ticks must be 1 through 20")
        if type(self.movement) is not MovementV1 or type(self.look) is not LookV1:
            raise ContractViolation("formal controls require V1 types")
        if self.operation is not None and type(self.operation) not in _OPERATION_TYPES:
            raise ContractViolation("unsupported formal operation type")
        cancel = self.cancel_request_sequence_id
        if cancel is not None:
            require_nonnegative_int(cancel, "cancel_request_sequence_id")
            if cancel >= self.request_sequence_id:
                raise ContractViolation("cancel must reference an earlier request")
            if self.operation is not None or self.movement != MovementV1() or self.look != LookV1():
                raise ContractViolation("cancel cannot introduce new controls or operations")
