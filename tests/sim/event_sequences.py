"""Seeded adversarial event sequences over the formal navigation path.

The generator changes only the simulated game and public task inputs.  The
Runtime, navigation driver, session, planner, admission and executors are the
same objects used by the deterministic formal-path scenarios.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import argparse
import json
from pathlib import Path
import random
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, Result, _goal, run
from tests.sim.scenarios import SCENARIOS
from tests.test_player_runtime import _task


class EventKind(StrEnum):
    GOAL_BACK = "goal_back"
    GOAL_OUT_OF_RANGE = "goal_out_of_range"
    CANCEL = "cancel"
    LATE_INPUT = "late_input"
    OMIT_RECEIPT = "omit_receipt"
    LOSE_ARBITRATION = "lose_arbitration"
    EXTERNAL_PUSH = "external_push"
    EXTERNAL_PUSH_BACKWARD = "external_push_backward"
    REMOVE_LANDING_SUPPORT = "remove_landing_support"


_EVENT_WEIGHTS = {
    EventKind.GOAL_BACK: 3,
    EventKind.GOAL_OUT_OF_RANGE: 2,
    EventKind.CANCEL: 1,
    EventKind.LATE_INPUT: 3,
    EventKind.OMIT_RECEIPT: 3,
    EventKind.LOSE_ARBITRATION: 2,
    EventKind.EXTERNAL_PUSH: 2,
    EventKind.EXTERNAL_PUSH_BACKWARD: 2,
    EventKind.REMOVE_LANDING_SUPPORT: 2,
}


@dataclass(frozen=True, slots=True)
class GeneratedEvent:
    kind: EventKind
    tick: int

    def __post_init__(self) -> None:
        if type(self.kind) is not EventKind:
            raise TypeError("generated event kind must be typed")
        if type(self.tick) is not int or self.tick < 1:
            raise ValueError("generated event tick must be positive")


@dataclass(frozen=True, slots=True)
class GeneratedSequence:
    seed: int
    scenario: str
    max_ticks: int
    events: tuple[GeneratedEvent, ...]

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("sequence seed must be int")
        if not self.scenario:
            raise ValueError("sequence scenario is required")
        if type(self.max_ticks) is not int or self.max_ticks < 1:
            raise ValueError("sequence maximum tick must be positive")
        if tuple(sorted(self.events, key=lambda item: (item.tick, item.kind.value))) != self.events:
            raise ValueError("generated events must be ordered")


@dataclass(frozen=True, slots=True)
class SequenceOutcome:
    sequence: GeneratedSequence
    result: Result | None
    exception: str | None = None
    event_applications: tuple["EventApplication", ...] = ()

    @property
    def failed_invariant(self) -> bool:
        return (
            self.exception is not None
            or self.result is None
            or bool(self.result.violations)
            or self.result.outcome not in {"success", "failed", "cancelled"}
            or self.result.reason == "navigation_internal_contract_failure"
        )

    @property
    def failed_gate(self) -> bool:
        return self.failed_invariant or self.result is None or not self.result.verification_complete


@dataclass(frozen=True, slots=True)
class EventApplication:
    kind: EventKind
    tick: int
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"dispatched", "applied", "skipped"}:
            raise ValueError("event application status is invalid")


def generate_sequence(
    seed: int,
    *,
    event_count: int = 6,
    scenario: str = "direct_drop_2",
    max_ticks: int = 300,
) -> GeneratedSequence:
    """Generate a stable mixture; the seed is part of every saved repro."""
    if type(event_count) is not int or not 0 <= event_count <= 32:
        raise ValueError("event count must be between zero and 32")
    rng = random.Random(seed)
    kinds = list(EventKind)
    chosen = rng.choices(
        kinds,
        weights=[_EVENT_WEIGHTS[kind] for kind in kinds],
        k=event_count,
    )
    ticks = []
    occupied = set()
    for kind in chosen:
        lower, upper = (
            (14, 34)
            if kind is EventKind.REMOVE_LANDING_SUPPORT else
            (10, 79)
        )
        tick = rng.randint(lower, upper)
        while tick in occupied:
            tick = lower if tick == upper else tick + 1
        occupied.add(tick)
        ticks.append(tick)
    events = tuple(sorted(
        (GeneratedEvent(kind, tick) for kind, tick in zip(chosen, ticks)),
        key=lambda item: (item.tick, item.kind.value),
    ))
    return GeneratedSequence(seed, scenario, max_ticks, events)


def _revision_event(
    kind: EventKind,
    revision: int,
    tick: int,
    position: tuple[float, float, float],
) -> Event:

    def revise(context) -> None:
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal(
            "goal", revision, goal, context.clock[0],
            damage_budget=TaskDamageBudget(
                context.risk_policy_id, context.damage_points,
            ),
        )
        context.goal_state = goal
        context.goal_position = position

    return Event(
        f"{kind.value}-{revision}",
        lambda context, at=tick: context.tick >= at,
        revise,
        kind=kind.value,
    )


def _cancel_event(tick: int, ordinal: int) -> Event:
    return Event(
        f"cancel-{ordinal}",
        lambda context, at=tick: context.tick >= at,
        lambda context: context.driver.release("generated_sequence_cancel"),
        kind=EventKind.CANCEL.value,
    )


def _configured(sequence: GeneratedSequence):
    try:
        base = next(item for item in SCENARIOS if item.name == sequence.scenario)
    except StopIteration as error:
        raise ValueError(f"unknown generated-sequence scenario: {sequence.scenario}") from error
    late = set()
    omitted = set()
    impulses = {}
    world_edits = {}
    runtime_events = []
    arbitration_ticks = set()
    revision = 1
    for ordinal, event in enumerate(sequence.events, start=1):
        if event.kind in {EventKind.GOAL_BACK, EventKind.GOAL_OUT_OF_RANGE}:
            revision += 1
            position = (
                base.start
                if event.kind is EventKind.GOAL_BACK else
                (base.goal[0], base.goal[1], base.scene.volume[2][1] + 20.5)
            )
            runtime_events.append(_revision_event(
                event.kind, revision, event.tick, position,
            ))
        elif event.kind is EventKind.CANCEL:
            runtime_events.append(_cancel_event(event.tick, ordinal))
        elif event.kind is EventKind.LATE_INPUT:
            late.add(event.tick)
        elif event.kind is EventKind.OMIT_RECEIPT:
            omitted.add(event.tick)
        elif event.kind is EventKind.LOSE_ARBITRATION:
            arbitration_ticks.add(event.tick)
        elif event.kind is EventKind.EXTERNAL_PUSH:
            # The navigation-only harness omits C1's external-motion recovery
            # driver.  Use a shove that perturbs an owned route without turning
            # the test into an unmodelled knockback/fall-recovery scenario.
            impulses[event.tick] = (0.0, 0.0, .15)
        elif event.kind is EventKind.EXTERNAL_PUSH_BACKWARD:
            impulses[event.tick] = (0.0, 0.0, -.15)
        elif event.kind is EventKind.REMOVE_LANDING_SUPPORT:
            if not base.landing_support_cells:
                raise ValueError(
                    f"scenario has no declared landing support: {base.name}"
                )
            world_edits.setdefault(event.tick, {}).update({
                cell: None for cell in base.landing_support_cells
            })
    configured = replace(
        base,
        name=f"generated-{sequence.seed}",
        events=runtime_events,
        perturbations=Perturbations(
            late_ticks=frozenset(late),
            impulses=impulses,
            world_edits=world_edits,
            omitted_receipt_ticks=frozenset(omitted),
        ),
        max_ticks=sequence.max_ticks,
    )
    return configured, frozenset(arbitration_ticks)


class _ArbitratedControl:
    def __init__(self, arbitration_ticks: frozenset[int]) -> None:
        self.arbitration_ticks = arbitration_ticks
        self.source = None
        self.sequence = 0
        self.dispatched_ticks: set[int] = set()
        self.applied_ticks: set[int] = set()

    def __call__(self, context):
        runtime = context.driver.runtime
        deadline = context.clock[0] + 500_000_000
        if context.tick not in self.arbitration_ticks:
            if self.source is not None:
                runtime.cancel_source(self.source.source_id)
            context.driver.tick(BehaviorProfileV0(), deadline)
            return ()
        if self.source is None:
            self.source = runtime.register_ordered_source("generated-arbitration-loss")
        self.sequence += 1
        intent = ActionIntentV1(
            ordered_intent_id(self.source, self.sequence),
            self.source.source_id,
            self.source.episode_id,
            runtime.observation.sequence_id,
            ActionPriorityV0.SAFETY,
            context.clock[0],
            deadline,
            movement=MovementV1(),
        )
        envelope = OrderedIntentV1(self.source, self.sequence, intent)
        navigation = context.driver.prepare_proposals(deadline)
        result = runtime.control_frame(
            _task(deadline),
            BehaviorProfileV0(),
            deadline,
            proposals=navigation + (ControlFrameProposalV1((envelope,)),),
        )
        context.driver.adopt_result(result)
        self.dispatched_ticks.add(context.tick)
        diagnostics = context.driver.last_frame_diagnostics
        if diagnostics is not None:
            proposed = set(diagnostics.proposed_movement_intents)
            suppressed = {
                intent_id for intent_id, _ in diagnostics.suppressed_intents
            }
            if proposed.intersection(suppressed):
                self.applied_ticks.add(context.tick)
        return (intent.intent_id,)


def run_sequence(sequence: GeneratedSequence) -> SequenceOutcome:
    configured, arbitration_ticks = _configured(sequence)
    arbitration = _ArbitratedControl(arbitration_ticks)
    try:
        result = run(
            configured,
            control_step=(
                None if not arbitration_ticks else
                arbitration
            ),
        )
        applied = set(result.applied_perturbations)
        applied.update(
            (EventKind.LOSE_ARBITRATION.value, tick)
            for tick in arbitration.applied_ticks
        )
        dispatched = set(result.event_dispatches)
        dispatched.update(result.dispatched_perturbations)
        dispatched.update(
            (EventKind.LOSE_ARBITRATION.value, tick)
            for tick in arbitration.dispatched_ticks
        )
        applications = tuple(
            EventApplication(
                event.kind,
                event.tick,
                (
                    "applied" if (event.kind.value, event.tick) in applied
                    else "dispatched" if (event.kind.value, event.tick) in dispatched
                    else "skipped"
                ),
            )
            for event in sequence.events
        )
        return SequenceOutcome(sequence, result, event_applications=applications)
    except Exception as error:  # A public-call exception is itself a repro.
        return SequenceOutcome(
            sequence,
            None,
            f"{type(error).__name__}: {error}",
        )


def shrink_sequence(
    sequence: GeneratedSequence,
    fails: Callable[[GeneratedSequence], bool],
) -> GeneratedSequence:
    """Greedily produce an event-minimal, earlier-triggering counterexample."""
    current = sequence
    changed = True
    while changed:
        changed = False
        for index in range(len(current.events)):
            candidate = replace(
                current,
                events=current.events[:index] + current.events[index + 1:],
            )
            if fails(candidate):
                current = candidate
                changed = True
                break
    for index, event in enumerate(current.events):
        for tick in range(1, event.tick):
            revised = replace(event, tick=tick)
            events = list(current.events)
            events[index] = revised
            events.sort(key=lambda item: (item.tick, item.kind.value))
            candidate = replace(current, events=tuple(events))
            if fails(candidate):
                current = candidate
                break
    return current


def _document(sequence: GeneratedSequence, outcome: SequenceOutcome) -> dict:
    return {
        "schema_version": "mc2p.navigation-event-sequence.v1",
        "sequence": {
            **asdict(sequence),
            "events": [
                {"kind": event.kind.value, "tick": event.tick}
                for event in sequence.events
            ],
        },
        "exception": outcome.exception,
        "event_applications": [
            {
                "kind": application.kind.value,
                "tick": application.tick,
                "status": application.status,
            }
            for application in outcome.event_applications
        ],
        "result": None if outcome.result is None else {
            "outcome": outcome.result.outcome,
            "reason": outcome.result.reason,
            "ticks": outcome.result.ticks,
            "damage": outcome.result.damage,
            "violations": outcome.result.violations,
            "events": outcome.result.events,
            "verification": None if outcome.result.verification is None else asdict(outcome.result.verification),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--shrink", action="store_true")
    args = parser.parse_args()
    sequence = generate_sequence(args.seed, event_count=args.count)
    outcome = run_sequence(sequence)
    if args.shrink and outcome.failed_gate:
        sequence = shrink_sequence(
            sequence,
            lambda candidate: run_sequence(candidate).failed_invariant,
        )
        outcome = run_sequence(sequence)
    document = _document(sequence, outcome)
    encoded = json.dumps(document, ensure_ascii=False, indent=2)
    if args.output is None:
        print(encoded)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
