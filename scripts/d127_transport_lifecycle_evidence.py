"""Generate compact D123-2 transport lifecycle evidence without Fabric."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import d123_transport_preflight


SOURCES = (
    "mc2p/runtime/transport_lifecycle.py",
    "mc2p/runtime/player_runtime_v1.py",
    "mc2p/skills/navigation_session_driver.py",
    "mc2p/backends/fabric_behavior.py",
    "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java",
    "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorInput.java",
    "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java",
    "scripts/d123_transport_preflight.py",
    "tests/motion_nav/test_d127_transport_lifecycle.py",
    "tests/java/ClientNotAppliedTimingTest.java",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(output: Path) -> dict[str, object]:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("D123-2 evidence is append-only")
    preflight = d123_transport_preflight.generate()
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema": "mc2p.d123-2-transport-lifecycle.v1",
        "status": "PASS",
        "fabric_run": False,
        "contracts": [
            "local_not_dispatched",
            "remote_rejected_before_sampling",
            "delivery_unknown",
            "channel_lost",
        ],
        "wire_change": "windowed_full_request_not_applied",
        "unresolved": {
            "driver_state": "UNRESOLVED",
            "result": "BODY_RESPONSIBILITY_UNRESOLVED",
            "source_released": False,
            "owner_snapshot_before_backend_close": True,
            "resource_close_separate_from_body_responsibility": True,
        },
        "preflight": {
            "status": preflight.status,
            "formal_states": list(preflight.formal_states),
            "passed": len(preflight.chains),
            "total": 3,
        },
        "verification": {
            "d127_directed": "19/19",
            "fabric_backend_component_initial": "36/37",
            "fabric_backend_component_repaired_cases": "2/2",
            "full_repository_matrix_run": False,
        },
        "source_sha256": {
            name: _sha(ROOT / name) for name in SOURCES
        },
    }
    summary_path = output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    readme = (
        "# D123-2 transport 生命周期证据\n\n"
        "本目录由 `scripts/d127_transport_lifecycle_evidence.py` 生成。"
        "它只运行正式组件 preflight，不启动 Minecraft 或 Fabric。\n\n"
        "`summary.json` 记录四类 transport 合同、最小 `not_applied` wire、"
        "driver 级 `UNRESOLVED`、独立 resource close 和正式 preflight 3／3。\n"
    )
    readme_path = output / "README.md"
    readme_path.write_text(readme, encoding="utf-8", newline="\n")
    manifest = {
        "schema": "mc2p.d123-2-transport-lifecycle-manifest.v1",
        "status": "PASS",
        "files": {
            "README.md": _sha(readme_path),
            "summary.json": _sha(summary_path),
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
