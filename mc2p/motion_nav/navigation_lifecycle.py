"""Typed task lifecycle for one navigation session."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.common import ContractViolation


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


@dataclass(slots=True)
class NavigationLifecycle:
    state: NavigationSessionState = NavigationSessionState.READY
    last_event: NavigationSessionEvent | None = None

    def record(self, event: NavigationSessionEvent) -> SessionEventPolicy:
        if type(event) is not NavigationSessionEvent:
            raise ContractViolation("navigation lifecycle event must be typed")
        self.last_event = event
        return SESSION_EVENT_TABLE[(self.state, event)]

    def set_state(self, state: NavigationSessionState) -> None:
        if type(state) is not NavigationSessionState:
            raise ContractViolation("navigation lifecycle state must be typed")
        self.state = state
