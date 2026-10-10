"""Count-bounded access to the existing Java 1.21 step; no copied formulas."""
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import hashlib
import json

from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from .contracts import SearchBudget, SearchReason


@dataclass(frozen=True, slots=True)
class ScanCounts:
    nodes: int = 0
    physics_steps: int = 0
    tail_ticks: int = 0


class CountLimit(Exception):
    def __init__(self, reason: SearchReason):
        self.reason = reason


class CountedPhysics:
    """One request owns counters; search and scanning share the same instance.

    Nodes cover frontier expansion, branch/tail scans and public geometry checks.
    Incomplete step attempts consume physics budget too.
    """

    def __init__(self, budget: SearchBudget):
        self.budget = budget
        self.nodes = self.physics_steps = self.tail_ticks = 0

    @property
    def counts(self):
        return ScanCounts(self.nodes, self.physics_steps, self.tail_ticks)

    def node(self):
        if self.nodes >= self.budget.max_nodes:
            raise CountLimit(SearchReason.NODE_BUDGET)
        self.nodes += 1

    def step(self, state, command, world, *, tail=False):
        if self.physics_steps >= self.budget.max_physics_steps:
            raise CountLimit(SearchReason.PHYSICS_STEP_BUDGET)
        self.physics_steps += 1
        self.tail_ticks += int(tail)
        return step(state, command, world, JAVA_1_21_RULESET)


def _canonical(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def trajectory_digest(value) -> str:
    """Stable across hash seeds, including unordered request goal sets."""
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
