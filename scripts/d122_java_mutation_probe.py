"""Compile real Java D122 tests against bounded source mutations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / ".venv/Library/bin"
MAPPED = ROOT / ".gradle/caches/fabric-loom/minecraftMaven/net/minecraft/minecraft-merged/1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2/minecraft-merged-1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2.jar"
ACTION = ROOT / "mc2p/backends/runtime_overlays/mc121_actions"


def run_case(name: str, relative: str, old: str, new: str, harness: str, sources: tuple[str, ...], cp: str = "") -> dict:
    with TemporaryDirectory(prefix="mc2p-d122-mutation-") as directory:
        temp = Path(directory)
        copied = {}
        for source in sources:
            src = ACTION / source
            dst = temp / source
            shutil.copyfile(src, dst)
            copied[source] = dst
        target = copied[relative]
        text = target.read_text("utf-8")
        if text.count(old) != 1:
            raise RuntimeError(f"mutation anchor drift: {name}")
        target.write_text(text.replace(old, new), "utf-8", newline="\n")
        out = temp / "classes"
        out.mkdir()
        compile_result = subprocess.run(
            [str(JAVA / "javac.exe"), "-encoding", "UTF-8", "-cp", cp, "-d", str(out),
             *(str(copied[item]) for item in sources), str(ROOT / "tests/java" / harness)],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        if compile_result.returncode:
            return {"name": name, "detected": True, "phase": "compile"}
        tested = subprocess.run(
            [str(JAVA / "java.exe"), "-cp", str(out) + (";" + cp if cp else ""), Path(harness).stem],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        return {"name": name, "detected": tested.returncode != 0, "phase": "assertion"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    schedule = ("ClientTimingSchedule.java",)
    input_sources = ("ClientRequestGate.java", "ClientTimingSchedule.java", "ClientBehaviorInput.java")
    action_sources = ("ClientActionRequest.java",)
    admission_sources = ("ClientActionRequest.java", "ClientRequestGate.java", "ClientTimingAdmission.java")
    rejection_sources = ("ClientActionRequest.java", "ClientRequestGate.java", "ClientTimingSchedule.java",
                         "ClientTimingAdmission.java", "ClientBehaviorInput.java")
    gson = str(next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar")))
    cases = [
        ("late_window_check", "ClientTimingSchedule.java", "movementTick > pending.latest", "false", "ClientTimingScheduleTest.java", schedule, ""),
        ("rejected_pending_clears_default", "ClientTimingSchedule.java", "pending = null;\n        appliedPending = null;\n        pendingPrepared = false;", "pending = null;\n        appliedPending = null;\n        fallback = null;\n        pendingPrepared = false;", "ClientTimingScheduleTest.java", schedule, ""),
        ("default_duration_shortened", "ClientTimingSchedule.java", "defaultRemaining--;", "defaultRemaining = 0;", "ClientTimingScheduleTest.java", schedule, ""),
        ("pending_payload_reactivated", "ClientTimingSchedule.java", "DefaultPlan next = pending.nextDefault;\n        pending = null;", "DefaultPlan next = pending.nextDefault;\n        // pending retained", "ClientBehaviorInputTest.java", input_sources, str(MAPPED)),
        ("activation_rejection_committed", "ClientBehaviorInput.java", "if (activationObserver.activate(selected.activationPayload())) {", "if (true) {", "ClientBehaviorInputTest.java", input_sources, str(MAPPED)),
        ("successor_default_dropped", "ClientTimingSchedule.java", "DefaultPlan next = pending.nextDefault;\n        pending = null;", "DefaultPlan next = null;\n        pending = null;", "ClientTimingScheduleTest.java", schedule, ""),
        ("stale_sneak_projection", "ClientBehaviorInput.java", "sneaking = sneak && allowed.getAsBoolean() && (timed || gate.leaseActive(now));", "if (sneak && allowed.getAsBoolean() && (timed || gate.leaseActive(now))) sneaking = true;", "ClientBehaviorInputTest.java", input_sources, str(MAPPED)),
        ("lease_replayed_without_new_prepare", "ClientBehaviorInput.java", "if (!defaultActive) gate.endControlTick();", "if (!defaultActive) { /* mutation: retain lease */ }", "ClientBehaviorInputTest.java", input_sources, str(MAPPED)),
        ("future_response_released_early", "ClientTimingSchedule.java", "event(\"window_pending\", admittedAtTick, episode, sequence);\n        holdResponse = true;", "event(\"window_pending\", admittedAtTick, episode, sequence);\n        holdResponse = false;", "ClientTimingScheduleTest.java", schedule, ""),
        ("fallback_armed_repeated", "ClientTimingSchedule.java", "&& movementTick < fallback.activationTick && !fallbackArmedReported", "&& movementTick < fallback.activationTick", "ClientTimingScheduleTest.java", schedule, ""),
        ("default_cut_by_wall_expiry", "ClientBehaviorInput.java", "else if (gate.wallClockExpired(clock.getAsLong()) && !timing.hasFallback()) clear();", "else if (gate.wallClockExpired(clock.getAsLong())) { timing.cancelAll(); clear(); }", "ClientBehaviorInputTest.java", input_sources, str(MAPPED)),
        ("default_relation_not_checked_at_admission", "ClientActionRequest.java", "|| safeDefault.activationTick != base + ticks)", "|| false)", "ClientActionTimingDecodeTest.java", action_sources, str(next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar")))),
        ("default_relation_not_checked_at_activation", "ClientTimingSchedule.java", "|| next.activationTick != movementTick + pending.ticks)", "|| false)", "ClientTimingScheduleTest.java", schedule, ""),
        ("permission_revoke_hides_pending", "ClientTimingSchedule.java", "if (pending != null) event(\"window_missed\", movementTick, pending.episode, pending.sequence);", "if (false) event(\"window_missed\", movementTick, pending.episode, pending.sequence);", "ClientTimingScheduleTest.java", schedule, ""),
        ("safe_default_budget_overflow", "ClientActionRequest.java", "return Long.MAX_VALUE;", "return Math.addExact(Math.multiplyExact(wait, 50_000_000L), safeDefault.wallBudgetNs);", "ClientActionTimingDecodeTest.java", action_sources, str(next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar")))),
        ("timing_rejection_skips_identity", "ClientTimingAdmission.java", "String gateDenied = gate.admitPending(action.episode(), action.sequence(), action.observation(),\n                latestObservation, budget, action.ticks());", "String gateDenied = null;", "ClientTimingAdmissionTest.java", admission_sources, str(next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar")))),
        ("rejected_pending_clears_incumbent", "ClientTimingSchedule.java", "pending = null;\n            pendingPrepared = false;\n            if (fallback == null) holdResponse = false;", "pending = null;\n            pendingPrepared = false;\n            fallback = null;\n            if (fallback == null) holdResponse = false;", "ClientTimingScheduleTest.java", schedule, ""),
        ("rejected_response_released_early", "ClientTimingSchedule.java", "if (fallback == null) return false;\n        holdResponse = true;", "if (fallback == null) return false;\n        holdResponse = false;", "ClientTimingScheduleTest.java", schedule, ""),
        ("hard_release_replaces_typed_hold", "ClientTimingAdmission.java", "return hardRelease ? RejectionDisposition.HARD_RELEASE : RejectionDisposition.HOLD_INCUMBENT;", "return RejectionDisposition.HARD_RELEASE;", "ClientTimingAdmissionTest.java", admission_sources, str(next((ROOT / ".gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1").glob("*/*.jar")))),
        ("duplicate_rejection_clears_incumbent", "ClientBehaviorInput.java",
         "return installedThisCall && timing.rejectInstalled(sequence);",
         "return timing.rejectInstalled(sequence);", "ClientTimingRejectIsolationTest.java",
         rejection_sources, str(MAPPED) + ";" + gson),
    ]
    results = [run_case(*case) for case in cases]
    value = {"schema_version": "mc2p.d122-java-mutations.v1", "cases": results,
             "all_detected": all(item["detected"] for item in results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n", "utf-8")
    if not value["all_detected"]:
        raise SystemExit("one or more Java timing mutations survived")
    print(f"D122_JAVA_MUTATIONS_OK:{len(results)}")


if __name__ == "__main__":
    main()
