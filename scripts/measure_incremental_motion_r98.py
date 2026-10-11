"""Command-line entry for the isolated D098 P1 measurement."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_measurement = importlib.import_module(
    "experiments.motion_navigation.trajectory_proto.measurement"
)
DecisionParts = _measurement.DecisionParts
main = _measurement.main


if __name__ == "__main__":
    raise SystemExit(main())
