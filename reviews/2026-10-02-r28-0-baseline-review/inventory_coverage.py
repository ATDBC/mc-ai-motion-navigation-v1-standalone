"""Which migration-inventory functions does a run actually execute?  (c9f7d41)

Usage, from the repository root of a c9f7d41 checkout, after collecting branch coverage with
coverage.py (`coverage run --branch --include='mc2p/motion_nav/*,mc2p/skills/navigation_session_driver.py' ...`):

    PYTHONPATH=.:<coverage install> python reviews/2026-10-02-r28-0-baseline-review/inventory_coverage.py \
        <label>=<coverage data file> [<label>=<file> ...]

Reads evidence/motion_navigation/r28-baseline-v1/migration-inventory.json for the 41 functions and
reports, per coverage file: executed body statements and taken arcs (starting inside the body) per function.
"""
import json
import pathlib
import sys

import coverage

INVENTORY = json.loads(pathlib.Path(
    "evidence/motion_navigation/r28-baseline-v1/migration-inventory.json").read_text("utf-8"))


def measure(data_file):
    cov = coverage.Coverage(data_file=data_file)
    cov.load()
    data = cov.get_data()
    rows = {}
    for fn in INVENTORY["functions"]:
        path = str(pathlib.Path(fn["file"]).resolve())
        analysis = cov._analyze(path)
        # the def line itself runs at import time; count only the body
        statements = {line for line in analysis.statements if fn["line"] < line <= fn["end_line"]}
        executed = statements & set(analysis.executed)
        possible = {arc for arc in analysis.arc_possibilities if fn["line"] < arc[0] <= fn["end_line"]}
        taken = possible & set(analysis.arcs_executed)
        rows[fn["id"]] = (len(executed), len(statements), len(taken), len(possible), fn["step"])
    return rows


labels, results = [], []
for spec in sys.argv[1:]:
    label, path = spec.split("=", 1)
    labels.append(label)
    results.append(measure(path))

header = "function".ljust(58) + "step".ljust(6) + "".join(f"{label:>22}" for label in labels)
print(header)
print(" " * 64 + "".join(f"{'lines   / arcs':>22}" for _ in labels))
totals = [[0, 0, 0, 0] for _ in labels]
for fn in INVENTORY["functions"]:
    cells = []
    for i, rows in enumerate(results):
        ex, st, tk, ps, step = rows[fn["id"]]
        for j, v in enumerate((ex, st, tk, ps)):
            totals[i][j] += v
        cells.append(f"{ex:>4}/{st:<4} {tk:>4}/{ps:<4}  ")
    print(fn["id"].ljust(58) + fn["step"].ljust(6) + "".join(f"{c:>22}" for c in cells))
print()
for label, (ex, st, tk, ps) in zip(labels, totals):
    unrun = sum(1 for rows in [results[labels.index(label)]] for v in rows.values() if v[0] == 0)
    print(f"{label}: statements {ex}/{st} ({100 * ex / st:.1f}%), branch arcs {tk}/{ps} ({100 * tk / ps:.1f}%), "
          f"functions never entered: {unrun}/{len(INVENTORY['functions'])}")
