"""Design metrics for the motion-navigation redesign (M0-M5).  Read-only, static (AST) analysis.

    python -B design_metrics.py <checkout root> [--json <out.json>]

Measures the problems listed in the design assessment so every redesign step can report the same
numbers before and after:

  layers        lines per layer of mc2p/motion_nav (coordination share)
  dispatch      `type(x) is/in` and isinstance checks on action segment classes, per file and per
                question category (category map is defined for the e108a32 baseline; sites in
                functions the map does not know are reported as "unclassified")
  spread        production files that mention each segment class (excluding its definition and
                imports) -- the files a new or changed action type has to touch
  solve_kind    MotionSolveKind references outside the solver
  session       NavigationSession size: lines, methods, assigned self attributes, long methods
  async         methods with the same name in PlanningCoordinator and MotionRouteCoordinator
  guards        verbatim identical `if` conditions between admission entry pairs and between
                route_admission and route_validation
  lifecycle     string literals used as RuntimeNavigationDriver lifecycle states

Files under mc2p/motion_nav/actions/ or named action_spec*.py count as action implementations and are
excluded from `dispatch` and `spread`; that is where per-action logic is supposed to live after M1.
"""
import argparse
import ast
import collections
import itertools
import json
import pathlib
import re
import sys

SEGMENTS = ("WalkSegment", "JumpUpSegment", "StepSegment", "JumpGapSegment", "ControlledDropSegment")

LAYERS = {
    "physics_geometry_knowledge": "physics_1_21 physics_types physics_adapter physics_rollout geometry support_surfaces "
                                  "block_motion_traits world_model ground_modes ground_motion observed_block_adapter "
                                  "environment_identity runtime_adapter goal_observation step_transition movement_transition",
    "planning_solving": "known_map_planner motion_solver motion_candidate bridge_planner jump_up air_motion "
                        "motion_residual motion_risk controlled_drop jump_gap",
    "execution_control": "fixed_route action_route_executor online_motion ground_traversal landing_edge_probe "
                         "segment_entry safe_ground_control body_control action_route action_preconditions "
                         "external_motion external_motion_recovery world_interaction",
    "coordination": "navigation_session navigation_handoff navigation_owners planning_coordinator motion_coordination "
                    "execution_supervisor retry_ledger async_work route_body_controller probe_body_controller "
                    "route_admission planner_worker route_validation navigation_lifecycle motion_worker",
}

# What the coordination/execution code is asking about the action at each baseline site.
CATEGORY = {
    "A_controller_selection": {("action_route_executor.py", f) for f in (
        "_activate", "decide", "_landed_on_current_action_destination", "enter_upcoming_action_boundary")}
        | {("route_body_controller.py", "activity"), ("route_body_controller.py", "advance")},
    "B_body_commitment_and_stop_safety": {
        ("action_route_executor.py", "requires_safe_handoff"), ("action_route_executor.py", "stop_protection"),
        ("action_route_executor.py", "start"), ("navigation_session.py", "_needs_same_frame_stop_protection"),
        ("motion_coordination.py", "decide"), ("navigation_session.py", "_prepare_route_action")},
    "C_background_motion_solving": {("motion_coordination.py", f) for f in (
        "_air_action_direction", "_air_action_landing", "_following_motion_direction", "_planned_gap_request",
        "_planned_continuation", "_accept_result", "_submit_action", "_upcoming_gap_index",
        "_prepare_upcoming_from_applied_state")},
    "D_preconditions_and_information": {
        ("action_preconditions.py", "check_action_precondition"), ("navigation_session.py", "observation_request"),
        ("navigation_session.py", "_current_action_precondition"),
        ("navigation_session.py", "_upcoming_action_precondition_index"),
        ("navigation_session.py", "_begin_action_acquisition"),
        ("navigation_session.py", "current_action_requires_route_look")},
    "E_risk_and_damage": {
        ("navigation_session.py", "_route_expected_damage_points"), ("navigation_session.py", "_reserve_route_risk"),
        ("action_route_executor.py", "_commit_drop_damage_if_started")},
    "F_geometry_and_progress": {
        ("navigation_session.py", "_record_retry_route_progress"), ("route_admission.py", "_action_route_length"),
        ("route_admission.py", "_terminal_meets_current_goal")},
    "G_walk_proof_and_validation": {("route_admission.py", f) for f in (
        "__post_init__", "admit", "_body_meets_route_entry", "_surface_action_route", "_surface_validation_plan",
        "admit_surface", "update")}
        | {("navigation_session.py", "_ordinary_walk_can_continue_during_direct"),
           ("navigation_session.py", "_proposal")},
    "H_route_type_registry": {("action_route.py", "__post_init__")},
}


