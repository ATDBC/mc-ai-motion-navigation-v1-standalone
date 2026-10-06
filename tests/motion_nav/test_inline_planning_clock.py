"""Inline simulation budgets are deterministic; production budgets stay real."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import count
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.motion_nav import known_map_planner
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest,
    SurfacePlanningStatus,
)
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import (
    flat_surface_world, ordinary_profile,
)
from tests.sim.runner import InlinePlannerWorker


class InlinePlanningClockTests(unittest.TestCase):
    def fixture(self):
        world = flat_surface_world(5)
        snapshot = KnownMapSnapshotBuilder(
            world.view(), KnownMapBounds(0, 4, 1, 1, 0, 4, True),
        ).advance(world.view(), 100_000).snapshot
        request = SurfacePlanningRequest(
            1, "deterministic-budget", "budget-goal", 1, world.session.value,
            SurfaceNodeId(0, 0, 1, 0), SurfaceNodeId(4, 4, 1, 0),
        )
        return snapshot, ordinary_profile(), step_profile(), request

    def inline(self, fixture):
        worker = InlinePlannerWorker()
        worker.submit_surface_snapshot(*fixture)
        return worker.poll_latest()

    def test_inline_result_does_not_change_when_host_clock_jumps_or_runs_slow(self):
        fixture = self.fixture()
        reference = self.inline(fixture)
        self.assertIs(reference.status, SurfacePlanningStatus.COMPLETE)
        for interval in (1_000_000_000, 60_000_000_000):
            with self.subTest(host_interval_ns=interval):
                host = SimpleNamespace(perf_counter_ns=count(0, interval).__next__)
                with patch.object(known_map_planner, "time", host):
                    changed = self.inline(fixture)
                    self.assertIs(known_map_planner.time, host)
                self.assertEqual(changed, reference)

    def test_production_entry_and_process_worker_keep_real_time_budget(self):
        snapshot, ground, step, request = self.fixture()
        tiny = replace(request, maximum_planning_seconds=1e-9)
        candidate = known_map_planner.plan_known_surface_snapshot(
            snapshot, ground, step, tiny,
        )
        self.assertIs(candidate.status, SurfacePlanningStatus.TIMEOUT)
        with PlannerWorker() as worker:
            worker.submit_surface_snapshot(snapshot, ground, step, tiny)
            # Queue timeout bounds a broken process, not the search verdict.
            candidate = worker._results.get(timeout=10)
        self.assertIs(candidate.status, SurfacePlanningStatus.TIMEOUT)

    def test_search_expansion_limit_still_bounds_inline_work(self):
        snapshot, ground, step, request = self.fixture()
        limited = replace(request, maximum_expansions=1)
        candidate = self.inline((snapshot, ground, step, limited))
        reference = self.inline((snapshot, ground, step, request))
        self.assertIs(reference.status, SurfacePlanningStatus.COMPLETE)
        self.assertIs(candidate.status, SurfacePlanningStatus.TIMEOUT)
        # The reported count includes the node where the limit was checked;
        # that node's outgoing edges are never expanded.
        self.assertLessEqual(candidate.expanded_nodes, limited.maximum_expansions + 1)
        self.assertLess(candidate.expanded_nodes, reference.expanded_nodes)

    def test_inline_clock_does_not_change_another_thread_or_global_time(self):
        from tests.sim.planning_clock import deterministic_planning_clock
        import time
        fixture = self.fixture()
        clock = time.perf_counter_ns
        host = SimpleNamespace(perf_counter_ns=count(0, 1_000_000_000).__next__)
        with patch.object(known_map_planner, "time", host):
            with deterministic_planning_clock():
                self.assertIs(time.perf_counter_ns, clock)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    candidate = pool.submit(
                        known_map_planner.plan_known_surface_snapshot, *fixture,
                    ).result(timeout=10)
                self.assertIs(candidate.status, SurfacePlanningStatus.TIMEOUT)
            self.assertIs(known_map_planner.time, host)
