"""Read-only v2 candidate inventory; review evidence is not a mutation result."""
from __future__ import annotations
import argparse
import ast
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
# Each target names the actual Python binding used by formal calls. Targets
# overlap across review clues; path collection wraps each binding only once.
SPECS = (
    ("navigation_session._admit_async_event", "dead_path", "dead", "navigation_session.NavigationSession._admit_async_event",
     "async work owners", "typed owner admissions replace this unused facade"),
    ("navigation_session._cell_fact_id", "dead_path", "dead", "navigation_session.NavigationSession._cell_fact_id",
     "information/planning fact creator", "blocking facts are constructed at their owned use sites"),
    ("motion_coordination._upcoming_air_index", "dead_path", "dead", "motion_coordination.MotionRouteCoordinator._upcoming_air_index",
     "MotionRouteCoordinator", "_upcoming_gap_index and observed successor solving replace this unused scan"),
    ("K1.admit-vs-surface", "legacy_adapter", "K1", "route_admission.RouteAdmitter.admit|route_admission.RouteAdmitter.admit_surface",
     "RouteAdmitter", "legacy WalkGraph candidate and surface candidate are distinct typed protocols"),
    ("K2.local-vs-ground-direct", "duplicate_check", "K2", (
        'route_admission.RouteAdmitter.admit_local_direct|route_admission.RouteAdmitter.adm'
        'it_ground_direct'
    ),
     "RouteAdmitter", "same-support reach and different-support proved corridor have distinct geometric entry contracts"),
    ("K3.direct-request-classification", "duplicate_check", "K3", (
        'navigation_session.NavigationSession._can_attempt_ground_direct_request|navigation'
        '_session.NavigationSession._can_attempt_local_direct_request'
    ),
     "NavigationSession/GoalRequestLedger", (
         'five common eligibility facts share request-time owner; SurfaceSearchNeed '
         'distinguishes the alternatives'
     )),
    ("K4.connection-vs-surface-edge", "duplicate_check", "K4",
     "route_admission.RouteAdmitter._connection|route_validation.query_surface_walk_edge",
     "RouteAdmitter / surface-edge geometry proof",
     (
         'both sweep then sample support; actual-body-to-first-node connection uses 0.10 '
         'height/0.80 distance and two samples, surface-edge replay uses equal-height '
         'tolerance, four samples and support fraction gate; owner and shared-helper review '
         'remain pending'
     )),
    ("validation.first-proof-vs-replay", "duplicate_check", "validation", (
        'route_admission.RouteAdmitter._surface_validation_plan|route_admission.ActiveRoute'
        'Tracker.validate|route_validation.replay_walk_validation_recipe'
    ),
     "RouteAdmitter + ActiveRouteTracker", (
         'first construction and changed-cell replay occur at different times; replay is '
         'required after world changes'
     )),
    ("K5.proof-record-construction", "duplicate_check", "K5", (
        'route_admission._DirectWalkProofContext.__post_init__|route_validation.WalkValidat'
        'ionRecipe.__post_init__'
    ),
     "each immutable proof record", "admission context and transported replay recipe protect distinct boundary records"),
    ("K6.surface-admission", "audit_only", "K6", "route_admission.RouteAdmitter.admit_surface",
     "RouteAdmitter", (
         'large function alone is no deletion evidence; typed metadata, entry and '
         'capability checks stay protected'
     )),
    ("K6.ground-admission", "audit_only", "K6", "route_admission.RouteAdmitter.admit_ground_direct",
     "RouteAdmitter", (
         'large function alone is no deletion evidence; current-body connection and proof '
         'construction are separate checks'
     )),
    ("F1.revised-direct-information", "legacy_adapter", "F1-session", (
        'navigation_session.NavigationSession._activate_ground_direct_route|navigation_sess'
        'ion.NavigationSession._activate_local_route'
    ),
     "NavigationSession/InformationAcquisitionState", "candidate information wait must preserve incumbent body ownership during revisions"),
    ("F1.revised-request-reissue", "legacy_adapter", "F1-session", (
        'navigation_session.NavigationSession._reissue_request_from_current|navigation_sess'
        'ion.NavigationSession._wait_for_active_terminal'
    ),
     "PlanningCoordinator + NavigationHandoffCoordinator", "current-anchor planning and old-body terminal wait are different responsibilities"),
    ("F1.body-revision-selection", "duplicate_check", "F1-supervisor", (
        'execution_supervisor.ExecutionSupervisor.offer_route|execution_supervisor.Executio'
        'nSupervisor.select_body|execution_supervisor.ExecutionSupervisor._advance_incumben'
        't_prefix'
    ),
     "ExecutionSupervisor", "pending candidate ownership and incumbent safe prefix protect different bodies in the same frame"),
    ("F1.route-world-revalidation", "duplicate_check", "F1-supervisor", (
        'execution_supervisor.ExecutionSupervisor._validate_routes|execution_supervisor.Exe'
        'cutionSupervisor.continue_rejected_candidate'
    ),
     "ExecutionSupervisor/ActiveRouteTracker", "incumbent and pending route may have different changed dependencies and outcomes"),
    ("navigation_session._wait_for_active_terminal", "legacy_adapter", "coverage", "navigation_session.NavigationSession._wait_for_active_terminal",
     "NavigationHandoffCoordinator", (
         'one production reissue caller retains terminal-stop responsibility even if frozen '
         'sets never enter it'
     )),
)


def target_nodes(target):
    module, *names = target.split(".")
    path = ROOT / f"mc2p/motion_nav/{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in names:
        tree = next(node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name)
    return path, tree


