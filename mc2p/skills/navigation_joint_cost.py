"""Bounded route/gaze timing costs. Scores are neither seconds nor probabilities."""
from __future__ import annotations

from mc2p.contracts.common import ContractViolation, require_nonnegative_int


def union_duration_ns(intervals: tuple[tuple[int, int], ...]) -> int:
    if type(intervals) is not tuple or len(intervals) > 16:
        raise ContractViolation('joint intervals must be a bounded tuple')
    for interval in intervals:
        if type(interval) is not tuple or len(interval) != 2:
            raise ContractViolation('joint interval requires start and end')
        start, end = interval
        require_nonnegative_int(start, 'interval start')
        require_nonnegative_int(end, 'interval end')
        if end < start:
            raise ContractViolation('joint interval end precedes start')
    total = 0
    right = 0
    for start, end in sorted(intervals):
        total += max(0, end-max(start, right))
        right = max(right, end)
    return total


def joint_score(candidate) -> float:
    from mc2p.skills.normal_navigation_types import JointCandidate
    if type(candidate) is not JointCandidate:
        raise ContractViolation('joint score requires frozen JointCandidate')
    return (union_duration_ns(candidate.intervals)/1e9
            + 4*candidate.progress_debt_seconds + candidate.recovery_seconds
            + 2*candidate.uncertainty_penalty + 4*candidate.gaze_debt)
