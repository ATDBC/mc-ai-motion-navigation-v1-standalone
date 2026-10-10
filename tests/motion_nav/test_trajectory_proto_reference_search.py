"""Reference search must find and verify one shared, stopped trajectory."""
import ast
from dataclasses import FrozenInstanceError, replace
import inspect
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto.contracts import (
    InputTier, SearchBudget, SearchReason, SearchStatus, TimingBranch,
)
from experiments.motion_navigation.trajectory_proto.reference_search import reference_search
from experiments.motion_navigation.trajectory_proto.reference_search import _goal_check
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture
from experiments.motion_navigation.trajectory_proto.commitment import (
    CandidateRejection, CommitmentProof, ScanResult, ScanStatus, StopTail, TailStatus,
    _Incomplete, _risks, scan_commitment,
)
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, CalculationStatus
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.movement_transition import GoalSupport, MovementMode, ResourceState
from mc2p.motion_nav.geometry import QueryStatus, SupportResult, query_support, required_cells_for_sweep
from mc2p.motion_nav.world_model import Aabb, CellFact, CellKnowledge, WorldView


def fixed_candidate(fixture, inputs, *, anchor=None, prelude=None, goal=None):
    request = fixture.request
    anchor = anchor or request.anchor_state
    fact = request.branch_preludes[1][0]
    fact = replace(fact, tick_input=prelude or fact.tick_input)
    late = step(anchor, fact.tick_input, fixture.world, JAVA_1_21_RULESET)
    if late.status is not CalculationStatus.OK:
        raise AssertionError("known prelude fixture must produce a complete real entry")
    return replace(request, entry_states=(anchor, late.next_state), input_prefix=inputs,
                   branch_preludes=((), (fact,)), goal=goal or request.goal,
                   budget=replace(request.budget, max_trajectory_ticks=len(inputs)))


def edge_support_fixture():
    fixture = representative_fixture("jump_gap_1")
    inputs = reference_search(fixture.request, fixture.world).inputs
    facts = {}
    for x in range(-3, 4):
        for y in range(-5, 7):
            for z in range(-3, 13):
                fact = fixture.world.cell((x, y, z))
                if y == 0 and z >= 2 and x >= 1:
                    fact = CellFact(CellKnowledge.AIR, fact.stamp)
                facts[(x, y, z)] = fact
    world = PhysicsWorldView(WorldView.detached(fixture.world.session, 3, 0, facts),
                             JAVA_1_21_RULESET)
    anchor = replace(fixture.request.anchor_state, position=(1.25, 1., .5))
    wait = fixture.request.branch_preludes[1][0].tick_input
    late = step(anchor, wait, world, JAVA_1_21_RULESET).next_state
    goal = replace(fixture.request.goal, region=Aabb(1.1, 1., 2.3, 1.4, 1.05, 3.3))
    request = replace(fixture.request, entry_states=(anchor, late), goal=goal,
                      input_prefix=inputs,
                      budget=replace(fixture.request.budget, max_trajectory_ticks=len(inputs)))
    return fixture, request, world, inputs