def is_action_impl(path: pathlib.Path) -> bool:
    return "actions" in path.parts or path.name.startswith("action_spec")


def parse(path):
    source = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return source, ast.parse(source)


def enclosing(tree):
    spans = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    def find(line):
        inside = [s for s in spans if s[0] <= line <= s[1]]
        return min(inside, key=lambda s: s[1] - s[0])[2] if inside else "<module>"
    return find


def class_methods(tree, name):
    node = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name), None)
    if node is None:
        return None, {}
    return node, {f.name: f for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}


def if_conditions(node):
    return {" ".join(ast.unparse(n.test).split()) for n in ast.walk(node) if isinstance(n, ast.If)}


def measure(root: pathlib.Path) -> dict:
    nav = root / "mc2p/motion_nav"
    files = sorted(nav.rglob("*.py"))
    lines = {p.relative_to(nav).as_posix(): len(p.read_text(encoding="utf-8").splitlines()) for p in files}
    total = sum(lines.values())
    layer_of = {f"{n}.py": layer for layer, names in LAYERS.items() for n in names.split()}
    layers = collections.Counter()
    for name, count in lines.items():
        layers[layer_of.get(name, "other")] += count

    dispatch, spread = [], collections.defaultdict(set)
    solve_kind = collections.Counter()
    pattern = re.compile(r"(type\(|isinstance\()")
    for path in sorted((root / "mc2p").rglob("*.py")):
        source, tree = parse(path)
        find = enclosing(tree)
        impl = is_action_impl(path)
        for number, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                continue
            names = [s for s in SEGMENTS if re.search(rf"\b{s}\b", line)]
            if names and not impl and not re.match(rf"class ({'|'.join(SEGMENTS)})\b", stripped):
                for s in names:
                    spread[s].add(path.relative_to(root).as_posix())
            if names and pattern.search(line) and not impl:
                function = find(number)
                category = next((c for c, keys in CATEGORY.items() if (path.name, function) in keys), "unclassified")
                dispatch.append({"file": path.name, "line": number, "function": function,
                                 "category": category, "segments": names})
            if "MotionSolveKind." in line and path.name != "motion_solver.py" and not impl:
                solve_kind[path.name] += line.count("MotionSolveKind.")
    for s in SEGMENTS:
        spread[s].discard("mc2p/motion_nav/action_route.py")

    _, session_tree = parse(nav / "navigation_session.py")
    session, methods = class_methods(session_tree, "NavigationSession")
    attributes = {n.attr for n in ast.walk(session) if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name) and n.value.id == "self" and isinstance(n.ctx, ast.Store)}
    sizes = sorted(((f.end_lineno - f.lineno + 1), name) for name, f in methods.items())

    _, planning = class_methods(parse(nav / "planning_coordinator.py")[1], "PlanningCoordinator")
    _, motion = class_methods(parse(nav / "motion_coordination.py")[1], "MotionRouteCoordinator")
    shared_async = sorted((set(planning) & set(motion)) - {"__init__"})

    _, admitter = class_methods(parse(nav / "route_admission.py")[1], "RouteAdmitter")
    conditions = {name: if_conditions(admitter[name]) for name in
                  ("admit", "admit_surface", "admit_local_direct", "admit_ground_direct") if name in admitter}
    pairs = {f"{a}~{b}": len(conditions[a] & conditions[b])
             for a, b in (("admit", "admit_surface"), ("admit_local_direct", "admit_ground_direct"))
             if a in conditions and b in conditions}
    validation = nav / "route_validation.py"
    if validation.exists():
        pairs["route_admission~route_validation"] = len(
            if_conditions(parse(nav / "route_admission.py")[1]) & if_conditions(parse(validation)[1]))

    driver_source = (root / "mc2p/skills/navigation_session_driver.py").read_text(encoding="utf-8")
    lifecycle = len(re.findall(r'"(running|stopping|failed|cancelled|success|stopped|ready|interaction_required|'
                               r'interaction_suspended)"', driver_source))

    by_category = collections.Counter(d["category"] for d in dispatch)
    by_file = collections.Counter(d["file"] for d in dispatch)
    by_segment = collections.Counter(s for d in dispatch for s in d["segments"])
    return {
        "motion_nav_lines": total,
        "layers": {k: {"lines": v, "share": round(v / total, 3)} for k, v in sorted(layers.items())},
        "dispatch_sites": len(dispatch),
        "dispatch_by_category": dict(sorted(by_category.items())),
        "dispatch_by_file": dict(by_file.most_common()),
        "dispatch_sites_mentioning_segment": dict(by_segment.most_common()),
        "files_mentioning_segment": {s: sorted(spread[s]) for s in SEGMENTS},
        "solve_kind_references_outside_solver": dict(solve_kind.most_common()),
        "session": {"lines": len((nav / "navigation_session.py").read_text(encoding="utf-8").splitlines()),
                    "methods": len(methods), "assigned_self_attributes": len(attributes),
                    "methods_over_100_lines": sum(1 for size, _ in sizes if size > 100),
                    "longest": [f"{name}:{size}" for size, name in sizes[-6:]]},
        "async_same_named_methods": shared_async,
        "identical_guards": pairs,
        "driver_lifecycle_string_literals": lifecycle,
        "dispatch": dispatch,
    }


