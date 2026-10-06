"""Comparisons on ActionSpec enum fields outside mc2p/motion_nav/actions/ (M1, 5124bd7).

    python -B proxy_dispatch_check.py <checkout root>

`spec.controller_family is ControllerFamily.AIR` is still a branch on what kind of action this is.
navigation-design-metrics-v2 counts only segment-class symbols, so these comparisons are not in its
42.  The script lists them, plus the remaining direct segment-class checks in the same functions,
so the executor's controller selection can be read as one picture.  Read-only.
"""
import ast
import pathlib
import re
import sys

ENUMS = ("ControllerFamily", "BodyCommitment", "MotionSolveKind")
SEGMENTS = ("WalkSegment", "JumpUpSegment", "StepSegment", "JumpGapSegment", "ControlledDropSegment")


def main(root):
    rows = []
    for path in sorted((root / "mc2p").rglob("*.py")):
        if "actions" in path.parts or path.name == "motion_solver.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        spans = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        for number, line in enumerate(source.splitlines(), 1):
            if line.strip().startswith(("import ", "from ")):
                continue
            enum = [e for e in ENUMS if re.search(rf"\b{e}\.[A-Z_]+", line)]
            if enum:
                inside = [s for s in spans if s[0] <= number <= s[1]]
                function = min(inside, key=lambda s: s[1] - s[0])[2] if inside else "<module>"
                rows.append((path.name, number, function, enum[0], line.strip()[:110]))
    print(f"enum-field comparisons outside actions/: {len(rows)}")
    for row in rows:
        print("  %s:%d [%s] %s: %s" % row)
    executor = (root / "mc2p/motion_nav/action_route_executor.py").read_text(encoding="utf-8").splitlines()
    print("\naction_route_executor.py lines that choose a controller or entry profile by action kind:")
    for number, line in enumerate(executor, 1):
        if (re.search(r"type\((next_)?action\) is (JumpUpSegment|StepSegment|WalkSegment)", line)
                or "controller_family is ControllerFamily" in line):
            print(f"  {number}: {line.strip()[:110]}")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]))