def static_callers(name, directories=("mc2p",)):
    rows = []
    for directory in directories:
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr == name:
                    rows.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return sorted(rows)


def f1_function_review():
    # The source-reviewed diff is frozen so exported checkouts do not need
    # the private project's old Git objects to generate the inventory.
    path = ROOT / "tests/sim/manifests/navigation-structure-f1-functions.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_inventory(matrix):
    candidates = []
    for identifier, category, clue, target_list, owner, evidence in SPECS:
        targets = target_list.split("|")
        counts = matrix["candidates"][identifier]
        zero = not any(counts.values())
        entering_targets = {target: {group: matrix.get("target_calls", {}).get(group, {}).get(target, 0)
                                    for group in counts} for target in targets}
        unprotected_targets = [target for target, values in entering_targets.items() if not any(values.values())]
        locations, lines, branches = [], 0, 0
        for target in targets:
            path, node = target_nodes(target)
            locations.append({"target": target, "file": path.relative_to(ROOT).as_posix(),
                              "line": node.lineno, "end_line": node.end_lineno})
            lines += node.end_lineno - node.lineno + 1
            branches += sum(isinstance(child, (ast.If, ast.Match, ast.For, ast.While, ast.Try)) for child in ast.walk(node))
        condition_sets = []
        for target in targets:
            _, node = target_nodes(target)
            condition_sets.append({ast.unparse(child.test) for child in ast.walk(node) if isinstance(child, ast.If)})
        common_conditions = sorted(set.intersection(*condition_sets)) if len(condition_sets) > 1 else []
        dead = category == "dead_path"
        callers = static_callers(targets[0].split(".")[-1]) if dead else None
        if dead and callers:
            raise ValueError(f"dead-path candidate has production callers: {identifier}")
        closure = ("pending:S1_deletion_mutation" if dead else
                   "audit_only:review_boundary" if category == "audit_only" else
                   "retained:unprotected" if zero else
                   "pending:S2_owner_and_mutation_review")
        checks = {
            "dead": ["full motion_nav forward/reverse", "all five S0-R behavior indexes"],
            "K1": ["tests.motion_nav.test_known_map_planning", "tests.motion_nav.test_b07_step_route"],
            "K2": ["tests.motion_nav.test_d062_direct_walk_validation", "tests.motion_nav.test_d064_ground_direct_handoff", "F1 follow/world-change indexes"],
            "K3": ["test_f1_known_world_following", "follow-10", "world_changes-19"],
            "K4": ["tests.motion_nav.test_d058_runtime_validation", "world_changes-19"],
            "K5": ["tests.motion_nav.test_d058_validation_plan", "tests.motion_nav.test_d062_direct_walk_validation"],
            "K6": ["tests.motion_nav.test_b07_step_route", "tests.motion_nav.test_d062_direct_walk_validation", "product-v7-2000"],
        }.get(clue, ["test_r28_reanchor_handoff", "test_f1_known_world_following", "coordination-1448"])
        candidates.append({"id": identifier, "category": category, "clue": clue,
            "targets": targets, "locations": locations,
            "duplicate_of": targets[1:] if category == "duplicate_check" and len(targets) > 1 else None,
            "legacy_of": evidence if category in ("dead_path", "legacy_adapter") else None,
            "kept_owner": owner, "entering_sets": counts, "owner_review": evidence,
            "entering_targets": entering_targets,
            "unprotected_targets": unprotected_targets,
            "target_coverage_disposition": {target: "retained:unprotected" for target in unprotected_targets},
            "coverage_rule": (
                'a paired live target never protects a zero-call endpoint; no change to '
                'unprotected endpoints without a new formal scene or unreachable proof'
            ),
            "textual_common_if_conditions": common_conditions,
            "textual_matches_are_merge_proof": False,
            "fact_review": {"facts": evidence, "fact_owner": owner,
                            "lifecycle": "unused old facade" if dead else
                                         "first admission versus changed-world replay" if clue in ("K4", "validation") else
                                         "direct request classification/admission" if clue in ("K1", "K2", "K3") else
                                         "record construction/body candidate selection and release",
                            "conclusion": "no merge/deletion authorized in S0-R"},
            "production_callers": callers,
            "detecting_check": {"status": "planned_not_executed", "checks": checks,
                "plan": "S1: replace dead body with assertion and rerun direct/full/five sets" if dead else
                        (
                            'S2: remove only the proposed retained guard in a disposable checkout; require a '
                            'named check to fail before merging'
                        ),
                "current_evidence": "function entries and behavior baseline only; no mutation has been performed"},
            "expected_delta": {"lines": -lines if dead else None, "decision_branches": -branches if dead else None,
                "status": "AST body estimate; confirm actual diff in S1" if dead else
                          "no authorized deletion/merge yet; measure guarded edit before claiming reduction"},
            "risk": "async disposition identity/body-release safety" if dead else
                    "goal revisions, proof identity, changed-world validation and incumbent body continuity",
            "closure": closure})
    return {"schema_version": "mc2p.navigation-structure-deletion-inventory.v2",
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "production_modified": False, "candidates": candidates,
            "structure_gate_candidate_ids": [item["id"] for item in candidates if item["category"] != "audit_only"],
            "mutation_checks_executed": False, "f1_function_review": f1_function_review(),
            "closure_note": "pending S1/S2 is an unexecuted proposal, never deleted/merged evidence"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    value = build_inventory(json.loads(args.matrix.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidates": len(value["candidates"]), "mutations": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
