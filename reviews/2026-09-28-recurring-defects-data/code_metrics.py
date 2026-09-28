"""Structural facts used by the 2026-09-28 recurring-defects analysis.

Run from the repository root of an 8898cf9 checkout (git history is needed for
the growth table):
    python -B <this script>
"""
import ast
import glob
import re
import subprocess

SESSION = "mc2p/motion_nav/navigation_session.py"

src = open(SESSION).read()
tree = ast.parse(src)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NavigationSession")
methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)]
init = next(m for m in methods if m.name == "__init__")


def stored_attributes(node):
    return {
        n.attr for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == "self" and isinstance(n.ctx, ast.Store)
    }


attributes = stored_attributes(init)
writers = {m.name: stored_attributes(m) for m in methods}
print(f"NavigationSession: {cls.end_lineno - cls.lineno + 1} lines, {len(methods)} methods, "
      f"{len(attributes)} instance attributes")
print("largest methods:", sorted(((m.end_lineno - m.lineno + 1, m.name) for m in methods), reverse=True)[:5])
print("self._state = assignments:", len(re.findall(r"self\._state = ", src)),
      "| methods writing _state:", sum("_state" in w for w in writers.values()))
print("self._reason = assignments:", len(re.findall(r"self\._reason = ", src)),
      "| methods writing _reason:", sum("_reason" in w for w in writers.values()))

GROUPS = {
    "goal and request": {"_request", "_pending_goal", "_intent_sequence", "_source"},
    "planning and snapshot": {"_planner", "_planning_snapshot", "_planning_snapshot_request_id",
                              "_planning_changes", "_planning_margin", "_snapshot_builder",
                              "_snapshot_cells_per_step", "_owns_planner_worker"},
    "execution and handoff": {"_active_route", "_executor", "_coordinator", "_motion_worker",
                              "_owns_motion_worker", "_last_decision", "_restart_after_active_terminal",
                              "_cancel_reason"},
    "information and edge probe": {"_snapshot_missing", "_residual_missing", "_information_statuses",
                                   "_information_lower_required", "_information_wait_frames",
                                   "_information_wait_key", "_information_wait_last_sequence",
                                   "_edge_probe"},
    "task risk (damage)": {"_task_damage_budget", "_movement_damage_spent_points",
                           "_executor_reported_damage_points"},
    "recovery limits": {"_replan_key", "_replan_attempts"},
    "world interaction / bridging": {"_required_interaction", "_interaction_approach_pending",
                                     "_bridge_policy", "_bridge_remaining"},
    "world, residual, admission": {"_adapter", "_admitter", "_motion_residual", "_frame"},
    "lifecycle": {"_state", "_reason", "_closed"},
}
for name, group in GROUPS.items():
    print(f"  {name:<28}: {len(group & attributes)}")
print("  ungrouped:", sorted(attributes - set().union(*GROUPS.values())))

print("\nnavigation_session.py lines per exported commit:")
log = subprocess.run(["git", "log", "--format=%h %ad %s", "--date=format:%m-%d", "--reverse", "--", SESSION],
                     capture_output=True, text=True, check=True).stdout.splitlines()
for line in log:
    commit = line.split()[0]
    body = subprocess.run(["git", "show", f"{commit}:{SESSION}"], capture_output=True, text=True).stdout
    print(f"  {body.count(chr(10)):>5}  {line[:80]}")

print("\ntests:")
session_files, calculator_files, total, private = [], [], 0, 0
for path in sorted(glob.glob("tests/**/test_*.py", recursive=True)):
    text = open(path).read()
    total += len(re.findall(r"^\s*def test_", text, re.M))
    if "NavigationSession" in text or "navigation_session" in text:
        session_files.append(path)
        if "physics_1_21" in text:
            calculator_files.append(path)
        private += len(re.findall(r"session\._[a-z]\w*", text))
print(f"  test functions: {total}")
print(f"  test files that use NavigationSession: {len(session_files)}; "
      f"of these, files that also drive the 1.21 calculator: {len(calculator_files)}")
print(f"  accesses to private session attributes in those files: {private}")

print("\nfree-form reason strings used as control flow (reason == / in literal):")
hits = []
for path in glob.glob("mc2p/**/*.py", recursive=True):
    for number, line in enumerate(open(path), 1):
        if re.search(r"reason(_code)? (==|in|!=) [\"'({]", line):
            hits.append(f"{path}:{number}")
print(f"  {len(hits)} sites, e.g. {hits[:4]}")
