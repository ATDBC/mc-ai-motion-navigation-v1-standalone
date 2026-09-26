"""Bounded formal arbitration: persistent movement, never replayed deltas/operations."""
from dataclasses import dataclass, field, replace
import threading

from mc2p.contracts.action_v1 import (
    ActionIntentV1, ActionSnapshotV1, AttackEntityV1, LookV1,
    MovementTickWindowV1, MovementV1,
)
from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.contracts.intent_source import IntentSourceV1, OrderedIntentV1, ORDERED_PREFIX
from mc2p.runtime.intent_sources import OrderedSourceRegistry

_FORBIDDABLE = frozenset({"movement", "look", "operation", "open_inventory", "close_screen",
                         "interact_block", "mine_block", "click_slot", "select_hotbar",
                         "attack_entity"})


@dataclass(frozen=True, slots=True)
class ArbitrationDecisionV1:
    action: ActionSnapshotV1
    selected_intents: tuple[tuple[str, str], ...] = ()
    candidate_intent_ids: tuple[str, ...] = ()
    suppressed_intents: tuple[tuple[str, str], ...] = ()
    movement_tick_window: MovementTickWindowV1 | None = None
    schema_version: str = field(default="mc2p.arbitration-decision.v1", init=False)


def _rank(intent: ActionIntentV1) -> tuple[int, int, str]:
    return int(intent.priority), intent.submitted_at_monotonic_ns, intent.intent_id


def _heading_compatible(
    movement: ActionIntentV1,
    look: ActionIntentV1 | None,
) -> bool:
    if look is movement:
        return True
    tolerance = movement.movement_look_tolerance_degrees
    if (look is None or tolerance <= 0.0 or movement.look is None or look.look is None
            or movement.observation_sequence_id != look.observation_sequence_id):
        return False
    difference = abs(
        (movement.look.yaw_delta_degrees - look.look.yaw_delta_degrees + 180.0)
        % 360.0 - 180.0
    )
    return difference <= tolerance


def _within_observed_yaw_limit(
    movement: ActionIntentV1,
    look: ActionIntentV1 | None,
    observation_sequence_id: int,
) -> tuple[bool, str | None]:
    limit = movement.movement_observed_yaw_limit_degrees
    if limit is None:
        return True, None
    if movement.observation_sequence_id != observation_sequence_id:
        return False, "movement_observation_stale"
    if look is None:
        return True, None
    if look.observation_sequence_id != movement.observation_sequence_id:
        return False, "movement_observation_stale"
    yaw_change = abs(
        (look.look.yaw_delta_degrees + 180.0) % 360.0 - 180.0
    )
    if yaw_change > limit:
        return False, "observed_yaw_limit_exceeded"
    return True, None


