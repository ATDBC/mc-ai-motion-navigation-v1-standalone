"""D122-1 mutation and optional v1 calculator comparison; no Fabric claims."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib
import io
import json
from pathlib import Path
import random
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def changed_class(module_name, class_name, old, new):
    module = importlib.import_module(module_name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise AssertionError(f"mutation target count changed: {old!r}")
    namespace = {"__name__": module_name}
    exec(compile(source.replace(old, new), module.__file__, "exec"), namespace)
    return namespace[class_name]


def mutation_checks():
    from tests import test_d122_python_timing as tests
    from tests.sim import client_timing
    vectors = [
        ("drop_window_projection", "arbiter", "ContractAndWireTests.test_movement_winner_owns_both_timing_fields",
         "first_effect_window=movement.movement_tick_window if movement is not None else None,",
         "first_effect_window=None,"),
        ("early_replaces_current", "sampler", "CalculatorTimingTests.test_future_pending_does_not_replace_default_and_response_is_held",
         "            self.pending = action\n", "            self._activate(action, tick, now)\n            self.pending = action\n"),
        ("miss_clears_default", "sampler", "CalculatorTimingTests.test_missed_window_never_jumps_and_does_not_release_response_before_default_ends",
         '                self.event("window_missed", tick, action)\n',
         '                self.event("window_missed", tick, action)\n                self.default_parent = None\n                self.current = None\n'),
        ("ordinary_revives_old_default", "sampler", "CalculatorTimingTests.test_ordinary_request_clears_old_default_permanently",
         "        self.clear()\n        self.current = action\n",
         "        old_default = (self.default_parent, self.default_remaining)\n        self.clear()\n        self.default_parent, self.default_remaining = old_default\n        self.current = action\n"),
        ("default_budget_starts_at_parent", "sampler", "SamplingTableTests.test_default_wall_budget_starts_at_activation_and_never_revives_parent",
         "            self.default_remaining = action.safe_default.valid_for_ticks\n",
         "            self.default_remaining = action.safe_default.valid_for_ticks\n            self.default_wall_deadline = now + action.safe_default.wall_budget_ns\n"),
        ("missed_response_released_early", "sampler", "CalculatorTimingTests.test_missed_window_never_jumps_and_does_not_release_response_before_default_ends",
         "        return not self._missed or self.default_parent is None\n", "        return True\n"),
    ]
    report = []
    for name, target, test_name, old, new in vectors:
        if target == "arbiter":
            mutant = changed_class("mc2p.runtime.arbiter_v1", "ActionArbiterV1", old, new)
            context = patch.object(tests, "ActionArbiterV1", mutant)
        else:
            mutant = changed_class("tests.sim.client_timing", "ClientTimingSampler", old, new)
            context = patch.object(client_timing, "ClientTimingSampler", mutant)
        with context:
            suite = unittest.defaultTestLoader.loadTestsFromName(test_name, tests)
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        row = dict(mutation=name, checks=result.testsRun,
                   failures=len(result.failures), errors=len(result.errors), detected=not result.wasSuccessful())
        if not row["detected"]:
            raise AssertionError(f"mutation survived: {name}")
        report.append(row)
    return report


def v1_comparison(reference_path):
    from tests.sim import backend as current
    from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
    source = reference_path.read_bytes()
    name = "tests.sim._d122_v1_reference"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(source, str(reference_path), "exec"), module.__dict__)
        scenarios = []
        for scenario in range(4):
            instances = []
            for implementation in (module, current):
                scene = implementation.Scene(
                    {(x, 0, z): "minecraft:stone" for x in range(-3, 5) for z in range(-3, 5)},
                    ((-3, 4), (-4, 5), (-3, 4)))
                disturbances = implementation.Perturbations(
                    late_ticks=frozenset({4, 8, 13}) if scenario & 1 else frozenset(),
                    impulses={9: (.01, 0., 0.)} if scenario & 2 else {},
                    omitted_receipt_ticks=frozenset({5}) if scenario == 3 else frozenset())
                instances.append(implementation.CalculatorBackend([10], scene, (.5, 1., .5),
                                 perturbations=disturbances, episode="ep"))
            rng = random.Random(122 + scenario)
            rows = []
            for sequence in range(30):
                movement = MovementV1(rng.choice((-1, 0, 1)), rng.choice((-1, 0, 1)),
                                      rng.random() < .1, rng.random() < .2, False)
                pair = []
                for backend in instances:
                    action = ActionSnapshotV1("ep", sequence, backend.sequence, 10_000_000_000,
                                              movement=movement, valid_for_ticks=1 + sequence % 3)
                    result = backend.step(action, action.deadline_monotonic_ns)
                    pair.append(dict(state=asdict(backend.state), receipt=asdict(result.receipt),
                                     observation=asdict(result.observation), applied=backend.applied_commands))
                if pair[0] != pair[1]:
                    raise AssertionError(f"v1 behavior differed: scenario {scenario}, sequence {sequence}")
                rows.append(pair[1])
                if sequence % 7 == 0:
                    for backend in instances:
                        backend.free_tick()
            signature = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
            scenarios.append(dict(scenario=scenario, paired_requests=len(rows), identical=True, signature=signature))
        return dict(reference_sha256=hashlib.sha256(source).hexdigest(), scenarios=scenarios)
    finally:
        del sys.modules[name]


def repair_mutation_checks():
    from tests import test_d122_python_timing_repair as tests
    from tests.sim import client_timing
    vectors = [
        ("short_wall_allowed", "contract", "test_default_budget_must_cover_nominal_proof_duration",
         "        if self.wall_budget_ns < self.valid_for_ticks * 50_000_000:\n", "        if False:\n"),
        ("wall_truncates_proved_samples", "sampler", "test_armed_default_finishes_real_ticks_despite_wall_expiry",
         "            if self.default_remaining > 0:\n", "            if self.default_remaining > 0 and now < self.default_wall_deadline:\n"),
        ("exhausted_without_sample_coverage", "ledger", "test_exhausted_before_activation_cannot_retire_parent",
         "                    if (default is None or event.movement_tick_id < default.activation_tick + default.valid_for_ticks\n                            or observed != expected):\n",
         "                    if False:\n"),
        ("cancel_receipt_not_consumed", "ledger", "test_accepted_cancel_retires_target_default_and_cancel_record",
         "            cancel_target = record.action.cancel_request_sequence_id\n", "            cancel_target = None\n"),
        ("accepted_cancel_misclassified", "ledger", "test_accepted_cancel_retires_target_default_and_cancel_record",
         "                changed.append(self._resolve(sequence, InputApplicationStatus.CANCELLED,\n",
         "                changed.append(self._resolve(sequence, InputApplicationStatus.REJECTED,\n"),
        ("cancel_leaves_pending", "ledger", "test_cancel_clears_the_whole_default_and_pending_arrangement",
         "                            and not input_application_has_finished(old)):\n",
         "                            and old.action.safe_default is not None and not input_application_has_finished(old)):\n"),
        ("takeover_sample_not_consumed", "ledger", "test_replacement_must_have_a_real_sample_and_retires_old_default",
         "            if (sequence >= current_sequence or candidate.session != session\n",
         "            if (True or sequence >= current_sequence or candidate.session != session\n"),
        ("inactive_sample_ends_default", "ledger", "test_inactive_sample_alone_cannot_end_a_proved_default",
         "                return record  # v4 exhaustion/cancel or actual takeover resolves it.\n",
         "                return self.expire(record.control_sequence, at_tick=sample.movement_tick_id)\n"),
        ("deadline_releases_unfinished_default", "backend", "test_response_deadline_cannot_release_an_unfinished_default",
         "                    and self._client_timing.default_parent is None):\n", "                    ):\n"),
        ("cancel_erases_ambiguous_history", "ledger", "test_cancel_keeps_unrelated_ambiguous_history",
         "                    if (old.control_sequence in members and old.action.episode_id == receipt.episode_id\n",
         "                    if (old.control_sequence < sequence and old.action.episode_id == receipt.episode_id\n"),
        ("replaced_pending_stays_unresolved", "ledger", "test_replaced_pending_finishes_even_when_its_successor_misses",
         "                if status is ClientTimingStatusV1.WINDOW_PENDING:\n                    retire_pending(tick, sequence)\n",
         "                if status is ClientTimingStatusV1.WINDOW_PENDING:\n"),
        ("exhausted_neutral_rearms_parent", "ledger", "test_exhausted_and_neutral_at_same_tick_do_not_rearm_parent",
         "                if (default is not None and fact.state == \"neutral\"\n                        and tick >= default.activation_tick + default.valid_for_ticks):\n",
         "                if False:\n"),
        ("exhausted_neutral_becomes_parent_command", "ledger", "test_exhausted_and_neutral_at_same_tick_do_not_rearm_parent",
         "            return record  # Post-default neutral is not another parent command.\n",
         "            pass  # Incorrectly fall through to parent command application.\n"),
    ]
    rows = []
    for name, target, test_name, old, new in vectors:
        if target == "contract":
            mutant = changed_class("mc2p.contracts.action_v1", "SafeDefaultV1", old, new)
            mutant.__post_init__.__globals__["MovementV1"] = tests.MovementV1
            context = patch.object(tests, "SafeDefaultV1", mutant)
        elif target == "sampler":
            mutant = changed_class("tests.sim.client_timing", "ClientTimingSampler", old, new)
            context = patch.object(client_timing, "ClientTimingSampler", mutant)
        elif target == "backend":
            from tests.sim import backend
            mutant = changed_class("tests.sim.backend", "CalculatorBackend", old, new)
            context = patch.object(backend.CalculatorBackend, "step", mutant.step)
        else:
            mutant = changed_class("mc2p.motion_nav.online_motion", "InputApplicationLedger", old, new)
            from mc2p.motion_nav import online_motion
            namespace = mutant.observe_receipt.__globals__
            for symbol in ("InputApplicationRecord", "InputApplicationStatus", "input_application_has_finished"):
                namespace[symbol] = getattr(online_motion, symbol)
            context = patch.object(tests, "InputApplicationLedger", mutant)
        with context:
            suite = unittest.defaultTestLoader.loadTestsFromName("TimingRepairTests." + test_name, tests)
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        row = dict(mutation=name, checks=result.testsRun, failures=len(result.failures),
                   errors=len(result.errors), detected=bool(result.failures) and not result.errors)
        if not row["detected"]:
            raise AssertionError(f"repair mutation was not caught by an assertion: {name}: {row}")
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--v1-reference-source", type=Path)
    args = parser.parse_args()
    report = dict(schema="d122.python-contract-probe.v1", mutations=mutation_checks(),
                  repair_mutations=repair_mutation_checks())
    if args.v1_reference_source is not None:
        report["v1_comparison"] = v1_comparison(args.v1_reference_source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
