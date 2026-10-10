"""P0 contracts: frozen identity, bounded hypotheses and honest outcomes."""
import ast
from dataclasses import FrozenInstanceError, replace
from enum import StrEnum
import inspect
from pathlib import Path
import unittest

from experiments.motion_navigation.trajectory_proto.contracts import (
    ApplicationEvidence, InputTier, KnownInputApplication, SearchBudget, SearchReason, SearchStatus,
    TimingBranch, TrajectorySearchRequest, TrajectorySearchResult,
)
from experiments.motion_navigation.trajectory_proto.scenarios import P0_SCENARIOS
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState, TickInput
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldSessionId, WorldView,
)
from scripts.export_motion_navigation_standalone import collect_export_files, load_manifest


ROOT = Path(__file__).resolve().parents[2]


def physics_state():
    return PhysicsState(
        ruleset_id=JAVA_1_21_RULESET.ruleset_id,
        state_schema=JAVA_1_21_RULESET.state_schema,
        session=WorldSessionId("p0-test"), movement_tick_id=10,
        position=(0.5, 1.0, 0.5), velocity_blocks_per_tick=(0.0, 0.0, 0.0),
        yaw_radians=0.0, pitch_radians=0.0, pose="standing",
        body_width=0.6, body_height=1.8, on_ground=True,
        horizontal_collision=False, vertical_collision=False,
        sprinting=False, sneaking=False, jumping_cooldown_ticks=0,
        fall_distance_blocks=0.0, movement_speed_attribute=0.1,
        step_height_blocks=0.6, gravity_attribute=0.08, jump_strength_attribute=0.42,
        food_points=20, saturation_points=5.0, game_mode="survival", status_effects=(),
        swimming=False, submerged_in_water=False, climbing=False,
        fall_flying=False, flying=False, allow_flying=False,
    )


def request():
    state = replace(physics_state(), velocity_blocks_per_tick=(0., -.0784, 0.))
    command = TickInput(1.0, 0.0, False, False, False, 0.0)
    neutral = replace(command, forward=0.)
    stamp = ObservationStamp(state.session, 0, 0, "contract-fixture", 0)
    facts = {(x, y, z): CellFact(CellKnowledge.BLOCK if y == 0 else CellKnowledge.AIR,
             stamp, BlockGeometry.full_cube("minecraft:stone") if y == 0 else None)
             for x in range(-2, 3) for y in range(-2, 5) for z in range(-2, 4)}
    local = PhysicsWorldView(WorldView.detached(state.session, 3, 0, facts), JAVA_1_21_RULESET)
    late = step(state, neutral, local, JAVA_1_21_RULESET).next_state
    return TrajectorySearchRequest(
        request_id="request-1", entry_states=(state, late),
        world_session=state.session, geometry_revision=3,
        goal=GoalState(Aabb(0, 1, 2, 1, 1.1, 3), GoalSupport.SOLID,
                       frozenset({MovementMode.WALK}), frozenset({"standing"}), 0.0),
        goal_revision=4, task_damage_budget=TaskDamageBudget(),
        anchor_id="anchor-10", input_ledger_id="ledger-8",
        budget=SearchBudget(200, 2000, 40, 2),
        timing_branches=(TimingBranch.ON_TIME, TimingBranch.LATE_ONE_TICK),
        allowed_effect_ticks=(11, 12), input_prefix=(command,),
        branch_preludes=((), (application(neutral, 11, 99),)),
        first_candidate_control_sequence=100,
        supported_inputs=(command, neutral),
        input_tiers=(InputTier("A3", (command, neutral)),),
        route_guidance=((0.5, 1.0, 3.0),),
        stop_input=neutral,
    )


def application(command, effect_tick, control_sequence, *, actual=False):
    return KnownInputApplication(physics_state().session, control_sequence, effect_tick,
                                 command, f"control-{control_sequence}",
                                 ApplicationEvidence.APPLIED if actual else ApplicationEvidence.PREDICTED,
                                 effect_tick if actual else None)


