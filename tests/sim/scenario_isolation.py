"""Stable deep hash of the shared scenario definitions, including disturbances."""
from dataclasses import fields, is_dataclass
import hashlib
import json


def _value(value):
    if is_dataclass(value):
        return {field.name: _value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return sorted((repr(key), _value(item)) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_value(item) for item in value)
    if callable(value):
        return value.__module__ + "." + value.__qualname__
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def shared_scenario_hash():
    from tests.sim.scenarios import SCENARIOS
    return hashlib.sha256(json.dumps(_value(SCENARIOS), sort_keys=True).encode()).hexdigest()
