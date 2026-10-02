"""Measure the pilot's actual job, transport and lightweight admission separately."""
from dataclasses import replace
import argparse
import json
import math
from pathlib import Path
import pickle
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.motion_candidate import MotionCandidateContext, VerifiedMotionCandidate
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_worker import MotionSolverWorker, _execute_job
from mc2p.motion_nav.online_motion import InputApplicationLedger
from tests.motion_nav.test_action_continuity_formal import ActionContinuityFormalTests, _gap_case
from tests.motion_nav.test_action_continuity_admission import admit
from tests.motion_nav.test_b10_motion_candidate import VerifiedMotionExecutorTests
from mc2p.motion_nav import motion_solver


def summary(values):
    ordered = sorted(values)
    return dict(samples=len(values), median_ns=ordered[(len(values)-1)//2],
                p95_ns=ordered[math.ceil(.95*len(values))-1],
                p99_ns=ordered[math.ceil(.99*len(values))-1], maximum_ns=max(values))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be new')
    result, source_worker, _ = ActionContinuityFormalTests()._run(_gap_case())
    if result.outcome != 'success':
        raise RuntimeError('representative formal case failed')
    job = source_worker.jobs[0]
    base = _execute_job(job)
    proof = base.solve_result.proof
    if proof is None:
        raise RuntimeError(base.solve_result)
    actual_entry = replace(job.anchor, physics_state=proof.entry_state,
                          movement_tick_id=proof.anchor_movement_tick_id,
                          observation_sequence_id=job.anchor.observation_sequence_id+len(job.entry_prefix))
    ledger = InputApplicationLedger()
    for index, command in enumerate(job.entry_prefix):
        tick = job.anchor.movement_tick_id + index + 1
        VerifiedMotionExecutorTests.applied(ledger, job.anchor, 100+index, tick, command.movement)
    context = MotionCandidateContext('bench-request', 1, 'bench-goal', 1,
                                    'bench-route', 1, 1, 1, TaskDamageBudget(),
                                    proof.resource_incomplete_reasons)
    candidate = VerifiedMotionCandidate(proof, context)
    check = admit(candidate, actual_entry, input_ledger=ledger)
    if check.candidate is None:
        raise RuntimeError(check)
    timing = []
    # A complete recovery replay is forbidden on this measured path.
    with patch('mc2p.motion_nav.motion_solver.revalidate_air_transition',
               side_effect=AssertionError('synchronous revalidation')):
        for index in range(65):
            started = time.perf_counter_ns()
            accepted = admit(candidate, actual_entry, input_ledger=ledger)
            elapsed = time.perf_counter_ns()-started
            if accepted.candidate is None:
                raise RuntimeError(accepted)
            if index >= 5:
                timing.append(elapsed)
    # Count separately, so instrumentation does not contaminate timing.
    physics_calls = 0
    original = motion_solver.step
    def counted(*args, **kwargs):
        nonlocal physics_calls
        physics_calls += 1
        return original(*args, **kwargs)
    with patch.object(motion_solver, 'step', counted):
        measured = _execute_job(job)
    if measured.solve_result.proof != proof:
        raise RuntimeError('counting changed proof')
    roundtrips, compute = [], []
    started = time.perf_counter_ns()
    with MotionSolverWorker(max_pending=1) as worker:
        for index in range(21):
            if index:
                started = time.perf_counter_ns()
            if not worker.submit(job):
                raise RuntimeError('unexpected backpressure')
            stop = time.perf_counter_ns()+5_000_000_000
            received = ()
            while not received:
                if not worker.is_alive() or time.perf_counter_ns() >= stop:
                    raise RuntimeError('worker did not deliver within benchmark bound')
                received = worker.poll_available()
                if not received:
                    time.sleep(.0005)
            elapsed = time.perf_counter_ns()-started
            if received[0].solve_result.proof != proof:
                raise RuntimeError('process serialization changed proof')
            roundtrips.append(elapsed)
            compute.append(received[0].elapsed_ns)
    payload = dict(scope='one pilot job; not complete control-frame or Fabric deadline evidence',
                   prefix_ticks=len(job.entry_prefix), candidates=base.solve_result.candidates_evaluated,
                   physics_step_calls=physics_calls,
                   snapshot_pickle_bytes=len(pickle.dumps(job.world)),
                   job_pickle_bytes=len(pickle.dumps(job)), admission_ns=timing,
                   admission=summary(timing), cold_process_roundtrip_ns=roundtrips[0],
                   warm_process_roundtrip_ns=roundtrips[1:], warm_roundtrip=summary(roundtrips[1:]),
                   worker_compute_ns=compute, worker_compute=summary(compute),
                   recovery_horizon_ticks=proof.recovery_horizon_ticks,
                   strict_command_count=len(proof.commands))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in payload.items()
                      if key not in {'admission_ns','warm_process_roundtrip_ns','worker_compute_ns'}}))


if __name__ == '__main__':
    main()