class TrajectoryProtoContractsTests(unittest.TestCase):
    def test_stop_input_is_required_keyword_only_and_strictly_neutral(self):
        value = request()
        parameter = inspect.signature(TrajectorySearchRequest).parameters["stop_input"]
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, inspect.Parameter.empty)
        self.assertEqual(value.stop_input, TickInput(0., 0., False, False, False, 0.))
        for invalid in (
                TickInput(1., 0., False, False, False, 0.),
                TickInput(0., 1., False, False, False, 0.),
                TickInput(0., 0., True, False, False, 0.),
                TickInput(0., 0., False, True, False, 0.),
                TickInput(0., 0., False, False, True, 0.)):
            with self.subTest(invalid=invalid), self.assertRaises(ContractViolation):
                replace(value, stop_input=invalid,
                        supported_inputs=value.supported_inputs + (invalid,),
                        input_tiers=(InputTier("A3", value.supported_inputs + (invalid,)),))
        with self.assertRaises(ContractViolation):
            replace(value, supported_inputs=(value.supported_inputs[0],),
                    input_tiers=(InputTier("A3", (value.supported_inputs[0],)),))

    def test_input_tiers_are_required_cumulative_prefixes_with_stop_in_every_layer(self):
        value = request()
        parameter = inspect.signature(TrajectorySearchRequest).parameters["input_tiers"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        walk, stop = value.supported_inputs
        sprint = replace(walk, sprint=True)
        turn = replace(walk, movement_yaw_radians=1.)
        expanded = replace(
            value,
            supported_inputs=(walk, stop, sprint, turn),
            input_tiers=(
                InputTier("A3", (walk, stop)),
                InputTier("A5", (walk, stop, sprint)),
                InputTier("A15", (walk, stop, sprint, turn)),
            ),
        )
        self.assertEqual(tuple(tier.tier_id for tier in expanded.input_tiers),
                         ("A3", "A5", "A15"))
        invalid = (
            (InputTier("A3", (walk, stop)), InputTier("A5", (walk, sprint, stop))),
            (InputTier("A3", (walk, stop)), InputTier("A5", (walk, stop))),
            (InputTier("A3", (walk, stop)), InputTier("A5", (walk, stop, sprint)),
             InputTier("A15", (walk, stop, sprint))),
            (InputTier("A3", (walk,)), InputTier("A5", (walk, stop, sprint))),
        )
        for tiers in invalid:
            with self.subTest(tiers=tiers), self.assertRaises(ContractViolation):
                replace(expanded, input_tiers=tiers)
        with self.assertRaises(ContractViolation):
            replace(expanded, input_tiers=expanded.input_tiers[:-1])

    def test_minimum_terminal_speed_is_finite_nonnegative_and_within_goal_maximum(self):
        value = request()
        self.assertEqual(value.minimum_terminal_speed_blocks_per_second, 0.)
        allowed = replace(value, goal=replace(
            value.goal, maximum_terminal_speed_blocks_per_second=2.),
            minimum_terminal_speed_blocks_per_second=1.)
        self.assertEqual(allowed.minimum_terminal_speed_blocks_per_second, 1.)
        for invalid in (-1., float("inf"), float("nan"), 2.0001):
            with self.subTest(value=invalid), self.assertRaises(ContractViolation):
                replace(allowed, minimum_terminal_speed_blocks_per_second=invalid)

    def test_same_tick_entries_cannot_claim_real_late_branch(self):
        value = request()
        with self.assertRaises(ContractViolation):
            replace(value, entry_states=(value.entry_states[0], value.entry_states[0]))

    def test_late_candidate_entry_tick_is_one_after_common_anchor(self):
        value = request()
        late = replace(value.entry_states[1], movement_tick_id=11)
        corrected = replace(value, entry_states=(value.entry_states[0], late))
        self.assertEqual(tuple(state.movement_tick_id + 1 for state in corrected.entry_states),
                         corrected.allowed_effect_ticks)

    def test_exact_five_typed_results(self):
        self.assertTrue(issubclass(SearchStatus, StrEnum))
        self.assertEqual({value.value for value in SearchStatus}, {
            "FOUND", "NEEDS_INFORMATION", "BLOCKED", "NO_TRAJECTORY_IN_BUDGET", "STALE"})

    def test_classification_preserves_unknown_evidence_and_budget_meanings(self):
        cases = (
            (SearchStatus.FOUND, SearchReason.VERIFIED_TRAJECTORY),
            (SearchStatus.NEEDS_INFORMATION, SearchReason.UNKNOWN_WORLD),
            (SearchStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION),
            (SearchStatus.NEEDS_INFORMATION, SearchReason.UNPROVEN_RESOURCES),
            (SearchStatus.BLOCKED, SearchReason.KNOWN_NECESSARY_CONDITION),
            (SearchStatus.NO_TRAJECTORY_IN_BUDGET, SearchReason.TIMING_BRANCH_BUDGET),
            (SearchStatus.NO_TRAJECTORY_IN_BUDGET, SearchReason.NODE_BUDGET),
            (SearchStatus.NO_TRAJECTORY_IN_BUDGET, SearchReason.PHYSICS_STEP_BUDGET),
            (SearchStatus.NO_TRAJECTORY_IN_BUDGET, SearchReason.TRAJECTORY_TICK_BUDGET),
            (SearchStatus.NO_TRAJECTORY_IN_BUDGET, SearchReason.SEARCH_EXHAUSTED),
            (SearchStatus.STALE, SearchReason.WORLD_DEPENDENCY),
            (SearchStatus.STALE, SearchReason.ANCHOR),
            (SearchStatus.STALE, SearchReason.GOAL_REVISION),
            (SearchStatus.STALE, SearchReason.INPUT_LEDGER),
            (SearchStatus.STALE, SearchReason.REQUEST_IDENTITY),
        )
        for status, reason in cases:
            with self.subTest(reason=reason):
                result = TrajectorySearchResult("request-1", status, reason)
                self.assertIs(result.status, status)
                for wrong in SearchStatus:
                    if wrong is not status:
                        with self.assertRaises(ContractViolation):
                            replace(result, status=wrong)

    def test_untyped_or_candidate_only_blocker_cannot_label_request_blocked(self):
        with self.assertRaises(ContractViolation):
            TrajectorySearchResult("request-1", SearchStatus.BLOCKED, "candidate_collision")
        with self.assertRaises(ContractViolation):
            TrajectorySearchResult("request-1", "STALE", SearchReason.ANCHOR)

    def test_request_and_nested_data_are_frozen_and_hashable(self):
        value = request()
        self.assertIsInstance(hash(value), int)
        with self.assertRaises(FrozenInstanceError):
            value.request_id = "new"
        with self.assertRaises(FrozenInstanceError):
            value.budget.max_nodes = 400
        with self.assertRaises(FrozenInstanceError):
            value.entry_states[0].movement_tick_id = 11

    def test_request_binds_each_identity(self):
        value = request()
        changes = {
            "request_id": "request-2", "geometry_revision": 4, "goal_revision": 5,
            "anchor_id": "anchor-11", "input_ledger_id": "ledger-9",
            "task_damage_budget": TaskDamageBudget("bounded", 1.0),
        }
        for name, changed in changes.items():
            self.assertNotEqual(value, replace(value, **{name: changed}))
        with self.assertRaises(ContractViolation):
            replace(value, world_session=WorldSessionId("other"))

    def test_immutable_nonempty_entry_inputs_and_guidance(self):
        value = request()
        for name in ("entry_states", "timing_branches", "allowed_effect_ticks",
                     "input_prefix", "supported_inputs", "route_guidance", "branch_preludes"):
            with self.subTest(name=name), self.assertRaises(ContractViolation):
                replace(value, **{name: list(getattr(value, name))})
        for name in ("entry_states", "timing_branches", "supported_inputs"):
            with self.subTest(name=name), self.assertRaises(ContractViolation):
                replace(value, **{name: ()})
        with self.assertRaises(ContractViolation):
            replace(value, route_guidance=((float("nan"), 1.0, 0.0),))

    def test_entry_timing_is_at_most_two_frozen_hypotheses(self):
        self.assertEqual(tuple(TimingBranch), (TimingBranch.ON_TIME, TimingBranch.LATE_ONE_TICK))
        value = request()
        one = replace(value, entry_states=value.entry_states[:1],
                      timing_branches=(TimingBranch.ON_TIME,), allowed_effect_ticks=(11,),
                      branch_preludes=((),))
        self.assertEqual(len(one.entry_states), 1)
        for branches in ((TimingBranch.LATE_ONE_TICK,),
                         (TimingBranch.ON_TIME, TimingBranch.ON_TIME),
                         (TimingBranch.LATE_ONE_TICK, TimingBranch.ON_TIME),
                         (TimingBranch.ON_TIME, TimingBranch.LATE_ONE_TICK, TimingBranch.ON_TIME)):
            with self.subTest(branches=branches), self.assertRaises(ContractViolation):
                replace(value, timing_branches=branches)

    def test_branches_share_one_prefix_and_effect_tick_differs_by_one(self):
        value = request()
        for ticks in ((11, 13), (10, 11), (11,), (True, 12)):
            with self.subTest(ticks=ticks), self.assertRaises(ContractViolation):
                replace(value, allowed_effect_ticks=ticks)
        with self.assertRaises(ContractViolation):
            replace(value, input_prefix=(value.input_prefix, value.input_prefix))
        unsupported = TickInput(0.0, 1.0, False, False, False, 0.0)
        with self.assertRaises(ContractViolation):
            replace(value, input_prefix=(unsupported,))

    def test_prelude_is_explicit_immutable_and_bound_to_common_time(self):
        value = request()
        self.assertEqual(value.anchor_state, value.entry_states[0])
        self.assertEqual(value.branch_preludes[0], ())
        self.assertEqual(value.branch_preludes[1][0].effect_tick, 11)
        missing = replace(value, branch_preludes=((), None))
        self.assertIsNone(missing.branch_preludes[1])
        for preludes in ((None, value.branch_preludes[1]),
                         ((), ()), ((), value.branch_preludes[1] * 2),
                         ((), list(value.branch_preludes[1]))):
            with self.subTest(preludes=preludes), self.assertRaises(ContractViolation):
                replace(value, branch_preludes=preludes)
        for changed in (replace(value.branch_preludes[1][0], effect_tick=12),
                        replace(value.branch_preludes[1][0], control_sequence=100)):
            with self.assertRaises(ContractViolation):
                replace(value, branch_preludes=((), (changed,)))

    def test_application_fact_distinguishes_prediction_from_actual_receipt(self):
        value = request().branch_preludes[1][0]
        self.assertIs(value.evidence, ApplicationEvidence.PREDICTED)
        self.assertIsNone(value.actual_movement_tick)
        received = application(value.tick_input, 11, 99, actual=True)
        self.assertEqual(received.actual_movement_tick, 11)
        with self.assertRaises(ContractViolation):
            replace(value, actual_movement_tick=11)
        with self.assertRaises(ContractViolation):
            replace(received, actual_movement_tick=12)
        with self.assertRaises(FrozenInstanceError):
            value.effect_tick = 12

    def test_budget_rejects_bool_nonpositive_and_noninteger_values(self):
        budget = request().budget
        for name in ("max_nodes", "max_physics_steps", "max_trajectory_ticks", "max_timing_branches"):
            for invalid in (True, False, 0, -1, 1.5, "2"):
                with self.subTest(name=name, value=invalid), self.assertRaises(ContractViolation):
                    replace(budget, **{name: invalid})

    def test_p0_trajectory_and_branch_caps(self):
        budget = request().budget
        with self.assertRaises(ContractViolation):
            replace(budget, max_trajectory_ticks=41)
        with self.assertRaises(ContractViolation):
            replace(budget, max_timing_branches=3)
        value = request()
        with self.assertRaises(ContractViolation):
            replace(value, budget=replace(budget, max_trajectory_ticks=1),
                    input_prefix=value.input_prefix * 2)
        # The hypothesis contract remains intact even when search cannot afford both.
        constrained = replace(value, budget=replace(budget, max_timing_branches=1))
        self.assertEqual(len(constrained.timing_branches), 2)
        self.assertEqual(constrained.budget.max_timing_branches, 1)

    def test_request_rejects_untyped_goal_damage_budget_states_and_identities(self):
        for name, value in (("goal", object()), ("task_damage_budget", 0),
                            ("budget", object()), ("entry_states", (object(),)),
                            ("geometry_revision", True), ("goal_revision", -1),
                            ("request_id", ""), ("anchor_id", ""), ("input_ledger_id", "")):
            with self.subTest(name=name), self.assertRaises(ContractViolation):
                replace(request(), **{name: value})

    def test_scenarios_register_frozen_p0_families_without_search(self):
        self.assertIs(type(P0_SCENARIOS), tuple)
        by_id = {scenario.scenario_id: scenario for scenario in P0_SCENARIOS}
        required = {"flat_walk", "flat_sprint", "turn_90", "jump_up_straight",
                    "jump_up_after_turn", "jump_gap_1", "jump_gap_2", "jump_gap_3",
                    "half_slab_chain", "stairs_chain", "short_landing_after_input_loss",
                    "unknown_landing", "known_blocked", "budget_exhausted",
                    "jump_up_commitment_scan", "jump_gap_1_commitment_scan",
                    "jump_gap_2_commitment_scan", "jump_gap_3_commitment_scan"}
        self.assertTrue(required.issubset(by_id))
        self.assertEqual(len(by_id), len(P0_SCENARIOS))
        self.assertIs(by_id["unknown_landing"].expected_status, SearchStatus.NEEDS_INFORMATION)
        self.assertIs(by_id["known_blocked"].expected_status, SearchStatus.BLOCKED)
        self.assertIs(by_id["budget_exhausted"].expected_status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        for scenario in P0_SCENARIOS:
            self.assertEqual(scenario.stage, "P0")
            self.assertLessEqual(scenario.budget.max_trajectory_ticks, 40)
            self.assertEqual(scenario.timing_branches, tuple(TimingBranch))
            self.assertIsInstance(hash(scenario), int)
            self.assertTrue(scenario.coverage_scope)
            with self.assertRaises(FrozenInstanceError):
                scenario.stage = "P1"

    def test_production_has_no_experimental_imports(self):
        violations = []
        paths = tuple((ROOT / "mc2p").rglob("*.py")) + tuple((ROOT / "scripts").rglob("*.py"))
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text("utf-8-sig"))):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    if name == "experiments" or name.startswith("experiments."):
                        violations.append((str(path.relative_to(ROOT)), name))
        self.assertEqual(violations, [])

    def test_standalone_export_includes_isolated_prototype_sources(self):
        files = collect_export_files(load_manifest())
        exported = {str(path) for path in files}
        for name in ("contracts", "physics", "scenarios", "commitment", "reference_search"):
            self.assertIn(f"experiments/motion_navigation/trajectory_proto/{name}.py", exported)

    def test_standalone_export_includes_prototype_tests_plans_and_reviews(self):
        manifest = load_manifest()
        files = collect_export_files(manifest)
        exported = {str(path) for path in files}
        for name in ("contracts", "commitment", "reference_search"):
            self.assertIn(f"tests/motion_nav/test_trajectory_proto_{name}.py", exported)
        for name in ("task-3-brief", "task-3-report", "task-3-review",
                     "task-4-brief", "task-4-report", "task-4-review", "task-4-fix-report",
                     "timing-branch-audit", "task-4-timing-fix-report",
                     "task-5a-brief", "task-5a-report", "task-5a-review"):
            self.assertIn(
                f".superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/{name}.md",
                exported)
        self.assertIn("docs/motion_navigation/stages/TP-continuous-short-trajectory-prototype-plan.md",
                      exported)
        self.assertIn("mc2p/motion_nav/physics_types.py", exported)
        self.assertIn("tests/motion_nav/test_b09r_physics_contracts.py", exported)

    def test_prototype_sources_never_depend_on_test_modules(self):
        violations = []
        for path in (ROOT / "experiments/motion_navigation/trajectory_proto").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text("utf-8-sig"))):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                violations.extend(name for name in names if name == "tests" or name.startswith("tests."))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
