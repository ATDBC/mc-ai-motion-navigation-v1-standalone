"""Real fixed-scene sensor probe. Fixture truth is evaluator-only, never actor capability."""
from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
from mc2p.backends.craftground_runtime import CraftGroundClockModeV0, CraftGroundObservationModeV0, load_sandbox_manifest
from mc2p.contracts.action_v1 import ActionSnapshotV1, ClickSlotV1, CloseScreenV1, InteractBlockV1, LookV1, MovementV1, SelectHotbarV1
from mc2p.contracts.report import FailureCodeV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.observation_request_v3 import OBSERVATION_V3, ObservationRequestV3
from mc2p.runtime.trace import trace_projection
from scripts.client_behavior_probe_support import sandbox_provenance
from scripts.formal_observation_v3_evidence import validate_formal_observations_v3
from scripts.block_parity_evidence import parity_environment,parity_file_check,frozen_probe_sources
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.probe_craftground_timing_parallel import run_bounded_process
from scripts.probe_observation_v2_gui import _port_free, _run_id
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail
from scripts.visibility_fixture_world import _no_links, build_visibility_fixture, install_visibility_fixture

STAGES = ("front", "turned_away", "turned_back", "partial_occlusion", "full_occlusion", "occlusion_return",
          "out_of_range", "range_return", "cow_targeted", "cow_off_target", "ore_exposed", "ore_covered", "final_neutral")


def step_with_profile(backend, action, deadline: int, *, field_profile: str):
    """Keep the requested field profile explicit at the real backend call boundary."""
    return backend.step(action, deadline, observation_request=ObservationRequestV3(field_profile))


from scripts.visibility_v3_scenario import await_fixture_front, run_visibility_scenario


def evaluate_visibility_stages(stages: dict) -> list[dict]:
    def entities(stage, kind):
        return [e for e in stages.get(stage, {}).get("perception", {}).get("value", {}).get("visible_entities", [])
                if e["entity_type"] == "minecraft:" + kind]

    visible = ("front", "turned_back", "partial_occlusion", "occlusion_return", "range_return")
    anchors = [entities(stage, "armor_stand") for stage in visible]
    cows = [entities(stage, "cow") for stage in ("front", "cow_targeted", "cow_off_target")]
    complete = set(stages) == set(STAGES)
    sequences = [stages[s]["sequence_id"] for s in STAGES if s in stages]
    ore = [any(block["block_id"] == "minecraft:diamond_ore"
               for block in stages.get(s, {}).get("perception", {}).get("value", {}).get("blocks", []))
           for s in ("ore_exposed", "ore_covered")]
    formal_v3 = complete and all(stage.get("schema_version") == "mc2p.observation.v3"
        and stage.get("privileged_fields_present") == []
        and "blocks" in stage.get("perception", {}).get("value", {})
        and "block_rays" not in stage.get("perception", {}).get("value", {}) for stage in stages.values())
    def target(stage):
        group = stages.get(stage, {}).get("targeting", {})
        return group.get("value") if group.get("status") == "valid" else None
    cow_target, cow_off = target("cow_targeted"), target("cow_off_target")
    ore_target, cover_target = target("ore_exposed"), target("ore_covered")
    targeting = all(stages.get(stage, {}).get("field_profile") == "interaction_v1"
        for stage in ("cow_targeted", "cow_off_target", "ore_exposed", "ore_covered"))
    targeting = targeting and (len(cows[1]) == 1 and cow_target is not None and cow_target.get("hit_kind") == "entity"
        and cow_target.get("entity_ref") == cows[1][0]["track_id"] and cow_off is not None
        and len(cows[2])==1
        and not (cow_off.get("hit_kind") == "entity" and cow_off.get("entity_ref") == cows[2][0]['track_id'])
        and ore_target is not None and ore_target.get("hit_kind") == "block"
        and ore_target.get("block_position") == [-2, -61, 3] and ore_target.get("face") == "up"
        and cover_target is not None and cover_target.get("hit_kind") == "block"
        and cover_target.get("block_position") == [-2, -60, 3])
    checks = {
        "all_stages_one_ordered_episode": complete and len({s["episode_id"] for s in stages.values()}) == 1
            and all(type(s) is int for s in sequences) and all(a < b for a, b in zip(sequences, sequences[1:])),
        "anchor_visible_and_reidentified": all(len(a) == 1 for a in anchors)
            # Range excursion may unload/recreate client entities. No UUID-based identity oracle.
            and len({e["track_id"] for a in anchors[:-1] for e in a}) == 1
            and all(e["display_name"] == "visible-anchor" for a in anchors for e in a),
        "anchor_absent_when_hidden": all(s in stages and not entities(s, "armor_stand")
                                        for s in ("turned_away", "full_occlusion", "out_of_range")),
        "native_equipment_visibility": bool(anchors[0]) and any(
            slot == "main_hand" and item == {"empty": False, "item_id": "minecraft:diamond_sword"}
            for e in anchors[0] for slot, item in e["equipment"])
            and all(len(c) == 1 for c in cows) and all(not e["equipment"] for c in cows for e in c),
        "cow_target_name_visibility": all(len(c) == 1 for c in cows)
            and [c[0]["display_name"] for c in cows if c] == [None, "target-only-name", None],
        "independent_targeting_transitions": targeting,
        "native_v3_block_state_evidence": formal_v3,
        "ore_cover_removes_all_ore_blocks": all(s in stages for s in ("ore_exposed", "ore_covered")) and ore == [True, False],
    }
    return [{"name": k, "passed": bool(v)} for k, v in checks.items()]


