from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

run_matrix = importlib.import_module(
    "experiments.motion_navigation.trajectory_proto.closed_loop_measurement"
).run_matrix


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated D098 P3 rolling closed loop")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("rounds must be positive")
    payload = run_matrix(rounds=args.rounds, compact=args.compact)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
