"""Bounded, deterministic multi-tick execution for explicitly supplied inputs."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
import time
from typing import Callable

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, PhysicsRuleset, PhysicsState, ResourceStatus,
    ResourceUpdate, StepResult, TickInput,
)
from mc2p.motion_nav.world_model import BlockPos


class RolloutOutputMode(StrEnum):
    FULL = "full"
    SUMMARY = "summary"


class RolloutStopReason(StrEnum):
    COMPLETE = "complete"
    MAX_TICKS = "max_ticks"
    MAX_DEPENDENCIES = "max_dependencies"
    WALL_TIME_BUDGET = "wall_time_budget"
    CANCELLED = "cancelled"
    EXTERNAL_EVENT = "external_event"
    STEP_INCOMPLETE = "step_incomplete"


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    """An event that invalidates free prediction at a complete tick boundary."""

    boundary_tick: int
    kind: str

    def __post_init__(self) -> None:
        if type(self.boundary_tick) is not int or self.boundary_tick < 0:
            raise ContractViolation("external event boundary must be a nonnegative tick")
        require_identifier(self.kind, "external event kind")


@dataclass(frozen=True, slots=True)
class RolloutOptions:
    output_mode: RolloutOutputMode = RolloutOutputMode.FULL
    max_ticks: int | None = None
    max_dependency_cells: int | None = None
    max_wall_time_seconds: float | None = None
    external_events: tuple[ExternalEvent, ...] = ()
    cancel_check: Callable[[int], bool] | None = None

    def __post_init__(self) -> None:
        if type(self.output_mode) is not RolloutOutputMode:
            raise ContractViolation("invalid rollout output mode")
        for name in ("max_ticks", "max_dependency_cells"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ContractViolation(f"{name} must be a nonnegative integer")
        if self.max_wall_time_seconds is not None:
            value = self.max_wall_time_seconds
            if (type(value) not in (int, float) or not math.isfinite(float(value))
                    or value < 0):
                raise ContractViolation("wall time budget must be nonnegative and finite")
        if (type(self.external_events) is not tuple
                or any(type(item) is not ExternalEvent for item in self.external_events)):
            raise ContractViolation("external events must be an immutable tuple")
        if tuple(sorted(self.external_events, key=lambda item: item.boundary_tick)) != self.external_events:
            raise ContractViolation("external events must be sorted by boundary tick")
        if self.cancel_check is not None and not callable(self.cancel_check):
            raise ContractViolation("cancel check must be callable")


@dataclass(frozen=True, slots=True)
class RolloutResult:
    stop_reason: RolloutStopReason
    ticks_completed: int
    final_state: PhysicsState
    states: tuple[PhysicsState, ...]
    dependencies: tuple[BlockPos, ...]
    events: tuple[tuple[int, str], ...]
    resource_update: ResourceUpdate
    steps: tuple[StepResult, ...] = ()
    external_event: ExternalEvent | None = None
    incomplete_step: StepResult | None = None


def rollout(initial_state: PhysicsState, inputs: tuple[TickInput, ...],
            world: PhysicsWorldView, ruleset: PhysicsRuleset,
            options: RolloutOptions | None = None) -> RolloutResult:
    """Evaluate a fixed input sequence without sleeping or mutating shared state."""
    if (type(initial_state) is not PhysicsState or type(inputs) is not tuple
            or any(type(item) is not TickInput for item in inputs)):
        raise ContractViolation("rollout requires a physics state and immutable tick inputs")
    if type(world) is not PhysicsWorldView or type(ruleset) is not PhysicsRuleset:
        raise ContractViolation("rollout requires a physics world and ruleset")
    options = options or RolloutOptions()
    if type(options) is not RolloutOptions:
        raise ContractViolation("invalid rollout options")

    current = initial_state
    states: list[PhysicsState] = []
    steps: list[StepResult] = []
    events: list[tuple[int, str]] = []
    exhaustion_delta = 0.0
    resource_reasons = set()
    dependencies: set[BlockPos] = set()
    completed = 0
    started = time.perf_counter()
    events_by_boundary = {event.boundary_tick: event for event in options.external_events}

    def result(reason: RolloutStopReason, *, event=None, incomplete=None) -> RolloutResult:
        resources = ResourceUpdate(
            ResourceStatus.CONDITIONAL if resource_reasons else ResourceStatus.COMPLETE,
            exhaustion_delta,
            tuple(sorted(resource_reasons)),
        )
        return RolloutResult(
            stop_reason=reason, ticks_completed=completed, final_state=current,
            states=tuple(states) if options.output_mode is RolloutOutputMode.FULL else (),
            dependencies=tuple(sorted(dependencies)), events=tuple(events),
            resource_update=resources,
            steps=tuple(steps) if options.output_mode is RolloutOutputMode.FULL else (),
            external_event=event, incomplete_step=incomplete,
        )

    while completed < len(inputs):
        event = events_by_boundary.get(completed)
        if event is not None:
            return result(RolloutStopReason.EXTERNAL_EVENT, event=event)
        if options.cancel_check is not None and options.cancel_check(completed):
            return result(RolloutStopReason.CANCELLED)
        if options.max_ticks is not None and completed >= options.max_ticks:
            return result(RolloutStopReason.MAX_TICKS)
        if (options.max_wall_time_seconds is not None
                and time.perf_counter() - started >= options.max_wall_time_seconds):
            return result(RolloutStopReason.WALL_TIME_BUDGET)

        calculated = step(current, inputs[completed], world, ruleset)
        if calculated.status is not CalculationStatus.OK:
            return result(RolloutStopReason.STEP_INCOMPLETE, incomplete=calculated)
        proposed_dependencies = dependencies | set(calculated.dependencies)
        if (options.max_dependency_cells is not None
                and len(proposed_dependencies) > options.max_dependency_cells):
            return result(RolloutStopReason.MAX_DEPENDENCIES)
        dependencies = proposed_dependencies
        assert calculated.next_state is not None
        assert calculated.resource_update is not None
        events.extend((completed, name) for name in calculated.events)
        exhaustion_delta += calculated.resource_update.exhaustion_delta
        resource_reasons.update(calculated.resource_update.incomplete_reasons)
        current = calculated.next_state
        completed += 1
        if options.output_mode is RolloutOutputMode.FULL:
            states.append(current)
            steps.append(calculated)

    event = events_by_boundary.get(completed)
    if event is not None:
        return result(RolloutStopReason.EXTERNAL_EVENT, event=event)
    return result(RolloutStopReason.COMPLETE)
