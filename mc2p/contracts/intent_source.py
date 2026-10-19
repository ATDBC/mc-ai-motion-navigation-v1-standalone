"""Versioned internal ordered submissions; never part of the client Action wire."""
from dataclasses import dataclass, field
import re

from mc2p.contracts.action_v1 import ActionIntentV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.contracts.observation_request_v3 import ObservationRequestV3


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


@dataclass(frozen=True, slots=True)
class ControlFrameEventV1:
    record_type: str
    payload: dict
    schema_version: str = field(default='mc2p.control-frame-event.v1', init=False)

    def __post_init__(self) -> None:
        require_identifier(self.record_type, 'control frame event type')
        if type(self.payload) is not dict:
            raise ContractViolation('control frame event payload must be a dictionary')


@dataclass(frozen=True, slots=True)
class ControlFrameProposalV1:
    """One skill's bounded contribution to the next shared control frame."""

    intents: tuple[OrderedIntentV1, ...] = ()
    observation_request: ObservationRequestV3 | None = None
    task_events: tuple[ControlFrameEventV1, ...] = ()
    schema_version: str = field(default='mc2p.control-frame-proposal.v1', init=False)

    def __post_init__(self) -> None:
        if (type(self.intents) is not tuple
                or any(type(intent) is not OrderedIntentV1 for intent in self.intents)
                or len(self.intents) > ORDERED_SOURCE_CAPACITY):
            raise ContractViolation('control frame intents must be a bounded ordered tuple')
        identities = tuple(intent.intent.intent_id for intent in self.intents)
        if len(set(identities)) != len(identities):
            raise ContractViolation('control frame contains duplicate intent identities')
        if (self.observation_request is not None
                and type(self.observation_request) is not ObservationRequestV3):
            raise ContractViolation('control frame observation request is invalid')
        if (type(self.task_events) is not tuple
                or any(type(event) is not ControlFrameEventV1
                       for event in self.task_events)
                or len(self.task_events) > 16):
            raise ContractViolation('control frame task events must be a bounded tuple')
