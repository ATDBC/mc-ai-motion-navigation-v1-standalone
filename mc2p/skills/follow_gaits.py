"""Explicit movement requests; requested sprint is never evidence of actual sprint."""
from dataclasses import dataclass
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int


def fixed_movement(mode: str) -> MovementV1:
    movements = {'slow': MovementV1(forward=1,sneak=True), 'normal': MovementV1(forward=1),
                 'fast': MovementV1(forward=1,sprint=True), 'max': MovementV1(forward=1,sprint=True,jump=True)}
    if not isinstance(mode,str) or mode not in movements:
        raise ContractViolation('not a fixed gait')
    return movements[mode]


@dataclass
class FixedDistanceGate:
    approach: float = 3.5
    hold: float = 2.5
    retreat: float = 1.5
    _approaching: bool = False

    def __post_init__(self) -> None:
        for name in ('approach','hold','retreat'): require_finite(getattr(self,name),name)
        if not 0 < self.retreat < self.hold < self.approach:
            raise ContractViolation('invalid fixed distances')

    def update(self, distance: float) -> str:
        require_finite(distance,'target distance')
        if distance < 0: raise ContractViolation('negative distance')
        if distance <= self.hold: self._approaching = False
        elif distance > self.approach: self._approaching = True
        if distance < self.retreat: return 'retreat'
        return 'approach' if self._approaching else 'hold'


@dataclass(frozen=True, slots=True)
class AutoGaitConfig:
    target_distance: float = 3.0
    hold_band: float = .35
    restart_band: float = .75
    normal_error: float = 1.0
    fast_error: float = 3.0
    max_error: float = 6.0
    switch_hysteresis: float = .5
    min_dwell_ns: int = 300_000_000

    def __post_init__(self) -> None:
        for name in ('target_distance','hold_band','restart_band','normal_error','fast_error','max_error','switch_hysteresis'):
            require_finite(getattr(self,name),name)
        require_nonnegative_int(self.min_dwell_ns,'gait dwell')
        if (not 1.5 <= self.target_distance <= 6 or not 0 < self.hold_band < self.restart_band
                or not 0 < self.normal_error < self.fast_error < self.max_error
                or not 0 <= self.switch_hysteresis < self.normal_error):
            raise ContractViolation('invalid auto gait configuration')


class AutoGaitSelector:
    def __init__(self, config: AutoGaitConfig) -> None:
        if type(config) is not AutoGaitConfig: raise ContractViolation('auto requires explicit config')
        self.config = config
        self.selected = 'hold'
        self._changed_at = self._last_now = None

    def choose(self, distance: float, closing_speed: float, own_speed: float, yaw_error: float,
               safe_jump: bool, now_ns: int) -> str:
        for name,value in (('distance',distance),('closing speed',closing_speed),('own speed',own_speed),('yaw error',yaw_error)):
            require_finite(value,name)
        require_nonnegative_int(now_ns,'gait time')
        if distance < 0 or own_speed < 0 or type(safe_jump) is not bool:
            raise ContractViolation('invalid gait inputs')
        if self._last_now is not None and now_ns<self._last_now: raise ContractViolation('gait clock regressed')
        self._last_now = now_ns
        config = self.config
        safety_exit = self.selected=='max' and (not safe_jump or abs(yaw_error)>20)
        if distance<=config.target_distance+config.hold_band or (
                self.selected=='hold' and distance<=config.target_distance+config.restart_band):
            candidate = 'hold'
        else:
            error = distance-config.target_distance-max(0,closing_speed)*.35-own_speed*.10
            candidate = ('slow' if error<config.normal_error else 'normal' if error<config.fast_error
                         else 'fast' if error<config.max_error else 'max')
            gaits = ('slow','normal','fast','max')
            if self.selected in gaits and candidate!=self.selected and not safety_exit:
                current, desired = gaits.index(self.selected),gaits.index(candidate)
                bounds = (config.normal_error,config.fast_error,config.max_error)
                crossed = (error>=bounds[current]+config.switch_hysteresis if desired>current
                           else error<bounds[current-1]-config.switch_hysteresis)
                if not crossed or (self._changed_at is not None and now_ns-self._changed_at<config.min_dwell_ns):
                    candidate = self.selected
            # This cap is applied after dwell/hysteresis: safety is never delayed.
            if candidate=='max' and (not safe_jump or abs(yaw_error)>20):
                candidate = 'fast' if abs(yaw_error)<=35 else 'normal'
        if candidate!=self.selected:
            self.selected,self._changed_at = candidate,now_ns
        return self.selected
