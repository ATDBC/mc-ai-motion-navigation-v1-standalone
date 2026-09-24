"""Strict user-intent-only protocol, separate from actor observations and actions."""
from dataclasses import dataclass
import json
import math

MAX_FRAME_BYTES = 8192
MODES = frozenset({'slow', 'normal', 'fast', 'max', 'auto'})
KINDS = frozenset({'follow_start', 'follow_stop', 'follow_mode', 'follow_distance',
                   'follow_status', 'demo_stop', 'heartbeat'})


def decode_object(raw: bytes) -> dict:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise ValueError('invalid control frame size')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('duplicate control key')
            value[key] = item
        return value
    def bad_constant(_): raise ValueError('nonfinite control value')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=bad_constant)
    if type(value) is not dict: raise ValueError('control frame must be an object')
    return value


@dataclass(frozen=True, slots=True)
class DemoCommandV1:
    sequence: int
    kind: str
    mode: str | None = None
    distance_blocks: float | None = None

    def __post_init__(self):
        if type(self.sequence) is not int or not 1 <= self.sequence <= 2**63-1:
            raise ValueError('invalid control sequence')
        if type(self.kind) is not str or self.kind not in KINDS:
            raise ValueError('unknown control kind')
        if self.kind == 'follow_mode':
            if type(self.mode) is not str or self.mode not in MODES:
                raise ValueError('invalid follow mode')
        elif self.mode is not None:
            raise ValueError('mode not allowed for this command')
        if self.kind == 'follow_distance':
            if type(self.distance_blocks) not in (int, float) or not 1.5 <= self.distance_blocks <= 6 or not math.isfinite(self.distance_blocks):
                raise ValueError('auto distance must be 1.5 through 6 blocks')
        elif self.distance_blocks is not None:
            raise ValueError('distance not allowed for this command')


def decode_command(raw: bytes) -> DemoCommandV1:
    value = decode_object(raw)
    keys = {'schema_version', 'sequence', 'kind'}
    if value.get('kind') == 'follow_mode': keys.add('mode')
    if value.get('kind') == 'follow_distance': keys.add('distance_blocks')
    if set(value) != keys or value['schema_version'] != 'mc2p.demo-command.v1':
        raise ValueError('invalid control command schema/fields')
    return DemoCommandV1(**{k: v for k, v in value.items() if k != 'schema_version'})
