"""Test-only D122 transition oracle. It is not a client implementation.

This finite model freezes request admission versus physical activation. It has
no physics, transport or Minecraft hooks and cannot prove production safety.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    sequence: int
    episode: str = "ep"
    observation: int = 0
    window: tuple[int, int] | None = None
    wall_deadline: int = 10_000
    movement: str = "jump"
    valid_for_ticks: int = 1


class TimingOracle:
    def __init__(self, *, landing_tick: int, duration: int, default_wall_budget: int = 10_000):
        if landing_tick < 1 or not 1 <= duration <= 20:
            raise ValueError("invalid frozen timing")
        self.tick = landing_tick - 1
        self.activation_tick = landing_tick + 1
        self.duration = duration
        self.episode = "ep"
        self.observation = 0
        self.last_sequence = 25
        self.pending = None
        self.current = "parent"
        self.current_remaining = 1
        self.default = True
        self.default_started = None
        self.default_wall_budget = default_wall_budget
        self.default_wall_started = None
        self.permitted = True
        self.admissions = 0
        self.activation_checks = 0
        self.response_ready = True
        self.missed = False
        self.events = []

    def admit(self, request: Request, *, now: int = 0):
        if request.episode != self.episode or request.sequence <= self.last_sequence:
            return "rejected_identity"
        # Like the existing gate, consume the sequence even on later rejection.
        self.last_sequence = request.sequence
        self.admissions += 1
        if request.observation != self.observation or now >= request.wall_deadline:
            return "rejected_basis"
        if request.window is None:
            self.clear()
            self.current = request.movement
            self.current_remaining = request.valid_for_ticks
            self.response_ready = True
            return "ordinary_applied"
        self.pending = request
        self.response_ready = False
        return "window_pending"

    def clear(self):
        self.pending = None
        self.current = None
        self.current_remaining = 0
        self.default = False
        self.default_started = None
        self.default_wall_started = None
        self.response_ready = True

    def cancel(self):
        self.clear()

    def reset(self, episode="ep"):
        self.clear()
        self.episode = episode
        self.last_sequence = -1

    def sample(self, *, now: int = 0):
        self.tick += 1
        if not self.permitted:
            self.clear()
            return "neutral", "disallowed"
        if self.pending is not None:
            request = self.pending
            self.activation_checks += 1
            # No observation or sequence revalidation: admission already did it.
            if request.episode != self.episode or now >= request.wall_deadline:
                self.pending = None
                self.missed = True
            elif self.tick > request.window[1]:
                self.pending = None
                self.missed = True
                self.events.append((self.tick, "window_missed", request.sequence))
            elif self.tick >= request.window[0]:
                self.clear()
                self.current = request.movement
                self.current_remaining = request.valid_for_ticks - 1
                self.events.append((self.tick, "window_applied", request.sequence))
                return request.movement, "window_applied"
        if self.tick < self.activation_tick:
            if self.default:
                self.events.append((self.tick, "fallback_armed", 25))
            if self.current_remaining > 0:
                self.current_remaining -= 1
                return self.current, "leased"
            return "neutral", "neutral"
        if self.default:
            if self.default_started is None:
                self.default_started = self.tick
                self.default_wall_started = now
                self.current = None  # Expired parent inputs can never revive.
                self.current_remaining = 0
            if self.tick - self.default_started < self.duration:
                self.events.append((self.tick, "default_active", 25))
                return "sneak", "default_active"
            self.default = False
            self.response_ready = True
            self.events.append((self.tick, "default_exhausted", 25))
            return "neutral", "default_exhausted"
        if self.current_remaining > 0:
            self.current_remaining -= 1
            return self.current, "leased"
        self.current = None
        return "neutral", "neutral"


def transition_cases(landing_tick: int, duration: int):
    """Executable acceptance vectors for later Python and Java clients."""
    results = []
    for event in ("early", "on_time", "late", "never", "cancel", "reset",
                  "episode", "higher_sequence", "ordinary", "permission",
                  "wall_expired", "default_exhaustion"):
        model = TimingOracle(landing_tick=landing_tick, duration=duration)
        model.sample()  # L, parent is applied, armed response is delivered.
        target = model.activation_tick
        request = Request(26, window=(target, target))
        if event == "early":
            request = Request(26, window=(target + 2, target + 2))
        if event == "late":
            model.sample()  # L+1 already uses the prearmed default.
        if event not in ("never", "default_exhaustion"):
            assert model.admit(request) == "window_pending"
        if event == "cancel":
            model.cancel()
        elif event == "reset":
            model.reset()
        elif event == "episode":
            model.reset("next-episode")
        elif event == "higher_sequence":
            assert model.admit(Request(27, window=(target + 1, target + 1))) == "window_pending"
            assert model.admit(request) == "rejected_identity"
        elif event == "ordinary":
            assert model.admit(Request(27, movement="walk")) == "ordinary_applied"
        elif event == "permission":
            model.permitted = False
        elif event == "wall_expired":
            model.pending = Request(26, window=(target, target), wall_deadline=1)
        # Observations can progress while an already admitted request is pending.
        model.observation += 3
        rows = []
        for _ in range(duration + 4):
            movement, status = model.sample(now=2 if event == "wall_expired" else 0)
            rows.append({"tick": model.tick, "movement": movement, "status": status,
                         "response_ready": model.response_ready})
        results.append({"event": event, "L": landing_tick, "duration": duration,
                        "rows": rows, "events": model.events,
                        "admissions": model.admissions,
                        "activation_checks": model.activation_checks})
    return results
