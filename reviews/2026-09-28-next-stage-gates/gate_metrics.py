"""Structural gate G1 for the navigation coordination work.

Run from the repository root of the checkout under review:
    python -B <this script>                 # structure of the current checkout
    python -B <this script> --base <commit> # also compare against an earlier commit
Exit code 1 when a hard criterion fails.  Soft criteria print WARN only.
"""
import argparse
import ast
import re
import subprocess
import sys

SESSION = "mc2p/motion_nav/navigation_session.py"
SUPERVISOR = "mc2p/motion_nav/execution_supervisor.py"
MONITOR = "tests/sim/monitor.py"
CONCRETE_CONTROLLER_MODULES = (
    "mc2p.motion_nav.landing_edge_probe",
    "mc2p.motion_nav.action_route_executor",
    "mc2p.motion_nav.motion_coordination",
)
SESSION_SHARE_LIMIT = 0.10
# record() was renamed admit_event() in 340cac6; both are lifecycle event entry points.
LIFECYCLE_EVENT_METHODS = ("record", "admit_event")


def session_facts(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NavigationSession")
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    init = next(m for m in methods if m.name == "__init__")

    def self_targets(node, name=None):
        found = []
        for sub in ast.walk(node):
            targets = []
            if isinstance(sub, ast.Assign):
                targets = sub.targets
            elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                targets = [sub.target]
            for target in targets:
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id == "self" and (name is None or target.attr == name)):
                    found.append(target.attr)
        return found

    state_writes = {m.name: len(self_targets(m, "_state")) for m in methods}
    state_writes = {name: count for name, count in state_writes.items() if count}
    discarded_records = 0
    used_records = 0
    for node in ast.walk(cls):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in LIFECYCLE_EVENT_METHODS and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_lifecycle"):
            used_records += 1
    for node in ast.walk(cls):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in LIFECYCLE_EVENT_METHODS
                and isinstance(node.value.func.value, ast.Attribute)
                and node.value.func.value.attr == "_lifecycle"):
            discarded_records += 1
    return {
        "file_lines": len(source.splitlines()),
        "class_lines": cls.end_lineno - cls.lineno + 1,
        "methods": len(methods),
        "fields": set(self_targets(init)),
        "state_writes": state_writes,
        "record_calls": used_records,
        "record_discarded": discarded_records,
    }


def supervisor_concrete_imports(source):
    tree = ast.parse(source)
    return sorted({node.module for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module in CONCRETE_CONTROLLER_MODULES})


def git(*args):
    return subprocess.run(("git",) + args, check=True, capture_output=True, text=True).stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    args = parser.parse_args()
    failures = []

    def verdict(label, ok, detail, hard=True):
        status = "PASS" if ok else ("FAIL" if hard else "WARN")
        if not ok and hard:
            failures.append(label)
        print(f"  [{status}] {label}: {detail}")

    now = session_facts(open(SESSION, encoding="utf-8").read())
    print("G1 structure of the current checkout")
    print(f"  {SESSION}: {now['file_lines']} lines; NavigationSession {now['class_lines']} lines, "
          f"{now['methods']} methods, {len(now['fields'])} __init__ fields")
    writes = sum(now["state_writes"].values())
    verdict("G1-1 methods that assign self._state", len(now["state_writes"]) <= 1,
            f"{len(now['state_writes'])} methods, {writes} assignments "
            f"({', '.join(sorted(now['state_writes']))})")
    verdict("G1-2 lifecycle event results discarded (record/admit_event)", now["record_discarded"] == 0,
            f"{now['record_discarded']} of {now['record_calls']} calls")
    concrete = supervisor_concrete_imports(open(SUPERVISOR, encoding="utf-8").read())
    verdict("G1-3 supervisor imports concrete controller modules", not concrete,
            ", ".join(concrete) or "none")
    print("  review item: monitor state/reason string comparisons (each must be a detection, not an exemption):")
    for number, line in enumerate(open(MONITOR, encoding="utf-8"), 1):
        if re.search(r"e\.(state|reason) == \"", line):
            print(f"    {MONITOR}:{number}: {line.strip()}")

    if args.base:
        before = session_facts(git("show", f"{args.base}:{SESSION}"))
        print(f"G1 change since {args.base}")
        new_fields = sorted(now["fields"] - before["fields"])
        verdict("G1-4 new NavigationSession fields", not new_fields,
                f"{len(new_fields)} ({', '.join(new_fields)})" if new_fields else "0")
        new_writers = sorted(set(now["state_writes"]) - set(before["state_writes"]))
        verdict("G1-5 new methods that assign self._state", not new_writers,
                f"{len(new_writers)} ({', '.join(new_writers)})" if new_writers else "0")
        added_total = added_session = 0
        for row in git("diff", "--numstat", f"{args.base}..HEAD", "--", "mc2p").splitlines():
            added, _deleted, path = row.split("\t")
            if added == "-":
                continue
            added_total += int(added)
            if path == SESSION:
                added_session += int(added)
        share = added_session / added_total if added_total else 0.0
        verdict("G1-6 share of added source lines in the session", share <= SESSION_SHARE_LIMIT,
                f"{added_session}/{added_total} = {share:.0%} (limit {SESSION_SHARE_LIMIT:.0%}; "
                "above the limit the delivery record must explain why the work belongs to the session)",
                hard=False)
    print("hard criteria failed: " + (", ".join(failures) if failures else "none"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
