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


VISIBILITY_STAGES = (
    "front", "turned_away", "turned_back", "partial_occlusion",
    "full_occlusion", "occlusion_return", "out_of_range", "range_return",
    "cow_targeted", "cow_off_target", "ore_exposed", "ore_covered",
    "final_neutral",
)


def evaluate_visibility_stages(stages: dict) -> list[dict]:
    """Evaluate the shared V3 scene without importing a retired backend."""
    def entities(stage, kind):
        return [
            entity
            for entity in stages.get(stage, {}).get("perception", {}).get(
                "value", {},
            ).get("visible_entities", [])
            if entity["entity_type"] == "minecraft:" + kind
        ]

    visible = (
        "front", "turned_back", "partial_occlusion", "occlusion_return",
        "range_return",
    )
    anchors = [entities(stage, "armor_stand") for stage in visible]
    cows = [
        entities(stage, "cow")
        for stage in ("front", "cow_targeted", "cow_off_target")
    ]
    complete = set(stages) == set(VISIBILITY_STAGES)
    sequences = [
        stages[stage]["sequence_id"]
        for stage in VISIBILITY_STAGES if stage in stages
    ]
    ore = [
        any(
            block["block_id"] == "minecraft:diamond_ore"
            for block in stages.get(stage, {}).get("perception", {}).get(
                "value", {},
            ).get("blocks", [])
        )
        for stage in ("ore_exposed", "ore_covered")
    ]
    formal_v3 = complete and all(
        stage.get("schema_version") == "mc2p.observation.v3"
        and stage.get("privileged_fields_present") == []
        and "blocks" in stage.get("perception", {}).get("value", {})
        and "block_rays" not in stage.get("perception", {}).get("value", {})
        for stage in stages.values()
    )

    def target(stage):
        group = stages.get(stage, {}).get("targeting", {})
        return group.get("value") if group.get("status") == "valid" else None

    cow_target, cow_off = target("cow_targeted"), target("cow_off_target")
    ore_target, cover_target = target("ore_exposed"), target("ore_covered")
    targeting = all(
        stages.get(stage, {}).get("field_profile") == "interaction_v1"
        for stage in (
            "cow_targeted", "cow_off_target", "ore_exposed", "ore_covered",
        )
    )
    targeting = targeting and (
        len(cows[1]) == 1
        and cow_target is not None
        and cow_target.get("hit_kind") == "entity"
        and cow_target.get("entity_ref") == cows[1][0]["track_id"]
        and cow_off is not None
        and len(cows[2]) == 1
        and not (
            cow_off.get("hit_kind") == "entity"
            and cow_off.get("entity_ref") == cows[2][0]["track_id"]
        )
        and ore_target is not None
        and ore_target.get("hit_kind") == "block"
        and ore_target.get("block_position") == [-2, -61, 3]
        and ore_target.get("face") == "up"
        and cover_target is not None
        and cover_target.get("hit_kind") == "block"
        and cover_target.get("block_position") == [-2, -60, 3]
    )
    checks = {
        "all_stages_one_ordered_episode": complete
        and len({stage["episode_id"] for stage in stages.values()}) == 1
        and all(type(sequence) is int for sequence in sequences)
        and all(a < b for a, b in zip(sequences, sequences[1:])),
        "anchor_visible_and_reidentified": all(len(anchor) == 1 for anchor in anchors)
        and len({entity["track_id"] for anchor in anchors[:-1] for entity in anchor}) == 1
        and all(
            entity["display_name"] == "visible-anchor"
            for anchor in anchors for entity in anchor
        ),
        "anchor_absent_when_hidden": all(
            stage in stages and not entities(stage, "armor_stand")
            for stage in ("turned_away", "full_occlusion", "out_of_range")
        ),
        "native_equipment_visibility": bool(anchors[0])
        and any(
            slot == "main_hand"
            and item == {
                "empty": False, "item_id": "minecraft:diamond_sword",
            }
            for entity in anchors[0]
            for slot, item in entity["equipment"]
        )
        and all(len(cow) == 1 for cow in cows)
        and all(not entity["equipment"] for cow in cows for entity in cow),
        "cow_target_name_visibility": all(len(cow) == 1 for cow in cows)
        and [cow[0]["display_name"] for cow in cows if cow]
        == [None, "target-only-name", None],
        "independent_targeting_transitions": targeting,
        "native_v3_block_state_evidence": formal_v3,
        "ore_cover_removes_all_ore_blocks": all(
            stage in stages for stage in ("ore_exposed", "ore_covered")
        ) and ore == [True, False],
    }
    return [{"name": name, "passed": bool(passed)} for name, passed in checks.items()]


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
