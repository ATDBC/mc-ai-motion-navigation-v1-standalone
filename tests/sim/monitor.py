"""Tick-by-tick checks over observed body, command, and navigation facts."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.body_control import (
    BodyControlProgress, HandoffDisposition, HandoffEvidence,
)
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.motion_risk import RiskActionRecord, RiskActionState
from mc2p.motion_nav.retry_ledger import ProgressEvidence, ProgressKind


@dataclass(frozen=True, slots=True)
class TickEvidence:
    tick: int
    now_ns: int
    position: tuple[float, float, float]
    on_ground: bool
    controller_ids: tuple[str, ...]
    source_bound: bool
    state: str
    reason: str
    damage: float
    damage_limit: float
    committed_damage: float
    request_generation: int | None
    goal_revision: int | None
    wait_frames: int
    applied_request: int | None
    applied_movement: MovementV1
    issued_commands: tuple[tuple[int, MovementV1, int, int], ...]
    proposed_movement_intents: tuple[str, ...]
    selected_intents: tuple[tuple[str, str], ...]
    action_movement: MovementV1 | None
    goal_position: tuple[float, float, float]
    goal_state: GoalState
    velocity: tuple[float, float, float]
    pose: str
    yaw_radians: float
    risk_policy_id: str
    action_index: int | None = None
    retry_attempt_id: str | None = None
    entry_proof_valid: bool | None = None
    planning_failure_kind: str | None = None
    search_exhaustive: bool | None = None
    observation_sequence: int | None = None
    route_id: str | None = None
    submitted_request: int | None = None
    handoff: HandoffEvidence | None = None
    risk_actions: tuple[RiskActionRecord, ...] = ()
    retry_round_failures: int | None = None
    retry_total_failures: int | None = None
    retry_cause_counts: tuple[tuple[str, int], ...] = ()
    retry_progress_version: int = 0
    retry_progress_evidence: ProgressEvidence | None = None
    retry_approved_round: int | None = None
    retry_approved_total: int | None = None
    retry_approved_cause_counts: tuple[tuple[str, int], ...] = ()
    support_fraction: float | None = None
    illegal_transition_count: int = 0
    body_control_progress: BodyControlProgress | None = None


class InvariantMonitor:
    """Keep failures as evidence; never alter the simulated controller."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str, str]] = []
        self.coverage_gaps: set[str] = set()
        self._support_y: float | None = None
        self._unowned_airborne = False
        self._last_position: tuple[float, float, float] | None = None
        self._still_ticks = 0
        self._last_request: int | None = None
        self._origin: tuple[float, float, float] | None = None
        self._original_goal: tuple[float, float, float] | None = None
        self._best_route_progress = 0.0
        self._best_action_index = -1
        self._retries_without_progress: set[str] = set()
        self._completion_checked = False
        self._last_retry_round = 0
        self._last_retry_total = 0
        self._last_progress_version = 0
        self._last_retry_attempt_id: str | None = None
        self._last_source_bound: bool | None = None
        self._terminal_owned_ticks = 0
        self._body_progress_owner: str | None = None
        self._body_progress_revision = -1
        self._body_progress_action_index = -1
        self._body_progress_stalled_ticks = 0

    def _record(self, tick: int, code: str, detail: str) -> None:
        if not any(item[1] == code for item in self.violations):
            self.violations.append((tick, code, detail))

    def check(self, e: TickEvidence) -> None:
        if (e.goal_state.allowed_modes != frozenset({MovementMode.WALK})
                or e.goal_state.minimum_resources.values):
            raise ValueError("simulation goal monitor supports Walk with no resource minimum")
        owners = e.controller_ids
        selected_movement = tuple(intent_id for group, intent_id in e.selected_intents
                                  if group == "movement")
        selected_proposed = set(selected_movement).intersection(e.proposed_movement_intents)
        if (len(selected_movement) > 1 or len(selected_proposed) > 1
                or (e.action_movement is not None
                    and e.action_movement != MovementV1()
                    and e.proposed_movement_intents and not selected_proposed)):
            self._record(e.tick, "I6", "action movement has no single winning proposal")
        handoff = e.handoff
        fresh_handoff = (
            handoff is not None and e.observation_sequence is not None
            and handoff.observation_sequence_id + 1 == e.observation_sequence
        )
        if fresh_handoff and handoff is not None:
            if handoff.disposition is HandoffDisposition.TRANSFERABLE:
                if (handoff.successor_id != e.route_id
                        or handoff.control_sequence != e.submitted_request
                        or not selected_proposed):
                    self._record(e.tick, "I1", "transfer has no selected successor")
                    self._record(e.tick, "I6", "transfer sequence lost arbitration")
            elif (handoff.disposition is HandoffDisposition.RETAIN
                  and handoff.owner_id.startswith("route/")
                  and "route_executor" not in owners):
                self._record(e.tick, "I1", "retained route has no controller")
            elif (handoff.disposition is HandoffDisposition.RETAIN
                  and handoff.owner_id.startswith("landing-edge-probe/")
                  and "landing_edge_probe" not in owners):
                self._record(e.tick, "I1", "retained probe has no controller")
        if not e.on_ground:
            if not owners or not e.source_bound:
                self._unowned_airborne = True
                self._record(e.tick, "I1", "airborne without a bound body controller")
        elif self._support_y is not None:
            drop = self._support_y - e.position[1]
            if self._unowned_airborne and drop >= .99:
                self._record(e.tick, "I2", f"unowned fall of {drop:.2f} blocks")
            self._support_y = e.position[1]
            self._unowned_airborne = False
        if e.on_ground and self._support_y is None:
            self._support_y = e.position[1]
        if e.risk_actions:
            committed = 0.0
            for action in e.risk_actions:
                if (action.expected_damage_points
                        > action.available_at_reservation_points + 1e-9):
                    self._record(e.tick, "I3", "risk action exceeded its reserved availability")
                if action.state in {RiskActionState.COMMITTED,
                                    RiskActionState.SETTLED}:
                    committed += action.expected_damage_points
            if e.damage > committed + 1e-9:
                self._record(e.tick, "I3", "observed damage has no authorized commitment")
        elif e.damage > e.damage_limit + 1e-9 or e.committed_damage > e.damage_limit + 1e-9:
            self._record(e.tick, "I3", "damage or commitment exceeds task limit")
        terminal = e.state in {"complete", "failed", "cancelled", "closed"}
        if self._last_position is not None:
            moved = math.dist(e.position, self._last_position)
            self._still_ticks = 0 if moved > .01 or terminal else self._still_ticks + 1
        if self._origin is None:
            self._origin = e.position
            self._original_goal = e.goal_position
        assert self._original_goal is not None
        direction = tuple(self._original_goal[i] - self._origin[i] for i in (0, 2))
        length = math.hypot(*direction)
        progress = (0.0 if length < 1e-9 else sum(
            (e.position[i] - self._origin[i]) * direction[j] / length
            for j, i in enumerate((0, 2))))
        route_progressed = (
            e.on_ground and progress >= self._best_route_progress + .5
        )
        if route_progressed:
            self._best_route_progress = progress
            self._retries_without_progress.clear()
        action_progressed = (
            e.action_index is not None
            and e.action_index > self._best_action_index
        )
        if action_progressed:
            self._best_action_index = e.action_index
            self._retries_without_progress.clear()
        self._last_position = e.position
        body_progress = e.body_control_progress
        if (body_progress is None or terminal
                or body_progress.stall_limit_ticks is None):
            self._body_progress_owner = None
            self._body_progress_stalled_ticks = 0
        else:
            new_owner = body_progress.owner_id != self._body_progress_owner
            revision_advanced = (
                body_progress.owner_id == self._body_progress_owner
                and body_progress.progress_revision
                    > self._body_progress_revision
            )
            body_action_advanced = (
                body_progress.owner_id == self._body_progress_owner
                and body_progress.action_index is not None
                and body_progress.action_index
                    > self._body_progress_action_index
            )
            if (new_owner or revision_advanced or body_action_advanced
                    or route_progressed or action_progressed):
                self._body_progress_stalled_ticks = 0
            else:
                self._body_progress_stalled_ticks += 1
            self._body_progress_owner = body_progress.owner_id
            self._body_progress_revision = body_progress.progress_revision
            self._body_progress_action_index = (
                -1 if body_progress.action_index is None
                else body_progress.action_index
            )
        if ((e.wait_frames > 40 and not terminal)
                or self._still_ticks >= 100
                or (body_progress is not None
                    and body_progress.stall_limit_ticks is not None
                    and self._body_progress_stalled_ticks
                        > body_progress.stall_limit_ticks)):
            self._record(e.tick, "I4", f"unbounded wait in {e.state}/{e.reason}")
        if terminal and (e.source_bound or owners):
            self._terminal_owned_ticks += 1
            if self._terminal_owned_ticks > 20:
                self._record(
                    e.tick, "I10",
                    "terminal navigation retained its input source or body owner",
                )
        else:
            self._terminal_owned_ticks = 0
        if self._last_source_bound is True and not e.source_bound:
            if (not e.on_ground or e.support_fraction is None
                    or e.support_fraction <= 0.0):
                self._record(
                    e.tick, "I11",
                    "navigation released input without verified stable support",
                )
        self._last_source_bound = e.source_bound
        active_risk_actions = sum(
            action.state in {RiskActionState.RESERVED, RiskActionState.COMMITTED}
            for action in e.risk_actions
        )
        if active_risk_actions > 64:
            self._record(e.tick, "I12", "active risk record capacity exceeded")
        if e.illegal_transition_count:
            self._record(e.tick, "I13", "navigation attempted an illegal transition")
        if e.retry_total_failures is not None and e.retry_round_failures is not None:
            if (e.retry_approved_round is not None
                    and e.retry_approved_total is not None
                    and (e.retry_approved_total > 12
                         or e.retry_approved_round > 6
                         or any(count > 2 for _, count
                                in e.retry_approved_cause_counts))):
                self._record(e.tick, "I5", "approved retries exceeded task or round limit")
            if e.retry_total_failures < self._last_retry_total:
                self._record(e.tick, "I5", "task retry count decreased")
            if (e.retry_round_failures < self._last_retry_round
                    and e.retry_progress_version == self._last_progress_version):
                self._record(e.tick, "I5", "retry round reset without witnessed progress")
            if (e.retry_progress_version > self._last_progress_version):
                progress = e.retry_progress_evidence
                if (progress is None or
                    (progress.kind is ProgressKind.ROUTE_FRONTIER
                     and progress.support not in progress.valid_corridor)):
                    self._record(e.tick, "I5", "retry progress has no valid evidence")
            if (e.retry_attempt_id is not None
                    and e.retry_attempt_id == self._last_retry_attempt_id
                    and e.retry_total_failures > self._last_retry_total):
                self._record(e.tick, "I5", "duplicate failure counted twice")
            self._last_retry_round = e.retry_round_failures
            self._last_retry_total = e.retry_total_failures
            self._last_progress_version = e.retry_progress_version
            self._last_retry_attempt_id = e.retry_attempt_id
        elif e.retry_attempt_id is None:
            self.coverage_gaps.add("I5: task retry ledger unavailable")
        else:
            self._retries_without_progress.add(e.retry_attempt_id)
            if len(self._retries_without_progress) > 6:
                self._record(e.tick, "I5", "more than six failed attempts without progress")
        if e.applied_request is not None:
            matching = [c for c in e.issued_commands if c[0] == e.applied_request]
            if (not matching or matching[-1][1] != e.applied_movement
                    or not matching[-1][2] <= e.tick or e.now_ns >= matching[-1][3]):
                self._record(e.tick, "I6", "applied command has no valid issued lease")
        elif e.applied_movement != MovementV1():
            self._record(e.tick, "I6", "movement applied without command identity")
        if e.entry_proof_valid is False:
            self._record(e.tick, "I7", "action started without current proof")
        elif e.entry_proof_valid is None:
            self.coverage_gaps.add("I7: action-entry proof event unavailable before S3")
        if (e.planning_failure_kind == "no_route" and e.search_exhaustive is not True):
            self._record(e.tick, "I8", "no-route result lacks exhaustive search evidence")
        elif e.planning_failure_kind is None:
            self.coverage_gaps.add("I8: typed planner result unavailable in tick diagnostics")
        if e.state == "complete" and not self._completion_checked:
            self._completion_checked = True
            goal = e.goal_state
            region = goal.region
            inside = (region.min_x <= e.position[0] <= region.max_x
                      and region.min_y <= e.position[1] <= region.max_y
                      and region.min_z <= e.position[2] <= region.max_z)
            speed = 20.0 * math.hypot(e.velocity[0], e.velocity[2])
            support = GoalSupport.SOLID if e.on_ground else None
            heading_ok = True
            if goal.required_yaw_radians is not None:
                delta = math.atan2(math.sin(e.yaw_radians - goal.required_yaw_radians),
                                   math.cos(e.yaw_radians - goal.required_yaw_radians))
                heading_ok = abs(delta) <= goal.maximum_yaw_error_radians + 1e-12
            if not (inside and (goal.support is GoalSupport.ANY or support is goal.support)
                    and MovementMode.WALK in goal.allowed_modes
                    and e.pose in goal.allowed_poses
                    and speed <= goal.maximum_terminal_speed_blocks_per_second + 1e-12
                    and heading_ok and e.risk_policy_id == goal.risk_policy_id
                    ):
                self._record(e.tick, "I9", "complete violates full GoalState")
        if e.reason == "planning_internal_error":
            self._record(e.tick, "I9", "legal goal produced planning internal error")
