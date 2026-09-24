"""Bounded needs and conditional gaze candidates from current lawful evidence.

Candidate geometry predicts where a later filtered sample may be useful.  It
never creates observation evidence, refreshes memory, or authorizes movement.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_playground_types import PlaygroundView
from mc2p.skills.follow_types import FollowEntity
from mc2p.skills.navigation_evidence import ObservationCoverage
from mc2p.skills.navigation_memory import MemorySnapshot
from mc2p.skills.navigation_motion import report_navigation_motion
from mc2p.skills.perception_confirmation import PITCH_TOLERANCE_DEGREES, YAW_TOLERANCE_DEGREES
from mc2p.skills.perception_needs import (
    AimConstraint,
    CandidateOutcome,
    MotionEvidenceReport,
    MotionProposal,
    PerceptionCandidate,
    PerceptionConfig,
    PerceptionNeed,
)


EYE_HEIGHT_BLOCKS = 1.62
NAV_YAW_TOLERANCE_DEGREES = 8.0


def _wrap(angle: float) -> float:
    if -180.0<=angle<180.0:
        return angle
    return (angle + 180.0) % 360.0 - 180.0


def _config(config: PerceptionConfig | None) -> PerceptionConfig:
    if config is None:
        return PerceptionConfig()
    if type(config) is not PerceptionConfig:
        raise ContractViolation("candidate config must be PerceptionConfig")
    return config


def _current(snapshot: MemorySnapshot, view: PlaygroundView):
    if type(snapshot) is not MemorySnapshot or type(view) is not PlaygroundView:
        raise ContractViolation("candidates require MemorySnapshot and PlaygroundView")
    latest, base = snapshot.latest, view.base
    if (
        snapshot.invalid_reason is not None
        or latest is None
        or not latest.available
        or latest.pose is None
        or not base.available
        or base.own is None
        or snapshot.scope_id == ""
        or latest.stamp.scope
        != (base.episode_id, base.controller_clock_id, base.client_clock_id)
        or latest.stamp.sequence_id != base.sequence_id
        or latest.pose.position != base.own.position
        or latest.pose.yaw != base.own.yaw
        or latest.pose.pitch != base.own.pitch
        or latest.entities != base.entities
        or latest.coverage is None
        or latest.coverage.source_kind != "client_perception_filtered"
    ):
        return None
    return latest, base.own


def _logical_key(need: PerceptionNeed) -> tuple:
    point = need.point
    return (
        need.owner,
        need.purpose,
        need.condition,
        point.x,
        point.y,
        point.z,
        need.block,
        need.face,
        need.track_id,
    )


def _deduplicate(needs: tuple[PerceptionNeed, ...]) -> tuple[PerceptionNeed, ...]:
    retained: dict[tuple, PerceptionNeed] = {}
    for need in sorted(needs, key=lambda item: (-item.priority, item.need_id)):
        retained.setdefault(_logical_key(need), need)
    return tuple(sorted(retained.values(), key=lambda item: (-item.priority, item.need_id)))


def _gap_id(block: tuple[int, int, int] | None, reason: str) -> str:
    location = "unknown" if block is None else "/".join(str(value) for value in block)
    return f"motion-gap/{location}/{reason}"


def _gap_point(block: tuple[int, int, int] | None, proposal: MotionProposal | None) -> Vec3V0:
    if block is not None:
        return Vec3V0(block[0] + 0.5, block[1] + 1.0, block[2] + 0.5)
    if proposal is not None:
        return proposal.goal
    raise ContractViolation("unlocated motion gap requires a proposal")


def _bound_report(
    snapshot: MemorySnapshot,
    view: PlaygroundView,
    proposal: MotionProposal | None,
    report: MotionEvidenceReport | None,
    now_ns: int,
) -> MotionEvidenceReport | None:
    latest = snapshot.latest
    if proposal is not None:
        if latest is None or proposal.scope_id != snapshot.scope_id or proposal.based_on != latest.stamp:
            raise ContractViolation("motion proposal does not bind current evidence")
        return report_navigation_motion(
            snapshot,
            view,
            now_ns,
            proposal.floor,
            proposal.yaw_degrees,
            jump=proposal.movement.jump or not view.base.own.on_ground,
            allowed_player_contact=proposal.allowed_player_contact,
        )
    if report is None:
        return None
    if type(report) is not MotionEvidenceReport:
        raise ContractViolation("motion report must be MotionEvidenceReport or null")
    if latest is None or report.scope_id != snapshot.scope_id or report.based_on != latest.stamp:
        raise ContractViolation("motion report does not bind current evidence")
    return report


def make_needs(
    snapshot: MemorySnapshot,
    view: PlaygroundView,
    proposal: MotionProposal | None,
    report: MotionEvidenceReport | None,
    *,
    target_track_id: str | None,
    aim: AimConstraint | None,
    now_ns: int,
    deadline_ns: int,
    config: PerceptionConfig | None = None,
) -> tuple[PerceptionNeed, ...]:
    """Create at most eight stable logical needs from current lawful inputs."""

    cfg = _config(config)
    require_nonnegative_int(now_ns, "need generation time")
    require_nonnegative_int(deadline_ns, "need deadline")
    if deadline_ns <= now_ns:
        raise ContractViolation("need deadline must follow generation time")
    if proposal is not None and type(proposal) is not MotionProposal:
        raise ContractViolation("proposal must be MotionProposal or null")
    if report is not None and type(report) is not MotionEvidenceReport:
        raise ContractViolation("report must be MotionEvidenceReport or null")
    if aim is not None and type(aim) is not AimConstraint:
        raise ContractViolation("aim must be AimConstraint or null")
    current = _current(snapshot, view)
    if current is None:
        return ()
    latest, own = current
    if deadline_ns <= latest.stamp.received_at_ns:
        raise ContractViolation("need deadline must follow current evidence")

    values: list[PerceptionNeed] = []
    if proposal is not None and abs(_wrap(proposal.yaw_degrees-own.yaw))>8:
        radians=math.radians(proposal.yaw_degrees)
        # A geometric looking direction derived from the proposed route, not a
        # claim that a block/entity exists at this point.
        point=Vec3V0(own.position.x-math.sin(radians),own.position.y+EYE_HEIGHT_BLOCKS,
                     own.position.z+math.cos(radians))
        values.append(PerceptionNeed('motion-heading','follow_nav',snapshot.scope_id,
            latest.stamp,'route_check',95,deadline_ns,point,'filtered_check'))
    bound = _bound_report(snapshot, view, proposal, report, now_ns)
    if bound is not None:
        gaps = bound.guard.gaps
        if bound.reason is not None and not gaps:
            gaps = ()
            point = _gap_point(None, proposal)
            values.append(PerceptionNeed(
                _gap_id(None, bound.reason), "follow_nav", snapshot.scope_id, latest.stamp,
                "route_check", 90, deadline_ns, point, "motion_guard",
            ))
        for index, gap in enumerate(gaps):
            values.append(PerceptionNeed(
                _gap_id(gap.block, gap.reason), "follow_nav", snapshot.scope_id, latest.stamp,
                "route_check", max(1, 90 - index), deadline_ns,
                _gap_point(gap.block, proposal), "motion_guard",
            ))
        expiry_horizon = (now_ns + proposal.estimated_duration_ns
                          if proposal is not None else deadline_ns)
        if (bound.reason is None and bound.earliest_expiry_ns is not None
                and bound.earliest_expiry_ns <= expiry_horizon):
            earliest=min(bound.evidence,key=lambda item:(item[1].request_start_ns,item[0]),default=None)
            point = (_gap_point(earliest[0],proposal) if earliest is not None else
                     proposal.goal if proposal is not None else own.position)
            values.append(PerceptionNeed(
                "motion-expiry", "follow_nav", snapshot.scope_id, latest.stamp,
                "route_check", 80, deadline_ns, point, "motion_guard",
            ))

    if target_track_id is not None:
        target = next((item for item in view.base.entities if item.track_id == target_track_id), None)
        if target is not None:
            values.append(PerceptionNeed(
                f"visible-track/{target.track_id}", "follow_track", snapshot.scope_id,
                latest.stamp, "track", 70, deadline_ns,
                Vec3V0(target.position.x, target.position.y + target.size.y / 2.0,
                       target.position.z),
                "visible_track", track_id=target.track_id,
            ))

    if aim is not None and aim.active:
        source = aim.need
        if (source.scope_id != snapshot.scope_id
                or source.based_on.scope != latest.stamp.scope
                or source.based_on.source_backend != latest.stamp.source_backend):
            raise ContractViolation("aim does not bind current scope and evidence source")
        aim_deadline = min(source.deadline_ns, deadline_ns)
        if aim_deadline > now_ns and aim_deadline > latest.stamp.received_at_ns:
            values.append(replace(source, based_on=latest.stamp, deadline_ns=aim_deadline))

    return _deduplicate(tuple(values))[: cfg.max_needs]


@dataclass(frozen=True, slots=True)
class AngleRegion:
    yaw_min: float
    yaw_max: float
    pitch_min: float
    pitch_max: float


def _angles(own: Vec3V0, point: Vec3V0) -> tuple[float, float]:
    dx, dz = point.x - own.x, point.z - own.z
    horizontal = math.hypot(dx, dz)
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(math.atan2(own.y + EYE_HEIGHT_BLOCKS - point.y, horizontal))
    return _wrap(yaw), max(-90.0, min(90.0, pitch))


def _unwrapped(values: tuple[float, ...], reference: float) -> tuple[float, ...]:
    return tuple(reference + _wrap(value - reference) for value in values)


def _target_region(
    own: Vec3V0,
    entity: FollowEntity,
    coverage: ObservationCoverage,
    config: PerceptionConfig,
) -> AngleRegion:
    center, _ = _angles(own, Vec3V0(entity.position.x,
        entity.position.y + entity.size.y / 2.0, entity.position.z))
    points = tuple(Vec3V0(x, y, z)
        for x in (entity.position.x - entity.size.x / 2.0,
                  entity.position.x + entity.size.x / 2.0)
        for y in (entity.position.y, entity.position.y + entity.size.y)
        for z in (entity.position.z - entity.size.z / 2.0,
                  entity.position.z + entity.size.z / 2.0))
    pairs = tuple(_angles(own, point) for point in points)
    yaws = _unwrapped(tuple(pair[0] for pair in pairs), center)
    pitches = tuple(pair[1] for pair in pairs)
    yaw_half = coverage.horizontal_fov_degrees / 2.0 - config.target_yaw_margin_degrees
    pitch_half = coverage.vertical_fov_degrees / 2.0 - config.target_pitch_margin_degrees
    return AngleRegion(max(yaws) - yaw_half, min(yaws) + yaw_half,
                       max(pitches) - pitch_half, min(pitches) + pitch_half)


def _gap_regions(own: Vec3V0, point: Vec3V0, reference_yaw: float,
                 coverage: ObservationCoverage) -> tuple[AngleRegion, ...]:
    # V3's validated physical profile is fixed at 159 columns / 9 rows.
    # A point inside the FOV can sit BETWEEN every ray. Predict directions for
    # actual rays, not permission: only the new block sample resolves the guard.
    distance=math.sqrt((point.x-own.x)**2+(point.z-own.z)**2+
                       (point.y-own.y-EYE_HEIGHT_BLOCKS)**2)
    if distance>coverage.max_block_distance:
        return ()
    yaw,pitch=_angles(own,point)
    column_step=coverage.horizontal_fov_degrees/158.
    column=max(0,min(158,round((_wrap(yaw-reference_yaw)+coverage.horizontal_fov_degrees/2)/column_step)))
    camera_yaw=_wrap(yaw-(-coverage.horizontal_fov_degrees/2+column*column_step))
    result=[]
    for row in range(9):
        camera_pitch=pitch-(-coverage.vertical_fov_degrees/2+row*coverage.vertical_fov_degrees/8.)
        if -90<=camera_pitch<=90:
            result.append(AngleRegion(camera_yaw,camera_yaw,camera_pitch,camera_pitch))
    return tuple(result)


def _nearest_region(regions, yaw, pitch):
    available=tuple(region for region in regions if region is not None)
    def cost(region):
        candidate_yaw,candidate_pitch=_choose(region,yaw,pitch)
        return (abs(_wrap(candidate_yaw-yaw))/90.+abs(candidate_pitch-pitch)/45.,
                candidate_yaw,candidate_pitch)
    return min(available,key=cost) if available else None


def _center_region(own: Vec3V0, point: Vec3V0, coverage: ObservationCoverage) -> AngleRegion:
    # collectVisibleEntities gates the center using the declared profile before its
    # occlusion samples. Reserve 1 degree; this predicts no whole-body margin
    # or line of sight, and always requires a real post-observation.
    yaw,pitch=_angles(own,point)
    return AngleRegion(yaw-coverage.horizontal_fov_degrees/2+1.,
        yaw+coverage.horizontal_fov_degrees/2-1.,
        max(-90.,pitch-coverage.vertical_fov_degrees/2+1.),
        min(90.,pitch+coverage.vertical_fov_degrees/2-1.))


def intersect_angle_regions(left: AngleRegion, right: AngleRegion) -> AngleRegion | None:
    shift=360*round(((left.yaw_min+left.yaw_max)-(right.yaw_min+right.yaw_max))/720)
    right=replace(right,yaw_min=right.yaw_min+shift,yaw_max=right.yaw_max+shift)
    region = AngleRegion(max(left.yaw_min, right.yaw_min),
                         min(left.yaw_max, right.yaw_max),
                         max(left.pitch_min, right.pitch_min),
                         min(left.pitch_max, right.pitch_max))
    return region if region.yaw_min <= region.yaw_max and region.pitch_min <= region.pitch_max else None


def _choose(region: AngleRegion, yaw: float, pitch: float) -> tuple[float, float]:
    middle=(region.yaw_min+region.yaw_max)/2
    unwrapped_yaw=middle+_wrap(yaw-middle)
    chosen_yaw=(_wrap(yaw) if region.yaw_min<=unwrapped_yaw<=region.yaw_max
                else _wrap(min(region.yaw_max,max(region.yaw_min,unwrapped_yaw))))
    return (chosen_yaw,
            min(region.pitch_max, max(region.pitch_min, pitch)))


def _contains(region: AngleRegion | None, yaw: float, pitch: float) -> bool:
    if region is None or region.yaw_min>region.yaw_max or region.pitch_min>region.pitch_max:
        return False
    chosen=_choose(region,yaw,pitch)
    return abs(_wrap(chosen[0]-yaw))<1e-6 and abs(chosen[1]-pitch)<1e-6


def _motion_is_currently_legal(
    snapshot: MemorySnapshot,
    view: PlaygroundView,
    proposal: MotionProposal | None,
    now_ns: int,
) -> bool:
    if proposal is None or view.base.own is None or snapshot.latest is None:
        return False
    own = view.base.own
    speed = math.hypot(own.velocity.x, own.velocity.z)
    long_horizon = proposal.movement.jump or not own.on_ground
    expected_horizon = max(4.5, 14.0 * speed) if long_horizon else 0.45 + 2.0 * speed
    if (
        proposal.scope_id != snapshot.scope_id
        or proposal.based_on != snapshot.latest.stamp
        or abs(_wrap(proposal.yaw_degrees - own.yaw)) > NAV_YAW_TOLERANCE_DEGREES
        or proposal.movement.forward != 1
        or proposal.movement.strafe != 0
        or not math.isclose(proposal.horizon_blocks, expected_horizon, abs_tol=1e-6)
    ):
        return False
    report = report_navigation_motion(snapshot, view, now_ns, proposal.floor, own.yaw,
        jump=long_horizon, allowed_player_contact=proposal.allowed_player_contact)
    return (report.reason is None and report.earliest_expiry_ns is not None
            and now_ns <= report.earliest_expiry_ns)


def _outcomes(progress_debt: float, elapsed: float, unseen: float | None, wait: float):
    return (
        CandidateOutcome(True, progress_debt, elapsed, unseen, None,
                         ("post_observation_required",)),
        CandidateOutcome(False, 1.0, elapsed, None, elapsed+wait, ("still_missing",)),
    )


def soft_cost(candidate: PerceptionCandidate, config: PerceptionConfig | None = None) -> float:
    """Lazy compatibility view of the coordinator's single scoring function."""
    from mc2p.skills.active_perception import soft_cost as coordinator_soft_cost
    return coordinator_soft_cost(candidate, _config(config))


