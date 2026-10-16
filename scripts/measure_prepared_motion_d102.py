"""Explicit Windows G2 evidence collection, or read-only indexed recomputation."""
import argparse
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sim.d102_prepared_motion import recompute_run, run_d102_matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("fast", "spawn-smoke", "realtime", "fallback"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--measurement-mode", choices=("call-count", "performance"))
    parser.add_argument("--recompute", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.recompute:
            if args.input is None or args.stage or args.output or args.manifest or args.measurement_mode:
                raise ValueError("recompute requires only --input")
            result = recompute_run(args.input)
        else:
            if platform.system() != "Windows":
                raise ValueError("formal G2 platform must be Windows")
            if (args.stage is None or args.manifest is None or args.output is None
                    or args.measurement_mode is None or args.input):
                raise ValueError("recording requires --stage, --manifest, --output and --measurement-mode")
            result = run_d102_matrix(args.stage, manifest=args.manifest, output=args.output,
                                    command_argv=[sys.executable, *sys.argv],
                                    measurement_mode=args.measurement_mode)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if not result.get("gate_errors", result.get("errors")) else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("FAIL: " + str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
