"""Explicit online state for one point-navigation scope.

Only :meth:`observe` imports legal V3 evidence.  Derived beliefs retain the
original evidence stamps and never renew terrain merely because a policy read
the state.
"""
from __future__ import annotations

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.skills.navigation_belief import BeliefStore
from mc2p.skills.navigation_memory import MemoryLimits, MemorySnapshot, NavigationMemory
from mc2p.skills.normal_navigation_types import NormalNavigationConfig
from mc2p.skills.navigation_recovery import RecoveryLedger


class NavigationState:
    """Own all mutable online navigation evidence for one explicit scope."""

    def __init__(self, scope_id: str) -> None:
        require_identifier(scope_id, "navigation state scope")
        config = NormalNavigationConfig()
        limits = MemoryLimits(
            max_terrain=config.max_terrain,
            max_entities=config.max_entities,
            terrain_retention_ns=config.terrain_retention_ns,
            entity_retention_ns=config.entity_retention_ns,
            freshness_ns=config.freshness_ns,
            radius_blocks=config.memory_radius_blocks,
        )
        self._scope_id = scope_id
        self._memory = NavigationMemory(scope_id, limits)
        self._belief = BeliefStore()
        self._snapshot: MemorySnapshot | None = None
        self._last_observation: ObservationSnapshotV3 | None = None
        self._memory_generation = 1
        self._policy_caches: dict[str, dict[str, object]] = {}
        self._recovery_ledgers: dict[str, RecoveryLedger] = {}

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def memory(self) -> NavigationMemory:
        return self._memory

    @property
    def belief(self) -> BeliefStore:
        return self._belief

    @property
    def snapshot(self) -> MemorySnapshot:
        if self._snapshot is None:
            raise ContractViolation("navigation state has no observation")
        return self._snapshot

    @property
    def memory_generation(self) -> int:
        return self._memory_generation

    def policy_cache(self, group: str) -> dict[str, object]:
        NormalNavigationConfig(group)
        return self._policy_caches.setdefault(group, {})

    def recovery_ledger(self, group: str) -> RecoveryLedger:
        config = NormalNavigationConfig(group)
        return self._recovery_ledgers.setdefault(
            group, RecoveryLedger(total_budget_ns=config.obstacle_budget_ns)
        )

    def bind_policy(self, policy) -> None:
        if getattr(policy, "group", None) not in {"A", "B", "C", "D", "E", "goal_directed_exploration"} \
                or not callable(getattr(policy, "bind_state", None)):
            raise ContractViolation("navigation state requires a normal point policy")
        policy.bind_state(self)

    def observe(self, obs: ObservationSnapshotV3, now_ns: int) -> MemorySnapshot:
        if type(obs) is not ObservationSnapshotV3:
            raise ContractViolation("navigation state requires exact ObservationSnapshotV3")
        require_nonnegative_int(now_ns, "navigation state observation time")
        snapshot = self._memory.observe(
            obs,
            now_ns=now_ns,
            controller_clock_id=obs.controller_clock_id,
            scope_id=self._scope_id,
        )
        same_import = (
            obs is self._last_observation
            and self._snapshot is not None
            and snapshot.terrain_index.token is self._snapshot.terrain_index.token
        )
        try:
            if not same_import:
                self._belief.observe(snapshot)
        except BaseException:
            self._belief.clear()
            raise
        self._snapshot = snapshot
        self._last_observation = obs
        return snapshot
