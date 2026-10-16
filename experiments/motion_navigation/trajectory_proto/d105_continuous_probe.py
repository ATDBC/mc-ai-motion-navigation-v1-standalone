"""D105 A3 runs the frozen D101 workload with a sound-sparse proof."""
from __future__ import annotations

from mc2p.contracts.common import ContractViolation

from .commitment import ScanStatus, StopTail, TailStatus
from .d101_continuous_probe import (
    SingleJumpProbeResult,
    SingleJumpProbeSpec,
    _run_one,
    frozen_inputs,
)
from .scenarios import representative_fixture
from .sound_sparse_proof import (
    SoundSparseBranch,
    SoundSparseProof,
    scan_sound_sparse_commitment,
)


def saved_executable_tail(branch: SoundSparseBranch, boundary: int) -> StopTail:
    """Return only a tail already stored in the immutable D105 proof.

    Compact key tails carry the execution summary.  Boundary decisions retain
    the full tick-by-tick tail used to create that summary.  A non-key boundary
    may be used only when its decision also retains a full SAFE_STOP tail.
    """
    if type(branch) is not SoundSparseBranch or type(boundary) is not int:
        raise ContractViolation("saved tail lookup requires a typed proof branch and boundary")
    if not 0 <= boundary < len(branch.boundary_decisions):
        raise ContractViolation("saved tail boundary is outside the proof")
    decision = branch.boundary_decisions[boundary]
    tail = decision.tail
    if type(tail) is not StopTail or tail.status is not TailStatus.SAFE_STOP:
        raise ContractViolation("boundary has no saved executable safe tail")
    compact = branch.safety_tail(boundary)
    if compact is not None and (
            compact.inputs != tail.inputs
            or compact.start_state != tail.states[0]
            or compact.end_state != tail.states[-1]
            or compact.dependencies != tail.dependencies
            or compact.damage_points != tail.damage_points
            or compact.absolute_tick != tail.absolute_tick
            or compact.maximum_ticks != len(tail.inputs)):
        raise ContractViolation("compact safety tail disagrees with its saved execution")
    return tail


def _scan_sound_proof(*args, **kwargs):
    result = scan_sound_sparse_commitment(*args, **kwargs)
    if result.status is ScanStatus.VERIFIED_CANDIDATE and type(result.proof) is not SoundSparseProof:
        raise ContractViolation("D105 probe requires an exact SoundSparseProof")
    return result


def run_sound_single_jump_probe(
        spec: SingleJumpProbeSpec) -> tuple[SingleJumpProbeResult, ...]:
    """Run D101's frozen protocol loop without changing its legacy default."""
    if type(spec) is not SingleJumpProbeSpec:
        raise ContractViolation("D105 single-jump probe requires a typed spec")
    fixture = representative_fixture("jump_gap_1")
    inputs = frozen_inputs()
    return tuple(_run_one(
        spec,
        fixture,
        inputs,
        scanner=_scan_sound_proof,
        tail_resolver=saved_executable_tail,
    ) for _ in range(spec.rounds))
