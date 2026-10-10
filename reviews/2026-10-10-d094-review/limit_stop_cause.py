"""D094 审查探针：恢复额度耗尽时，交给执行器的停止原因是否改变。

运行：在 19231c6 和 34d8ba1 两个源树根目录分别执行
    PYTHONPATH=. python <本文件>
Session 在额度耗尽后调用 _request_ending(FAIL, request.cause)，
随后 supervisor.request_route_stop(request.cause) 把该原因交给执行器。
执行器只对 DEPENDENCY_CHANGED 改变停止方式（空中放弃已证明的剩余命令、
地面高速时按住潜行）。
"""
from dataclasses import replace

from mc2p.motion_nav.body_control import StopCause
from mc2p.motion_nav.navigation_handoff import HandoffDestination, NavigationHandoffCoordinator
from mc2p.motion_nav.retry_ledger import (
    RecoveryBudgetPolicy, RecoveryIdentity, RetryCause, RetryLedger, TaskDemandState,
)
from tests.motion_nav.test_navigation_handoff import _quiescent
from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner


def exhausted_stop_cause(cause):
    _, current, _, _, _ = gap_owner(DeferredMotionWorker())
    owner = NavigationHandoffCoordinator()
    budget = RetryLedger("probe", policy=RecoveryBudgetPolicy.finite(maximum_recoveries=1))
    # 第一次恢复用掉额度（与 D094 自己的测试相同写法）。
    permit = owner.observe_task_activity(
        budget=budget, observation_sequence=1, demand_state=TaskDemandState.UNMET)
    owner.request_recovery(
        request_id="first", destination=HandoffDestination.REPLAN, reason="first",
        budget=budget, activity_permit=permit, recovery_identity=RecoveryIdentity(1, "first"))
    handoff = replace(_quiescent(), world_session=current.session,
                      observation_sequence_id=current.body.sequence_id)
    owner.advance(current, handoff=handoff, goal_ready=True, start_ready=True,
                  missing_cells=(), unavailable_reason="unavailable", budget=budget)
    owner.complete_replanning() if hasattr(owner, "complete_replanning") else None
    # 第二次：与 Session 第 2688 行相同的 planning recovery，额度已耗尽。
    permit = owner.observe_task_activity(
        budget=budget, observation_sequence=2, demand_state=TaskDemandState.UNMET)
    result = owner.request_recovery(
        request_id="probe/planning-recovery/2", destination=HandoffDestination.REPLAN,
        reason="active_route_dependency_changed", budget=budget, cause=cause,
        retry_cause=RetryCause.DEPENDENCY, activity_permit=permit,
        recovery_identity=RecoveryIdentity(2, "active_route_dependency_changed"))
    request = owner.stop_request
    return result.status.value, None if request is None else (request.destination.value, request.cause.value)


for cause in (StopCause.DEPENDENCY_CHANGED, StopCause.INPUT_LOST, StopCause.MOTION_UNSOLVABLE):
    print(f"recovery cause={cause.value:20s} -> status, (destination, stop cause) = {exhausted_stop_cause(cause)}")
