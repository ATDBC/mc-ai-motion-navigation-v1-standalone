"""Deterministic group-level action arbitration."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading

from mc2p.contracts.action import (
    ActionIntentV0,
    ActionSnapshotV0,
    CameraActionV0,
    GuiActionV0,
    HotbarActionV0,
    InteractionActionV0,
    LocomotionActionV0,
)
from mc2p.contracts.common import (
    ContractViolation,
    require_identifier,
    require_nonnegative_int,
)


_CONTROL_GROUPS = (
    "locomotion",
    "camera",
    "interaction",
    "hotbar",
    "gui",
)


@dataclass(frozen=True, slots=True)
class ArbitrationDecisionV0:
    action: ActionSnapshotV0
    selected_intents: tuple[tuple[str, str], ...]
    candidate_intent_ids: tuple[str, ...]
    expired_intent_ids: tuple[str, ...]
    schema_version: str = field(default="mc2p.arbitration-decision.v0", init=False)


class ActionArbiterV0:
    """Resolve independently owned control groups into one full snapshot."""

    def __init__(self) -> None:
        self._intents: dict[str, ActionIntentV0] = {}
        self._lock = threading.RLock()

    def submit(self, intent: ActionIntentV0) -> None:
        if not isinstance(intent, ActionIntentV0):
            raise ContractViolation("arbiter accepts ActionIntentV0 only")
        with self._lock:
            self._intents[intent.intent_id] = intent

    def cancel_source(self, source_id: str) -> tuple[str, ...]:
        require_identifier(source_id, "source_id")
        with self._lock:
            removed = tuple(
                sorted(
                    intent_id
                    for intent_id, intent in self._intents.items()
                    if intent.source_id == source_id
                )
            )
            for intent_id in removed:
                del self._intents[intent_id]
            return removed

    def clear(self) -> None:
        with self._lock:
            self._intents.clear()

    def resolve(
        self,
        now_ns: int,
        action_sequence_id: int,
    ) -> ArbitrationDecisionV0:
        require_nonnegative_int(now_ns, "now_ns")
        require_nonnegative_int(action_sequence_id, "action_sequence_id")
        with self._lock:
            expired = tuple(
                sorted(
                    intent_id
                    for intent_id, intent in self._intents.items()
                    if intent.expires_at_monotonic_ns <= now_ns
                )
            )
            for intent_id in expired:
                del self._intents[intent_id]
            active = tuple(self._intents.values())

        group_values: dict[str, object] = {
            "locomotion": LocomotionActionV0(),
            "camera": CameraActionV0(),
            "interaction": InteractionActionV0(),
            "hotbar": HotbarActionV0(),
            "gui": GuiActionV0(),
        }
        selected: list[tuple[str, str]] = []
        for group in _CONTROL_GROUPS:
            candidates = [
                intent for intent in active if getattr(intent, group) is not None
            ]
            if not candidates:
                continue
            winner = max(
                candidates,
                key=lambda intent: (
                    int(intent.priority),
                    intent.submitted_at_monotonic_ns,
                    intent.intent_id,
                ),
            )
            group_values[group] = getattr(winner, group)
            selected.append((group, winner.intent_id))

        action = ActionSnapshotV0(
            action_sequence_id=action_sequence_id,
            locomotion=group_values["locomotion"],  # type: ignore[arg-type]
            camera=group_values["camera"],  # type: ignore[arg-type]
            interaction=group_values["interaction"],  # type: ignore[arg-type]
            hotbar=group_values["hotbar"],  # type: ignore[arg-type]
            gui=group_values["gui"],  # type: ignore[arg-type]
        )
        return ArbitrationDecisionV0(
            action=action,
            selected_intents=tuple(selected),
            candidate_intent_ids=tuple(
                sorted(intent.intent_id for intent in active)
            ),
            expired_intent_ids=expired,
        )

