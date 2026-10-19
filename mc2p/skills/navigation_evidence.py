"""Immutable navigation evidence from legal V3 only; never a current world map.

Receipt and request times belong to Python, sample intervals to the JVM. No
cross-clock arithmetic, inferred eye origin, free voxels, or hidden prediction.
"""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, FieldStatusV0, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ClientSampleTimingV2, SelfStateV2, ObservationGroupV2
from mc2p.contracts.observation_v3 import (
    ObservedBlockV3, ObservationSnapshotV3, PerceptionStateV3, TargetingStateV3,
)
from mc2p.skills.follow_types import FollowEntity
from mc2p.skills.local_perception import _absolute


@dataclass(frozen=True, slots=True)
class EvidenceStamp:
    episode_id: str
    sequence_id: int
    request_sequence_id: int | None
    controller_clock_id: str
    client_sample: ClientSampleTimingV2
    request_start_ns: int
    received_at_ns: int
    source_backend: str

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.episode_id, self.controller_clock_id, self.client_sample.clock_id

    def recent(self, now_ns: int, budget_ns: int) -> bool:
        """Delivery/request freshness only, NOT true server-state age or permission."""
        require_nonnegative_int(now_ns, "evidence controller now")
        require_nonnegative_int(budget_ns, "evidence freshness budget")
        if now_ns < self.received_at_ns:
            raise ContractViolation("future_observation")
        return now_ns - self.request_start_ns <= budget_ns


@dataclass(frozen=True, slots=True)
class ObservationPose:
    position: Vec3V0
    yaw: float
    pitch: float
    pose: str
    on_ground: bool
    horizontal_collision: bool


@dataclass(frozen=True, slots=True)
class ObservationCoverage:
    horizontal_fov_degrees: float
    vertical_fov_degrees: float
    max_block_distance: float
    entity_max_distance: float
    body_expansion_blocks: float
    block_epsilon_blocks: float
    entity_occlusion_epsilon_blocks: float
    entities_truncated: bool
    truncated_entity_count: int
    source_kind: str = "client_perception_filtered"
    sensor_profile_revision: int = 4
    block_visibility_model: str = "surface_depth"


@dataclass(frozen=True, slots=True)
class NavigationEvidence:
    stamp: EvidenceStamp
    available: bool
    reason: str | None = None
    pose: ObservationPose | None = None
    coverage: ObservationCoverage | None = None
    blocks: tuple[ObservedBlockV3, ...] = ()
    entities: tuple[FollowEntity, ...] = ()
    field_profile: str = "navigation_v1"
    targeting: ObservationGroupV2[TargetingStateV3] | None = None


def project_navigation_evidence(observation: ObservationSnapshotV3, *, now_ns: int,
                                controller_clock_id: str) -> NavigationEvidence:
    """Project history even when late. Availability is not action freshness."""
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("navigation requires exact ObservationSnapshotV3")
    require_nonnegative_int(now_ns, "navigation controller now")
    obs = observation
    if obs.privileged_fields_present:
        raise ContractViolation("privileged_observation")
    if obs.controller_clock_id != controller_clock_id:
        raise ContractViolation("controller_clock_mismatch")
    if obs.received_at_monotonic_ns > now_ns:
        raise ContractViolation("future_observation")
    stamp = EvidenceStamp(obs.episode_id, obs.sequence_id, obs.request_sequence_id,
                          obs.controller_clock_id, obs.client_sample,
                          obs.request_started_at_monotonic_ns, obs.received_at_monotonic_ns,
                          obs.source_backend)
    for name, expected, source in (("self_state", SelfStateV2, "client_player"),
                                   ("perception", PerceptionStateV3, "client_perception_filtered")):
        group = getattr(obs, name)
        if group.status is not FieldStatusV0.VALID:
            return NavigationEvidence(stamp, False, name + "_unavailable",
                                      field_profile=obs.field_profile,targeting=obs.targeting)
        if type(group.value) is not expected or group.source_kind != source:
            raise ContractViolation(name + "_invalid_source")
    own, perception = obs.self_state.value, obs.perception.value
    assert own is not None and perception is not None
    if perception.sensor_profile_revision != 4:
        raise ContractViolation("legacy_ray_profile_cannot_enter_current_navigation")
    if any("first_hit_ray" in block.sources for block in perception.blocks):
        raise ContractViolation("legacy_ray_profile_cannot_enter_current_navigation")
    pose = ObservationPose(own.position, own.yaw_degrees, own.pitch_degrees, own.pose,
                           own.is_on_ground, own.horizontal_collision)
    entities = tuple(FollowEntity(item.track_id, item.entity_type,
                     _absolute(own.position, item.relative_position), item.bounding_box_size)
                     for item in perception.visible_entities)
    if any(min(item.size.x, item.size.y, item.size.z) <= 0 for item in entities):
        raise ContractViolation("invalid_entity_geometry")
    coverage = ObservationCoverage(perception.horizontal_fov_degrees, perception.vertical_fov_degrees,
                 perception.max_block_distance, perception.entity_max_distance,
                 perception.body_expansion_blocks, perception.block_epsilon_blocks,
                 perception.entity_occlusion_epsilon_blocks, perception.entities_truncated,
                 perception.truncated_entity_count,
                 sensor_profile_revision=perception.sensor_profile_revision,
                 block_visibility_model="surface_depth")
    return NavigationEvidence(stamp, True, pose=pose, coverage=coverage,
                              blocks=perception.blocks, entities=entities,
                              field_profile=obs.field_profile,targeting=obs.targeting)
