"""Single Runtime submission boundary and non-blocking, no-catch-up cadence."""
from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import TaskIntentV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1
from mc2p.skills.follow import FollowDecision, FollowReport, RuleFollower
from mc2p.skills.local_perception import project_follow_view

UPDATE_INTERVAL_NS = 50_000_000
LEASE_NS = 250_000_000
CLEANUP_NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class FollowStep:
    decision: FollowDecision
    runtime_result: RuntimeStepResultV1
    report: FollowReport
    movement_selected: bool
    look_selected: bool


class FollowDriver:
    def __init__(self, runtime: PlayerRuntimeV1, follower: RuleFollower,
                 clock_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        if type(runtime) is not PlayerRuntimeV1 or type(follower) is not RuleFollower:
            raise ContractViolation("follow driver requires formal Runtime and rule follower")
        self.runtime, self.follower, self._clock = runtime, follower, clock_ns
        self.next_update_at_ns = 0
        self._intent_number = 0

    def _view(self, now_ns: int):
        if self.runtime.state is not RuntimeStateV1.READY or self.runtime.observation is None:
            raise ContractViolation("follow driver requires ready Runtime; sealed connections are not retried")
        return project_follow_view(self.runtime.observation, now_ns, self.follower.request.controller_clock_id)

    def _cleanup_task(self, task: TaskIntentV0, now_ns: int) -> TaskIntentV0:
        # This is an explicitly separate release transaction, never an extension of following.
        return replace(task, task_id=task.task_id + ".release", task_type="follow_release",
                       deadline_monotonic_ns=now_ns+CLEANUP_NS)

    def _dispatch(self, decision: FollowDecision, task: TaskIntentV0, profile: BehaviorProfileV0,
                  deadline_ns: int, now_ns: int) -> FollowStep:
        self.runtime.cancel_source(self.follower.source_id)
        intent_id = None
        try:
            if not decision.report.terminal:
                self._intent_number += 1
                intent_id = f"{self.follower.source_id}/{self._intent_number}"
                self.runtime.submit_intent(ActionIntentV1(intent_id, self.follower.source_id,
                    self.follower.request.episode_id, decision.report.observation_sequence_id, ActionPriorityV0.TASK,
                    now_ns, min(now_ns+LEASE_NS, deadline_ns, task.deadline_monotonic_ns,
                                self.follower.request.deadline_ns),
                    movement=decision.movement, look=decision.look, valid_for_ticks=1,
                    movement_requires_look=True))
            self.next_update_at_ns = now_ns + UPDATE_INTERVAL_NS
            result = self.runtime.step(task, profile, deadline_ns,
                                       observation_request=ObservationRequestV3())
        except BaseException as error:
            self.follower.cancel("runtime_submission_failed", state="failed")
            if self.runtime.state is RuntimeStateV1.READY:
                try:
                    self.runtime.cancel_source(self.follower.source_id)
                except BaseException as cleanup_error:
                    error.add_note("follow source cleanup also failed: " + repr(cleanup_error))
            raise
        selected = dict(result.decision.selected_intents) if result.decision else {}
        move_selected = intent_id is not None and selected.get("movement") == intent_id
        look_selected = intent_id is not None and selected.get("look") == intent_id
        receipt = result.backend_result.receipt if result.backend_result else None
        accepted = receipt is not None and receipt.status in {"executed", "confirmed_local", "cancelled"}
        report = decision.report
        if result.observation is None or result.report.failure is not None or not accepted:
            reason = result.report.failure.message if result.report.failure else "unconfirmed_control_receipt"
            self.follower.cancel(reason, state="failed")
            report = replace(report, state="failed", reason=reason, terminal=True)
            if self.runtime.state is RuntimeStateV1.READY:
                self.runtime.cancel_source(self.follower.source_id)
        else:
            preempted = (not decision.report.terminal and
                         ((decision.movement != MovementV1() and not move_selected)
                          or (decision.look != LookV1() and not look_selected)))
            if preempted:
                report = replace(report, state="preempted", reason="arbitration_selected_other_or_filtered")
            new_view = project_follow_view(result.observation, self._clock(), self.follower.request.controller_clock_id)
            self.follower.feedback(not preempted, new_view, self._clock())
        return FollowStep(decision, result, report, move_selected, look_selected)

    def tick(self, task: TaskIntentV0, profile: BehaviorProfileV0, deadline_ns: int) -> FollowStep | None:
        now = self._clock()
        view = self._view(now)
        if now >= min(deadline_ns, task.deadline_monotonic_ns):
            self.follower.cancel("task_deadline", state="timed_out")
        elif now < self.next_update_at_ns:
            return None  # The caller waits/polls; there is no queued catch-up control burst.
        decision = self.follower.decide(view, now)
        if decision.report.terminal:
            cleanup = self._cleanup_task(task, now)
            return self._dispatch(decision, cleanup, profile, cleanup.deadline_monotonic_ns, now)
        return self._dispatch(decision, task, profile, deadline_ns, now)

    def stop(self, task: TaskIntentV0, profile: BehaviorProfileV0, deadline_ns: int,
             reason: str = "player_stop") -> FollowStep:
        now = self._clock()
        view = self._view(now)
        self.follower.cancel(reason)
        decision = self.follower.decide(view, now)
        cleanup = self._cleanup_task(task, now)
        # A caller can shorten a live cleanup deadline, but an expired task cannot prevent release.
        deadline = min(cleanup.deadline_monotonic_ns, deadline_ns) if deadline_ns > now else cleanup.deadline_monotonic_ns
        return self._dispatch(decision, cleanup, profile, deadline, now)