def generate_candidates(
    snapshot: MemorySnapshot,
    view: PlaygroundView,
    needs: tuple[PerceptionNeed, ...],
    proposal: MotionProposal | None,
    now_ns: int,
    *,
    config: PerceptionConfig | None = None,
) -> tuple[PerceptionCandidate, ...]:
    """Generate finite conditional fragments; predicted coverage is never evidence."""

    cfg = _config(config)
    require_nonnegative_int(now_ns, "candidate generation time")
    if type(needs) is not tuple or any(type(item) is not PerceptionNeed for item in needs):
        raise ContractViolation("candidate needs must be an immutable PerceptionNeed tuple")
    if len(needs) > cfg.max_needs:
        raise ContractViolation("candidate needs exceed configured capacity")
    if proposal is not None and type(proposal) is not MotionProposal:
        raise ContractViolation("candidate proposal must be MotionProposal or null")
    current = _current(snapshot, view)
    if current is None:
        return ()
    latest, own = current
    if any(need.scope_id != snapshot.scope_id or need.based_on != latest.stamp for need in needs):
        raise ContractViolation("candidate need does not bind current evidence")
    unique = _deduplicate(needs)
    valid_ids = {need.need_id for need in needs}
    target_need = next((need for need in unique if need.condition == "visible_track"), None)
    gap_need = next((need for need in unique if need.condition == "motion_guard"), None)
    aim_need = next((need for need in unique
                     if need.condition in {"center_block_face", "observed_block"}), None)
    search_need = next((need for need in unique if need.condition == "filtered_check"), None)

    target_region = None
    if target_need is not None:
        target = next((item for item in view.base.entities
                       if item.track_id == target_need.track_id), None)
        if target is not None:
            assert latest.coverage is not None
            target_region = _target_region(own.position, target, latest.coverage, cfg)
            if (target_region.yaw_min>target_region.yaw_max
                    or target_region.pitch_min>target_region.pitch_max):
                target_region=None
    gap_regions = (_gap_regions(own.position, gap_need.point, own.yaw, latest.coverage)
                   if gap_need is not None else ())
    gap_region = _nearest_region(gap_regions,own.yaw,own.pitch)
    center_region=(_center_region(own.position,target_need.point,latest.coverage)
                   if target_need is not None else None)

    candidates: list[PerceptionCandidate] = []

    def add(kind: str, yaw: float, pitch: float, need_ids: tuple[str, ...], *,
            motion: MotionProposal | None = None, debt: float = 0.0,
            unseen: float | None = 0.0, future_motion: bool = False) -> None:
        ids = tuple(sorted(item for item in need_ids if item in valid_ids))
        turn = abs(_wrap(yaw - own.yaw)) / 90.0 + abs(pitch - own.pitch) / 45.0
        yaw_distance,pitch_distance=abs(_wrap(yaw-own.yaw)),abs(pitch-own.pitch)
        elapsed = max(yaw_distance/300.,pitch_distance/200.,
                      math.sqrt(2*yaw_distance/1200.),math.sqrt(2*pitch_distance/800.))
        if proposal is not None and (future_motion or motion is not None):
            elapsed+=proposal.estimated_duration_ns/1e9
        if kind=='hold' and debt>=1:
            elapsed+=cfg.unknown_seconds
        stages = (("move", "verify") if motion is not None else
                  ("observe", "move", "verify") if future_motion else
                  ("observe", "verify") if ids else ("observe",))
        candidate = PerceptionCandidate(
            f"candidate/{kind}/{len(candidates)}", kind, _wrap(yaw),
            max(-90.0, min(90.0, pitch)), motion, ids, stages,
            _outcomes(debt, elapsed, unseen,cfg.unknown_seconds), turn, 0,
            1 if proposal is not None and motion is None else 0,
        )
        semantic = (candidate.kind, round(candidate.yaw_degrees, 9),
                    round(candidate.pitch_degrees, 9), candidate.motion, candidate.need_ids)
        if not any((item.kind, round(item.yaw_degrees, 9), round(item.pitch_degrees, 9),
                    item.motion, item.need_ids) == semantic for item in candidates):
            candidates.append(candidate)

    active_ids = tuple(need.need_id for need in unique)
    legal_motion = proposal if _motion_is_currently_legal(snapshot, view, proposal, now_ns) else None
    def motion_for(yaw):
        return legal_motion if abs(_wrap(yaw-own.yaw))<=8 else None
    holds_target=target_need is None or _contains(target_region,own.yaw,own.pitch)
    holds_gap=gap_need is None or any(_contains(region,own.yaw,own.pitch) for region in gap_regions)
    holds_other=aim_need is None and not any(need.condition=='filtered_check' for need in unique)
    add("hold", own.yaw, own.pitch, active_ids, motion=legal_motion,
        debt=0.0 if holds_target and holds_gap and holds_other else 1.0,
        unseen=0.0 if target_region is not None else None)

    if target_region is not None and target_need is not None:
        yaw, pitch = _choose(target_region, own.yaw, own.pitch)
        add("target", yaw, pitch, (target_need.need_id,),
            motion=motion_for(yaw),
            debt=0.5 if gap_need is not None else 0.0, unseen=0.0)
    elif target_need is not None:
        yaw,pitch=_angles(own.position,target_need.point)
        add('target_partial',yaw,pitch,(target_need.need_id,),debt=1.,unseen=None)
        partial=candidates[-1]
        candidates[-1]=replace(partial,outcomes=(
            replace(partial.outcomes[0],unknown_reasons=('post_observation_required','target_margin_unavailable')),
            partial.outcomes[1]))

    if aim_need is not None:
        yaw, pitch = _angles(own.position, aim_need.point)
        add("aim", yaw, pitch, (aim_need.need_id,), debt=0.0, unseen=None)

    if search_need is not None:
        yaw, pitch = _angles(own.position, search_need.point)
        add("search", yaw, pitch, (search_need.need_id,), debt=0.0, unseen=None)
        if search_need.need_id in {'motion-heading','follow-preaim'} and target_need is not None:
            heading_region=AngleRegion(yaw-7.75,yaw+7.75,-90.,90.)
            whole=(intersect_angle_regions(heading_region,target_region)
                   if target_region is not None else None)
            partial=whole or intersect_angle_regions(heading_region,center_region)
            if partial is not None:
                # Repeatedly aiming only at the near tolerance edge spends a
                # stop/start on every small waypoint-bearing change. Use as
                # much of the existing route alignment region as tracking allows.
                partial_yaw,partial_pitch=_choose(partial,yaw,own.pitch)
                add('heading_track' if whole is not None else 'heading_track_partial',partial_yaw,partial_pitch,
                    (search_need.need_id,target_need.need_id),debt=0.,unseen=0.)
                if whole is None:
                    item=candidates[-1]
                    candidates[-1]=replace(item,outcomes=(replace(item.outcomes[0],
                        unknown_reasons=('post_observation_required','target_body_margin_unavailable',
                                         'occlusion_recheck_required')),item.outcomes[1]))

    if target_region is not None and gap_region is not None and target_need and gap_need:
        joint = _nearest_region((intersect_angle_regions(target_region,region)
                                 for region in gap_regions),own.yaw,own.pitch)
        if joint is not None:
            yaw, pitch = _choose(joint, own.yaw, own.pitch)
            add("joint", yaw, pitch, (target_need.need_id, gap_need.need_id),
                motion=motion_for(yaw),
                debt=0.0, unseen=0.0, future_motion=proposal is not None)

    if proposal is not None and target_need is not None and search_need is None and legal_motion is not None:
        # The 0.25-degree reserve is a new-turn target, not an extra movement
        # restriction. A currently legal <=8-degree heading must not be nudged.
        heading_region=AngleRegion(proposal.yaw_degrees-8.,proposal.yaw_degrees+8.,-90.,90.)
        whole=(intersect_angle_regions(heading_region,target_region) if target_region is not None else None)
        combined=whole or intersect_angle_regions(heading_region,center_region)
        if combined is not None:
            yaw,pitch=_choose(combined,own.yaw,own.pitch)
            add('route_track' if whole is not None else 'route_track_partial',yaw,pitch,
                (target_need.need_id,),motion=motion_for(yaw),debt=0.,unseen=0.)
            if whole is None:
                item=candidates[-1]
                candidates[-1]=replace(item,outcomes=(replace(item.outcomes[0],
                    unknown_reasons=('post_observation_required','target_body_margin_unavailable',
                                     'occlusion_recheck_required')),item.outcomes[1]))

    if gap_region is not None and center_region is not None and target_need and gap_need:
        whole=(_nearest_region((intersect_angle_regions(target_region,region) for region in gap_regions),
                               own.yaw,own.pitch) if target_region is not None else None)
        partial=(_nearest_region((intersect_angle_regions(center_region,region) for region in gap_regions),
                                 own.yaw,own.pitch) if whole is None else None)
        if partial is not None:
            yaw,pitch=_choose(partial,own.yaw,own.pitch)
            add('joint_partial',yaw,pitch,(target_need.need_id,gap_need.need_id),
                motion=motion_for(yaw),debt=0.,unseen=0.,future_motion=proposal is not None)
            item=candidates[-1]
            candidates[-1]=replace(item,outcomes=(replace(item.outcomes[0],
                unknown_reasons=('post_observation_required','target_body_margin_unavailable',
                                 'occlusion_recheck_required')),item.outcomes[1]))

    if gap_region is not None and gap_need is not None:
        yaw, pitch = _choose(gap_region, own.yaw, own.pitch)
        add("gap", yaw, pitch, (gap_need.need_id,),
            motion=motion_for(yaw),
            debt=0.5 if target_need is not None else 0.0,
            unseen=None if target_need is not None else 0.0,
            future_motion=proposal is not None)
        add("neutral_peek", yaw, pitch, (gap_need.need_id,),
            debt=0.5 if target_need is not None else 0.0,
            unseen=None if target_need is not None else 0.0,
            future_motion=proposal is not None)

    # Preference is neither sensor coverage nor task progress. It yields
    # completely to pending checks/aim/search and cannot authorize movement.
    if (target_need is not None and target_region is not None
            and all(need.condition == 'visible_track' for need in unique)
            and (proposal is None or legal_motion is not None)):
        target_yaw, target_pitch = _angles(own.position, target_need.point)
        preferred_yaw = own.yaw if proposal is not None else target_yaw
        preferred = intersect_angle_regions(target_region, AngleRegion(
            preferred_yaw, preferred_yaw, min(0., target_pitch), max(0., target_pitch)))
        if preferred is not None:
            yaw, pitch = _choose(preferred, own.yaw, own.pitch)
            # Preserve the actual moving yaw bit-for-bit; no preference micro-turn.
            if proposal is not None:
                yaw = own.yaw
            add('task_gaze', yaw, pitch, (target_need.need_id,),
                motion=legal_motion, debt=0., unseen=0.)
            satisfied = AngleRegion(
                preferred.yaw_min - YAW_TOLERANCE_DEGREES,
                preferred.yaw_max + YAW_TOLERANCE_DEGREES,
                preferred.pitch_min - PITCH_TOLERANCE_DEGREES,
                preferred.pitch_max + PITCH_TOLERANCE_DEGREES)
            candidates = [replace(item, task_gaze_debt=(0. if _contains(
                satisfied, item.yaw_degrees, item.pitch_degrees) else 1.))
                for item in candidates]

    return tuple(candidates[: cfg.max_candidates])


__all__ = [
    "AngleRegion",
    "generate_candidates",
    "intersect_angle_regions",
    "make_needs",
    "soft_cost",
]
