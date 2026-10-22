"""E137：正式 pre-trial 组装从 live anchor 到父请求出站，宿主侧花多少墙钟时间、花在哪里。

原样运行项目组件测试
`tests.motion_nav.test_d122_fabric_timing...test_component_runs_the_exact_fabric_pretrial_assembly_after_fixture_steps`
（四个方向，CalculatorBackend，正式 `_FabricExecutionCase.bind_and_start`）。只包装下列函数记录墙钟，不改返回值：
  - `NavigationSession.execution_anchor`：取得 live anchor 的时刻（t0）；
  - `CalculatorBackend.step`：第一个带 `first_effect_window` 的请求出站时刻（t1），以及当时 backend 的 movement tick；
  - 阶段：`assemble_frozen_fixture_pretrial`、`seal_snapshot_version`、`bind_fixed_successor`、
    `MotionCandidateAdmitter.admit_committed`、`install_manual_route`、`RuntimeNavigationDriver.prepare_proposals`、
    `RuntimeNavigationDriver.advance_prepared`（只计 t0—t1 之间的调用）；
  - `_replay_verified_commands`：t0—t1 之间被调用的次数与总耗时。
CalculatorBackend 的 movement tick 只在每次 step 时加一，不随墙钟前进；所以组件测试里窗口总能命中。
报告同时给出“若客户端按 20 Hz 实时推进，t0—t1 期间会过去多少个 tick”。
用法：python e137_anchor_to_dispatch_budget.py   （在检出根目录运行）
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

TEST = ("tests.motion_nav.test_d122_fabric_timing.D122FabricTimingTests."
        "test_component_runs_the_exact_fabric_pretrial_assembly_after_fixture_steps")


def main():
    from mc2p.motion_nav import motion_solver, navigation_session, motion_candidate, world_snapshot
    from mc2p.skills import navigation_session_driver
    from scripts import d122_fabric_timing as probe
    from tests.sim import backend as sim_backend

    rows, current = [], {}
    perf = time.perf_counter_ns

    def wrap(owner, name, label, *, method=True):
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            started = perf()
            try:
                return original(*args, **kwargs)
            finally:
                if current.get("t0") is not None and current.get("t1") is None:
                    current["phases"].setdefault(label, []).append((perf() - started) / 1e6)
        setattr(owner, name, wrapped)

    original_anchor = navigation_session.NavigationSession.execution_anchor

    def anchor(self, *args, **kwargs):
        result = original_anchor(self, *args, **kwargs)
        if current.get("armed"):
            current.update(armed=False, t0=perf(), anchor_tick=result.movement_tick_id, phases={}, t1=None)
        return result
    navigation_session.NavigationSession.execution_anchor = anchor

    # Only the anchor read inside the formal bind_and_start counts as t0.
    original_bind = probe._FabricExecutionCase.bind_and_start.__func__

    def bind_and_start(cls, *args, **kwargs):
        current.clear()
        current["armed"] = True
        return original_bind(cls, *args, **kwargs)
    probe._FabricExecutionCase.bind_and_start = classmethod(bind_and_start)

    original_step = sim_backend.CalculatorBackend.step

    def backend_step(self, action, *args, **kwargs):
        if (current.get("t0") is not None and current.get("t1") is None
                and getattr(action, "first_effect_window", None) is not None):
            current["t1"] = perf()
            current["backend_tick_at_dispatch"] = self.movement_tick
            current["window"] = [action.first_effect_window.earliest_tick, action.first_effect_window.latest_tick]
            elapsed_ms = (current["t1"] - current["t0"]) / 1e6
            rows.append({
                "anchor_movement_tick": current["anchor_tick"],
                "parent_window": current["window"],
                "calculator_tick_at_dispatch": current["backend_tick_at_dispatch"],
                "anchor_to_dispatch_ms": round(elapsed_ms, 1),
                "real_time_ticks_elapsed_at_20hz": int(elapsed_ms // 50),
                "phases_ms": {k: [round(v, 1) for v in vs] for k, vs in current["phases"].items()},
            })
            current.clear()
        return original_step(self, action, *args, **kwargs)
    sim_backend.CalculatorBackend.step = backend_step

    wrap(probe, "assemble_frozen_fixture_pretrial", "assemble_frozen_fixture_pretrial (replay)")
    wrap(motion_solver, "_replay_verified_commands", "_replay_verified_commands (all callers)")
    wrap(motion_solver, "bind_fixed_successor", "bind_fixed_successor")
    wrap(world_snapshot, "seal_snapshot_version", "seal_snapshot_version")
    wrap(motion_candidate.MotionCandidateAdmitter, "admit_committed", "admit_committed")
    wrap(navigation_session_driver.RuntimeNavigationDriver, "prepare_proposals", "driver.prepare_proposals")
    wrap(navigation_session_driver.RuntimeNavigationDriver, "advance_prepared", "driver.advance_prepared")
    wrap(navigation_session_driver.RuntimeNavigationDriver, "start", "driver.start")
    from scripts import d121_handoff_execution_probe as handoff
    wrap(handoff, "_install_manual_route", "install_manual_route")

    result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(
        unittest.defaultTestLoader.loadTestsFromName(TEST))
    print(json.dumps({"test_passed": result.wasSuccessful(), "directions": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
