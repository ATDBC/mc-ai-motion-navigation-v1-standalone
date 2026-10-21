"""CalculatorBackend's bounded v2 client sampler, before calculator physics.

No route/proof logic lives here. This is the same current/pending/default table
as the Java sampler; a response can remain held while movement keeps advancing.
"""
from dataclasses import dataclass

from mc2p.contracts.action_v1 import ActionSnapshotV1, LookV1, MovementV1
from mc2p.contracts.action_receipt import ClientTimingEventV1, ClientTimingStatusV1
from mc2p.contracts.common import ContractViolation


@dataclass(frozen=True, slots=True)
class TimingSelection:
    owner: ActionSnapshotV1 | None
    movement: MovementV1
    look: LookV1
    state: str


class ClientTimingSampler:
    def __init__(self, episode: str):
        self.episode = episode
        self.last_sequence = -1
        self.current = None
        self.remaining = 0
        self.pending = None
        self.default_parent = None
        self.default_remaining = 0
        self.default_wall_deadline = None
        self.events = []
        self.admissions = 0
        self.activation_checks = 0
        self.permitted = True
        self._missed = False
        self._one_shot = False

    def clear(self):
        self.current = self.pending = self.default_parent = None
        self.remaining = self.default_remaining = 0
        self.default_wall_deadline = None
        self._missed = self._one_shot = False

    def reset(self, episode: str):
        self.clear()
        self.episode = episode
        self.last_sequence = -1

    def event(self, status: str, tick: int, action: ActionSnapshotV1):
        self.events.append(ClientTimingEventV1(ClientTimingStatusV1(status), tick,
                                               action.episode_id, action.request_sequence_id))

    def admit(self, action: ActionSnapshotV1, tick: int, now: int, observation: int):
        self._consume_admission(action, now, observation)
        if action.cancel_request_sequence_id is not None:
            members = {item.request_sequence_id for item in
                       (self.current, self.pending, self.default_parent) if item is not None}
            if action.cancel_request_sequence_id not in members:
                raise ContractViolation("client cancel target is not in its current arrangement")
            self.clear()
            return
        if action.first_effect_window is not None:
            self.pending = action
            self._missed = False
            self.event("window_pending", tick, action)
            return
        self._activate(action, tick, now)

    def reject_admission(
        self, action: ActionSnapshotV1, tick: int, now: int,
        observation: int,
    ) -> None:
        """Consume one explicitly rejected request without clearing fallback."""
        self._consume_admission(action, now, observation)
        self._missed = True

    def _consume_admission(
        self, action: ActionSnapshotV1, now: int, observation: int,
    ) -> None:
        if action.episode_id != self.episode or action.request_sequence_id <= self.last_sequence:
            raise ContractViolation("client rejected duplicate or foreign request")
        # Sequence/observation admission runs once, never again at activation.
        self.last_sequence = action.request_sequence_id
        self.admissions += 1
        if action.observation_sequence_id != observation or now >= action.deadline_monotonic_ns:
            raise ContractViolation("client rejected stale observation or expired request")

    def _activate(self, action, tick, now):
        if action.safe_default is not None:
            default = action.safe_default
            # A parent must cover its full suffix at the 20 Hz contract rate.
            start_wait = max(0, default.activation_tick - tick) * 50_000_000
            if (default.activation_tick != tick + action.valid_for_ticks
                    or now + start_wait + default.wall_budget_ns > action.deadline_monotonic_ns):
                raise ContractViolation("parent cannot cover the declared default")
        self.clear()
        self.current = action
        self.remaining = action.valid_for_ticks
        self._one_shot = True
        if action.safe_default is not None:
            self.default_parent = action
            self.default_remaining = action.safe_default.valid_for_ticks
            self.event("fallback_armed", tick, action)

    @property
    def response_ready(self):
        if self.pending is not None:
            return False
        return not self._missed or self.default_parent is None

    def sample(self, tick: int, now: int) -> TimingSelection:
        neutral = TimingSelection(None, MovementV1(), LookV1(), "disallowed")
        if not self.permitted:
            self.clear()
            return neutral
        if self.pending is not None:
            action = self.pending
            self.activation_checks += 1
            window = action.first_effect_window
            if action.episode_id != self.episode or now >= action.deadline_monotonic_ns or tick > window.latest_tick:
                self.pending = None
                self._missed = True
                self.event("window_missed", tick, action)
            elif tick >= window.earliest_tick:
                self._activate(action, tick, now)
                self.event("window_applied", tick, action)
        parent = self.default_parent
        if (parent is not None and tick >= parent.safe_default.activation_tick
                and self.remaining == 0):
            if self.default_wall_deadline is None:
                self.default_wall_deadline = min(parent.deadline_monotonic_ns,
                                                  now + parent.safe_default.wall_budget_ns)
                self.current = None
            # Armed safety input is measured in real samples. Wall expiry
            # bounds response waiting, never truncates a proved suffix.
            if self.default_remaining > 0:
                self.default_remaining -= 1
                self.event("default_active", tick, parent)
                return TimingSelection(parent, parent.safe_default.movement, LookV1(), "default_active")
            self.default_parent = None
            self.event("default_exhausted", tick, parent)
            return TimingSelection(parent, MovementV1(), LookV1(), "lease_exhausted")
        action = self.current
        if action is None:
            return neutral
        if now >= action.deadline_monotonic_ns:
            return TimingSelection(action, MovementV1(), LookV1(), "expired")
        if self.remaining == 0:
            return TimingSelection(action, MovementV1(), LookV1(), "lease_exhausted")
        self.remaining -= 1
        look = action.look if self._one_shot else LookV1()
        self._one_shot = False
        state = "neutral" if action.movement == MovementV1() else "leased"
        return TimingSelection(action, action.movement, look, state)
