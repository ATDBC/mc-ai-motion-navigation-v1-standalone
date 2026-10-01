"""Run only the bounded R28 product fixture on the existing local Fabric launcher."""
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.probe_fabric_deployment_observation import main

if __name__ == "__main__":
    os.environ["MC2P_R28_PRODUCT_PROBE"] = "1"
    raise SystemExit(main(["--r25-planning-information-probe", *sys.argv[1:]]))
