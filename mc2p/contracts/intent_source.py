"""Versioned internal ordered submissions; never part of the client Action wire."""
from dataclasses import dataclass, field
import re

from mc2p.contracts.action_v1 import ActionIntentV1
from mc2p.contracts.common import ContractViolation, require_identifier


MAX_ORDERED_COUNTER = 2**63 - 1
ORDERED_SOURCE_CAPACITY = 64
ORDERED_PREFIX = 'ordered/'


def require_ordered_counter(value: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_ORDERED_COUNTER:
        raise ContractViolation(f'{name} must be a positive signed 64-bit integer')


@dataclass(frozen=True, slots=True)
class IntentSourceV1:
    scope_id: str
    episode_id: str
    slot: int
    generation: int
    source_id: str
    schema_version: str = field(default='mc2p.intent-source.v1', init=False)

    def __post_init__(self) -> None:
        if type(self.scope_id) is not str or re.fullmatch(r'[0-9a-f]{32}', self.scope_id) is None:
            raise ContractViolation('ordered scope must be a 128-bit hexadecimal identity')
        require_identifier(self.episode_id, 'episode_id')
        if len(self.episode_id) > 128:
            raise ContractViolation('ordered episode identifier exceeds 128 characters')
        if type(self.slot) is not int or not 0 <= self.slot < ORDERED_SOURCE_CAPACITY:
            raise ContractViolation('ordered source slot is out of range')
        require_ordered_counter(self.generation, 'source generation')
        if self.source_id != f'{ORDERED_PREFIX}{self.scope_id}/{self.slot}/{self.generation}':
            raise ContractViolation('ordered source identity does not match its scope/slot/generation')


def ordered_intent_id(source: IntentSourceV1, sequence: int) -> str:
    if type(source) is not IntentSourceV1:
        raise ContractViolation('ordered intent requires an IntentSourceV1')
    require_ordered_counter(sequence, 'intent sequence')
    return f'{source.source_id}/{sequence}'


@dataclass(frozen=True, slots=True)
class OrderedIntentV1:
    source: IntentSourceV1
    sequence: int
    intent: ActionIntentV1
    schema_version: str = field(default='mc2p.ordered-intent.v1', init=False)

    def __post_init__(self) -> None:
        identity = ordered_intent_id(self.source, self.sequence)
        if type(self.intent) is not ActionIntentV1:
            raise ContractViolation('ordered submission requires an ActionIntentV1')
        if (self.intent.intent_id != identity or self.intent.source_id != self.source.source_id
                or self.intent.episode_id != self.source.episode_id):
            raise ContractViolation('ordered intent does not match its source and sequence')
