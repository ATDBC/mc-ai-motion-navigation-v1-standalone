"""Bounded camera requests, not observations or proof that a target was seen."""
from __future__ import annotations

import math
import struct

from mc2p.contracts.action_v1 import LookV1
from mc2p.contracts.common import ContractViolation, require_finite, require_identifier, require_nonnegative_int
from mc2p.skills.navigation_views import PlaygroundView


def limited_velocity(previous: float, desired: float, acceleration: float, seconds: float) -> float:
    change=acceleration*seconds
    return max(previous-change,min(previous+change,desired))


def _client_float(value: float) -> float:
    return struct.unpack('!f',struct.pack('!f',value))[0]


def _client_float_noop(current: float, delta: float) -> bool:
    """Mirror the shared executor's float delta and float angle addition.

    This is representability, not an angular tolerance: a representable tiny
    turn still executes, and observation remains the only arrival evidence.
    """
    f32 = _client_float
    return delta!=0 and f32(f32(current)+f32(delta))==f32(current)


def _axis(error, previous, seconds, speed, acceleration, delta_limit, tolerance):
    if abs(error)<=tolerance:
        return 0.,0.,'target_tolerance'
    desired=math.copysign(min(speed,abs(error)/seconds,math.sqrt(2*acceleration*abs(error))),error)
    velocity=limited_velocity(previous,desired,acceleration,seconds)
    delta=max(-delta_limit,min(delta_limit,velocity*seconds))
    boundary='request_limit' if abs(delta-velocity*seconds)>1e-10 else None
    if delta*error>0 and abs(delta)>=abs(error):
        return error,0.,'target_reached'
    return delta,delta/seconds,boundary


class GazeController:
    """One body's last control only; no world handle, map or observation history.

    Arrival, clamps and releases are explicit boundary events. They are not
    excuses to exclude ordinary switching from actual-pose quality metrics.
    """
    def __init__(self):
        self.clear('initial')

    def clear(self, reason: str) -> None:
        require_identifier(reason,'gaze clear reason')
        if len(reason)>128:
            raise ContractViolation('gaze clear reason is unbounded')
        self._last_time=None
        self._scope=None
        self._sequence=-1
        self._yaw_velocity=self._pitch_velocity=0.
        self.pending_control=None
        self.boundary_events=(reason,)

    def command(self, view: PlaygroundView, yaw: float, pitch: float, now_ns: int,
                *, precise: bool=False) -> LookV1:
        require_nonnegative_int(now_ns,'gaze control time')
        require_finite(yaw,'gaze yaw'); require_finite(pitch,'gaze pitch')
        if type(view) is not PlaygroundView or type(precise) is not bool:
            raise ContractViolation('gaze requires a lawful view and boolean precision')
        base,own=view.base,view.base.own
        if (not base.available or own is None or now_ns<base.received_at_ns
                or not 0<=now_ns-base.request_start_ns<=500_000_000):
            self.clear('unavailable_or_stale_view')
            return LookV1()
        events=[]
        scope=(base.episode_id,base.controller_clock_id,base.client_clock_id)
        if self._scope is not None and self._scope!=scope:
            self.clear('scope_changed'); events.extend(self.boundary_events)
        if base.sequence_id<=self._sequence or (self._last_time is not None and now_ns<=self._last_time):
            self.boundary_events=('duplicate_or_nonpositive_control_sample',)
            return LookV1()
        if self._last_time is not None and now_ns-self._last_time>250_000_000:
            self.clear('control_gap'); events.extend(self.boundary_events)
        seconds=.05 if self._last_time is None else (now_ns-self._last_time)/1e9
        bounded_pitch=max(-90.,min(90.,pitch))
        if bounded_pitch!=pitch:
            events.append('pitch_target_clipped')
        # Gson emits a shortest round-tripping decimal for Java float angles.
        # Recover the stored float before subtracting a double geometry goal;
        # otherwise opposite one-ULP corrections can oscillate indefinitely.
        yaw_error=(yaw-_client_float(own.yaw)+180)%360-180
        pitch_error=bounded_pitch-_client_float(own.pitch)
        dy,self._yaw_velocity,yaw_event=_axis(yaw_error,self._yaw_velocity,seconds,300.,1200.,15.,0. if precise else 8.)
        dp,self._pitch_velocity,pitch_event=_axis(pitch_error,self._pitch_velocity,seconds,200.,800.,10.,0. if precise else 6.)
        if yaw_event: events.append('yaw_'+yaw_event)
        if pitch_event: events.append('pitch_'+pitch_event)
        if _client_float_noop(own.yaw,dy):
            dy=self._yaw_velocity=0.
            events.append('yaw_client_float_noop')
        if _client_float_noop(own.pitch,dp):
            dp=self._pitch_velocity=0.
            events.append('pitch_client_float_noop')
        command=LookV1(dy,dp)
        self._scope,self._sequence,self._last_time=scope,base.sequence_id,now_ns
        self.pending_control=(scope,base.sequence_id,now_ns,command)
        self.boundary_events=tuple(events)
        return command
