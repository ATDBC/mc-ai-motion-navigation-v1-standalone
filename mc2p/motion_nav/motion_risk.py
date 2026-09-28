"""Task-scoped damage limits used by motion solving and admission."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputApplicationStatus,
)


MOVEMENT_DAMAGE_BUDGET_RESOURCE = "movement_damage_budget_points"


def _finite_nonnegative(value: float, label: str) -> float:
    if (type(value) not in (int, float)
            or not math.isfinite(float(value))
            or float(value) < 0.0):
        raise ContractViolation(f"{label} must be finite and nonnegative")
    return float(value)


@dataclass(frozen=True, slots=True)
class TaskDamageBudget:
    """Damage one task authorizes a motion candidate to expect."""

    risk_policy_id: str = "no_expected_damage"
    maximum_expected_damage_points: float = 0.0

    def __post_init__(self) -> None:
        require_identifier(self.risk_policy_id, "damage risk policy id")
        object.__setattr__(
            self,
            "maximum_expected_damage_points",
            _finite_nonnegative(
                self.maximum_expected_damage_points,
                "maximum expected damage",
            ),
        )

    def allows(
        self,
        predicted_damage_points: float,
        *,
        health_points: float | None,
        absorption_points: float | None,
    ) -> bool:
        predicted = _finite_nonnegative(
            predicted_damage_points, "predicted damage",
        )
        if predicted > self.maximum_expected_damage_points:
            return False
        if predicted == 0.0:
            return True
        if health_points is None or absorption_points is None:
            return False
        health = _finite_nonnegative(health_points, "health points")
        absorption = _finite_nonnegative(absorption_points, "absorption points")
        return predicted < health + absorption


def conservative_plain_fall_damage_points(fall_distance_blocks: float) -> float:
    """Return the conservative vanilla bound for an ordinary block landing."""

    distance = _finite_nonnegative(fall_distance_blocks, "fall distance")
    return float(max(0, math.ceil(distance - 3.0)))


class RiskReservationStatus(StrEnum):
    RESERVED = "reserved"
    EXISTING = "existing"
    INSUFFICIENT = "insufficient"
    STALE_POLICY = "stale_policy"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    RELEASED_REQUIRES_NEW_ACTION = "released_requires_new_action"
    RISK_OVERRUN = "risk_overrun"


class RiskActionState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    SETTLED = "settled"
    RELEASED = "released"


class RiskCommitKind(StrEnum):
    APPLIED_COMMAND = "applied_command"
    OBSERVED_DEPARTURE = "observed_departure"


class RiskReleaseEvidence(StrEnum):
    ARBITRATION_LOST = "arbitration_lost"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"


class RiskSubmissionStatus(StrEnum):
    SUBMITTED = "submitted"
    DUPLICATE = "duplicate"
    CAPACITY_EXHAUSTED = "capacity_exhausted"


@dataclass(frozen=True, slots=True)
class RiskCommitEvidence:
    kind: RiskCommitKind
    control_sequence: int | None = None
    movement_tick_id: int | None = None
    observation_sequence: int | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not RiskCommitKind:
            raise ContractViolation("risk commit kind must be typed")
        if self.kind is RiskCommitKind.APPLIED_COMMAND:
            if (type(self.control_sequence) is not int or self.control_sequence < 0
                    or type(self.movement_tick_id) is not int
                    or self.movement_tick_id < 0):
                raise ContractViolation("applied risk command needs sequence and tick")
        elif (type(self.observation_sequence) is not int
              or self.observation_sequence < 0):
            raise ContractViolation("risk departure needs an observation sequence")


@dataclass(frozen=True, slots=True)
class RiskActionRecord:
    action_id: str
    expected_damage_points: float
    policy_revision: int
    authorized_limit_points: float
    available_at_reservation_points: float
    state: RiskActionState
    submitted_sequences: tuple[int, ...] = ()
    commit_evidence: RiskCommitEvidence | None = None
    observed_damage_points: float | None = None
    observed_damage_lower_bound_points: float = 0.0
    health_evidence_complete: bool | None = None


@dataclass(slots=True)
class _RiskHealthWindow:
    last_sequence: int
    last_health: float | None
    observed_loss: float = 0.0
    complete: bool = True
    saw_airborne: bool = False
    closed: bool = False


@dataclass(frozen=True, slots=True)
class RiskReservationResult:
    status: RiskReservationStatus
    record: RiskActionRecord | None


_MAX_RISK_ACTIONS = 64
_MAX_SUBMITTED_SEQUENCES = 64


class TaskRiskLedger:
    """Keep a task's held and committed damage across goal and route revisions."""

    def __init__(self, task_id: str, budget: TaskDamageBudget,
                 *, policy_revision: int = 0) -> None:
        require_identifier(task_id, "risk task id")
        if type(budget) is not TaskDamageBudget:
            raise ContractViolation("risk ledger requires a typed budget")
        if type(policy_revision) is not int or policy_revision < 0:
            raise ContractViolation("risk policy revision is invalid")
        self.task_id = task_id
        self.budget = budget
        self.policy_revision = policy_revision
        self._actions: dict[str, RiskActionRecord] = {}
        self._risk_overrun = False
        self._next_action_sequence = 0
        self._health_windows: dict[str, _RiskHealthWindow] = {}

    @property
    def risk_overrun(self) -> bool:
        return self._risk_overrun

    @property
    def committed_points(self) -> float:
        return sum(record.expected_damage_points
                   for record in self._actions.values()
                   if record.state in {RiskActionState.COMMITTED,
                                       RiskActionState.SETTLED})

    @property
    def held_points(self) -> float:
        return sum(record.expected_damage_points
                   for record in self._actions.values()
                   if record.state is RiskActionState.RESERVED)

    @property
    def available_points(self) -> float:
        if self._risk_overrun:
            return 0.0
        return max(0.0, self.budget.maximum_expected_damage_points
                   - self.committed_points - self.held_points)

    def action(self, action_id: str) -> RiskActionRecord | None:
        return self._actions.get(action_id)

    def snapshot_actions(self) -> tuple[RiskActionRecord, ...]:
        return tuple(self._actions.values())

    def next_action_id(self) -> str:
        self._next_action_sequence += 1
        return f"{self.task_id}/risk-{self._next_action_sequence}"

    def update_policy(self, budget: TaskDamageBudget, *, revision: int) -> None:
        if type(budget) is not TaskDamageBudget:
            raise ContractViolation("risk budget must be typed")
        if type(revision) is not int or revision <= self.policy_revision:
            raise ContractViolation("risk policy revision must advance")
        self.budget = budget
        self.policy_revision = revision

    def reserve(self, action_id: str, expected_damage_points: float,
                *, policy_revision: int) -> RiskReservationResult:
        require_identifier(action_id, "risk action id")
        expected = _finite_nonnegative(expected_damage_points,
                                       "expected action damage")
        old = self._actions.get(action_id)
        if old is not None:
            if abs(old.expected_damage_points - expected) > 1.0e-9:
                raise ContractViolation("risk action changed expected damage")
            if old.state is RiskActionState.RELEASED:
                return RiskReservationResult(
                    RiskReservationStatus.RELEASED_REQUIRES_NEW_ACTION, old,
                )
            if (self._risk_overrun and old.state is RiskActionState.RESERVED
                    and not old.submitted_sequences):
                return RiskReservationResult(
                    RiskReservationStatus.RISK_OVERRUN, old,
                )
            if (old.state is RiskActionState.RESERVED
                    and old.policy_revision != self.policy_revision
                    and not old.submitted_sequences):
                self._actions[action_id] = replace(
                    old, state=RiskActionState.RELEASED,
                )
                return RiskReservationResult(
                    RiskReservationStatus.STALE_POLICY,
                    self._actions[action_id],
                )
            return RiskReservationResult(RiskReservationStatus.EXISTING, old)
        if self._risk_overrun:
            return RiskReservationResult(RiskReservationStatus.RISK_OVERRUN, None)
        if policy_revision != self.policy_revision:
            return RiskReservationResult(RiskReservationStatus.STALE_POLICY, None)
        if len(self._actions) >= _MAX_RISK_ACTIONS:
            return RiskReservationResult(RiskReservationStatus.CAPACITY_EXHAUSTED, None)
        if expected > self.available_points + 1.0e-9:
            return RiskReservationResult(RiskReservationStatus.INSUFFICIENT, None)
        record = RiskActionRecord(
            action_id, expected, policy_revision,
            self.budget.maximum_expected_damage_points,
            self.available_points,
            RiskActionState.RESERVED,
        )
        self._actions[action_id] = record
        return RiskReservationResult(RiskReservationStatus.RESERVED, record)

    def mark_submitted(self, action_id: str,
                       control_sequence: int) -> RiskSubmissionStatus:
        record = self._actions[action_id]
        if type(control_sequence) is not int or control_sequence < 0:
            raise ContractViolation("risk submission sequence is invalid")
        if record.state is RiskActionState.RELEASED:
            raise ContractViolation("released risk action cannot submit input")
        if record.state is not RiskActionState.RESERVED:
            return RiskSubmissionStatus.DUPLICATE
        if self._risk_overrun and not record.submitted_sequences:
            raise ContractViolation("risk overrun forbids a new action start")
        if (record.policy_revision != self.policy_revision
                and not record.submitted_sequences):
            raise ContractViolation("risk policy changed before first submission")
        if control_sequence in record.submitted_sequences:
            return RiskSubmissionStatus.DUPLICATE
        if len(record.submitted_sequences) >= _MAX_SUBMITTED_SEQUENCES:
            # The selected input may already be in flight. Keep its risk held;
            # the caller must stop submitting this action and land safely.
            return RiskSubmissionStatus.CAPACITY_EXHAUSTED
        self._actions[action_id] = replace(
            record, submitted_sequences=(*record.submitted_sequences,
                                         control_sequence),
        )
        return RiskSubmissionStatus.SUBMITTED

    def commit(self, action_id: str, evidence: RiskCommitEvidence) -> bool:
        if type(evidence) is not RiskCommitEvidence:
            raise ContractViolation("risk commit needs typed application evidence")
        record = self._actions[action_id]
        if record.state in {RiskActionState.COMMITTED, RiskActionState.SETTLED}:
            return False
        if record.state is not RiskActionState.RESERVED:
            raise ContractViolation("released risk action cannot commit")
        if (evidence.kind is RiskCommitKind.APPLIED_COMMAND
                and evidence.control_sequence not in record.submitted_sequences):
            raise ContractViolation("risk command was not selected by Runtime")
        self._actions[action_id] = replace(
            record, state=RiskActionState.COMMITTED, commit_evidence=evidence,
        )
        return True

    def observe_health(self, action_id: str, *, observation_sequence: int,
                       health_points: float | None,
                       on_ground: bool) -> bool:
        """Conservatively charge observed health loss during a committed drop.

        Missing health or a skipped observation leaves the commitment in place
        without claiming that the observed loss is complete.
        """
        if type(observation_sequence) is not int or observation_sequence < 0:
            raise ContractViolation("risk health observation sequence is invalid")
        if health_points is not None:
            health_points = _finite_nonnegative(health_points, "observed health")
        if type(on_ground) is not bool:
            raise ContractViolation("risk health support flag is invalid")
        record = self._actions[action_id]
        if record.state in {RiskActionState.RELEASED, RiskActionState.SETTLED}:
            return False
        window = self._health_windows.get(action_id)
        if window is None:
            self._health_windows[action_id] = _RiskHealthWindow(
                observation_sequence, health_points,
                complete=health_points is not None,
                saw_airborne=not on_ground,
            )
            return False
        if window.closed:
            return False
        if observation_sequence <= window.last_sequence:
            return False
        if (record.state is RiskActionState.RESERVED
                and not record.submitted_sequences):
            window.last_sequence = observation_sequence
            window.last_health = health_points
            window.observed_loss = 0.0
            window.complete = health_points is not None
            window.saw_airborne = not on_ground
            return False
        if (observation_sequence != window.last_sequence + 1
                or health_points is None or window.last_health is None):
            window.complete = False
        if health_points is not None and window.last_health is not None:
            # Even with a missing intermediate sample, two known endpoints
            # prove at least their net decline. Keep the last known value.
            window.observed_loss += max(0.0, window.last_health - health_points)
        window.last_sequence = observation_sequence
        if health_points is not None:
            window.last_health = health_points
        window.saw_airborne |= not on_ground
        if window.observed_loss > record.observed_damage_lower_bound_points:
            record = replace(
                record,
                observed_damage_lower_bound_points=window.observed_loss,
            )
            self._actions[action_id] = record
        if (record.state is RiskActionState.COMMITTED
                and window.observed_loss
                    > record.expected_damage_points + 1.0e-9):
            self._risk_overrun = True
        if record.state is RiskActionState.COMMITTED and on_ground \
                and window.saw_airborne:
            window.closed = True
            if not window.complete:
                self._actions[action_id] = replace(
                    record, health_evidence_complete=False,
                )
                return False
            self.settle(action_id, observed_damage_points=window.observed_loss)
            self._actions[action_id] = replace(
                self._actions[action_id], health_evidence_complete=True,
            )
            return True
        return False

    def close_health_window(self, action_id: str, *, on_ground: bool) -> bool:
        """Freeze an ended action without refunding its committed allowance."""
        if type(on_ground) is not bool:
            raise ContractViolation("risk action closure support flag is invalid")
        if not on_ground:
            return False
        record = self._actions[action_id]
        if record.state not in {RiskActionState.COMMITTED,
                                RiskActionState.SETTLED}:
            return False
        window = self._health_windows.get(action_id)
        if window is None or window.closed:
            return False
        window.closed = True
        if record.state is RiskActionState.COMMITTED:
            if window.complete:
                self.settle(action_id, observed_damage_points=window.observed_loss)
                self._actions[action_id] = replace(
                    self._actions[action_id], health_evidence_complete=True,
                )
            else:
                self._actions[action_id] = replace(
                    record, health_evidence_complete=False,
                )
        return True

    def release_unstarted(self, action_id: str,
                          evidence: RiskReleaseEvidence, *,
                          input_ledger: InputApplicationLedger | None = None) -> bool:
        if type(evidence) is not RiskReleaseEvidence:
            raise ContractViolation("risk release evidence must be typed")
        record = self._actions[action_id]
        if record.state is RiskActionState.RELEASED:
            return True
        if record.state is not RiskActionState.RESERVED:
            return False
        if record.submitted_sequences:
            if evidence is not RiskReleaseEvidence.CONFIRMED_NOT_APPLIED \
                    or input_ledger is None:
                return False
            records = {item.control_sequence: item
                       for item in input_ledger.snapshot()}
            if any(sequence not in records
                   or records[sequence].applied_ticks
                   or records[sequence].status is not InputApplicationStatus.REJECTED
                   for sequence in record.submitted_sequences):
                return False
        self._actions[action_id] = replace(record, state=RiskActionState.RELEASED)
        return True

    def settle(self, action_id: str, *, observed_damage_points: float) -> bool:
        observed = _finite_nonnegative(observed_damage_points,
                                       "observed action damage")
        record = self._actions[action_id]
        if record.state is RiskActionState.SETTLED:
            return (record.observed_damage_points is not None
                    and record.observed_damage_points
                        > record.expected_damage_points + 1.0e-9)
        if record.state is not RiskActionState.COMMITTED:
            raise ContractViolation("uncommitted risk action cannot settle")
        self._actions[action_id] = replace(
            record, state=RiskActionState.SETTLED,
            observed_damage_points=observed,
        )
        exceeded = observed > record.expected_damage_points + 1.0e-9
        self._risk_overrun |= exceeded
        return exceeded

    def authorized_commit(self, action_id: str) -> tuple[int, float] | None:
        record = self._actions[action_id]
        if record.state not in {RiskActionState.COMMITTED,
                                RiskActionState.SETTLED}:
            return None
        return record.policy_revision, record.authorized_limit_points
