"""Read-only call attribution for D121's control-frame comparison gate."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, contextmanager
import inspect
import json
import pickle
from pathlib import Path
import sys
import threading
from time import perf_counter_ns
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@contextmanager
def count_control_calls():
    from mc2p.motion_nav import physics_1_21, prepared_motion_proof, motion_solver
    functions = {
        physics_1_21.step: "physics",
        prepared_motion_proof.canonical_motion_digest: "digest",
        prepared_motion_proof.canonical_motion_bytes: "canonical",
        prepared_motion_proof.canonical_bundle_bytes: "canonical_bundle",
    }
    counts = Counter()
    main_thread = threading.get_ident()

    def counted(original, kind):
        def invoke(*args, **kwargs):
            if threading.get_ident() == main_thread:
                caller = inspect.currentframe().f_back
                observed_tick = args[0].movement_tick_id + 1 if kind == "physics" else None
                counts[(kind, Path(caller.f_code.co_filename).name, caller.f_code.co_name, observed_tick)] += 1
            return original(*args, **kwargs)
        return invoke

    with ExitStack() as stack:
        for name, module in tuple(sys.modules.items()):
            if not name.startswith("mc2p.") or module is None:
                continue
            for attribute, value in tuple(vars(module).items()):
                if callable(value):
                    for function, kind in functions.items():
                        if value is function:
                            stack.enter_context(patch.object(module, attribute, counted(function, kind)))
        original = motion_solver.VerifiedMotionResult.start_variant
        stack.enter_context(patch.object(motion_solver.VerifiedMotionResult, "start_variant", counted(original, "variant")))
        stack.enter_context(patch.object(pickle, "dumps", counted(pickle.dumps, "pickle")))
        stack.enter_context(patch.object(pickle, "loads", counted(pickle.loads, "pickle")))
        yield counts


def _rows(counts):
    return [{"kind": kind, "file": file, "caller": caller, "observed_tick": tick, "calls": calls}
            for (kind, file, caller, tick), calls in sorted(counts.items())]


def baseline():
    from scripts.d116_handoff_timeline import run_representative_timeline
    from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
    original = RuntimeNavigationDriver.tick
    rows = []
    def tick(driver, *args, **kwargs):
        tick_id = driver.runtime.observation.self_state.value.movement_tick_id
        if tick_id not in (20, 35, 36, 37):
            return original(driver, *args, **kwargs)
        with count_control_calls() as counts:
            begin = perf_counter_ns()
            result = original(driver, *args, **kwargs)
            elapsed = perf_counter_ns() - begin
        rows.append({"source_tick": tick_id, "elapsed_ms": elapsed / 1e6, "calls": _rows(counts)})
        return result
    with patch.object(RuntimeNavigationDriver, "tick", tick):
        result = run_representative_timeline(max_ticks=39)
    return {"mode": "baseline-0e6b0141", "frames": rows,
            "timeline_boundary": [row for row in result["timeline"] if row["movement_tick"] == 20]}


def current():
    from scripts.d121_handoff_execution_probe import ExecutionCase
    from mc2p.contracts.motion_activation import ActivationResult, ActivationStatus, ActivationRejectReason
    from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
    rows = []
    for branch in ("ready", "activation_reject", "cancel"):
        case = ExecutionCase()
        try:
            case.prepare_successor()
            for _ in range(26):
                case.tick()
            if branch == "cancel":
                case.session.cancel("hotpath-attribution")
            with ExitStack() as stack:
                if branch == "activation_reject":
                    reject = ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
                    stack.enter_context(patch.object(ExecutionSupervisor, "commit_activation", return_value=reject))
                for _ in range(1 if branch == "ready" else 11):
                    tick_id = case.backend.movement_tick
                    with count_control_calls() as counts:
                        begin = perf_counter_ns()
                        result = case.tick()
                        elapsed = perf_counter_ns() - begin
                    rows.append({"branch": branch, "source_tick": tick_id,
                        "elapsed_ms": elapsed / 1e6, "calls": _rows(counts),
                        "failure": result.report.failure})
                    if case.driver.source is None:
                        break
        finally:
            case.close()
    return {"mode": "current", "frames": rows}


if __name__ == "__main__":
    result = baseline() if "--baseline" in sys.argv else current()
    if "--output" in sys.argv:
        output = Path(sys.argv[sys.argv.index("--output") + 1])
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"output": str(output), "frames": result["frames"]}, indent=2))
    else:
        print(json.dumps(result, indent=2))