class TrajectoryProtoReferenceSearchTests(unittest.TestCase):
    def test_one_twelfth_support_is_not_safe_goal_support(self):
        _, request, world, inputs = edge_support_fixture()
        state = request.entry_states[0]
        for command in inputs:
            state = step(state, command, world, JAVA_1_21_RULESET).next_state
        check, missing = _goal_check(request, TimingBranch.ON_TIME, state, world,
                                     CountedPhysics(request.budget))
        self.assertEqual(missing, ())
        self.assertAlmostEqual(check.support_fraction, 1. / 12.)
        self.assertFalse(check.accepted)

    def test_one_twelfth_support_request_is_rejected_but_old_rule_would_find(self):
        from experiments.motion_navigation.trajectory_proto import commitment
        from experiments.motion_navigation.trajectory_proto import reference_search as source

        _, request, world, _ = edge_support_fixture()
        result = reference_search(request, world)
        self.assertIsNot(result.status, SearchStatus.FOUND)
        self.assertIsNone(result.proof)

        def old_support_rule(state, support):
            return state.on_ground and support.status is QueryStatus.FEASIBLE

        with (patch.object(commitment, "support_is_safe", side_effect=old_support_rule),
              patch.object(source, "support_is_safe", side_effect=old_support_rule)):
            legacy = reference_search(request, world)
        self.assertIs(legacy.status, SearchStatus.FOUND)
        self.assertTrue(all(check.accepted for check in legacy.goal_checks))
        self.assertTrue(all(check.support_fraction < .15 for check in legacy.goal_checks))

    def test_unclosed_risk_rejects_candidate_without_a_proof(self):
        _, request, world, inputs = edge_support_fixture()
        state = request.entry_states[0]
        for command in inputs:
            state = step(state, command, world, JAVA_1_21_RULESET).next_state
        tails = (
            StopTail(3, TailStatus.UNSAFE, (), (state,), (), 0., 13),
            StopTail(len(inputs), TailStatus.SAFE_STOP, (), (state,), (), 0., 34),
        )
        with self.assertRaises(_Incomplete) as caught:
            _risks(tails, CountedPhysics(request.budget), world, set())
        self.assertIs(caught.exception.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(caught.exception.reason, CandidateRejection.UNRECOVERED_RISK)

    def test_nonempty_resource_goal_needs_unproven_resource_information(self):
        fixture = representative_fixture("flat_walk")
        goal = replace(fixture.request.goal,
                       minimum_resources=ResourceState((("food_points", 20.),)))
        result = reference_search(replace(fixture.request, goal=goal), fixture.world)
        self.assertIs(result.status, SearchStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.UNPROVEN_RESOURCES)
        self.assertIsNone(result.proof)

    def test_nonzero_terminal_goal_uses_the_actual_candidate_speed(self):
        fixture = representative_fixture("flat_walk")
        request = fixture.request
        walk = request.supported_inputs[0]
        state = step(request.anchor_state, walk, fixture.world, JAVA_1_21_RULESET).next_state
        speed = 20. * abs(state.velocity_blocks_per_tick[2])
        goal = replace(request.goal,
                       region=Aabb(state.position[0] - .01, state.position[1],
                                   state.position[2] - .01, state.position[0] + .01,
                                   state.position[1] + .05, state.position[2] + .01),
                       maximum_terminal_speed_blocks_per_second=speed + 1.e-9)
        request = replace(request, entry_states=(request.anchor_state,),
                          timing_branches=(TimingBranch.ON_TIME,), allowed_effect_ticks=(11,),
                          branch_preludes=((),), input_prefix=(walk,), goal=goal,
                          budget=replace(request.budget, max_trajectory_ticks=1))
        result = reference_search(request, fixture.world)
        self.assertIs(result.status, SearchStatus.FOUND)
        terminal = result.proof.branches[0].states[-1]
        actual_speed = 20. * math.hypot(terminal.velocity_blocks_per_tick[0],
                                       terminal.velocity_blocks_per_tick[2])
        self.assertGreater(actual_speed, 0.)
        self.assertAlmostEqual(actual_speed, speed)
        self.assertTrue(result.goal_checks[0].accepted)

    def test_nonzero_terminal_goal_is_rejected_when_its_stop_tail_is_unsafe(self):
        fixture = representative_fixture("jump_gap_1")
        base = fixture.request
        anchor = replace(base.anchor_state, position=(.5, 1., .9),
                         velocity_blocks_per_tick=(0., -.0784, .1))
        walk = base.supported_inputs[0]
        terminal = step(anchor, walk, fixture.world, JAVA_1_21_RULESET).next_state
        speed = 20. * math.hypot(terminal.velocity_blocks_per_tick[0],
                                 terminal.velocity_blocks_per_tick[2])
        goal = replace(base.goal,
                       region=Aabb(terminal.position[0] - .01, terminal.position[1],
                                   terminal.position[2] - .01, terminal.position[0] + .01,
                                   terminal.position[1] + .05, terminal.position[2] + .01),
                       maximum_terminal_speed_blocks_per_second=speed + 1.e-9)
        request = replace(base, entry_states=(anchor,), timing_branches=(TimingBranch.ON_TIME,),
                          allowed_effect_ticks=(anchor.movement_tick_id + 1,),
                          branch_preludes=((),), input_prefix=(walk,), goal=goal,
                          budget=replace(base.budget, max_trajectory_ticks=1))
        check, missing = _goal_check(request, TimingBranch.ON_TIME, terminal, fixture.world,
                                     CountedPhysics(request.budget))
        self.assertEqual(missing, ())
        self.assertTrue(check.accepted)
        scan = scan_commitment(request, (walk,), fixture.world,
                               _boundary_evidence(request, (walk,)))
        self.assertIs(scan.status, ScanStatus.CANDIDATE_REJECTED)
        self.assertIs(scan.reason, CandidateRejection.FINAL_STOP_UNSAFE)
        self.assertIsNone(scan.proof)
        result = reference_search(request, fixture.world)
        self.assertIsNot(result.status, SearchStatus.FOUND)
        self.assertIsNone(result.proof)

    def test_tail_not_settled_candidate_does_not_end_the_request(self):
        from experiments.motion_navigation.trajectory_proto import reference_search as source

        fixture = representative_fixture("flat_walk")
        original = source.scan_commitment
        candidates = []

        def reject_first(request, inputs, world, boundary_inputs, *, counter):
            candidates.append(inputs)
            if len(candidates) == 1:
                return ScanResult(ScanStatus.CANDIDATE_REJECTED,
                                  CandidateRejection.TAIL_NOT_SETTLED, counter.counts)
            return original(request, inputs, world, boundary_inputs, counter=counter)

        with patch.object(source, "scan_commitment", side_effect=reject_first):
            result = reference_search(fixture.request, fixture.world)
        self.assertIs(result.status, SearchStatus.FOUND)
        self.assertGreaterEqual(len(candidates), 2)
        self.assertNotEqual(result.inputs, candidates[0])

    def assert_found(self, outcome, fixture):
        self.assertIs(outcome.status, SearchStatus.FOUND)
        self.assertIsInstance(outcome.proof, CommitmentProof)
        self.assertTrue(outcome.inputs)
        self.assertEqual(len(outcome.goal_checks), len(fixture.request.entry_states))
        self.assertTrue(all(check.accepted for check in outcome.goal_checks))
        for branch in outcome.proof.branches:
            self.assertEqual(branch.tails[-1].status, TailStatus.SAFE_STOP)
            self.assertEqual(branch.states[-1].velocity_blocks_per_tick[0], 0.)
            self.assertEqual(branch.states[-1].velocity_blocks_per_tick[2], 0.)
            state = branch.states[-1]
            self.assertTrue(fixture.request.goal.accepts(
                position=state.position, support=GoalSupport.SOLID, mode=MovementMode.WALK,
                pose=state.pose, speed_blocks_per_second=0.,
                resources=ResourceState((("food_points", float(state.food_points)),)),
                yaw_radians=state.yaw_radians))
        self.assertEqual(outcome.input_order, fixture.request.supported_inputs)
        self.assertLessEqual(outcome.counts.nodes, fixture.request.budget.max_nodes)
        self.assertLessEqual(outcome.counts.physics_steps, fixture.request.budget.max_physics_steps)
        self.assertEqual(outcome.winning_tier, "A3")
        self.assertGreater(outcome.completed_candidates, 0)
        self.assertGreater(outcome.commitment_scans, 0)

    def test_expanding_the_input_alphabet_preserves_the_old_winning_tier(self):
        for name in ("flat_walk", "jump_up_straight", "jump_gap_1"):
            with self.subTest(scenario=name):
                fixture = representative_fixture(name)
                inputs = fixture.request.supported_inputs
                sprint = replace(inputs[0], sprint=True)
                turn = replace(inputs[0], movement_yaw_radians=math.pi / 2.)
                request = fixture.request
                if name == "flat_walk":
                    region = request.goal.region
                    goal = replace(request.goal,
                                   region=Aabb(-3., region.min_y, region.min_z,
                                               region.max_x, region.max_y, region.max_z),
                                   required_yaw_radians=None,
                                   maximum_yaw_error_radians=None)
                    request = replace(request, goal=goal)
                    center = ((goal.region.min_x + goal.region.max_x) / 2.,
                              (goal.region.min_z + goal.region.max_z) / 2.)
                    walk_state = step(request.anchor_state, inputs[0], fixture.world,
                                      JAVA_1_21_RULESET).next_state
                    turn_state = step(request.anchor_state, turn, fixture.world,
                                      JAVA_1_21_RULESET).next_state
                    self.assertLess(math.hypot(turn_state.position[0] - center[0],
                                               turn_state.position[2] - center[1]),
                                    math.hypot(walk_state.position[0] - center[0],
                                               walk_state.position[2] - center[1]))
                base = reference_search(request, fixture.world)
                expanded_inputs = inputs + (sprint, turn)
                expanded_request = replace(
                    request,
                    supported_inputs=expanded_inputs,
                    input_tiers=(
                        fixture.request.input_tiers[0],
                        InputTier("A5", inputs + (sprint,)),
                        InputTier("A15", expanded_inputs),
                    ),
                )
                expanded = reference_search(expanded_request, fixture.world)
                self.assertEqual(expanded.winning_tier, "A3")
                self.assertEqual(expanded.inputs, base.inputs)
                self.assertEqual(expanded.goal_checks, base.goal_checks)
                self.assertEqual(expanded.counts, base.counts)
                self.assertEqual(expanded.completed_candidates, base.completed_candidates)
                self.assertEqual(expanded.commitment_scans, base.commitment_scans)

    def test_flat_rest_to_goal_and_safe_stop_is_found(self):
        fixture = representative_fixture("flat_walk")
        self.assertEqual(fixture.request.anchor_state.velocity_blocks_per_tick[2], 0.)
        outcome = reference_search(fixture.request, fixture.world)
        self.assert_found(outcome, fixture)

    def test_one_block_up_is_found_without_action_solver(self):
        fixture = representative_fixture("jump_up_straight")
        outcome = reference_search(fixture.request, fixture.world)
        self.assert_found(outcome, fixture)
        self.assertTrue(any(command.jump for command in outcome.inputs))
        self.assertEqual(outcome.proof.branches[0].states[-1].position[1], 2.)

    def test_one_gap_uses_same_candidate_after_real_delay(self):
        fixture = representative_fixture("jump_gap_1")
        outcome = reference_search(fixture.request, fixture.world)
        self.assert_found(outcome, fixture)
        self.assertTrue(any(command.jump for command in outcome.inputs))
        on_time, late = outcome.proof.branches
        self.assertEqual(on_time.states[1].movement_tick_id, 11)
        self.assertEqual(late.states[1].movement_tick_id, 12)
        self.assertEqual(late.prelude_states[0], fixture.request.anchor_state)
        self.assertEqual(late.prelude_states[-1], fixture.request.entry_states[1])
        self.assertEqual(outcome.branch_total_ticks, (outcome.candidate_ticks, outcome.candidate_ticks + 1))

    def test_gap_risk_recovers_only_on_real_landing_support(self):
        fixture = representative_fixture("jump_gap_1")
        outcome = reference_search(fixture.request, fixture.world)
        self.assert_found(outcome, fixture)
        for branch in outcome.proof.branches:
            self.assertTrue(branch.risk_intervals)
            for risk in branch.risk_intervals:
                self.assertEqual(risk.first_committed_boundary,
                                 risk.last_abandon_boundary + 1)
                self.assertGreater(risk.recovered_boundary,
                                   risk.first_committed_boundary)
                state = branch.states[risk.recovered_boundary]
                cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
                local = WorldView.detached(fixture.world.session, 3, 0,
                                          {p: fixture.world.cell(p) for p in cells})
                support = query_support(state.body_box, local)
                self.assertIs(support.status, QueryStatus.FEASIBLE)
                self.assertGreater(support.support_fraction, 0.)
                self.assertTrue(set(support.dependencies).issubset(dict(outcome.proof.dependency_facts)))
                self.assertTrue(any(not sample.on_ground for sample in
                                    branch.states[risk.first_committed_boundary:risk.recovered_boundary]))

    def test_unknown_from_recovery_support_query_is_information(self):
        from experiments.motion_navigation.trajectory_proto import commitment
        fixture = representative_fixture("jump_gap_1")
        original = commitment.query_support
        reads = []

        def unknown_at_landing(body, world):
            support = original(body, world)
            if body.min_z > 2.:
                reads.append(body)
                return SupportResult(QueryStatus.NEEDS_INFORMATION, support.dependencies,
                                     0., 0., ((0, 0, 2),))
            return support

        with patch.object(commitment, "query_support", side_effect=unknown_at_landing):
            outcome = reference_search(fixture.request, fixture.world)
        self.assertTrue(reads)
        self.assertIs(outcome.status, SearchStatus.NEEDS_INFORMATION)
        self.assertIs(outcome.reason, SearchReason.UNKNOWN_WORLD)
        self.assertEqual(outcome.missing_cells, ((0, 0, 2),))
        self.assertIsNone(outcome.proof)

    def test_permissive_goal_speed_accepts_actual_nonzero_candidate_with_safe_tail(self):
        fixture = representative_fixture("flat_walk")
        request = replace(fixture.request, goal=replace(fixture.request.goal,
                          maximum_terminal_speed_blocks_per_second=100.))
        outcome = reference_search(request, fixture.world)
        self.assertIs(outcome.status, SearchStatus.FOUND)
        self.assertTrue(all(check.accepted for check in outcome.goal_checks))
        for branch in outcome.proof.branches:
            speed = 20. * math.hypot(branch.states[-1].velocity_blocks_per_tick[0],
                                     branch.states[-1].velocity_blocks_per_tick[2])
            self.assertGreater(speed, 0.)
            self.assertLessEqual(speed, request.goal.maximum_terminal_speed_blocks_per_second)
            self.assertIs(branch.tails[-1].status, TailStatus.SAFE_STOP)

    def test_nominal_goal_is_insufficient_when_walk_wait_misses_goal(self):
        fixture = representative_fixture("flat_walk")
        found = reference_search(fixture.request, fixture.world)
        self.assertEqual(found.commitment_scans, 1)
        z = found.proof.branches[0].states[-1].position[2]
        goal = replace(fixture.request.goal, region=Aabb(.35, 1., z - .01, .65, 1.05, z + .01))
        request = fixed_candidate(fixture, found.inputs, goal=goal)
        neutral = reference_search(request, fixture.world)
        self.assert_found(neutral, replace(fixture, request=request))
        walk_request = fixed_candidate(fixture, found.inputs, goal=goal,
                                       prelude=fixture.request.supported_inputs[0])
        result = reference_search(walk_request, fixture.world)
        self.assertIs(result.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        self.assertIsNone(result.proof)

    def test_real_walk_wait_that_cannot_stop_rejects_shared_candidate(self):
        fixture = representative_fixture("jump_gap_1")
        walk, jump, neutral = fixture.request.supported_inputs
        anchor = replace(fixture.request.anchor_state, position=(.5, 1., 1.),
                         velocity_blocks_per_tick=(0., -.0784, .1))
        inputs = (jump,) + (walk,) * 10 + (neutral,) * 20
        request = fixed_candidate(fixture, inputs, anchor=anchor, prelude=neutral)
        baseline = reference_search(request, fixture.world)
        self.assert_found(baseline, replace(fixture, request=request))
        walk_request = fixed_candidate(fixture, inputs, anchor=anchor, prelude=walk)
        late = walk_request.entry_states[1]
        self.assertTrue(late.on_ground)
        self.assertAlmostEqual(late.position[2], 1.1980000090740741)
        result = reference_search(walk_request, fixture.world)
        self.assertIs(result.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        self.assertIsNone(result.proof)

    def test_nodes_physics_ticks_and_timing_have_distinct_budget_reasons(self):
        fixture = representative_fixture("flat_walk")
        cases = ((SearchBudget(1, 65536, 40, 2), SearchReason.NODE_BUDGET),
                 (SearchBudget(4096, 1, 40, 2), SearchReason.PHYSICS_STEP_BUDGET),
                 (SearchBudget(4096, 65536, 1, 2), SearchReason.TRAJECTORY_TICK_BUDGET),
                 (SearchBudget(4096, 65536, 40, 1), SearchReason.TIMING_BRANCH_BUDGET))
        for budget, reason in cases:
            with self.subTest(reason=reason):
                result = reference_search(replace(fixture.request, budget=budget), fixture.world)
                self.assertIs(result.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
                self.assertIs(result.reason, reason)
                self.assertIsNone(result.proof)
                self.assertLessEqual(result.counts.nodes, budget.max_nodes)
                self.assertLessEqual(result.counts.physics_steps, budget.max_physics_steps)

    def test_search_and_scanner_share_almost_spent_total_physics_budget(self):
        fixture = representative_fixture("flat_walk")
        found = reference_search(fixture.request, fixture.world)
        request = replace(fixture.request, budget=replace(fixture.request.budget,
                                                         max_physics_steps=found.counts.physics_steps - 1))
        from experiments.motion_navigation.trajectory_proto import physics
        calls = []
        original = physics.step

        def observed(*args):
            calls.append(args)
            return original(*args)

        with patch.object(physics, "step", side_effect=observed):
            result = reference_search(request, fixture.world)
        self.assertIs(result.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        self.assertIs(result.reason, SearchReason.PHYSICS_STEP_BUDGET)
        self.assertIsNone(result.proof)
        self.assertEqual(result.counts.physics_steps, request.budget.max_physics_steps)
        self.assertEqual(len(calls), request.budget.max_physics_steps)
        self.assertGreater(result.expanded_nodes, 1)
        self.assertGreater(result.counts.tail_ticks, 0)
        self.assertEqual(result.commitment_scans, 0)

    def test_all_replays_goal_queries_and_frontier_share_node_budget(self):
        fixture = representative_fixture("flat_walk")
        baseline = reference_search(fixture.request, fixture.world)
        exact = replace(fixture.request, budget=replace(fixture.request.budget,
                                                        max_nodes=baseline.counts.nodes))
        self.assertIs(reference_search(exact, fixture.world).status, SearchStatus.FOUND)
        limited = replace(exact, budget=replace(exact.budget, max_nodes=exact.budget.max_nodes - 1))
        result = reference_search(limited, fixture.world)
        self.assertIs(result.reason, SearchReason.NODE_BUDGET)
        self.assertEqual(result.counts.nodes, limited.budget.max_nodes)
        self.assertIsNone(result.proof)

    def test_real_prelude_replay_is_required_even_if_declared_tick_is_right(self):
        fixture = representative_fixture("flat_walk")
        late = replace(fixture.request.entry_states[1], jumping_cooldown_ticks=3)
        request = replace(fixture.request, entry_states=(fixture.request.anchor_state, late),
                          input_prefix=reference_search(fixture.request, fixture.world).inputs)
        request = replace(request, budget=replace(request.budget, max_trajectory_ticks=len(request.input_prefix)))
        result = reference_search(request, fixture.world)
        self.assertIs(result.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        self.assertIsNone(result.proof)

    def test_missing_wait_evidence_never_defaults_to_neutral(self):
        fixture = representative_fixture("flat_walk")
        request = replace(fixture.request, branch_preludes=((), None))
        result = reference_search(request, fixture.world)
        self.assertIs(result.status, SearchStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.MISSING_INPUT_APPLICATION)
        self.assertIsNone(result.proof)

    def test_unknown_world_and_stale_identity_keep_typed_classification(self):
        fixture = representative_fixture("flat_walk")
        facts = {(x, y, z): fixture.world.cell((x, y, z))
                 for x in range(-3, 4) for y in range(-5, 7) for z in range(-3, 13)}
        del facts[(0, 0, 0)]
        unknown = PhysicsWorldView(WorldView.detached(fixture.world.session, 3, 0, facts), JAVA_1_21_RULESET)
        result = reference_search(fixture.request, unknown)
        self.assertIs(result.status, SearchStatus.NEEDS_INFORMATION)
        self.assertIs(result.reason, SearchReason.UNKNOWN_WORLD)
        self.assertIn((0, 0, 0), result.missing_cells)
        stale = reference_search(replace(fixture.request, geometry_revision=4), fixture.world)
        self.assertIs(stale.status, SearchStatus.STALE)
        self.assertIs(stale.reason, SearchReason.WORLD_DEPENDENCY)

    def test_candidate_collision_never_claims_request_blocked(self):
        fixture = representative_fixture("jump_up_straight")
        walk = fixture.request.supported_inputs[0]
        request = replace(fixture.request, input_prefix=(walk,) * 10)
        result = reference_search(request, fixture.world)
        self.assertIs(result.status, SearchStatus.NO_TRAJECTORY_IN_BUDGET)
        self.assertIs(result.reason, SearchReason.SEARCH_EXHAUSTED)
        self.assertIsNone(result.proof)

    def test_repeated_requests_have_equal_states_inputs_hashes_and_counts(self):
        for name in ("flat_walk", "jump_up_straight", "jump_gap_1"):
            with self.subTest(scenario=name):
                fixture = representative_fixture(name)
                first = reference_search(fixture.request, fixture.world)
                second = reference_search(fixture.request, fixture.world)
                self.assertEqual(first, second)
                self.assertEqual(first.result_hash, second.result_hash)
                self.assertEqual(first.proof.trajectory_hash, second.proof.trajectory_hash)
                with self.assertRaises(FrozenInstanceError):
                    first.status = SearchStatus.BLOCKED

    def test_search_source_has_no_wall_clock_solver_or_test_dependency(self):
        from experiments.motion_navigation.trajectory_proto import reference_search as source
        from experiments.motion_navigation.trajectory_proto import primitive_search
        paths = (Path(source.__file__), Path(primitive_search.__file__),
                 Path(inspect.getfile(representative_fixture)))
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = ([node.module] if isinstance(node, ast.ImportFrom)
                               else [alias.name for alias in node.names])
                    self.assertFalse(any(module and (module.startswith("tests") or "solver" in module
                                         or module in ("time", "datetime")) for module in modules))
                if isinstance(node, ast.Call):
                    self.assertFalse(isinstance(node.func, ast.Attribute)
                                     and node.func.attr in ("perf_counter", "monotonic", "time", "now"))
        self.assertIn("without cross-history merging", reference_search(
            representative_fixture("flat_walk").request, representative_fixture("flat_walk").world).coverage)


if __name__ == "__main__":
    unittest.main()
