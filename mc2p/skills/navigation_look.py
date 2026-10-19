"""An issued look is not observed coverage; validate selected post-observations."""
from dataclasses import dataclass

from mc2p.contracts.action_v1 import LookV1
from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int


def wrap(angle):
    return (angle+180)%360-180


@dataclass(frozen=True, slots=True)
class LookRequest:
    scope: tuple[str,str,str]
    sequence_id: int
    client_sample_end_ns: int
    yaw: float
    pitch: float
    yaw_tolerance: float
    pitch_tolerance: float
    requested_at_ns: int


class ObservationGate:
    def __init__(self):
        self.clear()

    def clear(self):
        self.pending = None
        self._confirmed = None

    def request(self, view, yaw, pitch, now_ns, *, gaze=None,
                yaw_tolerance=8.0, pitch_tolerance=6.0):
        require_nonnegative_int(now_ns,'look request time')
        require_finite(yaw,'look yaw'); require_finite(pitch,'look pitch')
        require_finite(yaw_tolerance, 'look yaw tolerance')
        require_finite(pitch_tolerance, 'look pitch tolerance')
        if not 0 <= yaw_tolerance <= 180 or not 0 <= pitch_tolerance <= 90:
            raise ContractViolation('look tolerance is out of range')
        base,own=view.base,view.base.own
        if not base.available or own is None or not 0<=now_ns-base.request_start_ns<=500_000_000:
            raise ContractViolation('look request requires fresh legal view')
        self.pending=LookRequest((base.episode_id,base.controller_clock_id,base.client_clock_id),
            base.sequence_id,base.client_sample_end_ns,wrap(yaw),max(-90,min(90,pitch)),
            yaw_tolerance,pitch_tolerance,now_ns)
        self._confirmed=None
        if gaze is not None:
            # Explicit navigation-only smoothing. Confirmation remains a separate
            # actual post-observation check; no default behavior switch here.
            return gaze.command(view,yaw,pitch,now_ns)
        dy,dp=wrap(yaw-own.yaw),pitch-own.pitch
        return LookV1(0 if abs(dy)<=yaw_tolerance else max(-15,min(15,dy)),
                      0 if abs(dp)<=pitch_tolerance else max(-10,min(10,dp)))

    def feedback(self, selected, view, now_ns):
        if type(selected) is not bool:
            raise ContractViolation('look feedback must be boolean')
        require_nonnegative_int(now_ns,'look feedback time')
        self._confirmed=None
        request=self.pending; base,own=view.base,view.base.own
        if (not selected or request is None or not base.available or own is None
                or (base.episode_id,base.controller_clock_id,base.client_clock_id)!=request.scope
                or base.sequence_id<=request.sequence_id
                or base.client_sample_start_ns<request.client_sample_end_ns
                or not 0<=now_ns-base.request_start_ns<=500_000_000
                or base.received_at_ns<request.requested_at_ns or now_ns<base.received_at_ns
                or abs(wrap(own.yaw-request.yaw))>request.yaw_tolerance
                or abs(own.pitch-request.pitch)>request.pitch_tolerance):
            return
        self._confirmed=base

    def confirmed(self, view, now_ns):
        require_nonnegative_int(now_ns,'look confirmation time')
        base,own=view.base,view.base.own
        return bool(self._confirmed is not None and base==self._confirmed and base.available
            and own is not None and 0<=now_ns-base.request_start_ns<=500_000_000)
