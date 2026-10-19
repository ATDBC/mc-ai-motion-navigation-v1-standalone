"""E120：在 Linux 上复跑 D120 的四方向离线入口扫描（不含只在 Windows 本地保存的 raw 门禁）。

只调用项目脚本 scripts/d120_momentum_entry_calibration.py 的 build_manifest、scan_direction 和
validate_coverage。报告清单哈希是否与公开证据 offline/manifest.json 一致、每个方向的样本数、
后缀数、通过数、主物理步数，以及 validate_coverage 是否通过。
用法：python e120_d120_offline_scan.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from scripts import d120_momentum_entry_calibration as calibration  # noqa: E402


def main():
    started = time.perf_counter()
    manifest = calibration.build_manifest()
    published = json.loads(Path("evidence/motion_navigation/d120-momentum-entry-v1/offline/manifest.json")
                           .read_text(encoding="utf-8"))
    rows = []
    for direction in range(4):
        report = calibration.scan_direction(manifest, direction)
        try:
            calibration.validate_coverage(manifest, report)
            verdict = "passed"
        except ValueError as error:
            verdict = f"failed: {error}"
        suffixes = [result for sample in report["samples"] for result in sample["suffixes"].values()]
        rows.append({
            "direction": direction,
            "samples": len(report["samples"]),
            "suffixes": len(suffixes),
            "suffixes_passed": sum(1 for result in suffixes if result["passed"]),
            "main_physics_steps": report["main_physics_steps"],
            "validate_coverage": verdict,
        })
    print(json.dumps({
        "manifest_sha256": manifest["sha256"],
        "matches_published_manifest": manifest == published,
        "directions": rows,
        "total_samples": sum(r["samples"] for r in rows),
        "total_suffixes": sum(r["suffixes"] for r in rows),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
