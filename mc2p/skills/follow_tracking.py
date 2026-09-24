"""Single lawful target binding and finite search budget, independent of game/backend access."""
from dataclasses import replace
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.skills.follow_types import MOVEMENT_FRESHNESS_NS, TARGET_TTL_NS
from mc2p.skills.follow_playground_types import PlaygroundView, TrackedTarget
from mc2p.skills.local_perception import project_follow_view


def project_playground_view(observation: ObservationSnapshotV3, now_ns: int, clock_id: str) -> PlaygroundView:
    base = project_follow_view(observation,now_ns,clock_id)
    if not base.available: return PlaygroundView(base,None,None,None,())
    own = observation.self_state.value
    unsupported = (own.pose not in {'standing','crouching'} or own.is_swimming or own.is_submerged_in_water
        or own.is_climbing or own.is_fall_flying or own.is_flying or own.is_burning
        or own.game_mode in {'creative','spectator'})
    base = replace(base,own=replace(base.own,unsupported_motion=unsupported))
    return PlaygroundView(base,own.pose,own.food_points,own.game_mode,tuple(e.effect_id for e in own.status_effects))


class TargetTracker:
    def __init__(self): self.clear()

    def clear(self):
        self._view = None
        self._track_id = self._position = self._size = self._seen = None
        self._last_now = 0
        self._invalidated = None
        self._loss_started = None
        self._search_turns = 0
        self._last_search_sequence = -1

    def bind_unique(self, view: PlaygroundView, now_ns: int) -> TrackedTarget:
        require_nonnegative_int(now_ns,'target bind time')
        base = view.base
        if self._view is not None: raise ContractViolation('target already bound')
        if (not base.available or base.own is None or base.own.unsupported_motion or base.own.dead
                or not 0 <= now_ns-base.received_at_ns <= MOVEMENT_FRESHNESS_NS or base.entities_truncated):
            raise ContractViolation('target binding requires fresh supported legal view')
        players = [e for e in base.entities if e.entity_type=='minecraft:player']
        if len(players)!=1: raise ContractViolation('target binding requires one legally visible player')
        target, = players
        self._view, self._track_id = view,target.track_id
        self._position,self._size,self._seen = target.position,target.size,base.received_at_ns
        self._last_now = now_ns
        return self.observe(view,now_ns)

    def _invalidate(self, reason):
        self._position = self._size = None
        self._invalidated = TrackedTarget('invalidated',self._track_id,None,None,self._seen,reason)
        return self._invalidated

    def observe(self, view: PlaygroundView, now_ns: int) -> TrackedTarget:
        require_nonnegative_int(now_ns,'target observation time')
        if self._view is None: raise ContractViolation('target is not bound')
        if self._invalidated is not None: return self._invalidated
        base,old = view.base,self._view.base
        if (base.episode_id,base.controller_clock_id,base.client_clock_id) != (old.episode_id,old.controller_clock_id,old.client_clock_id):
            return self._invalidate('session_or_clock_changed')
        if now_ns < self._last_now: return self._invalidate('controller_time_regression')
        self._last_now = now_ns
        if base.sequence_id < old.sequence_id: return self._invalidate('sequence_regression')
        if base.available and base.sequence_id==old.sequence_id and view!=self._view:
            return self._invalidate('sequence_rewritten')
        if base.received_at_ns>now_ns: return self._invalidate('future_observation')
        fresh = base.sequence_id>old.sequence_id
        if fresh:
            if (base.received_at_ns<old.received_at_ns or base.request_start_ns<old.request_start_ns
                    or base.client_sample_start_ns<old.client_sample_end_ns):
                return self._invalidate('sample_time_regression')
            self._view = view
        visible = False
        if base.available and now_ns-base.received_at_ns<=MOVEMENT_FRESHNESS_NS:
            matches = [e for e in base.entities if e.track_id==self._track_id]
            if len(matches)>1: return self._invalidate('duplicate_target_reference')
            if matches:
                target, = matches
                if target.entity_type!='minecraft:player': return self._invalidate('target_type_changed')
                visible = True
                if fresh:
                    self._position,self._size,self._seen = target.position,target.size,base.received_at_ns
                    self._loss_started=None; self._search_turns=0
        if visible:
            return TrackedTarget('visible',self._track_id,self._position,self._size,self._seen)
        if self._loss_started is None: self._loss_started = now_ns
        if now_ns-self._seen>=TARGET_TTL_NS: self._position = self._size = None
        waiting = now_ns-self._loss_started>=10_000_000_000 or self._search_turns>=16
        status = 'waiting_target' if waiting else 'remembered' if self._position is not None else 'lost'
        return TrackedTarget(status,self._track_id,self._position,self._size,self._seen,
                             'search_budget_exhausted' if waiting else base.reason)

    def consume_search_turn(self, view: PlaygroundView, now_ns: int) -> bool:
        """Controller calls only when issuing a scan. Same observation never spends twice."""
        target = self.observe(view,now_ns)
        if (target.status not in {'remembered','lost'} or not view.base.available
                or not 0 <= now_ns-view.base.received_at_ns<=MOVEMENT_FRESHNESS_NS
                or view.base.sequence_id<=self._last_search_sequence): return False
        self._last_search_sequence=view.base.sequence_id
        self._search_turns += 1
        return True