def report(m):
    out = [f"motion_nav lines: {m['motion_nav_lines']}"]
    for layer, value in m["layers"].items():
        out.append(f"  {layer:<30} {value['lines']:>6}  {value['share']:.0%}")
    out.append(f"\naction-type dispatch sites outside action implementations: {m['dispatch_sites']}")
    out.append("  by category: " + ", ".join(f"{k} {v}" for k, v in m["dispatch_by_category"].items()))
    out.append("  by file:     " + ", ".join(f"{k} {v}" for k, v in m["dispatch_by_file"].items()))
    out.append("  sites naming each segment: " + ", ".join(f"{k} {v}" for k, v in m["dispatch_sites_mentioning_segment"].items()))
    out.append("\nproduction files mentioning each segment class (besides action_route.py):")
    for s, paths in m["files_mentioning_segment"].items():
        out.append(f"  {s:<22} {len(paths)}: " + " ".join(p.split('/')[-1] for p in paths))
    out.append("\nMotionSolveKind references outside motion_solver.py: " +
               ", ".join(f"{k} {v}" for k, v in m["solve_kind_references_outside_solver"].items()))
    s = m["session"]
    out.append(f"\nNavigationSession: {s['lines']} lines, {s['methods']} methods, {s['assigned_self_attributes']} "
               f"assigned self attributes, {s['methods_over_100_lines']} methods over 100 lines; longest {s['longest']}")
    out.append(f"\nsame-named methods in PlanningCoordinator and MotionRouteCoordinator: "
               f"{len(m['async_same_named_methods'])} {m['async_same_named_methods']}")
    out.append("verbatim identical if-conditions: " + ", ".join(f"{k} {v}" for k, v in m["identical_guards"].items()))
    out.append(f"RuntimeNavigationDriver lifecycle string literals: {m['driver_lifecycle_string_literals']}")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("--json", type=pathlib.Path)
    args = parser.parse_args()
    m = measure(args.root)
    print(report(m))
    if args.json:
        args.json.write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
