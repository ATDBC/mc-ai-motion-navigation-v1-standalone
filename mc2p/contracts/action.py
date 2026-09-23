"""Player Runtime V0 action intent and complete snapshot contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from mc2p.contracts.common import (
    ContractViolation,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)


class ActionPriorityV0(IntEnum):
    BEHAVIOR = 100
    TASK = 200
    PLAYER = 300
    SAFETY = 400


@dataclass(frozen=True, slots=True)
class LocomotionActionV0:
    forward: bool = False
    back: bool = False
    left: bool = False
    right: bool = False
    jump: bool = False
    sneak: bool = False
    sprint: bool = False

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if type(getattr(self, name)) is not bool:
                raise ContractViolation(f"locomotion {name} must be bool")
        if self.forward and self.back:
            raise ContractViolation("forward and back cannot both be active")
        if self.left and self.right:
            raise ContractViolation("left and right cannot both be active")


@dataclass(frozen=True, slots=True)
class CameraActionV0:
    pitch_delta: float = 0.0
    yaw_delta: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("pitch_delta", self.pitch_delta),
            ("yaw_delta", self.yaw_delta),
        ):
            require_finite(value, name)
            if not -180.0 <= float(value) <= 180.0:
                raise ContractViolation(f"{name} must be within [-180, 180]")


@dataclass(frozen=True, slots=True)
class InteractionActionV0:
    attack: bool = False
    use: bool = False

    def __post_init__(self) -> None:
        if type(self.attack) is not bool or type(self.use) is not bool:
            raise ContractViolation("interaction values must be bool")


@dataclass(frozen=True, slots=True)
class HotbarActionV0:
    selected_slot: int | None = None

    def __post_init__(self) -> None:
        if self.selected_slot is not None and (
            type(self.selected_slot) is not int
            or not 1 <= self.selected_slot <= 9
        ):
            raise ContractViolation("hotbar selected_slot must be 1 through 9")


@dataclass(frozen=True, slots=True)
class GuiActionV0:
    drop: bool = False
    inventory: bool = False

    def __post_init__(self) -> None:
        if type(self.drop) is not bool or type(self.inventory) is not bool:
            raise ContractViolation("GUI values must be bool")


@dataclass(frozen=True, slots=True)
class ActionIntentV0:
    intent_id: str
    source_id: str
    priority: ActionPriorityV0
    submitted_at_monotonic_ns: int
    expires_at_monotonic_ns: int
    locomotion: LocomotionActionV0 | None = None
    camera: CameraActionV0 | None = None
    interaction: InteractionActionV0 | None = None
    hotbar: HotbarActionV0 | None = None
    gui: GuiActionV0 | None = None
    schema_version: str = field(default="mc2p.action-intent.v0", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.intent_id, "intent_id")
        require_identifier(self.source_id, "source_id")
        if not isinstance(self.priority, ActionPriorityV0):
            raise ContractViolation("priority must be ActionPriorityV0")
        require_nonnegative_int(
            self.submitted_at_monotonic_ns,
            "submitted_at_monotonic_ns",
        )
        require_nonnegative_int(
            self.expires_at_monotonic_ns,
            "expires_at_monotonic_ns",
        )
        if self.expires_at_monotonic_ns <= self.submitted_at_monotonic_ns:
            raise ContractViolation("intent expires_at must follow submitted_at")
        groups = self.claimed_groups
        if not groups:
            raise ContractViolation("intent must claim at least one control group")
        expected = {
            "locomotion": LocomotionActionV0,
            "camera": CameraActionV0,
            "interaction": InteractionActionV0,
            "hotbar": HotbarActionV0,
            "gui": GuiActionV0,
        }
        for name in groups:
            if not isinstance(getattr(self, name), expected[name]):
                raise ContractViolation(f"{name} has invalid action group type")

    @property
    def claimed_groups(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "locomotion",
                "camera",
                "interaction",
                "hotbar",
                "gui",
            )
            if getattr(self, name) is not None
        )


@dataclass(frozen=True, slots=True)
class ActionSnapshotV0:
    action_sequence_id: int
    locomotion: LocomotionActionV0
    camera: CameraActionV0
    interaction: InteractionActionV0
    hotbar: HotbarActionV0
    gui: GuiActionV0
    schema_version: str = field(default="mc2p.action-snapshot.v0", init=False)

    def __post_init__(self) -> None:
        require_nonnegative_int(self.action_sequence_id, "action_sequence_id")
        for value, expected in (
            (self.locomotion, LocomotionActionV0),
            (self.camera, CameraActionV0),
            (self.interaction, InteractionActionV0),
            (self.hotbar, HotbarActionV0),
            (self.gui, GuiActionV0),
        ):
            if not isinstance(value, expected):
                raise ContractViolation("action snapshot groups must be complete")

    @classmethod
    def neutral(cls, action_sequence_id: int) -> ActionSnapshotV0:
        return cls(
            action_sequence_id=action_sequence_id,
            locomotion=LocomotionActionV0(),
            camera=CameraActionV0(),
            interaction=InteractionActionV0(),
            hotbar=HotbarActionV0(),
            gui=GuiActionV0(),
        )
