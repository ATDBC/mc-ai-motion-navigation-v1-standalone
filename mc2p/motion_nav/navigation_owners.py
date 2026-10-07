"""Small state owners used by the navigation coordinator.

Each owner advances its own facts. Lifecycle, planning and body authority stay
with their existing owners; information suggestions do not grant control.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
import math

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.action_v1 import LookV1
from mc2p.motion_nav.action_preconditions import AcquisitionGrant
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.movement_transition import GoalState
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
from mc2p.motion_nav.probe_body_controller import ProbeOutcome, ProbeOutcomeKind
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe, LandingEdgeProbeState
from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence, ProgressKind,
    RetryLedger, RetryLedgerCapacityExceeded, WaitPolicy, WaitVerdict,
)
from mc2p.motion_nav.landing_evidence import direct_drop_visual_evidence_sufficient
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.async_work import AsyncComputationScope, ComputationInvalidationCause
from mc2p.contracts.common import require_identifier


@dataclass(frozen=True, slots=True)
class PendingGoalRevision:
    """One validated goal revision waiting for information or body release."""

    goal_id: str
    goal_revision: int
    goal_state: GoalState
    damage_budget: TaskDamageBudget

    def __post_init__(self) -> None:
        if not isinstance(self.goal_id, str) or not self.goal_id:
            raise ContractViolation("pending goal id is required")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("pending goal revision is invalid")
        if type(self.goal_state) is not GoalState:
            raise ContractViolation("pending goal state must be typed")
        if type(self.damage_budget) is not TaskDamageBudget:
            raise ContractViolation("pending goal damage budget must be typed")


@dataclass(slots=True)
class GoalRequestLedger:
    """Own the one planning request currently visible to the planner."""

    request: Any | None = None
    _reach_policy: GoalReachPolicy | None = field(default=None, init=False)
    _planning_policy: GoalPlanningPolicy | None = field(default=None, init=False)
    _computation_scope: AsyncComputationScope | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.request is not None:
            self.accept(self.request)

    @property
    def current_computation_scope(self) -> AsyncComputationScope:
        if self._computation_scope is None:
            raise ContractViolation("goal ledger computation scope is not bound")
        return self._computation_scope

    def bind_computation_scope(self, task_id: str, world_session_id: str) -> AsyncComputationScope:
        require_identifier(task_id, "computation task")
        require_identifier(world_session_id, "computation world session")
        if self._computation_scope is None:
            self._computation_scope = AsyncComputationScope(world_session_id, task_id, 1)
        elif task_id != self._computation_scope.task_id:
            raise ContractViolation("goal ledger cannot change computation task")
        elif world_session_id != self._computation_scope.world_session_id:
            return self.invalidate_computation(ComputationInvalidationCause.WORLD_CHANGED,
                                               world_session_id=world_session_id)
        return self._computation_scope

    def invalidate_computation(self, cause: ComputationInvalidationCause, *,
                               world_session_id: str | None = None) -> AsyncComputationScope:
        if type(cause) is not ComputationInvalidationCause:
            raise ContractViolation("computation invalidation cause must be typed")
        scope = self.current_computation_scope
        if cause is ComputationInvalidationCause.WORLD_CHANGED:
            require_identifier(world_session_id, "new computation world session")
        elif world_session_id is not None:
            raise ContractViolation("only world invalidation may change world session")
        self._computation_scope = AsyncComputationScope(
            world_session_id or scope.world_session_id, scope.task_id, scope.generation + 1)
        return self._computation_scope

    def resume_computation_after_reanchor(self, previous: GoalRequestLedger) -> None:
        """A fresh same-task ledger continues the closed Session's scope."""
        if (type(previous) is not GoalRequestLedger or previous is self
                or self.request is not None or self._computation_scope is not None):
            raise ContractViolation("computation continuation requires a fresh goal ledger")
        if previous._computation_scope is not None:
            self._computation_scope = previous._computation_scope
            self.invalidate_computation(ComputationInvalidationCause.NEW_STATE_ANCHOR)
        self.select_reach_policy(previous.reach_policy)
        self.select_planning_policy(previous.planning_policy)

    @property
    def reach_policy(self) -> GoalReachPolicy:
        return self._reach_policy or GoalReachPolicy.COMPLETE_ON_REACH

    def select_reach_policy(self, policy: GoalReachPolicy) -> None:
        if type(policy) is not GoalReachPolicy:
            raise ContractViolation("goal reach policy must be typed")
        if self._reach_policy is not None and policy is not self._reach_policy:
            raise ContractViolation("goal revision cannot change task reach policy")
        self._reach_policy = policy

    @property
    def planning_policy(self) -> GoalPlanningPolicy:
        return self._planning_policy or GoalPlanningPolicy.BACKGROUND_PLANNER

    def select_planning_policy(self, policy: GoalPlanningPolicy) -> None:
        if type(policy) is not GoalPlanningPolicy:
            raise ContractViolation("goal planning policy must be typed")
        if self._planning_policy is not None and policy is not self._planning_policy:
            raise ContractViolation("goal revision cannot change task planning policy")
        self._planning_policy = policy

    def accept(self, request: Any | None) -> None:
        if request is not None:
            self.select_reach_policy(request.reach_policy)
        self.request = request

    def advance(self, session_id: str, **changes: Any) -> Any:
        """Revise the current request without changing computation scope."""
        if self.request is None:
            raise ContractViolation("goal request ledger has no active request")
        if not isinstance(session_id, str) or not session_id:
            raise ContractViolation("goal request session id is required")
        sequence = self.request.sequence + 1
        advanced = replace(
            self.request,
            sequence=sequence,
            request_id=f"{session_id}-request-{sequence}",
            **changes,
        )
        self.accept(advanced)
        return advanced


