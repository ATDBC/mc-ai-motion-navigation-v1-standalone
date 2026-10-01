"""First-pass owner hints for the branches of NavigationSession.propose / observe (c9f7d41).

For every if/elif/while/try inside the two functions, collect the `self.<attribute>` names used in
the condition and in the branch body, map attributes to the owners named in R28 (architecture 17),
and report the owner set.  This is a starting point for the branch-level inventory, not a decision:
a branch that touches several owners is exactly what R28-1 has to split by hand.

    python propose_owner_hint.py            # from the repository root of a c9f7d41 checkout
"""
import ast
import collections
import pathlib
import re

SOURCE = pathlib.Path("mc2p/motion_nav/navigation_session.py")
OWNER_PATTERNS = (
    ("Handoff", r"^_handoff|^_pending_goal|^_stop|stop_request|_resume|_goal_revision|_request$|^_request_"),
    ("Planning", r"^_planning|^_planner|^_snapshot|^_candidate|^_route_admitter|^_admission|^_request_generation"),
    ("Information", r"^_information|^_edge_probe|^_probe|^_snapshot_missing|^_required_interaction|^_interaction"),
    ("Supervisor/Executor", r"^_supervisor|^_executor|^_active|^_controller|^_local_|^_body"),
    ("Budget", r"^_retry_ledger|^_risk_ledger|^_task_damage|^_movement_damage|^_executor_reported_damage"),
    ("Lifecycle", r"^_state$|^_lifecycle|^_transition|^_reason$|^_terminal"),
)


def owners(names):
    result = set()
    for name in names:
        for owner, pattern in OWNER_PATTERNS:
            if re.search(pattern, name):
                result.add(owner)
                break
        else:
            result.add("other:" + name)
    return result


tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
for name in ("propose", "observe"):
    fn = functions[name]
    counts = collections.Counter()
    multi = []
    for node in ast.walk(fn):
        if not isinstance(node, (ast.If, ast.While, ast.Try)):
            continue
        attrs = {n.attr for n in ast.walk(node)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"}
        calls = {n.func.attr for n in ast.walk(node)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"}
        found = {o for o in owners(attrs - calls) if not o.startswith("other:")}
        key = " + ".join(sorted(found)) if found else "(no owner state; calls only)"
        counts[len(found)] += 1
        if len(found) >= 3:
            multi.append((node.lineno, key))
    total = sum(counts.values())
    print(f"{name}: {total} branches; by number of distinct owner states touched: "
          + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    for line, key in multi[:12]:
        print(f"    line {line}: {key}")
    if len(multi) > 12:
        print(f"    ... {len(multi) - 12} more branches touching >= 3 owners")
