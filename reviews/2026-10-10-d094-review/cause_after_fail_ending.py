"""D094 审查探针：输入失联补走期间，发生与之无关的 FAIL 终止，报告原因是什么。

运行：在仓库根目录 PYTHONPATH=. python <本文件>
对照 D094 决定："只有这次恢复直接引发的规划或额度失败继续保留 INPUT_LOST"，
"之后的无关失败不继承它"。显式 cancel 已有测试（报告 CANCELLED）；这里换成
三种走 FAIL 目的地的结束：内部契约故障、风险登记失败、控制不可用。
"""
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.motion_nav.body_control import StopCause
from scripts.action_entry_late_hardening import scenario_for
from tests.sim.runner import run


def probe(name, act):
    reports = []
    fired = False

    def step(context):
        nonlocal fired
        context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
        session = context.session
        if not fired and session.report.failure_cause is StopCause.INPUT_LOST:
            act(session)
            fired = True
        request = session._handoff.stop_request
        reports.append((session.report, None if request is None else request.cause))
        return ()

    result = run(scenario_for("column_landing_turn", 4), control_step=step)
    report, stop_cause = reports[-1]
    first_stop = next((cause for _, cause in reports[::-1] if cause is not None), None)
    print(f"{name:28s} outcome={result.outcome:9s} reason={result.reason:40s} "
          f"report.failure_cause={str(None if report.failure_cause is None else report.failure_cause.value):12s} "
          f"last_stop_request.cause={None if first_stop is None else first_stop.value}")


def risk_failure(session):
    session._risk_failure_reason = "risk_probe"
    from mc2p.motion_nav.navigation_handoff import HandoffDestination
    session._request_ending(HandoffDestination.FAIL, StopCause.CANCELLED, "risk_probe")


probe("explicit cancel (baseline)", lambda s: s.cancel("probe_cancel"))
probe("internal contract failure", lambda s: s.handle_internal_contract_failure("probe_internal_fault"))
probe("risk ledger failure", risk_failure)