@dataclass(slots=True)
class PlanningPipelineState:
    """Own snapshot construction and request-local world changes."""

    builder: Any | None = None
    snapshot: Any | None = None
    snapshot_request_id: str | None = None
    changed_cells: set[BlockPos] = field(default_factory=set)
    attempt_started_movement_tick: int | None = None
    attempt_started_monotonic_ns: int | None = None
    attempt_deadline_monotonic_ns: int | None = None
    submitted_request_id: str | None = None
    submitted_movement_tick: int | None = None
    submitted_monotonic_ns: int | None = None
    result_deadline_monotonic_ns: int | None = None

    def clear_submission(self) -> None:
        self.submitted_request_id = None
        self.submitted_movement_tick = None
        self.submitted_monotonic_ns = None
        self.result_deadline_monotonic_ns = None

    def clear(self) -> None:
        self.builder = None
        self.snapshot = None
        self.snapshot_request_id = None
        self.changed_cells.clear()
        self.attempt_started_movement_tick = None
        self.attempt_started_monotonic_ns = None
        self.attempt_deadline_monotonic_ns = None
        self.clear_submission()


@dataclass(frozen=True, slots=True)
class AcquiredInformationFacts:
    acquired_cells: tuple[BlockPos, ...]
    align_probe_entry: bool = False
    progressed: bool = False


@dataclass(frozen=True, slots=True)
class InformationAdvance:
    statuses: tuple[tuple[BlockPos, str], ...]
    missing_cells: tuple[BlockPos, ...]
    lower_required: frozenset[BlockPos]
    look: LookV1 | None
    start_or_continue_probe: bool
    acquired_cells: tuple[BlockPos, ...] = ()
    wait_verdict: WaitVerdict | None = None
    capacity_exhausted: bool = False

    def timeout_reason(self, *, frontier_truncated: bool = False) -> str:
        if frontier_truncated:
            return "information_frontier_truncated"
        if self.capacity_exhausted:
            return "information_capacity_exhausted"
        statuses = tuple(status for _, status in self.statuses)
        if statuses and all(status == "occluded" for status in statuses):
            return "information_occluded_requires_observation_position"
        if statuses and all(status == "out_of_range" for status in statuses):
            return "information_out_of_range"
        return "information_unavailable_timeout"


