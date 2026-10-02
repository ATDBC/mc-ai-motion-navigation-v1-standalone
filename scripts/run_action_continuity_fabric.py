"""Run the finite continuity pilot on the existing isolated Fabric launcher."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.probe_fabric_deployment_observation import main


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--direction', type=int, choices=range(4))
    extra, rest = parser.parse_known_args()
    os.environ['MC2P_ACTION_CONTINUITY_PROBE'] = '1'
    if extra.direction is not None:
        os.environ['MC2P_ACTION_CONTINUITY_DIRECTION'] = str(extra.direction)
    raise SystemExit(main(['--r25-planning-information-probe', *rest]))
