"""Run only the bounded R28 product fixture on the existing local Fabric launcher."""
import os
from pathlib import Path
import sys
import argparse
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.probe_fabric_deployment_observation import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--batch5', action='store_true')
    parser.add_argument('--case-start', type=int, default=0)
    parser.add_argument('--case-count', type=int, default=14)
    parser.add_argument('--late-probability', type=float, choices=(0.,.2), default=0.)
    extra, rest = parser.parse_known_args()
    if extra.batch5:
        if not 0 <= extra.case_start < 28 or not 1 <= extra.case_count <= 14 or extra.case_start+extra.case_count > 28:
            parser.error('batch5 cases must be within the frozen 28 cases and count <= 14')
        os.environ['MC2P_R28_BATCH5'] = '1'
        os.environ['MC2P_R28_CASE_START'] = str(extra.case_start)
        os.environ['MC2P_R28_CASE_COUNT'] = str(extra.case_count)
        os.environ['MC2P_R28_LATE_PROBABILITY'] = str(extra.late_probability)
    os.environ["MC2P_R28_PRODUCT_PROBE"] = "1"
    raise SystemExit(main(["--r25-planning-information-probe", *rest]))
