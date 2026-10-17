"""One-shot confirmation of perception needs from lawful post-observations."""

from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int
from mc2p.skills.navigation_evidence import EvidenceStamp, NavigationEvidence
from mc2p.skills.perception_needs import MotionEvidenceReport, PerceptionNeed


FRESHNESS_NS = 500_000_000
YAW_TOLERANCE_DEGREES = 8.0
PITCH_TOLERANCE_DEGREES = 6.0


def _wrap(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def _lawful_post(need: PerceptionNeed, evidence: NavigationEvidence) -> bool:
    current = evidence.stamp
    prior = need.based_on
    return (
        current.episode_id == prior.episode_id
        and current.controller_clock_id == prior.controller_clock_id
        and current.client_sample.clock_id == prior.client_sample.clock_id
        and current.source_backend == prior.source_backend
        and current.sequence_id > prior.sequence_id
        and current.client_sample.started_at_monotonic_ns
        >= prior.client_sample.completed_at_monotonic_ns
        and current.received_at_ns >= prior.received_at_ns
    )


def _actual_filtered_sample(evidence: NavigationEvidence) -> bool:
    return bool(
        evidence.available
        and evidence.pose is not None
        and evidence.coverage is not None
        and evidence.coverage.source_kind == "client_perception_filtered"
    )


def need_met(
    need: PerceptionNeed,
    evidence: NavigationEvidence,
    motion_report: MotionEvidenceReport | None,
) -> bool:
    """Check common post-evidence predicates, never authorize movement.

    A motion need has no movement proposal. The owning coordinator MUST first
    bind the report to its current proposal (direction, horizon, jump and
    contact allowance). This predicate cannot establish that caller-level
    binding; neither a True result nor the bounded summary is permission.
    """

    if type(need) is not PerceptionNeed:
        raise ContractViolation("confirmation need must be PerceptionNeed")
    if type(evidence) is not NavigationEvidence:
        raise ContractViolation("confirmation evidence must be NavigationEvidence")
    if motion_report is not None and type(motion_report) is not MotionEvidenceReport:
        raise ContractViolation("confirmation motion report must be MotionEvidenceReport or null")
    if not _actual_filtered_sample(evidence) or not _lawful_post(need, evidence):
        return False

    if need.condition == "filtered_check":
        return True
    if need.condition == "visible_track":
        return any(entity.track_id == need.track_id for entity in evidence.entities)
    if need.condition == "observed_block":
        return any(block.position == need.block for block in evidence.blocks)
    if need.condition == "center_block_face":
        from mc2p.skills.targeting import block_target_matches
        return block_target_matches(evidence.field_profile,evidence.targeting,
                                    block_position=need.block,face=need.face)
    if need.condition == "motion_guard":
        if motion_report is None or evidence.pose is None:
            return False
        expected_floor = round(evidence.pose.position.y) - 1
        return (
            motion_report.scope_id == need.scope_id
            and motion_report.based_on == evidence.stamp
            and motion_report.reason is None
            and not motion_report.guard.gaps
            and motion_report.guard.inspected_steps > 0
            and abs(_wrap(motion_report.checked_yaw_degrees - evidence.pose.yaw)) <= 1e-6
            and motion_report.floor == expected_floor
            and bool(motion_report.evidence)
            and motion_report.earliest_expiry_ns is not None
            and motion_report.earliest_expiry_ns >= evidence.stamp.received_at_ns
        )
    return False


@dataclass(frozen=True, slots=True)
class _Pending:
    need: PerceptionNeed
    yaw: float
    pitch: float


@dataclass(frozen=True, slots=True)
class _Candidate:
    request: _Pending
    evidence: NavigationEvidence


class PerceptionConfirmation:
    """Hold one pending request and at most one causal post-observation."""

    def __init__(self) -> None:
        self._last_registered: EvidenceStamp | None = None
        self.clear()

    def clear(self) -> None:
        self._pending: _Pending | None = None
        self._candidate: _Candidate | None = None

    def request(self, need: PerceptionNeed, yaw: float, pitch: float) -> None:
        if type(need) is not PerceptionNeed:
            raise ContractViolation("confirmation need must be PerceptionNeed")
        require_finite(yaw, "confirmation yaw")
        require_finite(pitch, "confirmation pitch")
        self._pending = _Pending(need, _wrap(float(yaw)), max(-90.0, min(90.0, float(pitch))))
        self._candidate = None

    def feedback(self, selected: bool, evidence: NavigationEvidence, now_ns: int) -> None:
        if type(selected) is not bool:
            raise ContractViolation("confirmation selected feedback must be boolean")
        if type(evidence) is not NavigationEvidence:
            raise ContractViolation("confirmation evidence must be NavigationEvidence")
        require_nonnegative_int(now_ns, "confirmation feedback time")
        self._candidate = None
        pending = self._pending
        if not selected or pending is None:
            return
        stamp = evidence.stamp
        previous = self._last_registered
        if (previous is not None and stamp.scope == previous.scope
                and stamp.source_backend == previous.source_backend
                and stamp.sequence_id <= previous.sequence_id):
            return
        if (
            not _lawful_post(pending.need, evidence)
            or stamp.request_start_ns > stamp.received_at_ns
            or now_ns < stamp.received_at_ns
            or not 0 <= now_ns - stamp.request_start_ns <= FRESHNESS_NS
            or now_ns >= pending.need.deadline_ns
        ):
            return
        self._last_registered = stamp
        self._candidate = _Candidate(pending, evidence)

    def consume(
        self,
        evidence: NavigationEvidence,
        now_ns: int,
        *,
        motion_report: MotionEvidenceReport | None = None,
    ) -> bool:
        if type(evidence) is not NavigationEvidence:
            raise ContractViolation("confirmation evidence must be NavigationEvidence")
        require_nonnegative_int(now_ns, "confirmation consume time")
        if motion_report is not None and type(motion_report) is not MotionEvidenceReport:
            raise ContractViolation("confirmation motion report must be MotionEvidenceReport or null")
        candidate = self._candidate
        if candidate is None or evidence != candidate.evidence:
            return False

        self._candidate = None
        self._pending = None
        stamp = evidence.stamp
        if (
            now_ns < stamp.received_at_ns
            or not 0 <= now_ns - stamp.request_start_ns <= FRESHNESS_NS
            or now_ns >= candidate.request.need.deadline_ns
            or not evidence.available
            or evidence.pose is None
            or abs(_wrap(evidence.pose.yaw - candidate.request.yaw)) > YAW_TOLERANCE_DEGREES
            or abs(evidence.pose.pitch - candidate.request.pitch) > PITCH_TOLERANCE_DEGREES
            or (candidate.request.need.condition == "motion_guard"
                and (motion_report is None or motion_report.earliest_expiry_ns is None
                     or now_ns > motion_report.earliest_expiry_ns))
        ):
            return False
        return need_met(candidate.request.need, evidence, motion_report)


__all__ = ["PerceptionConfirmation", "need_met"]
