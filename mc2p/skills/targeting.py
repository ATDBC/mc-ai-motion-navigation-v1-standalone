"""Pure current-target checks; never observe a world or issue an action."""
from mc2p.contracts.common import (
    ContractViolation, FieldStatusV0, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.observation_v2 import ObservationGroupV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3, TargetingStateV3


_FACES = frozenset({'down','up','north','south','west','east'})


def block_target_matches(field_profile, targeting, *, block_position, face):
    """Value predicate only. Caller must separately establish time and causality."""
    if (type(block_position) is not tuple or len(block_position)!=3
            or any(type(v) is not int for v in block_position)):
        raise ContractViolation('target query block must be an integer triple')
    if face is not None and (type(face) is not str or face not in _FACES):
        raise ContractViolation('target query face is invalid')
    if (field_profile!='interaction_v1' or type(targeting) is not ObservationGroupV2
            or targeting.status is not FieldStatusV0.VALID
            or targeting.source_kind!='client_perception_filtered'
            or type(targeting.value) is not TargetingStateV3):
        return False
    target=targeting.value
    return (target.hit_kind=='block' and target.block_position==block_position
            and (face is None or target.face==face))


def confirmed_block_target(observation: ObservationSnapshotV3, *, block_position: tuple[int,int,int],
                           face: str | None, now_ns: int, controller_clock_id: str,
                           freshness_ns: int = 500_000_000) -> bool:
    """Confirm a fresh current target; not completion or permission to bypass validation."""
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation('target confirmation requires exact ObservationSnapshotV3')
    require_nonnegative_int(now_ns,'target confirmation time')
    require_nonnegative_int(freshness_ns,'target freshness')
    require_identifier(controller_clock_id,'target controller clock')
    obs=observation
    matches=block_target_matches(obs.field_profile,obs.targeting,block_position=block_position,face=face)
    return bool(matches and not obs.privileged_fields_present
        and obs.controller_clock_id==controller_clock_id
        and obs.received_at_monotonic_ns<=now_ns
        and 0<=now_ns-obs.request_started_at_monotonic_ns<=freshness_ns
        and obs.self_state.status is FieldStatusV0.VALID
        and obs.perception.status is FieldStatusV0.VALID)


def confirmed_entity_target(
    observation: ObservationSnapshotV3,
    *,
    entity_ref: str,
    now_ns: int,
    controller_clock_id: str,
    freshness_ns: int = 500_000_000,
) -> bool:
    """Confirm a fresh exact entity under the formal current crosshair."""
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation('entity confirmation requires exact ObservationSnapshotV3')
    require_identifier(entity_ref, 'target entity reference')
    require_nonnegative_int(now_ns, 'entity confirmation time')
    require_nonnegative_int(freshness_ns, 'entity target freshness')
    require_identifier(controller_clock_id, 'entity target controller clock')
    obs = observation
    targeting = obs.targeting
    target = targeting.value
    matches = (
        obs.field_profile == 'interaction_v1'
        and targeting.status is FieldStatusV0.VALID
        and targeting.source_kind == 'client_perception_filtered'
        and type(target) is TargetingStateV3
        and target.hit_kind == 'entity'
        and target.entity_ref == entity_ref
    )
    visible = (
        obs.perception.status is FieldStatusV0.VALID
        and obs.perception.value is not None
        and any(entity.track_id == entity_ref for entity in obs.perception.value.visible_entities)
    )
    return bool(
        matches and visible and not obs.privileged_fields_present
        and obs.controller_clock_id == controller_clock_id
        and obs.received_at_monotonic_ns <= now_ns
        and 0 <= now_ns - obs.request_started_at_monotonic_ns <= freshness_ns
        and obs.self_state.status is FieldStatusV0.VALID
    )
