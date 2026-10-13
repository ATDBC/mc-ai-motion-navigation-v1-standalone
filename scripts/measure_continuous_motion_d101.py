"""Windows-only D101 M0 gate; a failed gate never starts a later stage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.motion_navigation.trajectory_proto.d101_measurement import recompute_m0, run_m0
from experiments.motion_navigation.trajectory_proto.physics import _canonical


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("M0",))
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recompute", action="store_true", help="verify hashes and summarize existing raw records")
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("rounds must be positive")
    summary = recompute_m0(args.output) if args.recompute else run_m0(rounds=args.rounds, output=args.output)
    print(json.dumps(_canonical(summary), ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
