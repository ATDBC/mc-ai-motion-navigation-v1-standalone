"""Verbatim-identical `if` conditions shared by RouteAdmitter's admission entry points.  (1f0fefe)

    python admission_guard_overlap.py <path to mc2p/motion_nav/route_admission.py>

A shared condition is only a lead for the S0 inventory (category 2: the same check repeated in
adjacent layers); whether two entries can share one guard still needs an owner/replacement and a
formal check per item.
"""
import ast
import collections
import itertools
import sys

source = open(sys.argv[1], encoding="utf-8").read().replace("\r\n", "\n")
tree = ast.parse(source)
admitter = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RouteAdmitter")
methods = {n.name: n for n in admitter.body if isinstance(n, ast.FunctionDef)}
entries = ["admit", "admit_surface", "admit_current_request", "admit_local_direct", "admit_ground_direct"]
print(f"RouteAdmitter: {admitter.end_lineno - admitter.lineno + 1} lines, {len(methods)} methods")
conditions = {}
for name in entries:
    node = methods[name]
    conditions[name] = {" ".join(ast.unparse(n.test).split()) for n in ast.walk(node) if isinstance(n, ast.If)}
    print(f"  {name:<24} {node.end_lineno - node.lineno + 1:>4} lines, {len(conditions[name]):>3} distinct if-conditions")
print("\nverbatim-identical if-conditions per pair of entry points:")
for a, b in itertools.combinations(entries, 2):
    shared = sorted(conditions[a] & conditions[b])
    if shared:
        print(f"\n  {a} & {b}: {len(shared)}")
        for c in shared:
            print(f"    - {c[:160]}")
