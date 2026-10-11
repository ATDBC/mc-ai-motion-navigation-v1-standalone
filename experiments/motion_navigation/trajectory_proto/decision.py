"""One complete D098 decision with one request-owned physics budget."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import time

from .commitment import ScanStatus
from .contracts import SearchReason, TrajectorySearchRequest
from .incremental_proof import PermitStatus, prove_incrementally, validate_permit
from .physics import CountedPhysics, CountLimit
from .reference_search import _boundary_evidence
from .rollout_trace import RolloutFailure, build_controller_trace
from .template_controller import ControllerTree, TemplateGeometry, evaluate
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import TickInput


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """The sole owner of physics counts for one complete decision."""
    request: TrajectorySearchRequest
    counter: CountedPhysics

    @classmethod
    def create(cls, request: TrajectorySearchRequest) -> "DecisionContext":
        if type(request) is not TrajectorySearchRequest:
            raise ContractViolation("decision context requires a typed request")
        return cls(request, CountedPhysics(request.budget))


@dataclass(frozen=True, slots=True)
class ResolvedInputs:
    inputs: tuple[TickInput, ...]
    source: str
    lookup_ns: int
    controller_ns: int


class InputAdapter(Protocol):
    def resolve(self, context: DecisionContext, request: TrajectorySearchRequest,
                world: PhysicsWorldView) -> ResolvedInputs: ...


@dataclass(frozen=True, slots=True)
class FrozenInputAdapter:
    inputs: tuple[TickInput, ...]

    def resolve(self, context, request, world) -> ResolvedInputs:
        # Frozen A/C inputs intentionally perform no lookup and no controller
        # rollout.  Their steps begin at trace construction.
        return ResolvedInputs(self.inputs, "frozen_input", 0, 0)


@dataclass(frozen=True, slots=True)
class TableControllerAdapter:
    table: object
    template_id: str
    bin_id: str
    expected_inputs: tuple[TickInput, ...] | None = None

    def resolve(self, context, request, world) -> ResolvedInputs:
        started = time.perf_counter_ns()
        params = self.table.params(self.template_id, self.bin_id)
        looked_up = time.perf_counter_ns()
        tree = ControllerTree(request, world, TemplateGeometry.for_template(self.template_id),
                              counter=context.counter)
        generated = None
        reason = None
        for parameter in params:
            node, reason = evaluate(tree, parameter)
            if node is not None:
                generated = node.inputs
                break
        controlled = time.perf_counter_ns()
        if generated is None:
            raise RuntimeError(f"table controller missed {self.template_id}/{self.bin_id}: {reason}")
        if self.expected_inputs is not None and generated != self.expected_inputs:
            raise RuntimeError(f"controller input mismatch for {self.template_id}/{self.bin_id}")
        return ResolvedInputs(generated, "table_controller", looked_up - started,
                              controlled - looked_up)


@dataclass(frozen=True, slots=True)
class CompleteDecisionResult:
    status: ScanStatus
    reason: object
    inputs: tuple[TickInput, ...]
    input_source: str
    result: object | None
    trace: object | None
    physics_steps: int
    stage_physics_steps: dict[str, int]
    parts_ns: dict[str, int]
    counter_identity: int
    decision_timings: tuple


@dataclass(frozen=True, slots=True)
class CompleteDecisionTiming:
    kind: str
    boundary: int
    wall_ns: int
    physics_steps: int


def complete_decision(context: DecisionContext, request: TrajectorySearchRequest,
                      world: PhysicsWorldView, adapter: InputAdapter, *,
                      proof_mode="strict", runahead_steps=60) -> CompleteDecisionResult:
    """Run lookup/controller, trace, proof and permit gates on one counter."""
    if type(context) is not DecisionContext or context.request != request:
        raise ContractViolation("complete decision requires its original request context")
    counter = context.counter
    if counter.budget != request.budget or counter.physics_steps != 0:
        raise ContractViolation("complete decision requires one fresh request-owned counter")
    parts = {name: 0 for name in ("lookup", "controller", "trace", "proof", "permit")}
    steps = {name: 0 for name in ("controller", "trace", "proof")}
    resolved = ResolvedInputs((), "unresolved", 0, 0)
    trace = proof = None
    active_stage = "controller"
    stage_before = counter.physics_steps
    try:
        resolved = adapter.resolve(context, request, world)
        parts["lookup"], parts["controller"] = resolved.lookup_ns, resolved.controller_ns
        steps["controller"] = counter.physics_steps - stage_before

        active_stage = "trace"
        stage_before = counter.physics_steps
        started = time.perf_counter_ns()
        trace = build_controller_trace(request, resolved.inputs, world, counter=counter)
        parts["trace"] = time.perf_counter_ns() - started
        steps["trace"] = counter.physics_steps - stage_before

        active_stage = "proof"
        stage_before = counter.physics_steps
        started = time.perf_counter_ns()
        proof = prove_incrementally(request, resolved.inputs, world,
                                    _boundary_evidence(request, resolved.inputs), counter=counter,
                                    rollout_trace=trace, mode=proof_mode,
                                    runahead_steps=runahead_steps)
        parts["proof"] = time.perf_counter_ns() - started
        steps["proof"] = counter.physics_steps - stage_before
        if proof.status is not ScanStatus.VERIFIED_CANDIDATE:
            return CompleteDecisionResult(proof.status, proof.reason, resolved.inputs,
                                          resolved.source, proof, trace, counter.physics_steps,
                                          steps, parts, id(counter), ())

        permit_ns = {}
        checks = []
        for decision in proof.decisions:
            if decision.permit is None:
                continue
            started = time.perf_counter_ns()
            checks.append(validate_permit(decision.permit, request, world))
            permit_ns[id(decision)] = time.perf_counter_ns() - started
        parts["permit"] = sum(permit_ns.values())
        actionable = [decision for decision in proof.decisions if decision.kind != "plan"]
        timings = []
        proof_accounted = sum(decision.wall_ns for decision in proof.decisions)
        initial_ns = (parts["lookup"] + parts["controller"] + parts["trace"]
                      + max(0, parts["proof"] - proof_accounted))
        initial_steps = steps["controller"] + steps["trace"]
        plan = next((decision for decision in proof.decisions if decision.kind == "plan"), None)
        if plan is not None:
            initial_ns += plan.wall_ns
            initial_steps += plan.physics_steps
        for index, decision in enumerate(actionable):
            timings.append(CompleteDecisionTiming(
                decision.kind,
                decision.boundary,
                decision.wall_ns + permit_ns.get(id(decision), 0)
                + (initial_ns if index == 0 else 0),
                decision.physics_steps + (initial_steps if index == 0 else 0),
            ))
        if any(check.status is not PermitStatus.VALID for check in checks):
            return CompleteDecisionResult(ScanStatus.STALE, SearchReason.REQUEST_IDENTITY,
                                          resolved.inputs, resolved.source, proof, trace,
                                          counter.physics_steps, steps, parts, id(counter),
                                          tuple(timings))
        return CompleteDecisionResult(proof.status, None, resolved.inputs, resolved.source,
                                      proof, trace, counter.physics_steps, steps, parts, id(counter),
                                      tuple(timings))
    except CountLimit as limited:
        steps[active_stage] += counter.physics_steps - stage_before
        return CompleteDecisionResult(ScanStatus.NO_TRAJECTORY_IN_BUDGET, limited.reason,
                                      resolved.inputs, resolved.source, proof, trace,
                                      counter.physics_steps, steps, parts, id(counter), ())
    except RolloutFailure as failure:
        steps[active_stage] += counter.physics_steps - stage_before
        return CompleteDecisionResult(failure.status, failure.reason, resolved.inputs,
                                      resolved.source, proof, trace, counter.physics_steps,
                                      steps, parts, id(counter), ())
