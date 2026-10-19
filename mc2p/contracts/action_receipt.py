"""Immutable native client execution evidence; dispatch is not server confirmation."""
from dataclasses import dataclass, fields
from typing import TypeAlias

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int


@dataclass(frozen=True, slots=True)
class ClientInputApplicationV1:
    """Exact input values consumed by one real player movement tick."""

    schema_version: str
    movement_tick_id: int
    episode_id: str | None
    request_sequence_id: int | None
    sampled_at_jvm_ns: int
    state: str
    forward: float
    strafe: float
    jump: bool
    sneak: bool
    sprint: bool

    def __post_init__(self) -> None:
        if self.schema_version != "mc2p.input-application.v1":
            raise ContractViolation("invalid input application schema")
        require_nonnegative_int(self.movement_tick_id, "movement tick id")
        if (self.episode_id is None) != (self.request_sequence_id is None):
            raise ContractViolation("input application owner identity is partial")
        if self.episode_id is not None:
            require_identifier(self.episode_id, "episode id")
            require_nonnegative_int(self.request_sequence_id, "request sequence id")
        require_nonnegative_int(self.sampled_at_jvm_ns, "input sample time")
        if self.state not in {"leased", "neutral", "disallowed", "expired", "lease_exhausted"}:
            raise ContractViolation("invalid input application state")
        for name in ("forward", "strafe"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not -1.0 <= float(value) <= 1.0:
                raise ContractViolation(f"{name} must be within -1..1")
        for name in ("jump", "sneak", "sprint"):
            if type(getattr(self, name)) is not bool:
                raise ContractViolation(f"{name} must be boolean")
        if self.state in {"leased", "neutral"} and self.episode_id is None:
            raise ContractViolation("active input application requires an owner")
        if (self.state in {"disallowed", "expired", "lease_exhausted"}
                and (float(self.forward) != 0.0 or float(self.strafe) != 0.0
                     or self.jump or self.sneak or self.sprint)):
            raise ContractViolation("inactive input application must be neutral")

    @classmethod
    def from_mapping(cls, value: object) -> "ClientInputApplicationV1":
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ContractViolation("invalid input application fields")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ClientBehaviorReceiptV2:
    schema_version: str
    generation_id: int
    execution_path: str
    episode_id: str | None
    request_sequence_id: int | None
    status: str
    reason: str
    execution_thread: str
    on_client_thread: bool
    execution_phase: str
    world_tick: int
    action_keyboard_callbacks: int
    action_mouse_callbacks: int
    handled_screen_render_attempts: int
    handled_screen_render_completions: int
    input_samples: int
    leased_input_samples: int

    def __post_init__(self) -> None:
        if (self.schema_version != "mc2p.client_action_receipt.v2"
                or self.execution_path != "client_behavior_v1"
                or self.execution_phase != "client_tick_action_boundary"):
            raise ContractViolation("invalid behavior receipt schema/path/phase")
        for name in ("generation_id", "world_tick", "action_keyboard_callbacks", "action_mouse_callbacks",
                     "handled_screen_render_attempts", "handled_screen_render_completions",
                     "input_samples", "leased_input_samples"):
            require_nonnegative_int(getattr(self, name), name)
        if self.leased_input_samples > self.input_samples:
            raise ContractViolation("leased input samples exceed all input samples")
        if self.episode_id is not None:
            require_identifier(self.episode_id, "episode_id")
        if self.request_sequence_id is not None:
            require_nonnegative_int(self.request_sequence_id, "request_sequence_id")
        if type(self.on_client_thread) is not bool or type(self.execution_thread) is not str:
            raise ContractViolation("invalid client thread evidence")
        if type(self.status) is not str or self.status not in {
                "idle", "executed", "pending_confirmation", "confirmed_local",
                "operation_rejected", "rejected", "timed_out", "cancelled"}:
            raise ContractViolation("invalid behavior result state")
        require_identifier(self.reason, "reason")

    @classmethod
    def from_mapping(cls, value: object) -> "ClientBehaviorReceiptV2":
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ContractViolation("invalid behavior receipt fields")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ClientBehaviorReceiptV3:
    schema_version: str
    generation_id: int
    execution_path: str
    episode_id: str | None
    request_sequence_id: int | None
    status: str
    reason: str
    execution_thread: str
    on_client_thread: bool
    execution_phase: str
    world_tick: int
    action_keyboard_callbacks: int
    action_mouse_callbacks: int
    handled_screen_render_attempts: int
    handled_screen_render_completions: int
    input_samples: int
    leased_input_samples: int
    input_applications: tuple[ClientInputApplicationV1, ...]
    dropped_input_samples: int = 0
    oldest_retained_input_tick: int = 0

    def __post_init__(self) -> None:
        if self.schema_version != "mc2p.client_action_receipt.v3":
            raise ContractViolation("invalid behavior receipt schema")
        legacy = ClientBehaviorReceiptV2(
            "mc2p.client_action_receipt.v2", self.generation_id,
            self.execution_path, self.episode_id, self.request_sequence_id,
            self.status, self.reason, self.execution_thread,
            self.on_client_thread, self.execution_phase, self.world_tick,
            self.action_keyboard_callbacks, self.action_mouse_callbacks,
            self.handled_screen_render_attempts,
            self.handled_screen_render_completions,
            self.input_samples, self.leased_input_samples,
        )
        if (type(self.input_applications) is not tuple
                or len(self.input_applications) > 64
                or any(type(item) is not ClientInputApplicationV1
                       for item in self.input_applications)):
            raise ContractViolation("invalid input application batch")
        require_nonnegative_int(
            self.dropped_input_samples, "dropped input samples",
        )
        require_nonnegative_int(
            self.oldest_retained_input_tick, "oldest retained input tick",
        )
        if self.dropped_input_samples > self.input_samples:
            raise ContractViolation("dropped input samples exceed all input samples")
        ticks = tuple(item.movement_tick_id for item in self.input_applications)
        if ticks != tuple(sorted(set(ticks))):
            raise ContractViolation("input applications must be ordered and unique")
        if ticks and ticks[-1] > self.input_samples:
            raise ContractViolation("movement tick exceeds input sample count")
        del legacy

    @property
    def last_input_sample(self) -> ClientInputApplicationV1 | None:
        return self.input_applications[-1] if self.input_applications else None

    @classmethod
    def from_mapping(cls, value: object) -> "ClientBehaviorReceiptV3":
        names = {f.name for f in fields(cls)}
        legacy_names = names - {
            "dropped_input_samples", "oldest_retained_input_tick",
        }
        if (type(value) is not dict
                or frozenset(value) not in {frozenset(names), frozenset(legacy_names)}):
            raise ContractViolation("invalid behavior receipt fields")
        data = dict(value)
        data.setdefault("dropped_input_samples", 0)
        data.setdefault("oldest_retained_input_tick", 0)
        if type(data["input_applications"]) is not list:
            raise ContractViolation("input application batch must be a list")
        data["input_applications"] = tuple(
            ClientInputApplicationV1.from_mapping(item)
            for item in data["input_applications"]
        )
        return cls(**data)


ClientBehaviorReceipt: TypeAlias = ClientBehaviorReceiptV2 | ClientBehaviorReceiptV3


def behavior_receipt_from_mapping(value: object) -> ClientBehaviorReceipt:
    if type(value) is not dict:
        raise ContractViolation("behavior receipt mapping is required")
    schema = value.get("schema_version")
    if schema == "mc2p.client_action_receipt.v2":
        return ClientBehaviorReceiptV2.from_mapping(value)
    if schema == "mc2p.client_action_receipt.v3":
        return ClientBehaviorReceiptV3.from_mapping(value)
    raise ContractViolation("unsupported behavior receipt schema")
