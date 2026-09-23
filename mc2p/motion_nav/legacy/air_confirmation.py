"""Replaced positive-air coordinator retained for B02 compatibility tests.

The live path moved request budgeting, retry suppression and result ingestion
to ``NavigationObservationAdapter``. New runtime code must not use this
implementation directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.motion_nav.world_model import (
    BlockPos, CellKnowledge, ObservationStamp, WorldKnowledge, WorldSessionId,
)


@dataclass(frozen=True, slots=True)
class AirConfirmationBatch:
    stamp: ObservationStamp
    confirmed_air: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        if type(self.stamp) is not ObservationStamp:
            raise ContractViolation("air result requires an observation stamp")
        if (type(self.confirmed_air) is not tuple
                or self.confirmed_air != tuple(sorted(set(self.confirmed_air)))):
            raise ContractViolation("confirmed air must be sorted and unique")


class AirProbe(Protocol):
    def confirm_air(self, session: WorldSessionId, positions: tuple[BlockPos, ...],
                    request_stamp: ObservationStamp) -> AirConfirmationBatch: ...


@dataclass(frozen=True, slots=True)
class AirResolution:
    requested: tuple[BlockPos, ...]
    confirmed_air: tuple[BlockPos, ...]
    cached_air: tuple[BlockPos, ...]
    unresolved: tuple[BlockPos, ...]


class AirConfirmationService:
    """Coalesces one geometry request into one probe call and caches only positives."""

    def __init__(self, world: WorldKnowledge, probe: AirProbe, *, retry_after_ticks: int = 5) -> None:
        if type(world) is not WorldKnowledge or not hasattr(probe, "confirm_air"):
            raise ContractViolation("air service requires world knowledge and a probe")
        require_nonnegative_int(retry_after_ticks, "air retry interval")
        if retry_after_ticks == 0:
            raise ContractViolation("air retry interval must be positive")
        self._world = world
        self._probe = probe
        self._retry_after_ticks = retry_after_ticks
        self._last_attempt_tick: dict[BlockPos, int] = {}

    def ensure_air(self, positions: tuple[BlockPos, ...], request_stamp: ObservationStamp) -> AirResolution:
        if type(positions) is not tuple:
            raise ContractViolation("air request must be an immutable position tuple")
        if type(request_stamp) is not ObservationStamp or request_stamp.session != self._world.session:
            raise ContractViolation("air request belongs to another world session")
        ordered = tuple(sorted(set(positions)))
        view = self._world.view()
        cached = tuple(position for position in ordered
                       if view.cell(position).knowledge is CellKnowledge.AIR)
        candidates = []
        for position in ordered:
            state = view.cell(position).knowledge
            last = self._last_attempt_tick.get(position)
            if (state is CellKnowledge.UNKNOWN
                    and (last is None or request_stamp.world_tick - last >= self._retry_after_ticks)):
                candidates.append(position)
        requested = tuple(candidates)
        confirmed: tuple[BlockPos, ...] = ()
        if requested:
            batch = self._probe.confirm_air(self._world.session, requested, request_stamp)
            if type(batch) is not AirConfirmationBatch or batch.stamp.session != self._world.session:
                raise ContractViolation("air probe returned another world session")
            if batch.stamp.world_order < request_stamp.world_order:
                raise ContractViolation("air probe returned an older sample")
            if not set(batch.confirmed_air).issubset(requested):
                raise ContractViolation("air probe confirmed an unrequested position")
            for position in requested:
                self._last_attempt_tick[position] = request_stamp.world_tick
            self._world.confirm_air(batch.stamp, batch.confirmed_air)
            confirmed = batch.confirmed_air
        final = self._world.view()
        unresolved = tuple(position for position in ordered
                           if final.cell(position).knowledge is CellKnowledge.UNKNOWN)
        return AirResolution(requested, confirmed, cached, unresolved)

    def invalidate_attempts(self, positions: tuple[BlockPos, ...]) -> None:
        if type(positions) is not tuple:
            raise ContractViolation("air invalidation must be an immutable position tuple")
        for position in positions:
            self._last_attempt_tick.pop(position, None)
