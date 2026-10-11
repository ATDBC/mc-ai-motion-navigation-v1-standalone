"""Frozen D098 P3 matrix and Windows measurement for the isolated loop."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
import math
import platform
import sys

from mc2p.motion_nav.world_model import CellFact, CellKnowledge

from . import m0_probe
from .closed_loop import (
    ActionPhase,
    ApplicationStatus,
    FrozenInputPlan,
    LoopStatus,
    RollingProofOwner,
    SubmissionGateStatus,
    advance_backend,
    mutate_world_fact,
)
from .incremental_proof import prove_incrementally
from .measurement import _set_a, _percentile
from .reference_search import _boundary_evidence
from .rollout_trace import damage_points
from .physics import trajectory_digest


@dataclass(frozen=True, slots=True)
class MatrixSpec:
    scenario_id: str
    case_id: str
    timing: str
    injection: str
    phase: ActionPhase | None


_DEVIATIONS = (
    "position_0_01", "position_0_03", "yaw_2deg", "yaw_5deg",
    "unrelated_world", "dependency_changed", "dependency_unknown",
    "goal_revision", "cancel", "late_application", "missing_application",
)


def matrix_specs() -> tuple[MatrixSpec, ...]:
    rows = [
        MatrixSpec("exact-on-time", "jump_gap_1:A3", "on_time", "exact", None),
        MatrixSpec("exact-late-one", "jump_gap_1:A3", "late_one", "exact", None),
        MatrixSpec("continuity-turn-jump", "jump_up_after_turn:A15", "on_time", "exact", None),
        MatrixSpec("continuity-gap-walk", "jump_gap_continue:A3", "on_time", "exact", None),
    ]
    for injection in _DEVIATIONS:
        for phase in ActionPhase:
            rows.append(MatrixSpec(f"{injection}-{phase.value}", "jump_gap_1:A3",
                                   "on_time", injection, phase))
    return tuple(rows)


@lru_cache(maxsize=1)
def _cases():
    cases = {case[1]: case for case in _set_a(4)}
    for scenario, tier in (("jump_gap_continue", "A3"),
                           ("jump_up_after_turn_continue", "A15")):
        fixture = m0_probe._fixture_for(scenario, tier)
        result = m0_probe.run_case(scenario, tier, options=m0_probe.FORMAL_OPTIONS)
        inputs = result.inputs
        if scenario == "jump_gap_continue":
            # Make the handoff observable: the frozen controller candidate ends
            # on the landing tick, so append one proved ground-walk command.
            inputs = inputs + (replace(inputs[-1], forward=1.),)
        cases[f"{scenario}:{tier}"] = (
            "P3", f"{scenario}:{tier}", fixture.request, fixture.world,
            inputs, None, None,
        )
    return cases


def _known_fact(owner, world):
    for position in sorted(owner.bundle.proof.dependencies):
        fact = world.cell(position)
        if fact.knowledge is not CellKnowledge.UNKNOWN:
            return position, fact
    permit = owner.current_permit
    return permit.dependency_facts[0]


def _phase_boundary(phase: ActionPhase | None) -> int | None:
    # The frozen gap proof first exposes its commitment permit while deciding
    # boundary 3; its first consumed post-commit boundary is 4.
    return {
        None: None,
        ActionPhase.PRE_COMMIT: 0,
        ActionPhase.COMMIT_BOUNDARY: 3,
        ActionPhase.POST_COMMIT: 4,
    }[phase]


def _inject(spec, owner, state, world, request):
    cancel = False
    if spec.injection == "position_0_01":
        state = replace(state, position=(state.position[0] + .01, *state.position[1:]))
    elif spec.injection == "position_0_03":
        state = replace(state, position=(state.position[0] + .03, *state.position[1:]))
    elif spec.injection == "yaw_2deg":
        state = replace(state, yaw_radians=state.yaw_radians + math.radians(2.))
    elif spec.injection == "yaw_5deg":
        state = replace(state, yaw_radians=state.yaw_radians + math.radians(5.))
    elif spec.injection == "unrelated_world":
        _, source = _known_fact(owner, world)
        world = mutate_world_fact(
            world, (50, 50, 50), fact=CellFact(CellKnowledge.AIR, source.stamp, None))
    elif spec.injection in ("dependency_changed", "dependency_unknown"):
        position, source = _known_fact(owner, world)
        if spec.injection == "dependency_unknown":
            world = mutate_world_fact(world, position, unknown=True)
        else:
            replacement = (CellFact(CellKnowledge.AIR, source.stamp, None)
                           if source.knowledge is CellKnowledge.BLOCK
                           else CellFact(CellKnowledge.BLOCK, source.stamp,
                                         next(fact.block for _, fact in owner.current_permit.dependency_facts
                                              if fact.knowledge is CellKnowledge.BLOCK)))
            world = mutate_world_fact(world, position, fact=replacement)
    elif spec.injection == "goal_revision":
        region = request.goal.region
        # This is deliberately outside the old suffix's terminal envelope.  A
        # revision number alone, or a tiny still-satisfied edit, would not test
        # whether the owner validates the candidate against the new goal.
        shifted = replace(region, min_x=region.min_x + 1., max_x=region.max_x + 1.)
        request = replace(
            request, goal_revision=request.goal_revision + 1,
            goal=replace(request.goal, region=shifted),
        )
    elif spec.injection == "cancel":
        cancel = True
    return state, world, request, cancel


def run_scenario(spec: MatrixSpec) -> dict:
    case = _cases()[spec.case_id]
    _, _, request, world, inputs, _, _ = case
    owner = RollingProofOwner.start(request, world, FrozenInputPlan(inputs), runahead_steps=24)
    state = request.anchor_state
    current_world = world
    current_request = request
    injected = False
    target = _phase_boundary(spec.phase)
    latency = []
    initial_decision_ns = owner.bundle.total_ns
    physics_steps = owner.bundle.physics_steps
    invalidations = reproofs = 0
    max_reproofs = 0
    delayed = None
    missing_sequence = None
    decisions = []
    states = [state]
    applied_inputs = []
    injection_actual_phase = None
    goal_state_changed = False

    for _ in range(100):
        if (missing_sequence is not None and owner.pending is not None
                and state.movement_tick_id >= owner.pending.allowed_effect_ticks[-1]):
            # Application receipts for a movement tick are ingested after that
            # tick's observation.  Close the tick before asking the owner again;
            # do not fabricate another client input to make it "late".
            owner.ledger.close_application_tick(state.movement_tick_id)
        observation_boundary = owner.consumed
        if owner.pending is not None:
            application = owner.ledger.status(
                owner.pending.control_sequence, state.movement_tick_id)
            if application.status is ApplicationStatus.APPLIED:
                observation_boundary += 1
        should_inject = (not injected and spec.injection != "exact"
                         and observation_boundary == target
                         and spec.injection not in ("late_application", "missing_application"))
        cancel = False
        if should_inject and spec.injection not in ("late_application", "missing_application"):
            state, current_world, current_request, cancel = _inject(
                spec, owner, state, current_world, current_request)
            injected = True
            goal_state_changed = current_request.goal != request.goal

        decision = owner.decide(state, current_world, current_request, cancel=cancel)
        if should_inject:
            injection_actual_phase = decision.phase.value
        decisions.append(decision.status.value)
        latency.append((decision.complete_decision_ns + initial_decision_ns) / 1_000_000.)
        initial_decision_ns = 0
        physics_steps += decision.physics_steps
        invalidations += int(decision.old_suffix_invalidated)
        reproofs += int(decision.reproved)
        max_reproofs = max(max_reproofs, decision.reproof_count)

        if (injected and injection_actual_phase is None
                and spec.injection in ("late_application", "missing_application")):
            injection_actual_phase = decision.phase.value
        if decision.status in (LoopStatus.COMPLETED, LoopStatus.CANCELLED,
                               LoopStatus.BOUNDED_FAILURE,
                               LoopStatus.BODY_RESPONSIBILITY_UNRESOLVED):
            break
        if decision.status is LoopStatus.WAITING_APPLICATION:
            if delayed is not None:
                authority = owner.authorize_application(
                    delayed, state.movement_tick_id + 1)
                state = advance_backend(state, authority, current_world)
                states.append(state)
                applied_inputs.append(authority.command)
                delayed = None
            else:
                authority = owner.authorize_pending_hold(state.movement_tick_id + 1)
                state = advance_backend(state, authority, current_world)
                states.append(state)
            continue
        if decision.submission is None:
            raise RuntimeError(f"active scenario produced no command: {spec.scenario_id}")

        inject_application = (not injected and spec.injection in (
            "late_application", "missing_application") and owner.consumed == target)
        late = ((spec.timing == "late_one" and decision.submission.control_sequence
                 == request.first_candidate_control_sequence)
                or (inject_application and spec.injection == "late_application"))
        missing = inject_application and spec.injection == "missing_application"
        if inject_application:
            injected = True
        if late:
            delayed = decision.submission
            authority = owner.authorize_pending_hold(state.movement_tick_id + 1)
            state = advance_backend(state, authority, current_world)
            states.append(state)
        elif missing:
            missing_sequence = decision.submission.control_sequence
            authority = owner.authorize_pending_hold(state.movement_tick_id + 1)
            state = advance_backend(state, authority, current_world)
            states.append(state)
        else:
            authority = owner.authorize_application(
                decision.submission, state.movement_tick_id + 1)
            state = advance_backend(state, authority, current_world)
            states.append(state)
            applied_inputs.append(authority.command)
    else:
        return {"scenario": spec.scenario_id, "status": "unbounded", "unbounded": True}

    terminal = owner.terminal.status.value if owner.terminal is not None else decision.status.value
    actual_damage = damage_points(tuple(states))
    safety = 0
    if actual_damage > current_request.task_damage_budget.maximum_expected_damage_points:
        safety += 1
    if (spec.phase in (ActionPhase.COMMIT_BOUNDARY, ActionPhase.POST_COMMIT)
            and terminal == LoopStatus.BOUNDED_FAILURE.value and not owner.owner_retained):
        safety += 1
    landing_boundary = None
    landing_speed = 0.
    post_landing_command_exists = False
    for index in range(1, len(states)):
        if not states[index - 1].on_ground and states[index].on_ground:
            landing_boundary = index
            vx, _, vz = states[index].velocity_blocks_per_tick
            landing_speed = math.hypot(vx, vz) * 20.
            post_landing_command_exists = index < len(applied_inputs)
            break
    authority_complete = all(
        authority.owner_id == owner.owner_id
        and authority.ledger_id
        and authority.proof_request_id
        and authority.effect_tick > 0
        for authority in owner.advance_authorities
    ) and len(owner.advance_authorities) == len(states) - 1
    return {
        "scenario": spec.scenario_id,
        "case": spec.case_id,
        "timing": spec.timing,
        "injection": spec.injection,
        "phase": None if spec.phase is None else spec.phase.value,
        "injection_actual_phase": injection_actual_phase,
        "injected": spec.injection == "exact" or injected,
        "status": terminal,
        "reason": decision.reason,
        "decision_ms": latency,
        "physics_steps": physics_steps,
        "invalidations": invalidations,
        "reproofs": reproofs,
        "max_consecutive_reproofs": max_reproofs,
        "old_suffix_invalidated": owner.old_suffix_invalidated,
        "owner_retained": owner.owner_retained,
        "recovery_proof_obtained": owner.recovery_proof_obtained,
        "safety_violations": safety + len(decision.safety_violations),
        "damage_points": actual_damage,
        "applied_input_digest": trajectory_digest(tuple(applied_inputs)),
        "expected_input_digest": trajectory_digest(inputs),
        "exact_semantics_match": (spec.injection == "exact" and tuple(applied_inputs) == inputs),
        "unbounded": False,
        "missing_sequence": missing_sequence,
        "decisions": decisions,
        "goal_state_changed": goal_state_changed,
        "authorized_backend_advances": len(owner.advance_authorities),
        "backend_advances": len(states) - 1,
        "all_backend_advances_authorized": authority_complete,
        "advance_authorities": [
            {
                "owner_id": item.owner_id,
                "ledger_id": item.ledger_id,
                "control_sequence": item.control_sequence,
                "effect_tick": item.effect_tick,
                "source": item.source.value,
                "permit_boundary": item.permit_boundary,
                "proof_request_id": item.proof_request_id,
            }
            for item in owner.advance_authorities
        ],
        "continuity": {
            "landing_boundary": landing_boundary,
            "landing_speed_blocks_per_second": landing_speed,
            "owner_handoffs": len({item.owner_id for item in owner.advance_authorities}) - 1,
            "post_landing_command_exists": post_landing_command_exists,
        },
    }


def mutation_checks() -> dict[str, dict]:
    gap = m0_probe._fixture_for("jump_gap_1", "A3")
    inputs = m0_probe.run_case("jump_gap_1", "A3", options=m0_probe.FORMAL_OPTIONS).inputs
    rows = _boundary_evidence(gap.request, inputs)
    base = prove_incrementally(gap.request, inputs, gap.world, rows, mode="strict_runahead",
                               runahead_steps=24)
    one = prove_incrementally(gap.request, inputs, gap.world, rows, faults={"one_branch"},
                              mode="strict_runahead", runahead_steps=24)
    landing = prove_incrementally(gap.request, inputs, gap.world, rows,
                                  faults={"no_landing_proof"}, mode="strict_runahead",
                                  runahead_steps=24)
    commit = next(permit for permit in base.permits if permit.locked_count > 0)
    wrong = gap.request.stop_input if commit.commands[0] != gap.request.stop_input else inputs[0]
    dependency, _ = commit.dependency_facts[0]
    unknown_world = mutate_world_fact(gap.world, dependency, unknown=True)

    def run_gate(name, proof, permit, command, world) -> dict:
        owner = RollingProofOwner.start(
            gap.request, gap.world, FrozenInputPlan(inputs), runahead_steps=24)
        before = owner.ledger.submission_count
        check = owner.submit_through_gate(
            proof=proof,
            permit=permit,
            command=command,
            boundary=permit.boundary,
            request_tick=gap.request.anchor_state.movement_tick_id,
            allowed_effect_ticks=gap.request.allowed_effect_ticks,
            request=gap.request,
            world=world,
        )
        after = owner.ledger.submission_count
        return {
            "gate_passed": check.status is SubmissionGateStatus.ALLOWED,
            "submission_blocked": check.submission is None and after == before,
            "invariant_error": None if check.reason is None else check.reason.value,
            "gate_source": "rolling_owner_submission_gate",
            "ledger_submission_count": after - before,
            "mutant": name,
        }

    return {
        "unknown_as_air": run_gate(
            "unknown_as_free", base, commit, commit.commands[0], unknown_world),
        "one_timing_branch": run_gate(
            "one_branch", one, one.permits[0], one.permits[0].commands[0], gap.world),
        "no_landing_proof": run_gate(
            "no_landing_proof", landing,
            next(permit for permit in landing.permits if permit.locked_count > 0),
            next(permit for permit in landing.permits if permit.locked_count > 0).commands[0],
            gap.world),
        "post_commit_command_swap": run_gate(
            "unsafe_command", base, commit, wrong, gap.world),
    }


def _summary(rows, mutations):
    samples = [value for row in rows for value in row.get("decision_ms", ())]
    latency = {
        "samples": len(samples),
        "p50_ms": _percentile(samples, .50),
        "p95_ms": _percentile(samples, .95),
        "p99_ms": _percentile(samples, .99),
        "max_ms": max(samples),
    }
    safety = sum(row.get("safety_violations", 0) for row in rows)
    unbounded = sum(bool(row.get("unbounded")) for row in rows)
    post = [row for row in rows
            if row.get("injection_actual_phase") == ActionPhase.POST_COMMIT.value
            and row.get("injection") not in ("unrelated_world", "late_application")]
    post_recovered = sum(bool(row.get("recovery_proof_obtained")) for row in post)
    exact = [row for row in rows if row.get("injection") == "exact"]
    gates = {
        "exact_completed": all(row.get("status") == LoopStatus.COMPLETED.value for row in exact),
        "exact_semantics_match": all(row.get("exact_semantics_match") for row in exact),
        "zero_safety_violations": safety == 0,
        "all_results_bounded": unbounded == 0,
        "all_mutations_detected": all(
            not row["gate_passed"] and row["submission_blocked"]
            for row in mutations.values()),
        "post_commit_recovery": post_recovered == len(post),
        "p95_le_8_ms": latency["p95_ms"] <= 8.,
        "p99_le_15_ms": latency["p99_ms"] <= 15.,
        "max_lt_30_ms": latency["max_ms"] < 30.,
    }
    return {
        "decision_samples": len(samples),
        "decision_latency_ms": latency,
        "physics_steps": sum(row.get("physics_steps", 0) for row in rows),
        "safety_violations": safety,
        "body_responsibility_unresolved": sum(
            row.get("status") == LoopStatus.BODY_RESPONSIBILITY_UNRESOLVED.value
            for row in rows),
        "unbounded_results": unbounded,
        "invalidations": sum(row.get("invalidations", 0) for row in rows),
        "invalidation_rate": (sum(row.get("invalidations", 0) > 0 for row in rows)
                              / len(rows) if rows else 0.),
        "reproofs": sum(row.get("reproofs", 0) for row in rows),
        "max_consecutive_reproofs": max(row.get("max_consecutive_reproofs", 0) for row in rows),
        "post_commit_scenarios": len(post),
        "post_commit_recovered": post_recovered,
        "terminal_reasons": dict(sorted(Counter(
            row.get("reason") or row.get("status", "unknown") for row in rows).items())),
        "gates": gates,
        "passes_p3": all(gates.values()),
    }


def run_matrix(*, rounds=1, compact=False) -> dict:
    specs = matrix_specs()
    if compact:
        specs = tuple(row for row in specs if row.scenario_id in (
            "exact-on-time", "exact-late-one", "continuity-turn-jump",
            "position_0_01-pre_commit", "dependency_unknown-commit_boundary",
            "goal_revision-post_commit", "missing_application-pre_commit",
        ))
    rows = [run_scenario(spec) for _ in range(rounds) for spec in specs]
    mutations = mutation_checks()
    return {
        "schema": "d098-p3-closed-loop-v1",
        "rounds": rounds,
        "matrix_size": len(specs),
        "rows": rows,
        "mutation_checks": mutations,
        "summary": _summary(rows, mutations),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "formal_platform": "Windows",
        },
        "evidence_boundary": (
            "isolated calculator loop; experimental ledger; no Runtime, Session, Fabric or mc2p change"
        ),
    }
