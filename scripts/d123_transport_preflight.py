"""Repository-owned D123 transport lifecycle preflight."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mc2p.contracts.common import ContractViolation
from scripts.d122_formal_timing_probe import run_case


_POLICIES = ("timely_l1", "late_l2", "explicit_reject")
_STATES = (
    "timely",
    "late_window_rejected",
    "remote_rejected_before_sampling",
)


@dataclass(frozen=True, slots=True)
class FormalTransportChainResultV1:
    policy: str
    passed: bool
    transport_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransportPreflightResultV1:
    status: str
    formal_states: tuple[str, ...]
    chains: tuple[FormalTransportChainResultV1, ...]


def generate(
    *, runner: Callable[[str], dict[str, object]] = run_case,
) -> TransportPreflightResultV1:
    chains = []
    for policy in _POLICIES:
        row = runner(policy)
        if type(row) is not dict or row.get("passed") is not True:
            raise ContractViolation(
                f"formal transport chain did not pass: {policy}"
            )
        facts = row.get("transport_facts", ())
        if type(facts) not in (list, tuple):
            raise ContractViolation("formal transport facts must be a sequence")
        kinds = tuple(
            (fact.get("kind").value
             if hasattr(fact.get("kind"), "value") else fact.get("kind"))
            for fact in facts if type(fact) is dict
        )
        if len(kinds) != len(facts) or any(type(kind) is not str for kind in kinds):
            raise ContractViolation("formal transport fact is malformed")
        if (policy == "explicit_reject"
                and kinds != ("remote_rejected_before_sampling",)):
            raise ContractViolation(
                "explicit rejection lacks one formal not-applied fact"
            )
        chains.append(FormalTransportChainResultV1(policy, True, kinds))
    result = TransportPreflightResultV1("PASS", _STATES, tuple(chains))
    if not validate(result):
        raise ContractViolation("formal transport preflight result is invalid")
    return result


def validate(value: object) -> bool:
    if type(value) is not TransportPreflightResultV1:
        return False
    if (value.status != "PASS" or value.formal_states != _STATES
            or len(value.chains) != len(_POLICIES)):
        return False
    for expected, chain in zip(_POLICIES, value.chains):
        if (type(chain) is not FormalTransportChainResultV1
                or chain.policy != expected or chain.passed is not True):
            return False
    return value.chains[-1].transport_kinds == (
        "remote_rejected_before_sampling",
    )
