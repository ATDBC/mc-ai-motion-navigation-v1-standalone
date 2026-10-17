"""Legal V3 projection and episode/object-scoped target memory. No world queries."""
from __future__ import annotations

from dataclasses import replace

from mc2p.contracts.common import ContractViolation, FieldStatusV0, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import GuiStateV2, SelfStateV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3, PerceptionStateV3
from mc2p.skills.follow_types import (
    MOVEMENT_FRESHNESS_NS, TARGET_TTL_NS, FollowEntity, FollowRequest,
    FollowSelf, FollowView, TargetState,
)


def _absolute(origin: Vec3V0, relative: Vec3V0) -> Vec3V0:
    return Vec3V0(origin.x + relative.x, origin.y + relative.y, origin.z + relative.z)


def project_follow_view(observation: ObservationSnapshotV3, now_ns: int,
                        controller_clock_id: str) -> FollowView:
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("follow projection requires exact ObservationSnapshotV3")
    require_nonnegative_int(now_ns, "follow controller time")
    obs = observation
    view = FollowView(obs.episode_id, obs.sequence_id, obs.controller_clock_id,
                      obs.client_sample.clock_id, obs.client_sample.started_at_monotonic_ns,
                      obs.client_sample.completed_at_monotonic_ns,
                      obs.request_started_at_monotonic_ns, obs.received_at_monotonic_ns,
                      available=False, reason=None)
    reason = None
    if obs.controller_clock_id != controller_clock_id:
        reason = "controller_clock_mismatch"
    elif obs.privileged_fields_present:
        reason = "privileged_observation"
    elif now_ns < view.received_at_ns:
        reason = "future_observation"
    elif now_ns - view.received_at_ns > MOVEMENT_FRESHNESS_NS:
        reason = "stale_observation"
    elif view.received_at_ns - view.request_start_ns > MOVEMENT_FRESHNESS_NS:
        reason = "slow_observation_request"
    if reason:
        return replace(view, reason=reason)
    for name, value_type, source in (("self_state", SelfStateV2, "client_player"),
                                     ("gui", GuiStateV2, "client_screen_handler"),
                                     ("perception", PerceptionStateV3, "client_perception_filtered")):
        group = getattr(obs, name)
        if group.status is not FieldStatusV0.VALID or type(group.value) is not value_type or group.source_kind != source:
            return replace(view, reason=name + "_unavailable")
    own, perception = obs.self_state.value, obs.perception.value
    assert own is not None and perception is not None and obs.gui.value is not None
    restricted_self = FollowSelf(own.position, own.velocity, own.yaw_degrees, own.pitch_degrees,
                                 own.is_on_ground, own.is_dead, own.horizontal_collision,
                                 own.pose != "standing" or own.is_swimming or own.is_submerged_in_water
                                 or own.is_climbing or own.is_fall_flying or own.is_flying
                                 or own.is_burning or own.game_mode == "spectator")
    entities = tuple(FollowEntity(entity.track_id, entity.entity_type,
                                  _absolute(own.position, entity.relative_position), entity.bounding_box_size)
                     for entity in perception.visible_entities)
    if any(min(entity.size.x, entity.size.y, entity.size.z) <= 0 for entity in entities):
        return replace(view, reason="invalid_entity_geometry")
    return replace(view, available=True, own=restricted_self, gui_open=obs.gui.value.open,
                   observed_blocks=perception.blocks, entities=entities,
                   entities_truncated=perception.entities_truncated)


class TargetMemory:
    """One binding; loss/invalidation requires a new skill, never hidden reacquisition."""

    def __init__(self) -> None:
        self._request: FollowRequest | None = None
        self._view: FollowView | None = None
        self._position: Vec3V0 | None = None
        self._seen_ns: int | None = None
        self._last_now_ns = 0
        self._terminal: TargetState | None = None

    def bind(self, request: FollowRequest, view: FollowView) -> None:
        if self._request is not None:
            raise ContractViolation("target memory is already bound")
        if not view.available or view.episode_id != request.episode_id or view.controller_clock_id != request.controller_clock_id:
            raise ContractViolation("target binding requires available same-session observation")
        if not 0 <= request.started_at_ns - view.received_at_ns <= MOVEMENT_FRESHNESS_NS:
            raise ContractViolation("target binding requires current observation")
        target = next((e for e in view.entities if e.track_id == request.target_track_id), None)
        if target is None or target.entity_type != "minecraft:player":
            raise ContractViolation("target binding requires a currently visible player")
        self._request, self._view = request, view
        self._position, self._seen_ns = target.position, view.received_at_ns
        self._last_now_ns = request.started_at_ns

    def _invalidate(self, reason: str) -> TargetState:
        self._position = None
        self._terminal = TargetState("invalidated", None, self._seen_ns, reason)
        return self._terminal

    def observe(self, view: FollowView, now_ns: int) -> TargetState:
        require_nonnegative_int(now_ns, "target controller time")
        if self._request is None or self._view is None:
            raise ContractViolation("target memory is not bound")
        if self._terminal:
            return self._terminal
        old = self._view
        if (view.episode_id, view.controller_clock_id, view.client_clock_id) != (
                old.episode_id, old.controller_clock_id, old.client_clock_id):
            return self._invalidate("session_or_clock_changed")
        if now_ns < self._last_now_ns:
            return self._invalidate("controller_time_regression")
        self._last_now_ns = now_ns
        if view.sequence_id < old.sequence_id:
            return self._invalidate("sequence_regression")
        if view.available and view.sequence_id == old.sequence_id and view != old:
            return self._invalidate("sequence_rewritten")
        if self._seen_ns is not None and now_ns - self._seen_ns >= TARGET_TTL_NS:
            self._position = None
            self._terminal = TargetState("lost", None, self._seen_ns, "target_memory_expired")
            return self._terminal
        if not view.available:
            return TargetState("unavailable", None, self._seen_ns, view.reason)
        if view.received_at_ns > now_ns:
            return self._invalidate("future_observation")
        fresh_sequence = view.sequence_id > old.sequence_id
        if fresh_sequence:
            if (view.received_at_ns < old.received_at_ns or view.request_start_ns < old.request_start_ns
                    or view.client_sample_start_ns < old.client_sample_end_ns):
                return self._invalidate("sample_time_regression")
            self._view = view
            target = next((e for e in view.entities if e.track_id == self._request.target_track_id), None)
            if target is not None and target.entity_type != "minecraft:player":
                return self._invalidate("target_type_changed")
            if target is not None and now_ns - view.received_at_ns <= MOVEMENT_FRESHNESS_NS:
                self._position, self._seen_ns = target.position, view.received_at_ns
        current = any(e.track_id == self._request.target_track_id for e in view.entities)
        status = "visible" if current and now_ns - view.received_at_ns <= MOVEMENT_FRESHNESS_NS else "remembered"
        return TargetState(status, self._position, self._seen_ns)