class ActionArbiterV1:
    def __init__(self) -> None:
        self._intents: dict[str, ActionIntentV1] = {}
        self._seen: set[str] = set()
        self._ordered_sources = OrderedSourceRegistry()
        self._lock = threading.RLock()

    def submit(self, intent: ActionIntentV1) -> None:
        if type(intent) is not ActionIntentV1:
            raise ContractViolation("formal arbiter accepts ActionIntentV1 only")
        if intent.intent_id.startswith(ORDERED_PREFIX) or intent.source_id.startswith(ORDERED_PREFIX):
            raise ContractViolation('ordered namespace requires the ordered submission boundary')
        with self._lock:
            if intent.intent_id in self._seen:
                raise ContractViolation("intent id already submitted in this episode")
            if len(self._intents) >= 128 or len(self._seen) >= 4096:
                raise ContractViolation("formal intent capacity exceeded")
            self._intents[intent.intent_id] = intent
            self._seen.add(intent.intent_id)

    @property
    def ordered_source_stats(self) -> dict[str, int]:
        with self._lock:
            return {'retained_slots': self._ordered_sources.retained_slots,
                    'active_sources': self._ordered_sources.active_sources,
                    'active_intents': len(self._intents)}

    def register_ordered_source(self, label: str, *, episode_id: str) -> IntentSourceV1:
        with self._lock:
            return self._ordered_sources.register(label, episode_id)

    def submit_ordered_intent(self, envelope: OrderedIntentV1) -> None:
        with self._lock:
            slot = self._ordered_sources.validate_submission(envelope)
            if len(self._intents) >= 128:
                raise ContractViolation('formal intent capacity exceeded')
            self._intents[envelope.intent.intent_id] = envelope.intent
            slot.last_sequence = envelope.sequence

    def unregister_ordered_source(self, source: IntentSourceV1) -> tuple[str, ...]:
        with self._lock:
            self._ordered_sources.validate_source(source)
            removed = self.cancel_source(source.source_id)
            self._ordered_sources.unregister(source)
            return removed

    def cancel_source(self, source_id: str) -> tuple[str, ...]:
        require_identifier(source_id, "source_id")
        with self._lock:
            removed = tuple(sorted(key for key, intent in self._intents.items() if intent.source_id == source_id))
            for key in removed:
                del self._intents[key]
            return removed

    def clear(self) -> None:
        with self._lock:
            self._intents.clear()
            self._seen.clear()
            self._ordered_sources = OrderedSourceRegistry()

    def resolve(self, now_ns: int, episode_id: str, observation_sequence_id: int,
                request_sequence_id: int, deadline_monotonic_ns: int, *,
                controls_blocked: bool = False, forbidden_actions: tuple[str, ...] = ()) -> ArbitrationDecisionV1:
        require_nonnegative_int(now_ns, "now_ns")
        # Validate the complete envelope before consuming any once-only proposal.
        neutral = ActionSnapshotV1(episode_id, request_sequence_id, observation_sequence_id, deadline_monotonic_ns)
        if now_ns >= deadline_monotonic_ns:
            raise TimeoutError("arbitration deadline expired")
        if type(controls_blocked) is not bool:
            raise ContractViolation("controls_blocked must be bool")
        if (type(forbidden_actions) is not tuple
                or any(type(v) is not str or v not in _FORBIDDABLE for v in forbidden_actions)):
            raise ContractViolation("unsupported formal forbidden_actions vocabulary")
        with self._lock:
            suppressed: list[tuple[str, str]] = []
            for key, intent in tuple(self._intents.items()):
                reason = "wrong_episode" if intent.episode_id != episode_id else (
                    "expired" if intent.expires_at_monotonic_ns <= now_ns else None)
                if reason:
                    suppressed.append((key, reason))
                    del self._intents[key]
            active = tuple(i for i in self._intents.values() if i.submitted_at_monotonic_ns <= now_ns)
            winners: dict[str, ActionIntentV1] = {}
            for group in ("movement", "look", "operation"):
                candidates = []
                for intent in active:
                    if getattr(intent, group) is None:
                        continue
                    reason = None
                    constraint = group if group in forbidden_actions else (
                        intent.operation.kind if group == "operation" and intent.operation.kind in forbidden_actions else None)
                    if constraint is not None:
                        reason = "task_forbidden_" + constraint
                    elif group != "movement" and intent.observation_sequence_id != observation_sequence_id:
                        reason = "stale_observation"
                    elif controls_blocked and group in {"movement", "look"}:
                        reason = "gui_controls_blocked"
                    if reason:
                        suppressed.append((intent.intent_id, reason))
                    else:
                        candidates.append(intent)
                if candidates:
                    winners[group] = max(candidates, key=_rank)
                    suppressed.extend((i.intent_id, "lower_priority_" + group)
                                      for i in candidates if i is not winners[group])
            operation = winners.get("operation")
            nonneutral = [winners[g] for g, empty in (("movement", MovementV1()), ("look", LookV1()))
                          if g in winners and getattr(winners[g], g) != empty]
            if (operation is not None and nonneutral
                    and type(operation.operation) is not AttackEntityV1):
                if _rank(operation) >= max(map(_rank, nonneutral)):
                    for group in ("movement", "look"):
                        if group in winners:
                            suppressed.append((winners.pop(group).intent_id, "operation_control_conflict"))
                else:
                    suppressed.append((winners.pop("operation").intent_id, "operation_control_conflict"))
            movement = winners.get("movement")
            if (movement is not None and movement.movement_requires_look
                    and not _heading_compatible(movement, winners.get("look"))):
                suppressed.append((winners.pop("movement").intent_id, "required_look_not_selected"))
            movement = winners.get("movement")
            if movement is not None and movement.movement_conditioned_look_intent_id is not None:
                selected_look = winners.get("look")
                if (selected_look is None
                        or selected_look.intent_id
                           != movement.movement_conditioned_look_intent_id
                        or selected_look.observation_sequence_id
                           != movement.observation_sequence_id):
                    suppressed.append((
                        winners.pop("movement").intent_id,
                        "conditioned_look_not_selected",
                    ))
            movement = winners.get("movement")
            if movement is not None:
                compatible, reason = _within_observed_yaw_limit(
                    movement, winners.get("look"), observation_sequence_id,
                )
                if not compatible:
                    suppressed.append((
                        winners.pop("movement").intent_id,
                        reason or "observed_yaw_limit_exceeded",
                    ))
            movement = winners.get("movement")
            action = replace(neutral,
                movement=winners["movement"].movement if "movement" in winners else MovementV1(),
                look=winners["look"].look if "look" in winners else LookV1(),
                operation=winners["operation"].operation if "operation" in winners else None,
                valid_for_ticks=min((i.valid_for_ticks for i in winners.values()), default=1),
                deadline_monotonic_ns=min([deadline_monotonic_ns] + [i.expires_at_monotonic_ns for i in winners.values()]))
            # Consume all current one-shot proposals, including losers: no delayed stale GUI click/look.
            for intent in active:
                if (intent.movement is None or intent.movement_requires_look
                        or intent.movement_observed_yaw_limit_degrees is not None
                        or intent.movement_conditioned_look_intent_id is not None
                        or intent.movement_tick_window is not None):
                    del self._intents[intent.intent_id]
                elif intent.look is not None or intent.operation is not None:
                    self._intents[intent.intent_id] = replace(intent, look=None, operation=None)
            return ArbitrationDecisionV1(
                action,
                tuple((g, i.intent_id) for g, i in winners.items()),
                tuple(sorted(i.intent_id for i in active)),
                tuple(sorted(set(suppressed))),
                movement.movement_tick_window if movement is not None else None,
            )
