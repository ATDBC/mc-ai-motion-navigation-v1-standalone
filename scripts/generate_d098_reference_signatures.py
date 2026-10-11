"""CLI wrapper for the isolated D098 reference-signature generator."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    module = importlib.import_module(
        "experiments.motion_navigation.trajectory_proto.reference_signature_generator")
    return module.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
