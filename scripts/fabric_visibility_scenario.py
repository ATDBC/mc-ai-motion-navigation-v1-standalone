"""Shared visibility fixture exercised through the real standalone Runtime/arbiter."""
from pathlib import Path
import time

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1,MovementV1,LookV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import TaskIntentV0,SuccessCriterionV0,ComparisonOperatorV0
from mc2p.runtime.trace import trace_projection,JsonlTraceWriterV0
from scripts.control_probe_core import append_jsonl
from scripts.visibility_v3_scenario import run_visibility_scenario


class CloseDiagnosticsTrace:
    """The scenario records normal samples; capture the Runtime's extra close neutral too."""
    def __init__(self,directory: Path,backend):
        self.directory,self.backend=directory,backend
        self.trace=JsonlTraceWriterV0(directory/'trace.jsonl')

    def write(self,kind,payload):
        self.trace.write(kind,payload)
        if kind=='close_release':
            obs=payload['backend_result'].observation
            append_jsonl(self.directory/'diagnostics.jsonl',dict(episode_id=obs.episode_id,
                observation_sequence_id=obs.sequence_id,diagnostics=self.backend.last_diagnostics))

    def close(self): self.trace.close()


def run_visibility_runtime(runtime,backend,episode: str,directory: Path,deadline: int):
    task=TaskIntentV0('visibility-probe','visibility-fixture','{}',
        (SuccessCriterionV0('fixture_stages',ComparisonOperatorV0.GREATER_THAN,0,'stages'),),
        1800,deadline,True,0.)
    profile,stages,rows=BehaviorProfileV0(),{},[]
    count=0
    def diagnostic():
        row=dict(episode_id=episode,observation_sequence_id=runtime.observation.sequence_id,
                 diagnostics=backend.last_diagnostics)
        rows.append(row);append_jsonl(directory/'diagnostics.jsonl',row)
    def step(label,*,operation=None,movement=MovementV1(),look=LookV1(),field_profile='navigation_v1'):
        nonlocal count
        if count>=1800: raise TimeoutError('visibility action budget exceeded')
        count+=1
        runtime.cancel_source('visibility-probe')
        now=time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(f'visibility-{count}','visibility-probe',episode,
            runtime.observation.sequence_id,ActionPriorityV0.TASK,now,min(deadline,now+2_000_000_000),
            movement=movement,look=look,operation=operation,valid_for_ticks=1))
        result=runtime.step(task,profile,min(deadline,now+5_000_000_000),
                            observation_request=ObservationRequestV3(field_profile))
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f'visibility step failed: {label}: {result.report}')
        diagnostic()
        receipt=result.backend_result.receipt
        if result.report.status.value!='running' or receipt.status not in {'executed','confirmed_local','pending_confirmation'}:
            raise RuntimeError(f'visibility step failed: {label}: {result.report}')
        if result.observation.is_dead.value is not False: raise RuntimeError('visibility fixture player died')
        return trace_projection(receipt)
    def milestone(label):
        stages[label]=trace_projection(runtime.observation)
        append_jsonl(directory/'stages.jsonl',dict(label=label,observation=stages[label]))
        print(f'VISIBILITY_STAGE={label} sequence={runtime.observation.sequence_id}',flush=True)
    diagnostic()
    try:
        checks=run_visibility_scenario(lambda:runtime.observation,step,milestone,directory)
        return stages,rows,checks
    finally:
        runtime.cancel_source('visibility-probe')
