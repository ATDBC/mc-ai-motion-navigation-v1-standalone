"""Bounded evaluator-only diagnostics from authenticated formal observations.

The accumulator retains counters and scalar extrema only.  It never stores raw
observations, actions, or controller contexts and is not an actor input.
"""

from __future__ import annotations

import math
from typing import Any


_VARIANTS = {"m6_baseline", "smooth_only", "active_perception_v1"}
_MAX_REASON_KEYS = 64


def _strict_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative int")
    return value


def _finite_nonnegative(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return float(value)


class QualityDiagnostics:
    """Streaming phase diagnostics with fixed memory bounds."""

    __slots__ = (
        "start_ns", "end_ns", "variant", "sample_count", "outside_window_samples",
        "missing_pose_samples", "missing_perception_samples", "missing_context_samples",
        "context_sample_count", "_pose_sample_count", "_previous_collision",
        "_collision_samples", "horizontal_collision_rises", "_previous_health",
        "_health_transition_count", "health_loss_events", "health_loss_points",
        "_previous_dead", "death_samples", "death_rises", "_harm_counts",
        "_track_id", "_actual_first", "_actual_last", "_decision_first",
        "_decision_last", "_visibility", "_selected", "_reason_counts",
        "decision_reason_overflow_count", "_context_peaks", "_search_peaks",
        "_ap_count", "_ap_missing", "_ap_peaks", "_ap_reasons", "_ap_overflow",
        "_ap_actual_count", "_ap_pose_errors",
    )

    def __init__(self, start_ns: int, end_ns: int, variant: str) -> None:
        self.start_ns = _strict_nonnegative_int(start_ns, "quality diagnostic start")
        self.end_ns = _strict_nonnegative_int(end_ns, "quality diagnostic end")
        if self.end_ns <= self.start_ns:
            raise ValueError("quality diagnostic end must be after start")
        if type(variant) is not str or variant not in _VARIANTS:
            raise ValueError("unknown quality diagnostic variant")
        self.variant = variant
        self.sample_count = 0
        self.outside_window_samples = 0
        self.missing_pose_samples = 0
        self.missing_perception_samples = 0
        self.missing_context_samples = 0
        self.context_sample_count = 0
        self._pose_sample_count = 0
        self._previous_collision = None
        self._collision_samples = 0
        self.horizontal_collision_rises = 0
        self._previous_health = None
        self._health_transition_count = 0
        self.health_loss_events = 0
        self.health_loss_points = 0.0
        self._previous_dead = None
        self.death_samples = 0
        self.death_rises = 0
        self._harm_counts = {"air_below_max": 0, "fall_distance_positive": 0, "is_burning": 0}
        self._track_id = None
        self._actual_first = self._actual_last = None
        self._decision_first = self._decision_last = None
        self._visibility = {"not_visible": 0, "unknown": 0, "visible": 0}
        self._selected = {"both": 0, "look": 0, "movement": 0}
        self._reason_counts: dict[str, int] = {}
        self.decision_reason_overflow_count = 0
        self._context_peaks = {"observation_check_count": None, "target_history_size": None}
        self._ap_count=self._ap_missing=self._ap_overflow=self._ap_actual_count=0
        self._ap_peaks={name:None for name in ('planning_ns','frame_elapsed_ns','candidate_count','need_count','no_progress')}
        self._ap_reasons={}
        self._ap_pose_errors={'yaw':None,'pitch':None}
        self._search_peaks = {
            "candidate_count": None,
            "completed_checks": None,
            "elapsed_ns": None,
            "look_requests": None,
            "travelled_blocks": None,
        }

    @staticmethod
    def _group_value(raw: dict, name: str) -> dict | None:
        group = raw.get(name)
        if type(group) is not dict:
            raise ValueError(f"formal observation {name} group is invalid")
        value = group.get("value")
        if value is not None and type(value) is not dict:
            raise ValueError(f"formal observation {name} value is invalid")
        return value

    @staticmethod
    def _peak(target: dict[str, Any], name: str, value: object, *, integral: bool) -> None:
        number = (_strict_nonnegative_int(value, name) if integral
                  else _finite_nonnegative(value, name))
        old = target[name]
        if old is None or number > old:
            target[name] = number

    def _count_reason(self, reason: object) -> None:
        if type(reason) is not str or not reason or reason != reason.strip():
            raise ValueError("quality diagnostic decision reason is invalid")
        if reason in self._reason_counts:
            self._reason_counts[reason] += 1
        elif len(self._reason_counts) < _MAX_REASON_KEYS:
            self._reason_counts[reason] = 1
        else:
            self.decision_reason_overflow_count += 1

    def _observe_context(self, context: dict | None) -> None:
        if context is None:
            self.missing_context_samples += 1
            return
        if type(context) is not dict or type(context.get("decision")) is not dict:
            raise ValueError("quality diagnostic context is invalid")
        self.context_sample_count += 1
        decision = context["decision"]
        target = decision.get("target")
        if type(target) is not dict or type(target.get("track_id")) is not str or not target["track_id"]:
            raise ValueError("quality diagnostic target context is invalid")
        target_id = target["track_id"]
        if self._track_id is None:
            self._track_id = target_id
        elif self._track_id != target_id:
            raise ValueError("quality diagnostic target rebound")

        decision_distance = decision.get("distance")
        if decision_distance is not None:
            value = _finite_nonnegative(decision_distance, "decision target distance")
            if self._decision_first is None:
                self._decision_first = value
            self._decision_last = value

        accepted = context.get("accepted_intent_id")
        selected = context.get("selected")
        if type(selected) is not dict:
            raise ValueError("quality diagnostic selected context is invalid")
        movement = accepted is not None and selected.get("movement") == accepted
        look = accepted is not None and selected.get("look") == accepted
        self._selected["movement"] += movement
        self._selected["look"] += look
        self._selected["both"] += movement and look
        self._count_reason(decision.get("reason"))

        self._peak(self._context_peaks, "target_history_size",
                   decision.get("target_history_size"), integral=True)
        self._peak(self._context_peaks, "observation_check_count",
                   decision.get("observation_check_count"), integral=True)
        search = decision.get("search_report")
        if search is not None:
            if type(search) is not dict:
                raise ValueError("quality diagnostic search report is invalid")
            for name in ("candidate_count", "completed_checks", "elapsed_ns", "look_requests"):
                self._peak(self._search_peaks, name, search.get(name), integral=True)
            self._peak(self._search_peaks, "travelled_blocks", search.get("travelled_blocks"), integral=False)

    def _bind_visible_target(self, perception: dict | None) -> None:
        if perception is None:
            self.missing_perception_samples += 1
            self._visibility["unknown"] += 1
            return
        entities = perception.get("visible_entities")
        if type(entities) is not list:
            raise ValueError("quality diagnostic visible entities are invalid")
        if self._track_id is None:
            players = [item for item in entities
                       if type(item) is dict and item.get("entity_type") == "minecraft:player"]
            if len(players) == 1 and type(players[0].get("track_id")) is str:
                self._track_id = players[0]["track_id"]
        if self._track_id is None:
            self._visibility["unknown"] += 1
            return
        target = next((item for item in entities
                       if type(item) is dict and item.get("track_id") == self._track_id), None)
        if target is None:
            self._visibility["not_visible"] += 1
            return
        relative = target.get("relative_position")
        if type(relative) is not dict:
            raise ValueError("quality diagnostic target position is invalid")
        x = relative.get("x")
        z = relative.get("z")
        if type(x) not in (int, float) or type(z) not in (int, float) or not all(map(math.isfinite, (x, z))):
            raise ValueError("quality diagnostic target position is invalid")
        distance = math.hypot(x, z)
        if self._actual_first is None:
            self._actual_first = distance
        self._actual_last = distance
        self._visibility["visible"] += 1

    def _observe_pose(self, own: dict | None) -> None:
        if own is None:
            self.missing_pose_samples += 1
            self._previous_collision = self._previous_health = self._previous_dead = None
            return
        self._pose_sample_count += 1
        collision = own.get("horizontal_collision")
        dead = own.get("is_dead")
        burning = own.get("is_burning")
        if type(collision) is not bool or type(dead) is not bool or type(burning) is not bool:
            raise ValueError("quality diagnostic self-state flags are invalid")
        self._collision_samples += collision
        if collision and self._previous_collision is False:
            self.horizontal_collision_rises += 1
        self._previous_collision = collision
        health = _finite_nonnegative(own.get("health_points"), "actual health")
        if self._previous_health is not None:
            self._health_transition_count += 1
            if health < self._previous_health:
                self.health_loss_events += 1
                self.health_loss_points += self._previous_health - health
        self._previous_health = health
        self.death_samples += dead
        if dead and self._previous_dead is False:
            self.death_rises += 1
        self._previous_dead = dead
        fall = _finite_nonnegative(own.get("fall_distance_blocks"), "actual fall distance")
        air = _strict_nonnegative_int(own.get("air_ticks"), "actual air ticks")
        max_air = _strict_nonnegative_int(own.get("max_air_ticks"), "actual max air ticks")
        if air > max_air:
            raise ValueError("actual air exceeds maximum")
        self._harm_counts["is_burning"] += burning
        self._harm_counts["fall_distance_positive"] += fall > 0
        self._harm_counts["air_below_max"] += air < max_air

    def observe(self, raw: dict, action: dict | None, context: dict | None) -> None:
        if type(raw) is not dict:
            raise ValueError("quality diagnostic observation must be a dict")
        now_ns = _strict_nonnegative_int(raw.get("received_at_monotonic_ns"),
                                         "quality diagnostic observation time")
        if not self.start_ns <= now_ns < self.end_ns:
            self.outside_window_samples += 1
            return
        if action is not None and type(action) is not dict:
            raise ValueError("quality diagnostic action must be a dict or null")
        self.sample_count += 1
        self._observe_context(context)
        self._observe_ap(raw,context)
        self._observe_pose(self._group_value(raw, "self_state"))
        self._bind_visible_target(self._group_value(raw, "perception"))

    def _observe_ap(self, raw, context):
        event=None if context is None else context.get('active_perception')
        if event is None:
            self._ap_missing+=int(context is not None and self.variant!='m6_baseline')
            return
        if (self.variant=='m6_baseline' or type(event) is not dict
                or event.get('schema_version')!='mc2p.active-perception-step.v1'
                or event.get('variant')!=self.variant or type(event.get('perception')) is not dict):
            raise ValueError('invalid associated active perception diagnostics')
        status=event['perception']
        elapsed=event.get('planning_elapsed_ns',[])
        if (type(elapsed) is not list or len(elapsed)>8
                or any(type(value) is not int or not 0<=value<=250_000_000 for value in elapsed)
                or elapsed and (elapsed[0]!=0 or any(b<a for a,b in zip(elapsed,elapsed[1:])))):
            raise ValueError('invalid bounded planning time samples')
        self._ap_count+=1
        frame_bound=context.get('control_deadline_ns') is not None and self.variant=='active_perception_v1'
        entry_offset=elapsed[1] if frame_bound and len(elapsed)>=3 else 0
        for name in self._ap_peaks:
            if name=='frame_elapsed_ns':
                value=elapsed[-1] if frame_bound and elapsed else None
            elif name=='planning_ns' and elapsed:
                value=elapsed[-1]-entry_offset
            else:
                value=status.get(name)
            if value is not None:
                number=_strict_nonnegative_int(value,'active perception '+name)
                if (name=='candidate_count' and number>16) or (name=='need_count' and number>8):
                    raise ValueError('active perception collection exceeded capacity')
                self._peak(self._ap_peaks,name,number,integral=True)
        reason=status.get('motion_guard_reason') or status.get('reason')
        if type(reason) is not str or not reason or len(reason)>128:
            raise ValueError('invalid active perception constraint reason')
        if reason in self._ap_reasons or len(self._ap_reasons)<64:
            self._ap_reasons[reason]=self._ap_reasons.get(reason,0)+1
        else:
            self._ap_overflow+=1
        own=self._group_value(raw,'self_state')
        if (own is None or context['selected'].get('look')!=context['accepted_intent_id']
                or status.get('requested_yaw_degrees') is None or status.get('requested_pitch_degrees') is None):
            return
        errors={}
        for axis in ('yaw','pitch'):
            goal,actual=status['requested_'+axis+'_degrees'],own[axis+'_degrees']
            if any(type(value) not in (int,float) or not math.isfinite(value) for value in (goal,actual)):
                raise ValueError('invalid planned/actual gaze angle')
            errors[axis]=abs((goal-actual+180)%360-180) if axis=='yaw' else abs(goal-actual)
        self._ap_actual_count+=1
        for axis,error in errors.items():
            self._ap_pose_errors[axis]=max(error,self._ap_pose_errors[axis] or 0.)

    @staticmethod
    def _distance_report(first: float | None, last: float | None) -> dict[str, float | None]:
        return {
            "first_blocks": first,
            "last_blocks": last,
            "delta_blocks": None if first is None or last is None else last - first,
        }

    def _active_perception_status(self) -> dict[str, Any]:
        if self._ap_count:
            return {'status':'available','event_count':self._ap_count,'missing_event_count':self._ap_missing,
                'planned_vs_actual':{'status':'available' if self._ap_actual_count else 'unavailable',
                    'pose_sample_count':self._ap_actual_count,
                    'max_yaw_error_degrees':self._ap_pose_errors['yaw'],
                    'max_pitch_error_degrees':self._ap_pose_errors['pitch'],
                    'meaning':'requested goal versus selected actual post pose, not completion'},
                'planner_and_candidate':{'status':'available','peaks':dict(self._ap_peaks)},
                'constraint_counts':{'status':'available','reasons':dict(self._ap_reasons),
                                     'overflow_count':self._ap_overflow}}
        status = "not_applicable" if self.variant == "m6_baseline" else "unavailable"
        return {
            "planned_vs_actual": {"status": status},
            "planner_and_candidate": {"status": status},
            "constraint_counts": {"status": status},
            "status": status,
        }

    def report(self) -> dict[str, Any]:
        return {
            "schema_version": "mc2p.active-perception-diagnostics.v1",
            "variant": self.variant,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "sample_count": self.sample_count,
            "outside_window_samples": self.outside_window_samples,
            "missing_pose_samples": self.missing_pose_samples,
            "missing_perception_samples": self.missing_perception_samples,
            "missing_context_samples": self.missing_context_samples,
            "context_sample_count": self.context_sample_count,
            "context_coverage_fraction": (None if not self.sample_count
                                          else self.context_sample_count / self.sample_count),
            "horizontal_collision_samples": (None if not self._pose_sample_count
                                               else self._collision_samples),
            "horizontal_collision_rises": (None if not self._pose_sample_count
                                             else self.horizontal_collision_rises),
            "health_observed_transition_count": self._health_transition_count,
            "health_loss_events": (None if not self._health_transition_count else self.health_loss_events),
            "health_loss_points": (None if not self._health_transition_count else self.health_loss_points),
            "death_samples": None if not self._pose_sample_count else self.death_samples,
            "death_rises": None if not self._pose_sample_count else self.death_rises,
            "harm_flag_sample_counts": (None if not self._pose_sample_count else dict(self._harm_counts)),
            "actual_target_distance": self._distance_report(self._actual_first, self._actual_last),
            "decision_target_distance": self._distance_report(self._decision_first, self._decision_last),
            "target_visibility_samples": dict(self._visibility),
            "selected_counts": dict(self._selected),
            "decision_reason_counts": dict(self._reason_counts),
            "decision_reason_overflow_count": self.decision_reason_overflow_count,
            "context_peaks": dict(self._context_peaks),
            "search_peaks": dict(self._search_peaks),
            "active_perception_diagnostics": self._active_perception_status(),
            "provenance": "authenticated lawful post-observation and actual controller context; evaluator-only",
        }


__all__ = ["QualityDiagnostics"]
