"""List tracked first-party modules that import files deleted by 3e0c64b.

Run from the repository root of commit 3e0c64b:

    PYTHONPATH=. python -B <review-branch>/reviews/2026-09-26-d035-repro/dangling_first_party_imports.py
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import subprocess


def main() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], check=True, capture_output=True, text=True,
    ).stdout.split()
    broken: dict[str, list[str]] = {}
    for relative in tracked:
        if relative.startswith("reviews/"):
            continue
        tree = ast.parse(Path(relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                if name.split(".")[0] not in ("mc2p", "scripts", "tests"):
                    continue
                if importlib.util.find_spec(name) is None:
                    broken.setdefault(relative, []).append(name)
    for relative, names in sorted(broken.items()):
        print(f"{relative}: {', '.join(sorted(set(names)))}")
    print(f"{len(broken)} tracked files import missing first-party modules")


if __name__ == "__main__":
    main()
