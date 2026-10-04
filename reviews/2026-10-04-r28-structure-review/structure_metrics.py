"""Where did the R28 coordination code grow?  Static AST inventory at several commits.

    python structure_metrics.py [rev ...]      (run inside the repository)

For the coordination files named below, per commit:
  lines            physical lines
  branches         If / IfExp / Match nodes (same rule as the project's A0 count)
  contract_checks  `raise ContractViolation(...)` statements
  validation_lines lines of __post_init__ bodies plus `if ...: raise ContractViolation` blocks
  dataclasses      classes decorated with @dataclass
  longest          the longest function (name, lines)
and, for NavigationSession, the length of propose().
"""
import ast
import subprocess
import sys

FILES = [
    "navigation_session", "navigation_handoff", "navigation_owners", "planning_coordinator",
    "motion_coordination", "execution_supervisor", "retry_ledger", "async_work",
    "route_body_controller", "probe_body_controller", "route_admission", "planner_worker",
]
REVS = sys.argv[1:] or ["7348b64", "3c15744", "b1d43c2"]


def source(rev, name):
    try:
        return subprocess.check_output(["git", "show", f"{rev}:mc2p/motion_nav/{name}.py"],
                                       stderr=subprocess.DEVNULL).decode("utf-8").replace("\r\n", "\n")
    except subprocess.CalledProcessError:
        return ""


def raises_contract(node):
    return (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and getattr(node.exc.func, "id", None) == "ContractViolation")


def inventory(text):
    tree = ast.parse(text)
    out = dict(lines=len(text.splitlines()), branches=0, contract_checks=0,
               validation_lines=0, dataclasses=0, longest=("", 0), propose=0)
    validation = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.Match)):
            out["branches"] += 1
        if raises_contract(node):
            out["contract_checks"] += 1
        if isinstance(node, ast.If) and node.body and all(raises_contract(n) for n in node.body):
            validation.update(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.ClassDef) and any(
                "dataclass" in ast.unparse(d) for d in node.decorator_list):
            out["dataclasses"] += 1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = node.end_lineno - node.lineno + 1
            if node.name == "__post_init__":
                validation.update(range(node.lineno, node.end_lineno + 1))
            if length > out["longest"][1]:
                out["longest"] = (node.name, length)
            if node.name == "propose":
                out["propose"] = max(out["propose"], length)
    out["validation_lines"] = len(validation)
    return out


rows = {}
for rev in REVS:
    total = dict(lines=0, branches=0, contract_checks=0, validation_lines=0, dataclasses=0)
    per_file = {}
    for name in FILES:
        text = source(rev, name)
        if not text:
            continue
        inv = inventory(text)
        per_file[name] = inv
        for key in total:
            total[key] += inv[key]
    rows[rev] = (total, per_file)

print(f"{len(FILES)} coordination files: " + ", ".join(FILES) + "\n")
print(f"{'commit':<10}{'lines':>8}{'branches':>10}{'contract':>10}{'validation':>12}{'dataclass':>11}{'session':>9}{'propose':>9}")
for rev, (total, per_file) in rows.items():
    session = per_file.get("navigation_session", {})
    print(f"{rev:<10}{total['lines']:>8}{total['branches']:>10}{total['contract_checks']:>10}"
          f"{total['validation_lines']:>12}{total['dataclasses']:>11}{session.get('lines', 0):>9}{session.get('propose', 0):>9}")
first, last = REVS[0], REVS[-1]
print(f"\nper file, {first} -> {last} (lines / branches / validation lines)")
for name in FILES:
    a = rows[first][1].get(name, dict(lines=0, branches=0, validation_lines=0))
    b = rows[last][1].get(name, dict(lines=0, branches=0, validation_lines=0))
    print(f"  {name:<24}{a['lines']:>6} -> {b['lines']:<6}({b['lines'] - a['lines']:+6})"
          f"{b['branches'] - a['branches']:>+8}{b['validation_lines'] - a['validation_lines']:>+8}")
