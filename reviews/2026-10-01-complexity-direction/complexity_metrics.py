"""Growth, branching and churn metrics of the motion/navigation code across the published history.

Run from the repository root with the published commits available, for example:

    python reviews/2026-10-01-complexity-direction/complexity_metrics.py 403a23f

The first argument is the head commit; the history table always uses the fixed milestone list below.
"""
import re
import subprocess
import sys

MILESTONES = (
    "fdad6e3", "4977f18", "b381398", "7aa8483", "2dafa6d", "f097298", "556381a", "ba4acda",
    "211af42", "b740098", "59c45bb", "8898cf9", "d34a674", "eb4653c", "340cac6", "daa5cc4",
    "8b4b64f", "403a23f",
)
HEAD = sys.argv[1] if len(sys.argv) > 1 else "403a23f"
COORDINATION_MODULES = (
    "navigation_session.py", "planning_coordinator.py", "motion_coordination.py",
    "action_route_executor.py", "execution_supervisor.py", "async_work.py",
    "navigation_handoff.py", "known_map_planner.py",
)
CHURN_PATTERN = re.compile(r"reopen|remediation|hardening|refactor|root-fix|-r2\d-plan|enforce")


def git(*args):
    return subprocess.run(("git",) + args, capture_output=True, text=True, check=True).stdout


def files(commit, prefix, suffix=".py"):
    return [f for f in git("ls-tree", "-r", "--name-only", commit, prefix).splitlines() if f.endswith(suffix)]


def text(commit, path):
    return git("show", f"{commit}:{path}")


def history():
    print("1. History (motion_nav = mc2p/motion_nav/*.py; enums = StrEnum classes; "
          "reasons = distinct snake_case literals with >=3 parts)")
    print(f"{'commit':<9} {'date':<10} {'motion_nav':>10} {'tests':>7} {'docs(md)':>8} {'session':>8} "
          f"{'modules':>7} {'enums':>6} {'reasons':>8} {'contract':>8} {'decisions':>9}")
    for commit in MILESTONES:
        date = git("show", "-s", "--format=%cs", commit).strip()
        mn = files(commit, "mc2p/motion_nav")
        src = {p: text(commit, p) for p in mn}
        joined = "".join(src.values())
        session = src.get("mc2p/motion_nav/navigation_session.py", "").count("\n")
        tests = sum(text(commit, p).count("\n") for p in files(commit, "tests"))
        docs = sum(text(commit, p).count("\n") for p in files(commit, "docs/motion_navigation", ".md"))
        enums = len(re.findall(r"^class \w+\(StrEnum\)", joined, re.M))
        reasons = len(set(re.findall(r'"([a-z]+(?:_[a-z0-9]+){2,})"', joined)))
        contract = joined.count("raise ContractViolation")
        decisions = len(files(commit, "docs/motion_navigation/decisions", ".md"))
        lines = joined.count("\n")
        print(f"{commit:<9} {date:<10} {lines:>10} {tests:>7} {docs:>8} {session:>8} "
              f"{len(mn):>7} {enums:>6} {reasons:>8} {contract:>8} {decisions:>9}")


def modules():
    print(f"\n2. Coordination and planning modules at {HEAD}")
    print(f"{'module':<28} {'lines':>6} {'defs':>5} {'if':>5} {'elif':>5} {'fields':>6}")
    for name in COORDINATION_MODULES:
        src = text(HEAD, f"mc2p/motion_nav/{name}")
        fields = {m.group(1) for m in re.finditer(r"self\.(_[a-z_]+)\s*(?::[^=\n]*)?=(?!=)", src)}
        lines = src.count("\n")
        defs = len(re.findall(r"^\s*def ", src, re.M))
        ifs = len(re.findall(r"^\s*if ", src, re.M))
        elifs = len(re.findall(r"^\s*elif ", src, re.M))
        print(f"{name:<28} {lines:>6} {defs:>5} {ifs:>5} {elifs:>5} {len(fields):>6}")
    src = "".join(text(HEAD, p) for p in files(HEAD, "mc2p/motion_nav"))
    raises = src.count("raise ContractViolation")
    type_checks = len(re.findall(r"type[(][a-z_.]+[)] is not", src))
    post_inits = src.count("def __post_init__")
    enum_values = len(set(re.findall(r'^ +[A-Z_]+ = "([a-z_]+)"', src, re.M)))
    print(f"motion_nav total: raise ContractViolation={raises}, 'type(x) is not' checks={type_checks}, "
          f"__post_init__={post_inits}, distinct enum values={enum_values}")


def churn():
    print(f"\n3. Decisions and stage plans about reopening, remediation or hardening (first added)")
    for folder in ("docs/motion_navigation/decisions", "docs/motion_navigation/stages"):
        for path in files(HEAD, folder, ".md"):
            name = path.rsplit("/", 1)[1]
            if not CHURN_PATTERN.search(name):
                continue
            added = git("log", "--diff-filter=A", "--format=%cs %h", HEAD, "--", path).strip().splitlines()
            print(f"{added[-1] if added else '?':<19} {path}")


def commits():
    print(f"\n4. Published commits since 2026-09-26 (subject prefix shows feat/fix/refactor/release)")
    print(git("log", "--since=2026-09-26", "--format=%cs %h %s", HEAD).rstrip())


history()
modules()
churn()
commits()