@dataclass(slots=True)
class InformationAcquisitionState:
    """Own the facts and wait state for the one active information need."""

    missing_cells: tuple[BlockPos, ...] = ()
    residual_missing_cells: tuple[BlockPos, ...] = ()
    statuses: dict[BlockPos, str] = field(default_factory=dict)
    lower_required: set[BlockPos] = field(default_factory=set)
    completed_grant: AcquisitionGrant | None = None
    wait_frames: int = 0
    completed_probe: ProbeOutcome | None = None

    def observe(
        self, frame: NavigationFrame, changed_cells: tuple[BlockPos, ...], *,
        landing_acquisition_pending: bool, edge_probe: LandingEdgeProbe | None = None,
        ledger: RetryLedger | None = None, wait_owner_id: str | None = None,
    ) -> AcquiredInformationFacts:
        missing = set(self.missing_cells)
        visible = tuple(result for result in frame.air_query_results
                        if result.status == "visible_air" and result.position in missing)
        align_probe = (landing_acquisition_pending and edge_probe is not None
                       and edge_probe.state is LandingEdgeProbeState.HOLDING_EDGE
                       and any(edge_probe.has_observed_evidence(frame, result.position)
                               for result in visible))
        if landing_acquisition_pending:
            visible = tuple(result for result in visible
                            if direct_drop_visual_evidence_sufficient(
                                frame, result.position, edge_probe=edge_probe))
            if align_probe:
                visible = ()
        changed = set(changed_cells).intersection(missing)
        if landing_acquisition_pending:
            changed = {position for position in changed
                       if frame.world.cell(position).knowledge is CellKnowledge.BLOCK
                       or direct_drop_visual_evidence_sufficient(
                           frame, position, edge_probe=edge_probe)}
        acquired = tuple(sorted(changed | {result.position for result in visible}))
        progressed = False
        if ledger is not None:
            if wait_owner_id is None:
                raise ContractViolation(
                    "information progress requires its wait owner identity"
                )
            for x, y, z in acquired:
                progressed |= ledger.record_progress(ProgressEvidence(
                    ProgressKind.BLOCKING_FACT, frame.body.sequence_id,
                    fact_id=f"cell/{x}/{y}/{z}"))
            if progressed:
                self.end_wait(ledger, wait_owner_id)
        return AcquiredInformationFacts(acquired, align_probe, progressed)

    @staticmethod
    def end_wait(ledger: RetryLedger, owner_id: str) -> bool:
        """End the information wait only for the owner leaving that need."""
        if type(ledger) is not RetryLedger:
            raise ContractViolation("information wait requires a retry ledger")
        return ledger.end_wait_owned("information", owner_id)

    def advance(
        self, frame: NavigationFrame, *, landing_acquisition_pending: bool,
        edge_probe: LandingEdgeProbe | None = None,
        ledger: RetryLedger | None = None, wait_owner_id: str | None = None,
        wait_policy: WaitPolicy | None = None, now_ns: int | None = None,
        planning_wait: bool = False,
    ) -> InformationAdvance:
        missing = set(self.missing_cells)
        self.statuses = {position: status for position, status in self.statuses.items()
                         if position in missing}
        self.lower_required.intersection_update(missing)
        for result in frame.air_query_results:
            if result.position not in missing:
                continue
            lower = (result.status == "visible_air"
                     and not direct_drop_visual_evidence_sufficient(
                         frame, result.position, edge_probe=edge_probe)) or (
                         result.status == "occluded" and landing_acquisition_pending)
            if lower:
                self.lower_required.add(result.position)
            else:
                self.lower_required.discard(result.position)
            self.statuses[result.position] = result.status
        look = information_look_for_missing_cells(
            frame, self.missing_cells, self.statuses,
            lower_region_positions=frozenset(self.lower_required))
        unresolved = tuple(sorted((position, self.statuses.get(position, "unknown"))
                                  for position in self.missing_cells))
        verdict = None
        capacity_exhausted = False
        if unresolved and ledger is not None and edge_probe is None:
            if wait_owner_id is None or wait_policy is None or now_ns is None:
                raise ContractViolation("information wait requires owner, policy and clock")
            movement_tick = (frame.body.movement_tick_id
                             if frame.body.movement_tick_id is not None
                             else frame.body.sequence_id)
            try:
                if not planning_wait:
                    ledger.set_blockers(tuple(f"cell/{x}/{y}/{z}" for (x, y, z), _ in unresolved))
                token = ledger.begin_wait("information", wait_owner_id, wait_policy,
                                          movement_tick, now_ns)
                self.wait_frames = movement_tick - token.started_movement_tick
                verdict = ledger.check_wait("information", movement_tick, now_ns)
            except RetryLedgerCapacityExceeded:
                verdict = WaitVerdict.EXHAUSTED_TICKS
                capacity_exhausted = True
        acquired = self.observe(
            frame, (), landing_acquisition_pending=landing_acquisition_pending,
            edge_probe=edge_probe,
        )
        return InformationAdvance(
            statuses=unresolved, missing_cells=self.missing_cells,
            lower_required=frozenset(self.lower_required), look=look,
            start_or_continue_probe=bool(self.lower_required) and landing_acquisition_pending,
            acquired_cells=acquired.acquired_cells, wait_verdict=verdict,
            capacity_exhausted=capacity_exhausted,
        )

    def complete_probe(self, outcome: ProbeOutcome) -> None:
        if type(outcome) is not ProbeOutcome:
            raise ContractViolation("information completion requires a typed probe fact")
        self.completed_probe = outcome
        if outcome.kind is ProbeOutcomeKind.READY and outcome.grant is not None:
            self.completed_grant = outcome.grant

    def awaiting_probe_pose(self, frame: NavigationFrame) -> bool:
        outcome = self.completed_probe
        if outcome is None or outcome.kind is not ProbeOutcomeKind.STOPPED:
            return False
        if outcome.world_session != frame.session:
            self.completed_probe = None
            return False
        if frame.body.is_sneaking or frame.body.pose == "crouching":
            return True
        self.completed_probe = None
        return False

    def probe_wait_expired(
        self, probe: LandingEdgeProbe, frame: NavigationFrame,
        ledger: RetryLedger, policy: WaitPolicy, now_ns: int,
    ) -> bool:
        cell = probe.landing_cell
        wait_id = probe.acquisition_id or f"acquisition/{cell[0]}/{cell[1]}/{cell[2]}"
        movement_tick = (frame.body.movement_tick_id if frame.body.movement_tick_id is not None
                         else frame.body.sequence_id)
        ledger.begin_wait(wait_id, probe.owner_id, policy, movement_tick, now_ns)
        return ledger.check_wait(wait_id, movement_tick, now_ns) is not WaitVerdict.WAITING

    def clear_request(self) -> None:
        self.select_missing(())
        self.wait_frames = 0

    def select_missing(self, cells: tuple[BlockPos, ...]) -> None:
        """Replace query facts without rewriting the existing wait diagnostics."""
        self.missing_cells = cells
        self.statuses.clear()
        self.lower_required.clear()