def evaluate_execution(*, receipts: list[dict], rows: list[dict], observation_count: int,
                       process_stopped: bool, port_released: bool) -> list[dict]:
    checks = {
        "formal_actions_dispatched_without_device_callbacks": bool(receipts) and all(
            r.get("status") in {"executed", "confirmed_local", "pending_confirmation"}
            and r.get("execution_path") == "client_behavior_v1" and r.get("on_client_thread") is True
            and r.get("action_keyboard_callbacks") == r.get("action_mouse_callbacks") == 0 for r in receipts),
        "no_gui_drawing_completed": bool(receipts) and all(r.get("handled_screen_render_completions") == 0 for r in receipts),
        "continuous_zero_image_hidden_observations": len(rows) == observation_count == len(receipts) + 1 and observation_count > 1
            and len({r["session_id"] for r in rows}) == 1
            and [r["observation_sequence"] for r in rows] == list(range(1, observation_count + 1)) and all(
                r["image_bytes"] == r["framebuffer_capture_calls"] == r["image_encode_calls"] == 0
                and r["render_world_completions"] == 0 and r["window_visible"] is False
                and r["window_visible_at_creation"] is False for r in rows),
        "process_and_port_clean": process_stopped is True and port_released is True,
    }
    return [{"name": k, "passed": bool(v)} for k, v in checks.items()]


class VisibilityFixtureBackend(CraftGroundBehaviorBackendV1):
    """Test-only existing-save configuration, never part of the common actor executor."""
    def __init__(self, *, level_name: str, **kwargs):
        super().__init__(**kwargs)
        self.level_name = level_name
        self.reset_attempted = False

    @property
    def supported_scenarios(self):
        return frozenset({"visibility-fixture"})

    def _configure_initial_environment(self, initial, scenario_id):
        initial.level_display_name_to_play = self.level_name
        initial.bonus_chest = False

    def reset(self, request):
        if self.reset_attempted:
            return self._reset_failure(request, FailureCodeV0.CONFIGURATION,
                "visibility fixture requires a fresh verified save and client for every reset", retryable=False)
        self.reset_attempted = True
        return super().reset(request)


