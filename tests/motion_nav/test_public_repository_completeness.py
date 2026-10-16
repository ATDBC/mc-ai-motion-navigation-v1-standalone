from pathlib import Path
import hashlib
import json
import math
import unittest

from scripts.export_motion_navigation_standalone import (
    collect_export_files,
    load_manifest,
)
from scripts import probe_fabric_deployment_observation as deployment_probe


ROOT = Path(__file__).resolve().parents[2]


class PublicRepositoryCompletenessTests(unittest.TestCase):
    def test_d111_publication_inputs_and_published_attribution_are_complete(self):
        selected = {path.as_posix() for path in collect_export_files(load_manifest())}
        directory = "evidence/motion_navigation/d111-b10-gc-attribution-v1"
        required = [
            "docs/motion_navigation/decisions/0111-remove-b10-observer-gc-distortion.md",
            "docs/motion_navigation/stages/d111-b10-gc-attribution-and-buffer-plan.md",
            "docs/motion_navigation/acceptance/d111-b10-gc-attribution-and-buffer.md",
            *[f"{directory}/{name}" for name in (
                "README.md", ".gitattributes", "index.json", "SHA256SUMS.txt",
                "windows-regression/summary.json", "windows-regression/commands.txt",
                "fabric-b10/summary.json", "fabric-b10/command.txt", "fabric-b10/provenance.json")],
        ]
        self.assertEqual([name for name in required if name not in selected], [])
        root = ROOT / directory
        self.assertEqual((root / ".gitattributes").read_text("utf-8").strip(), "* -text")
        index = json.loads((root / "index.json").read_text("utf-8"))
        files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(set(index["files"]), files - {"index.json", "SHA256SUMS.txt"})
        for name, info in index["files"].items():
            raw = (root / name).read_bytes()
            self.assertEqual(len(raw), info["bytes"], name)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), info["sha256"], name)
        checksums = dict((name, digest) for digest, name in
                         (line.split("  ", 1) for line in (root / "SHA256SUMS.txt").read_text("utf-8").splitlines()))
        self.assertEqual(set(checksums), files - {"SHA256SUMS.txt"})
        for name, digest in checksums.items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest, name)
        summary = json.loads((root / "fabric-b10/summary.json").read_text("utf-8"))
        self.assertEqual(summary["status"], "FAILED/STOPPED")
        self.assertEqual(summary["formal_b10_runs"], 1)
        self.assertEqual(summary["failed_checks"], ["client-0:b10b_solver_is_bounded"])
        gc = summary["gc_pause_timeline"]
        self.assertTrue(gc["complete"])
        self.assertEqual((gc["event_count"], gc["interval_count"]), (656, 328))
        samples = summary["solver"]["samples"]
        self.assertEqual(len(samples), 142)
        for sample in samples:
            start, end = sample["solve_started_ns"], sample["solve_ended_ns"]
            self.assertEqual(end - start, sample["solve_time_ns"])
            clips = sorted((max(start, interval["start_ns"]), min(end, interval["end_ns"]))
                           for interval in gc["intervals"]
                           if min(end, interval["end_ns"]) > max(start, interval["start_ns"]))
            pause, previous_end = 0, start
            for low, high in clips:
                pause += max(0, high - max(low, previous_end))
                previous_end = max(previous_end, high)
            self.assertEqual(pause, sample["solve_gc_pause_overlap_ns"], sample["name"])
            self.assertEqual(end - start - pause, sample["solve_wall_minus_gc_pause_ns"], sample["name"])
        for name in ("solve_time_ns", "solve_wall_minus_gc_pause_ns"):
            values = sorted(sample[name] for sample in samples)
            expected = summary["solver"][name]
            self.assertEqual(expected["p95"], values[math.ceil(len(values) * .95) - 1])
            self.assertEqual(expected["p99"], values[math.ceil(len(values) * .99) - 1])
            self.assertEqual(expected["maximum"], values[-1])

    def test_d109_and_d110_publication_inputs_are_selected(self):
        selected_paths = {
            path.as_posix() for path in collect_export_files(load_manifest())
        }
        required = (
            "scripts/benchmark_d108_snapshot_materialization.py",
            "scripts/d105_ftick.py",
            "scripts/f1_known_world_following_fabric.py",
            "scripts/process_tree.py",
            "scripts/run_motion_nav_shards.py",
            "tests/test_async_trace.py",
            "tests/motion_nav/test_d102_registry_lifecycle.py",
            "tests/motion_nav/test_d107_shard_runner.py",
            "tests/motion_nav/test_d108_snapshot_materialization.py",
            "docs/motion_navigation/decisions/0109-retest-b10-budget-and-bound-lifecycle-cleanup.md",
            "docs/motion_navigation/stages/d109-b10-budget-and-lifecycle-repair-plan.md",
            "docs/motion_navigation/acceptance/d109-b10-budget-and-lifecycle-repair.md",
            "evidence/motion_navigation/d109-b10-lifecycle-v1/index.json",
            "evidence/motion_navigation/d109-b10-lifecycle-v1/SHA256SUMS.txt",
            "evidence/motion_navigation/d109-b10-lifecycle-v1/published-production-semantic-hashes.json",
            "evidence/motion_navigation/d109-b10-lifecycle-v1/d6-snapshot-materialization/result.json",
            "docs/motion_navigation/decisions/0110-repair-close-diagnostics-and-measure-solver-tail.md",
            "docs/motion_navigation/stages/d110-evidence-and-lifecycle-repair-plan.md",
            "docs/motion_navigation/acceptance/d110-evidence-and-lifecycle-repair.md",
            "evidence/motion_navigation/d110-evidence-lifecycle-v1/README.md",
            "evidence/motion_navigation/d110-evidence-lifecycle-v1/index.json",
            "evidence/motion_navigation/d110-evidence-lifecycle-v1/SHA256SUMS.txt",
            "evidence/motion_navigation/d110-evidence-lifecycle-v1/published-production-semantic-hashes.json",
            "evidence/motion_navigation/d110-evidence-lifecycle-v1/windows-regression/summary.json",
            "evidence/motion_navigation/d110-evidence-lifecycle-v1/fabric-b10/summary.json",
        )
        self.assertEqual(
            tuple(path for path in required if path not in selected_paths),
            (),
        )

    def test_f2_snapshot_selects_formal_tools_and_final_evidence(self):
        selected_paths = {
            path.as_posix() for path in collect_export_files(load_manifest())
        }
        required = (
            "scripts/f2_ground_route_evidence.py",
            "scripts/f2_ground_route_quality.py",
            "scripts/f2_ground_route_runtime.py",
            "scripts/f2r_piecewise_evidence.py",
            "tests/sim/manifests/navigation-product-r28-v8.json",
            "tests/motion_nav/test_f2r_piecewise_completion.py",
            "evidence/motion_navigation/action-spec-hardening-v1/metrics-baseline-corrected.json",
            "evidence/motion_navigation/F2R-piecewise-completion-v1/red/geometry/summary.json",
            "docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md",
            "docs/motion_navigation/stages/F2-non-center-ground-route-execution.md",
            "docs/motion_navigation/acceptance/F2-non-center-ground-route-execution.md",
            "docs/motion_navigation/decisions/0074-generalize-ground-route-execution-before-terminal-approach.md",
            "tests/sim/manifests/navigation-product-f2-ground-route-v1.json",
            "evidence/motion_navigation/F2-ground-route-v1/baseline/measurement-v2/error-copies.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/README.md",
            "evidence/motion_navigation/F2-ground-route-v1/final/SHA256SUMS",
            "evidence/motion_navigation/F2-ground-route-v1/final/source-consistency.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/windows/forward-final.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/windows/reverse-final.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/fabric/summary.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/fabric/trials.jsonl",
            "evidence/motion_navigation/F2-ground-route-v1/final/fabric/failures/corner-replay-red.json",
        )
        self.assertEqual(tuple(p for p in required if p not in selected_paths), ())
        self.assertNotIn('docs/superpowers/plans/2026-10-07-non-center-ground-route-execution.md', selected_paths)

    def test_motion_navigation_tests_keep_their_minimum_shared_dependencies(self):
        required = (
            "config/motion-navigation/reference-SHA256SUMS.txt",
            "config/motion-navigation/reference-scenes-v1.json",
            "config/motion-navigation/reference-versions-v1.json",
            "experiments/navigation-floating-wall-mazes-v1/layouts/maze_03_floating.json",
            "tests/observation_v2_fixtures.py",
            "tests/observation_v3_fixtures.py",
            "mc2p/contracts/action.py",
            "mc2p/contracts/action_receipt.py",
            "mc2p/contracts/action_v1.py",
            "mc2p/contracts/common.py",
            "mc2p/contracts/observation.py",
            "mc2p/contracts/observation_request_v3.py",
            "mc2p/contracts/observation_v2.py",
            "mc2p/contracts/observation_v3.py",
            "mc2p/backends/client_observation_payload.py",
            "mc2p/backends/client_observation_payload_v3.py",
        )

        missing = tuple(path for path in required if not (ROOT / path).is_file())

        self.assertEqual(missing, ())

    def test_standalone_manifest_selects_reviewed_runtime_dependencies(self):
        selected = collect_export_files(load_manifest())
        required = (
            "tests/test_navigation_motion.py",
            "tests/test_visible_equipment_projection.py",
            "tests/test_action_arbiter_v1.py",
            "tests/test_player_runtime_v1.py",
            "tests/test_runtime_failure_disposition.py",
            "tests/test_engagement_memory.py",
            "tests/test_fixed_melee_driver.py",
            "tests/test_moving_melee_driver.py",
            "tests/test_c1_navigation_session.py",
            "tests/test_b10_runtime_probe.py",
            "tests/test_b11_world_change_runtime.py",
            "tests/test_b12_attack_evidence_runtime.py",
            "tests/test_b12a_fabric_runtime.py",
            "tests/test_b12a_runtime_injection_acceptance.py",
            "tests/test_b12b_partial_combat_runtime.py",
            "tests/test_b12b_runtime_injection_acceptance.py",
            "tests/test_fabric_deployment_probe.py",
            "tests/test_surface_depth_tiles.py",
            "tests/test_surface_depth_cache.py",
            "tests/test_surface_observation_v3.py",
            "tests/test_client_block_observation_v3.py",
            "tests/test_visual_air_runtime.py",
            "tests/observation_v2_fixtures.py",
            "tests/observation_v3_fixtures.py",
            "mc2p/runtime/player_runtime_v1.py",
            "mc2p/backends/deployment_transport.py",
            "scripts/export_motion_navigation_standalone.py",
            "scripts/b11_world_change_runtime.py",
            "scripts/b12_attack_evidence_runtime.py",
            "scripts/b12a_fabric_runtime.py",
            "scripts/b12b_partial_combat_runtime.py",
            "scripts/java/FollowPlaygroundInitializer.java",
            "scripts/surface_depth_probe/build.py",
            "scripts/surface_depth_probe/native.py",
        )
        selected_paths = {item.as_posix() for item in selected}

        self.assertEqual(
            tuple(path for path in required if path not in selected_paths),
            (),
        )

    def test_standalone_manifest_contains_formal_probe_sources_not_old_ray_evidence(self):
        selected_paths = {
            item.as_posix() for item in collect_export_files(load_manifest())
        }
        frozen_sources = set()
        for name, value in vars(deployment_probe).items():
            if (name.endswith("_SOURCES") and isinstance(value, tuple)
                    and all(isinstance(item, str) for item in value)):
                frozen_sources.update(value)

        self.assertEqual(
            tuple(sorted(frozen_sources - selected_paths)),
            (),
        )
        retired = {
            "scripts/active_perception_diagnostics.py",
            "scripts/active_perception_evidence.py",
            "scripts/active_perception_metrics.py",
            "scripts/active_perception_world_change.py",
            "scripts/active_perception_world_change_evidence.py",
            "scripts/block_observation_v3_sources.py",
            "scripts/block_parity_evidence.py",
            "scripts/follow_playground_evidence.py",
            "scripts/follow_playground_long.py",
            "scripts/follow_playground_session.py",
            "scripts/normal_navigation_evidence.py",
            "scripts/probe_follow_playground.py",
            "scripts/probe_normal_navigation.py",
            "scripts/probe_observation_v2_visibility.py",
            "scripts/smoke_test_craftground_structured.py",
        }
        self.assertFalse(retired.intersection(selected_paths))
        self.assertFalse(any(
            path.startswith("mc2p/backends/legacy_ray_profile3/")
            for path in selected_paths
        ))
        self.assertFalse(any(
            path.startswith("tests/java/legacy_ray_profile3/")
            for path in selected_paths
        ))
        self.assertFalse(any(
            path.startswith("mc2p/backends/craftground")
            or (path.startswith("mc2p/backends/runtime_overlays/mc121_structured/")
                and path != "mc2p/backends/runtime_overlays/mc121_structured/BehaviorPlayerMixin.java")
            or path.startswith("tests/java/legacy_craftground/")
            for path in selected_paths
        ))
        self.assertIn(
            "mc2p/backends/runtime_overlays/mc121_structured/BehaviorPlayerMixin.java",
            selected_paths,
        )
        self.assertFalse(any(
            path.startswith("mc2p/skills/follow_playground")
            or path in {
                "mc2p/skills/normal_control_capabilities.py",
                "scripts/navigation_motion_evidence.py",
                "scripts/target_search_evidence.py",
            }
            for path in selected_paths
        ))


if __name__ == "__main__":
    unittest.main()