_FORMAL_HALF_FOV_DEGREES = 60.0
_INFORMATION_LOOK_MAX_DELTA_DEGREES = 36.0


def information_look_for_missing_cells(
        frame: NavigationFrame,
        positions: tuple[BlockPos, ...],
        statuses: dict[BlockPos, str],
        *,
        lower_region_positions: frozenset[BlockPos] = frozenset(),
) -> LookV1 | None:
    """Choose the same bounded information look used by formal navigation."""
    eye_x = frame.body.position[0]
    eye_y = frame.body.body_box.max_y - 0.18
    eye_z = frame.body.position[2]
    current_yaw = math.degrees(frame.body.yaw_radians)
    current_pitch = math.degrees(frame.body.pitch_radians)
    candidates: list[tuple[float, float, float]] = []
    for x, y, z in positions:
        status = statuses.get((x, y, z))
        if status in {"out_of_range", "occluded", "unavailable"}:
            continue
        dx = x + 0.5 - eye_x
        target_y = y + (0.05 if (x, y, z) in lower_region_positions else 0.5)
        dy = target_y - eye_y
        dz = z + 0.5 - eye_z
        horizontal = math.hypot(dx, dz)
        desired_yaw = (
            current_yaw
            if horizontal <= 1.0e-9
            else math.degrees(math.atan2(-dx, dz))
        )
        desired_pitch = -math.degrees(
            math.atan2(dy, max(horizontal, 1.0e-9))
        )
        yaw_error = (desired_yaw - current_yaw + 180.0) % 360.0 - 180.0
        pitch_error = desired_pitch - current_pitch
        if status is None and (
                abs(yaw_error) <= _FORMAL_HALF_FOV_DEGREES
                and abs(pitch_error) <= _FORMAL_HALF_FOV_DEGREES):
            continue
        candidates.append((
            yaw_error * yaw_error + pitch_error * pitch_error,
            yaw_error,
            pitch_error,
        ))
    if not candidates:
        return None
    _, yaw_error, pitch_error = min(candidates)
    limit = _INFORMATION_LOOK_MAX_DELTA_DEGREES
    yaw_delta = max(-limit, min(limit, yaw_error))
    pitch_delta = max(-limit, min(limit, pitch_error))
    if abs(yaw_delta) <= 1.0e-6 and abs(pitch_delta) <= 1.0e-6:
        return None
    return LookV1(yaw_delta, pitch_delta)
