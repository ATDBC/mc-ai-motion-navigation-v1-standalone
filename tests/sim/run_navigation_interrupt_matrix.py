"""Run the frozen review-20 interruption and late-input family."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import subprocess

from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS


SCHEMA = "mc2p.navigation-interrupt-matrix.v1"
TERMINAL = {"success", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class InterruptCase:
    scenario: str
    revised_goal: tuple[float, float, float]
    interruption: str
    interrupt_tick: int
    late_offset: int | None
    maximum_ticks: int

    @property
    def identifier(self) -> str:
        late = "none" if self.late_offset is None else str(self.late_offset)
        return (
            f"{self.scenario}-{self.interruption}-at-{self.interrupt_tick}"
            f"-late-{late}"
        )


def expand_cases(document: dict) -> tuple[InterruptCase, ...]:
    if document.get("schema_version") != SCHEMA:
        raise ValueError("unsupported interruption matrix manifest")
    first = document.get("interrupt_tick_start")
    last = document.get("interrupt_tick_end")
    maximum_ticks = document.get("maximum_ticks")
    if (type(first) is not int or type(last) is not int or last < first
            or type(maximum_ticks) is not int or maximum_ticks < 1):
        raise ValueError("invalid interruption matrix bounds")
    interruptions = document.get("interruptions")
    late_offsets = document.get("late_offsets")
    if interruptions != ["revise", "cancel"] or late_offsets != [None, 1, 2]:
        raise ValueError("interruption matrix axes are not frozen")
    known = {scenario.name for scenario in SCENARIOS}
    cases = []
    for entry in document.get("scenarios", []):
        name = entry.get("name")
        revised = entry.get("revised_goal")
        if (name not in known or not isinstance(revised, list)
                or len(revised) != 3):
            raise ValueError("invalid interruption matrix scenario")
        goal = tuple(float(value) for value in revised)
        for interruption in interruptions:
            for tick in range(first, last + 1):
                for offset in late_offsets:
                    cases.append(InterruptCase(
                        name, goal, interruption, tick, offset, maximum_ticks,
                    ))
    return tuple(cases)


def _revise_to(position: tuple[float, float, float]):
    def action(context) -> None:
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal(
            "goal", 2, goal, context.clock[0],
            damage_budget=TaskDamageBudget(
                context.risk_policy_id, context.damage_points,
            ),
        )
        context.goal_state = goal
        context.goal_position = position
    return action


def _cancel(context) -> None:
    context.driver.release("interrupt_matrix_cancel")


def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], text=True, capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def run_matrix(manifest: Path, output_root: Path) -> dict:
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite matrix: {output_root}")
    raw = manifest.read_bytes()
    cases = expand_cases(json.loads(raw))
    scenarios = {scenario.name: scenario for scenario in SCENARIOS}
    output_root.mkdir(parents=True)
    counts = Counter()
    results = []
    for case in cases:
        base = scenarios[case.scenario]
        action = _revise_to(case.revised_goal) if case.interruption == "revise" else _cancel
        late_ticks = (
            frozenset()
            if case.late_offset is None
            else frozenset({case.interrupt_tick + case.late_offset})
        )
        configured = replace(
            base,
            name=case.identifier,
            events=[Event(
                case.interruption,
                lambda context, tick=case.interrupt_tick: context.tick >= tick,
                action,
            )],
            perturbations=Perturbations(late_ticks=late_ticks),
            max_ticks=case.maximum_ticks,
        )
        result = run(configured)
        terminal = result.outcome in TERMINAL
        counts["terminal" if terminal else "nonterminal"] += 1
        if result.violations:
            counts["with_violations"] += 1
        if not result.verification_complete:
            counts["verification_incomplete"] += 1
        item = {
            "case": asdict(case),
            "outcome": result.outcome,
            "reason": result.reason,
            "ticks": result.ticks,
            "damage": result.damage,
            "violations": result.violations,
            "events": result.events,
            "terminal": terminal,
            "verification": asdict(result.verification) if result.verification else None,
        }
        results.append(item)
        if not terminal or result.violations or not result.verification_complete:
            (output_root / f"{case.identifier}.json").write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    summary = {
        "schema_version": SCHEMA,
        "source_commit": _commit(),
        "manifest": str(manifest),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "counts": dict(counts),
        "results": results,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    summary = run_matrix(args.manifest, args.output_root)
    print(json.dumps(summary["counts"], sort_keys=True))
    return int(any(summary["counts"].get(key, 0) for key in
                   ("nonterminal", "with_violations", "verification_incomplete")))


if __name__ == "__main__":
    raise SystemExit(main())
