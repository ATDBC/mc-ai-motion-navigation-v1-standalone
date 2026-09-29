"""Typed task lifecycle for one navigation session."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.body_control import HandoffDisposition, HandoffEvidence


class NavigationSessionState(StrEnum):
    READY = "ready"
    PLANNING = "planning"
    NEEDS_INFORMATION = "needs_information"
    EXECUTING = "executing"
    HANDOFF = "handoff"
    STOPPING = "stopping"
    REQUIRES_INTERACTION = "requires_interaction"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"

    # Source-compatible names while reports use the consolidated states.
    SNAPSHOTTING = "planning"
    CANCELLING = "stopping"


class NavigationSessionEvent(StrEnum):
    START_GOAL = "start_goal"
    REVISE_GOAL = "revise_goal"
    CANCEL = "cancel"
    CLOSE = "close"
    PLANNER_RESULT = "planner_result"
    ADMISSION_RESULT = "admission_result"
    CONTROLLER_REPORT = "controller_report"
    INFORMATION_ARRIVED = "information_arrived"
    INFORMATION_TIMED_OUT = "information_timed_out"
    DEPENDENCY_CHANGED = "dependency_changed"
    INTERACTION_CONFIRMED = "interaction_confirmed"
    RETRY_EXHAUSTED = "retry_exhausted"


class SessionEventPolicy(StrEnum):
    HANDLE = "handle"
    IGNORE = "ignore"
    REJECT = "reject"


class NavigationTransitionAction(StrEnum):
    """One explicit task-state change with a fixed destination."""

    RESET_READY = "reset_ready"
    BEGIN_PLANNING = "begin_planning"
    WAIT_FOR_INFORMATION = "wait_for_information"
    BEGIN_EXECUTION = "begin_execution"
    BEGIN_HANDOFF = "begin_handoff"
    BEGIN_STOPPING = "begin_stopping"
    REQUIRE_INTERACTION = "require_interaction"
    MARK_COMPLETE = "mark_complete"
    MARK_CANCELLED = "mark_cancelled"
    MARK_FAILED = "mark_failed"
    MARK_CLOSED = "mark_closed"
    RESUME_EXECUTION_AFTER_HANDOFF = "resume_execution_after_handoff"
    REPLAN_AFTER_HANDOFF = "replan_after_handoff"


_TERMINAL = {
    NavigationSessionState.COMPLETE,
    NavigationSessionState.CANCELLED,
    NavigationSessionState.FAILED,
    NavigationSessionState.CLOSED,
}


def _policy(state: NavigationSessionState,
            event: NavigationSessionEvent) -> SessionEventPolicy:
    if state in _TERMINAL:
        if event in {
            NavigationSessionEvent.PLANNER_RESULT,
            NavigationSessionEvent.ADMISSION_RESULT,
            NavigationSessionEvent.CONTROLLER_REPORT,
            NavigationSessionEvent.INFORMATION_ARRIVED,
            NavigationSessionEvent.DEPENDENCY_CHANGED,
        }:
            return SessionEventPolicy.IGNORE
        if event is NavigationSessionEvent.CLOSE:
            return SessionEventPolicy.HANDLE
        return SessionEventPolicy.REJECT
    if event is NavigationSessionEvent.START_GOAL:
        return (SessionEventPolicy.HANDLE
                if state is NavigationSessionState.READY
                else SessionEventPolicy.REJECT)
    if event is NavigationSessionEvent.INTERACTION_CONFIRMED:
        return (SessionEventPolicy.HANDLE
                if state is NavigationSessionState.REQUIRES_INTERACTION
                else SessionEventPolicy.REJECT)
    return SessionEventPolicy.HANDLE


SESSION_EVENT_TABLE = {
    (state, event): _policy(state, event)
    for state in NavigationSessionState
    for event in NavigationSessionEvent
}


def _transition_table() -> dict[
    tuple[NavigationSessionState, NavigationTransitionAction],
    NavigationSessionState,
]:
    table: dict[
        tuple[NavigationSessionState, NavigationTransitionAction],
        NavigationSessionState,
    ] = {}
    active = tuple(state for state in NavigationSessionState
                   if state not in _TERMINAL)

    def allow(states: tuple[NavigationSessionState, ...],
              action: NavigationTransitionAction,
              target: NavigationSessionState) -> None:
        for state in states:
            table[(state, action)] = target

    ordinary = tuple(state for state in active
                     if state is not NavigationSessionState.STOPPING)
    allow(ordinary, NavigationTransitionAction.RESET_READY,
          NavigationSessionState.READY)
    allow(ordinary, NavigationTransitionAction.BEGIN_PLANNING,
          NavigationSessionState.PLANNING)
    allow(ordinary, NavigationTransitionAction.WAIT_FOR_INFORMATION,
          NavigationSessionState.NEEDS_INFORMATION)
    allow(ordinary, NavigationTransitionAction.BEGIN_EXECUTION,
          NavigationSessionState.EXECUTING)
    allow(ordinary, NavigationTransitionAction.BEGIN_HANDOFF,
          NavigationSessionState.HANDOFF)
    allow(ordinary, NavigationTransitionAction.REQUIRE_INTERACTION,
          NavigationSessionState.REQUIRES_INTERACTION)
    allow(active, NavigationTransitionAction.BEGIN_STOPPING,
          NavigationSessionState.STOPPING)
    allow(active, NavigationTransitionAction.MARK_COMPLETE,
          NavigationSessionState.COMPLETE)
    allow(active, NavigationTransitionAction.MARK_CANCELLED,
          NavigationSessionState.CANCELLED)
    allow(active, NavigationTransitionAction.MARK_FAILED,
          NavigationSessionState.FAILED)
    allow(active, NavigationTransitionAction.MARK_CLOSED,
          NavigationSessionState.CLOSED)
    allow((NavigationSessionState.STOPPING,),
          NavigationTransitionAction.RESUME_EXECUTION_AFTER_HANDOFF,
          NavigationSessionState.EXECUTING)
    allow((NavigationSessionState.STOPPING,),
          NavigationTransitionAction.REPLAN_AFTER_HANDOFF,
          NavigationSessionState.PLANNING)
    allow((NavigationSessionState.COMPLETE,),
          NavigationTransitionAction.MARK_COMPLETE,
          NavigationSessionState.COMPLETE)
    allow((NavigationSessionState.CANCELLED,),
          NavigationTransitionAction.MARK_CANCELLED,
          NavigationSessionState.CANCELLED)
    allow((NavigationSessionState.FAILED,),
          NavigationTransitionAction.MARK_FAILED,
          NavigationSessionState.FAILED)
    allow((NavigationSessionState.CLOSED,),
          NavigationTransitionAction.MARK_CLOSED,
          NavigationSessionState.CLOSED)
    return table


SESSION_TRANSITION_TABLE = _transition_table()


@dataclass(frozen=True, slots=True)
class NavigationTransition:
    previous: NavigationSessionState
    action: NavigationTransitionAction
    current: NavigationSessionState


@dataclass(slots=True)
class NavigationLifecycle:
    state: NavigationSessionState = NavigationSessionState.READY
    last_event: NavigationSessionEvent | None = None
    transition_count: int = 0
    illegal_transition_count: int = 0

    def admit_event(self, event: NavigationSessionEvent) -> SessionEventPolicy:
        """Classify one event before any session-owned state is changed."""
        if type(event) is not NavigationSessionEvent:
            raise ContractViolation("navigation lifecycle event must be typed")
        self.last_event = event
        return SESSION_EVENT_TABLE[(self.state, event)]

    def transition(
        self, action: NavigationTransitionAction, *,
        handoff: HandoffEvidence | None = None,
    ) -> NavigationTransition:
        if type(action) is not NavigationTransitionAction:
            raise ContractViolation("navigation transition action must be typed")
        expected_handoff = {
            NavigationTransitionAction.RESUME_EXECUTION_AFTER_HANDOFF:
                HandoffDisposition.TRANSFERABLE,
            NavigationTransitionAction.REPLAN_AFTER_HANDOFF:
                HandoffDisposition.QUIESCENT,
        }.get(action)
        if expected_handoff is not None and (
                type(handoff) is not HandoffEvidence
                or handoff.disposition is not expected_handoff):
            self.illegal_transition_count += 1
            label = (
                "transferable handoff" if expected_handoff is
                HandoffDisposition.TRANSFERABLE else "quiescent handoff"
            )
            raise ContractViolation(
                f"navigation transition requires {label} evidence"
            )
        if expected_handoff is None and handoff is not None:
            self.illegal_transition_count += 1
            raise ContractViolation(
                "navigation transition does not accept handoff evidence"
            )
        previous = self.state
        current = SESSION_TRANSITION_TABLE.get((previous, action))
        if current is None:
            self.illegal_transition_count += 1
            if previous in _TERMINAL:
                raise ContractViolation("terminal navigation state cannot be left")
            raise ContractViolation(
                f"navigation transition is not allowed: "
                f"{previous.value} + {action.value}"
            )
        self.transition_count += 1
        self.state = current
        return NavigationTransition(previous, action, current)
