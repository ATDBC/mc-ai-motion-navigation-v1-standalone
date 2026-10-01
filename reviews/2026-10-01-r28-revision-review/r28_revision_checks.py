"""Static checks behind the review of the revised R28 plan (0701299; code identical to 403a23f).

Run from the repository root of a 0701299 checkout:

    python reviews/2026-10-01-r28-revision-review/r28_revision_checks.py

1. Every function named in the stage plan's migration inventory (section 6) exists, and how many
   lines of navigation_session.py those functions hold.
2. How much of the existing moving-target melee driver is successor / handoff / recovery /
   cancel plumbing -- the pattern the formal-follow probe must not repeat in the skill layer.
3. Whether the navigation session currently ends the task when the goal is reached.
"""
import ast
import pathlib
import re

INVENTORY = {
    "navigation_session.py": [
        "update_goal", "_stage_goal_revision_for_body_release", "_resume_pending_goal", "cancel", "close",
        "_finalize_close", "handle_internal_contract_failure", "observe", "propose", "_request_probe_stop",
        "_finish_pending_probe_terminal", "_wait_for_active_terminal", "_fail_planning_or_preserve_incumbent",
        "_reissue_request_from_current", "_resolve_pending_retry", "_clear_active_execution", "_retire_route",
        "_activate_planning_route", "_replace_request", "_accept_goal_request", "_advance_planning",
        "_end_probe_waits", "_end_session_waits", "_check_recovery_wait", "_probe_movement",
        "_information_look", "_begin_action_acquisition", "_transition",
    ],
    "planning_coordinator.py": ["advance", "_submit", "_record_admission", "retry_from_current", "_retry_or_fail"],
    "motion_coordination.py": ["_accept_result", "_retire_work", "_record_admission"],
    "retry_ledger.py": ["record_failure", "record_progress"],
    "safe_ground_control.py": ["verified_ground_rollout"],
}
PARTIAL = {"propose", "observe"}  # listed only for some of their branches


def function_sizes(path):
    src = path.read_text(encoding="utf-8")
    sizes = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sizes[node.name] = sizes.get(node.name, 0) + node.end_lineno - node.lineno + 1
    return src, sizes


print("1. Migration inventory (stage plan section 6)")
for name, functions in INVENTORY.items():
    src, sizes = function_sizes(pathlib.Path("mc2p/motion_nav", name))
    missing = [f for f in functions if f not in sizes]
    listed = sum(sizes.get(f, 0) for f in functions)
    print(f"  {name:<26} lines={src.count(chr(10)):>5} functions={len(functions):>2} "
          f"in_listed={listed:>5} missing={missing or 'none'}")
    if name == "navigation_session.py":
        partial = sum(sizes[f] for f in PARTIAL)
        print(f"    of which propose+observe (only partly in scope) = {partial}; "
              f"other listed helpers = {listed - partial}")
        print(f"    largest functions in the file: "
              f"{sorted(((v, k) for k, v in sizes.items()), reverse=True)[:6]}")

print("\n2. Skill-layer reference: moving_melee_driver.py")
path = pathlib.Path("mc2p/skills/moving_melee_driver.py")
src = path.read_text(encoding="utf-8")
pattern = re.compile(r"successor|transfer|handoff|relinquish|recovery|cancel|release|retain", re.I)
rows = []
for node in ast.walk(ast.parse(src)):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        body = ast.get_source_segment(src, node) or ""
        if pattern.search(node.name) or len(pattern.findall(body)) >= 4:
            rows.append((node.end_lineno - node.lineno + 1, node.name))
print(f"  file lines={src.count(chr(10))}; functions centred on successor/handoff/recovery/cancel: "
      f"{len(rows)} with {sum(r[0] for r in rows)} lines (keyword heuristic)")
print(f"  spawn_successor calls: {src.count('spawn_successor(')}; transfer_to_successor calls: "
      f"{src.count('transfer_to_successor(')}; replace_goal calls: {src.count('replace_goal(')}")

print("\n3. Does reaching the goal end the navigation task?")
session = pathlib.Path("mc2p/motion_nav/navigation_session.py").read_text(encoding="utf-8")
for line_no, line in enumerate(session.splitlines(), 1):
    if "goal_state_satisfied" in line and ("COMPLETE" in line or "_complete" in line or "SUCCEEDED" in line):
        print(f"  navigation_session.py:{line_no}: {line.strip()[:110]}")
