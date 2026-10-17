"""At most 64 source tombstones per scope. Caller owns the arbiter lock."""
from dataclasses import dataclass
from uuid import uuid4

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.contracts.intent_source import (
    IntentSourceV1, OrderedIntentV1, ORDERED_PREFIX, ORDERED_SOURCE_CAPACITY,
    require_ordered_counter,
)


@dataclass(slots=True)
class _SourceSlot:
    generation: int = 0
    source: IntentSourceV1 | None = None
    label: str | None = None
    last_sequence: int = 0


class OrderedSourceRegistry:
    def __init__(self) -> None:
        self._scope_id = uuid4().hex
        self._episode_id: str | None = None
        self._slots: list[_SourceSlot] = []

    @property
    def retained_slots(self) -> int:
        return len(self._slots)

    @property
    def active_sources(self) -> int:
        return sum(slot.source is not None for slot in self._slots)

    def register(self, label: str, episode_id: str) -> IntentSourceV1:
        require_identifier(label, 'ordered source label')
        require_identifier(episode_id, 'episode_id')
        if len(label) > 80 or len(episode_id) > 128:
            raise ContractViolation('ordered source label/episode exceeds its identifier budget')
        if self._episode_id is not None and episode_id != self._episode_id:
            raise ContractViolation('ordered source episode changed without clearing scope')
        if any(slot.label == label for slot in self._slots if slot.source is not None):
            raise ContractViolation('ordered source label already registered')
        index = next((i for i, slot in enumerate(self._slots) if slot.source is None), len(self._slots))
        if index == ORDERED_SOURCE_CAPACITY:
            raise ContractViolation('ordered source capacity exceeded')
        slot = self._slots[index] if index < len(self._slots) else _SourceSlot()
        generation = slot.generation + 1
        require_ordered_counter(generation, 'source generation')
        # The human label is metadata, not part of the bounded Runtime intent identifier.
        source = IntentSourceV1(self._scope_id, episode_id, index, generation,
                                f'{ORDERED_PREFIX}{self._scope_id}/{index}/{generation}')
        if index == len(self._slots):
            self._slots.append(slot)
        slot.generation, slot.source, slot.label, slot.last_sequence = generation, source, label, 0
        self._episode_id = episode_id
        return source

    def validate_source(self, source: IntentSourceV1) -> _SourceSlot:
        if type(source) is not IntentSourceV1:
            raise ContractViolation('ordered source handle must be IntentSourceV1')
        if source.scope_id != self._scope_id or source.slot >= len(self._slots):
            raise ContractViolation('ordered source belongs to an old or foreign scope')
        slot = self._slots[source.slot]
        if slot.source != source:
            raise ContractViolation('ordered source is unregistered or has an old generation')
        return slot

    def validate_submission(self, envelope: OrderedIntentV1) -> _SourceSlot:
        if type(envelope) is not OrderedIntentV1:
            raise ContractViolation('ordered arbiter requires OrderedIntentV1')
        slot = self.validate_source(envelope.source)
        if envelope.sequence <= slot.last_sequence:
            raise ContractViolation('ordered sequence is duplicate or out of order')
        return slot

    def unregister(self, source: IntentSourceV1) -> None:
        slot = self.validate_source(source)
        # Keep the generation tombstone. No list of previous handles/labels/intent IDs is retained.
        slot.source, slot.label = None, None
