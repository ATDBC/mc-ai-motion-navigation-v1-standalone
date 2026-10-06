"""Fixed, validated action declarations. No runtime registration or fallback."""
from types import MappingProxyType
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.motion_solver import MotionSolveKind
from mc2p.motion_nav.actions.contracts import ActionSpec, BodyCommitment, ControllerFamily, StopHold


class ActionRegistry:
    __slots__ = ('_specs', '_by_type')

    def __init__(self, specs: tuple[ActionSpec, ...]):
        if type(specs) is not tuple:
            raise ContractViolation('action declarations must be immutable')
        by_type = {}
        for spec in specs:
            if (type(spec) is not ActionSpec or not isinstance(spec.segment_type, type)
                    or type(spec.body_commitment) is not BodyCommitment
                    or type(spec.requires_verified_motion) is not bool
                    or type(spec.needs_background_solving) is not bool
                    or type(spec.tracks_damage) is not bool
                    or type(spec.stop_hold) is not StopHold
                    or type(spec.controller_family) is not ControllerFamily
                    or any(not callable(getattr(spec, member)) for member in (
                        'expected_damage_points', 'completed', 'entry_observation',
                        'precondition', 'damage_committed'))):
                raise ContractViolation('action requires explicit safety and behavior declarations')
            hold = spec.stop_hold
            if (type(hold.same_frame_protection) is not bool
                    or any(value is not None and type(value) is not MovementV1
                           for value in (hold.information_movement, hold.dependency_movement))):
                raise ContractViolation('action stop declaration must be typed')
            if spec.needs_background_solving:
                if type(spec.solve_kind) is not MotionSolveKind or not callable(spec.solve_geometry):
                    raise ContractViolation('background action requires solve kind and geometry')
            elif spec.solve_kind is not None or spec.solve_geometry is not None:
                raise ContractViolation('non-solving action must explicitly omit solve geometry')
            if ((spec.controller_family is ControllerFamily.AIR or spec.controller_factory is not None)
                    and not callable(spec.controller_factory)):
                raise ContractViolation('action controller factory must be explicit')
            if spec.segment_type in by_type:
                raise ContractViolation('duplicate action declaration')
            by_type[spec.segment_type] = spec
        self._specs, self._by_type = specs, MappingProxyType(by_type)

    @property
    def specs(self): return self._specs

    @property
    def registered_types(self): return frozenset(self._by_type)

    def for_type(self, segment_type: type) -> ActionSpec:
        try:
            return self._by_type[segment_type]
        except KeyError:
            raise ContractViolation('unregistered route action') from None

    def require(self, action) -> ActionSpec:
        return self.for_type(type(action))


from mc2p.motion_nav.actions.existing import EXISTING_SPECS
from mc2p.motion_nav.actions.controlled_drop import CONTROLLED_DROP_SPEC
ACTION_REGISTRY = ActionRegistry(EXISTING_SPECS + (CONTROLLED_DROP_SPEC,))
if len(ACTION_REGISTRY.registered_types) != 5:
    raise ContractViolation('the fixed action set requires all five declarations')


def action_spec(action) -> ActionSpec:
    return ACTION_REGISTRY.require(action)
