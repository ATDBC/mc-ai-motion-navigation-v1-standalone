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
    parser.add_argument('--start-delivery', action='store_true',
                        help='run D055 five-action startup/window checks')
    parser.add_argument('--kind', choices=('moving_gap', 'cold_gap', 'jump_up', 'drop_2', 'drop_5'))
    extra, rest = parser.parse_known_args()
    os.environ['MC2P_ACTION_CONTINUITY_PROBE'] = '1'
    if extra.direction is not None:
        os.environ['MC2P_ACTION_CONTINUITY_DIRECTION'] = str(extra.direction)
    if extra.start_delivery:
        os.environ['MC2P_MOTION_START_DELIVERY_PROBE'] = '1'
    if extra.kind is not None:
        if not extra.start_delivery:
            parser.error('--kind requires --start-delivery')
        os.environ['MC2P_MOTION_START_DELIVERY_KIND'] = extra.kind
    raise SystemExit(main(['--r25-planning-information-probe', *rest]))
