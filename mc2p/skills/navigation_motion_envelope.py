"""Read-only geometry shared with the frozen combined control guard.

These helpers describe dependencies, never grant an action lease or validate
evidence. The caller must still run check_control_proposal for each submission.
"""
from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int
from mc2p.skills.normal_direction_control import world_direction
from mc2p.skills.normal_navigation_guard import (
    COMBINED_BODY_MARGIN, _convex_hull, _polygon_open_rect,
)


def proposal_sweep(proposal):
    """The exact center hull used by check_control_proposal, without authority."""
    origin=(proposal.origin.x,proposal.origin.z)
    vx,vz=proposal.velocity.x,proposal.velocity.z
    speed=math.hypot(vx,vz)
    if not math.isfinite(speed) or speed>.65:
        raise ContractViolation('motion envelope exceeds combined guard speed bound')
    dx,dz=world_direction(proposal.yaw+proposal.look.yaw_delta_degrees,
        proposal.movement.forward,proposal.movement.strafe)
    travel=0. if dx==dz==0. else .45+2*speed
    end=(origin[0]+dx*travel,origin[1]+dz*travel)
    return _convex_hull((origin,end,(origin[0]+2*vx,origin[1]+2*vz),
        (end[0]+2*vx,end[1]+2*vz)))


def proposal_supports(proposal,floor=None):
    """Return only the grid supports touched by the complete guarded body hull."""
    if floor is None:floor=round(proposal.origin.y)-1
    sweep=proposal_sweep(proposal);margin=COMBINED_BODY_MARGIN
    min_x=math.floor(min(p[0] for p in sweep)-margin)
    max_x=math.floor(max(p[0] for p in sweep)+margin)
    min_z=math.floor(min(p[1] for p in sweep)-margin)
    max_z=math.floor(max(p[1] for p in sweep)+margin)
    return frozenset((x,floor,z) for x in range(min_x,max_x+1) for z in range(min_z,max_z+1)
        if _polygon_open_rect(sweep,x-margin,z-margin,x+1+margin,z+1+margin))


def proposal_depends_on(blocks,proposal):
    return not proposal_supports(proposal).isdisjoint(blocks)


@dataclass(frozen=True,slots=True)
class StoppingRequirement:
    distance: float
    prefix_distance: float
    covered: bool
    guard_distance: float
    reaction_ns: int


def stopping_requirement(velocity,*,speed_blocks_per_second,delivery_ns,
                         control_interval_ns=50_000_000,release_ns=250_000_000,
                         look_ns=0,max_prefix=8.):
    """Conservative scheduling distance within the existing local speed profile.

    The guard's .45+2*speed travel, 2*velocity release drift, and full combined
    body margin are retained. Observation delivery/turning can overlap, so their
    maximum is followed by the next control opportunity and release lease.
    The supplied speed is a scheduling profile, not a newly proved physical
    upper bound. Actual permission always uses the full live proposal guard.
    Requirements beyond the local prefix are reported, never silently clipped.
    """
    for value,name in ((speed_blocks_per_second,'review speed'),(max_prefix,'review prefix')):
        require_finite(value,name)
        if value<=0:raise ContractViolation(name+' must be positive')
    for value,name in ((delivery_ns,'delivery'),(control_interval_ns,'control interval'),
                       (release_ns,'release'),(look_ns,'look')):
        require_nonnegative_int(value,name)
    drift=math.hypot(velocity.x,velocity.z)
    if not math.isfinite(drift) or drift>.65:
        raise ContractViolation('review exceeds combined guard speed bound')
    guard_distance=.45+4*drift+COMBINED_BODY_MARGIN
    reaction_ns=max(delivery_ns,look_ns)+control_interval_ns+release_ns
    distance=guard_distance+speed_blocks_per_second*reaction_ns/1e9
    return StoppingRequirement(distance,min(max_prefix,max(4.,distance)),
        distance<=max_prefix,guard_distance,reaction_ns)