def run_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float,block_parity: bool=False) -> int:
    if os.environ.get('MC2P_BLOCK_PARITY_DIAGNOSTICS')!=('1' if block_parity else None):
        raise ValueError('parity environment does not match explicit probe configuration')
    manifest = load_sandbox_manifest(sandbox)
    if manifest.clock_mode is not CraftGroundClockModeV0.REFERENCE_20_TPS or manifest.observation_mode is not CraftGroundObservationModeV0.STRUCTURED_ONLY:
        raise ValueError("visibility probe requires a current reference_20_tps structured_only sandbox")
    if manifest.observation_schema_version != OBSERVATION_V3:
        raise ValueError("visibility probe requires an Observation V3 sandbox")
    source_before=frozen_probe_sources()
    deadline = time.perf_counter_ns() + round((timeout - 15) * 1e9)
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    backend, failure, fixture = None, None, None
    stages, checks, receipts, cleanup_failures, sequences, observations = {}, [], [], [], [], []
    step = None
    try:
        fixture = build_visibility_fixture(run_dir / "fixture", seed=seed, level_name="mc2p-visibility-" + run_dir.name.lower())
        # Check each existing ancestor BEFORE mkdir; the installer cannot undo earlier writes.
        _no_links(sandbox)
        game_run = sandbox / "run"
        if not game_run.exists(): game_run.mkdir()
        _no_links(game_run)
        saves = game_run / "saves"
        if not saves.exists(): saves.mkdir()
        _no_links(saves)
        installed = install_visibility_fixture(run_dir / "fixture", saves)
        write_json_atomic(run_dir / "fixture-provenance.json", {"manifest": fixture, "installed_save": str(installed)})
        backend = VisibilityFixtureBackend(level_name=fixture["level_name"], port=port, runtime_env_path=sandbox,
            clock_mode=manifest.clock_mode, observation_mode=manifest.observation_mode,
            observation_schema_version=OBSERVATION_V3)
        episode = f"visibility-{seed}"
        reset = backend.reset(ResetRequestV0("visibility-reset", episode, "visibility-fixture", seed, deadline))
        if not reset.succeeded or reset.observation is None:
            raise RuntimeError(f"visibility reset failed: {reset.failure}")
        observation = reset.observation

        def record():
            sequences.append((observation.episode_id, observation.sequence_id))
            projected = trace_projection(observation)
            observations.append(projected)
            append_jsonl(run_dir / "observations.jsonl", projected)
            if observation.is_dead.value is not False:
                raise RuntimeError("fixture player dead or unavailable")

        def step(label, *, operation=None, movement=MovementV1(), look=LookV1(), field_profile="navigation_v1"):
            nonlocal observation
            if len(receipts) >= 1800:
                raise TimeoutError("visibility action budget exceeded")
            action = ActionSnapshotV1(episode, len(receipts), observation.sequence_id, deadline,
                                      movement=movement, look=look, operation=operation)
            append_jsonl(run_dir / "requests.jsonl", {"label": label, "action": trace_projection(action)})
            observation = step_with_profile(backend, action, deadline, field_profile=field_profile).observation
            receipt = backend.last_behavior_receipt
            receipts.append(receipt)
            append_jsonl(run_dir / "receipts.jsonl", {"label": label, "receipt": receipt})
            record()
            if receipt["status"] not in {"executed", "confirmed_local", "pending_confirmation"}:
                raise RuntimeError(f"formal action failed: {label}: {receipt['status']}/{receipt['reason']}")
            return receipt

        def milestone(label):
            stages[label] = trace_projection(observation)
            append_jsonl(run_dir / "stages.jsonl", {"label": label, "observation": stages[label]})
            print(f"VISIBILITY_STAGE={label} sequence={observation.sequence_id}", flush=True)

        record()
        checks.extend(run_visibility_scenario(lambda: observation, step, milestone, run_dir))
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
        (run_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if backend is not None:
            if failure is not None and step is not None:
                try: step("failure_neutral")
                except Exception as error:
                    append_jsonl(run_dir / "recovery.jsonl", {"neutral_not_confirmed": str(error), "recovery": "close_and_recreate"})
            try: backend.close()
            except Exception as error: cleanup_failures.append(f"{type(error).__name__}: {error}")
    rows = []
    try:
        rows = _read_diagnostic_tail(diagnostic, offset)
        for row in rows: append_jsonl(run_dir / "jvm-diagnostics.jsonl", row)
    except Exception as error:
        failure = failure or {"type": type(error).__name__, "message": str(error)}
    cleanup = backend.cleanup_status if backend is not None else None
    checks.extend(evaluate_visibility_stages(stages))
    checks.append(parity_file_check(sandbox/'run/mc2p-block-parity.jsonl',observations,
                                  enabled=block_parity,require_profile_cycle=True))
    checks.append({"name": "all_formal_observations_are_native_v3", "passed":
                   bool(observations) and not validate_formal_observations_v3(observations)})
    checks.extend(evaluate_execution(receipts=receipts, rows=rows, observation_count=len(sequences),
        process_stopped=cleanup is not None and cleanup.process_stopped, port_released=cleanup is not None and cleanup.port_released))
    checks.append({"name": "formal_observation_episode_and_sequence_continuity", "passed": bool(sequences)
        and len({e for e, s in sequences}) == 1 and [s for e, s in sequences] == list(range(len(sequences)))})
    source_after=frozen_probe_sources()
    checks.append(dict(name='v3_python_and_java_sources_unchanged',passed=source_before==source_after))
    if failure is None and not all(c["passed"] for c in checks):
        failure = {"type": "VisibilityEvidenceFailure", "message": str([c["name"] for c in checks if not c["passed"]])}
    result = {"schema_version": "mc2p.observation-v3-visibility-probe.v1",
              "observation_schema_version": "mc2p.observation.v3", "knowledge_model": "block_state_v1",
              "field_profiles": ["navigation_v1", "interaction_v1"],
              "block_parity": block_parity,
              "core_sources_before": source_before,"core_sources_after": source_after,
              "status": "passed" if failure is None and not cleanup_failures else "failed",
              "seed": seed, "provenance": sandbox_provenance(sandbox), "fixture_manifest": fixture,
              "stage_sequences": {k: v["sequence_id"] for k, v in stages.items()}, "action_count": len(receipts),
              "observation_count": len(sequences), "checks": checks, "primary_failure": failure, "cleanup_failures": cleanup_failures,
              "limits": ["reference sensor fixture, not full timing equivalence", "no independent deployment evidence",
                         "no general equipment-slot/partial-slot visibility or bounded-track acceptance"]}
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-path", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=21001)
    parser.add_argument("--port", type=int, default=8128)
    parser.add_argument("--timeout-seconds", type=float, default=240)
    parser.add_argument("--artifacts-dir", type=Path, default=ROOT / "artifacts/observation-v3-visibility")
    parser.add_argument('--block-parity',action='store_true',help='test-only same-tick comparison, not a speed measurement')
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds < 60:
        parser.error("timeout must be finite and at least 60 seconds")
    # Preserve the user's lexical path until every existing component has been checked.
    sandbox = args.sandbox_path.absolute()
    try:
        _no_links(sandbox)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.worker:
        if args.run_dir is None: parser.error("worker requires --run-dir")
        worker_dir = args.run_dir.absolute()
        try:
            _no_links(worker_dir)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        try:
            sandbox_provenance(sandbox)
        except (RuntimeError, ValueError) as error:
            parser.error(str(error))
        return run_worker(worker_dir, sandbox, args.seed, args.port, args.timeout_seconds,
                          **({'block_parity':True} if args.block_parity else {}))
    if not _port_free(args.port):
        parser.error("requested control port is occupied")
    artifact_root = args.artifacts_dir.absolute()
    existing_ancestor = artifact_root
    while not existing_ancestor.exists():
        if existing_ancestor.parent == existing_ancestor:
            parser.error(f"output path has no existing ancestor: {artifact_root}")
        existing_ancestor = existing_ancestor.parent
    try:
        _no_links(existing_ancestor)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        sandbox_provenance(sandbox)
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))
    run_dir = artifact_root / _run_id()
    run_dir.mkdir(parents=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run-dir", str(run_dir), "--sandbox-path", str(sandbox),
               "--seed", str(args.seed), "--port", str(args.port), "--timeout-seconds", str(args.timeout_seconds)]
    if args.block_parity: command.append('--block-parity')
    supervision = run_bounded_process(command, cwd=ROOT, environment=parity_environment(dict(os.environ),args.block_parity), log_path=run_dir / "worker.log",
                                      timeout_seconds=args.timeout_seconds)
    write_json_atomic(run_dir / "supervision.json", trace_projection(supervision))
    if (run_dir / "result.json").is_file():
        result = json.loads((run_dir / "result.json").read_text("utf-8"))
    else:
        result = {"status": "failed", "primary_failure": supervision.primary_failure or "worker_result_missing",
                  "cleanup_failures": list(supervision.cleanup_failures)}
        write_json_atomic(run_dir / "result.json", result)
    # Worker evidence is not the final verdict: supervisor/port failures must also fail result.json.
    write_json_atomic(run_dir / "worker-result.json", result)
    port_free = _port_free(args.port)
    parent_ok = (supervision.return_code == 0 and supervision.primary_failure is None
                 and not supervision.cleanup_failures and supervision.process_stopped and port_free)
    result["checks"] = list(result.get("checks", [])) + [{"name": "parent_supervision_and_port_clean", "passed": parent_ok}]
    result["parent_supervision"] = trace_projection(supervision)
    result["parent_port_free"] = port_free
    if not parent_ok:
        result["status"] = "failed"
        result["primary_failure"] = result.get("primary_failure") or {"type": "SupervisorFailure",
            "message": f"code={supervision.return_code}, failure={supervision.primary_failure}, stopped={supervision.process_stopped}, port_free={port_free}, cleanup={supervision.cleanup_failures}"}
        result["cleanup_failures"] = list(result.get("cleanup_failures", [])) + list(supervision.cleanup_failures)
    write_json_atomic(run_dir / "result.json", result)
    print(f"OBSERVATION_V3_VISIBILITY_RUN_DIR={run_dir}")
    if parent_ok and result.get("status") == "passed":
        print("OBSERVATION_V3_VISIBILITY_OK")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
