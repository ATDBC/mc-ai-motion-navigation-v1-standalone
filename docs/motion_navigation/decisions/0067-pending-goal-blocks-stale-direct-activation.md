# D067：待补事实的新目标阻止旧局部入口激活

日期：2026-10-06。状态：代码、正式链检查和同场 Fabric 复验已完成；独立复审没有未关闭的 P0／P1／P2，`move_stop_800_resume_2_0` 已正式通过。

## 1. 要解决的问题

D066 后只重跑了 `move_stop_800_resume_2_0`。目标恢复移动时，Session 在 tick 852 接受 rev11，随后同一控制帧进入 `FAILED/same_support_local_path_unavailable`。

当时的新目标仍缺 42 个格子事实。`update_goal()` 已经把 rev11 保存为 pending goal，并要求补充观察；但旧 revision 留下的 local direct 入口仍在。`propose()` 随后用“旧起点／旧终点节点 + 新 GoalState”调用 local admission，错误地把等待信息变成终态失败。

只读诊断记录为 `GOAL_SELECTION / BLOCKED / CURRENT_BODY_CANNOT_CONNECT`。它描述的是错误触发的旧 local admission，不能据此把 42 个未知格解释成已知阻塞。

这不是站位选择或连接几何再次失败。真正的边界是：新目标还在等待事实时，旧局部入口没有执行资格。

## 2. 决定

`PendingGoalRevision` 继续由现有目标账本和信息获取链拥有。只要 pending goal 存在：

- 不激活残留的 same-support local direct；
- 不激活残留的 ground direct；
- 不丢弃当前身体 owner，也不清空它的安全收尾责任；
- 保持 `NEEDS_INFORMATION`，通过正式 `ObservationRequestV3` 请求缺失事实。

事实到达后，只允许现有 `_resume_pending_goal()` 从当前观察重新求起点和目标支撑面，再交给既有 `_accept_goal_request()`。本修正不另建目标重绑入口，不增加请求序号，不购买恢复，也不把 missing 改写成 blocked。

如果事实齐全后仍找不到支撑面，继续返回原有明确不可用结果。没有 pending goal 的 local、ground-direct 和后台规划行为不变。

## 3. 验收

正式公共时间线检查覆盖两种残留入口：

1. 旧请求是 same-support local direct；
2. 旧请求是跨多个同高方块的 ground direct。

两种情况下，新 revision 在同一帧发现目标面缺事实后，两个 admitter 都不得被调用，Session 必须保持 `NEEDS_INFORMATION`，正式观察请求必须非空。下一帧补齐所请求的事实后，pending goal 必须清除，请求的目标节点必须按当前目标面重算。

相关 D062、D064、目标策略、生命周期和 F1 检查为 86/86。独立复审确认中央 guard 覆盖真实时间线，且不阻断 incumbent 身体责任，没有未关闭的 P0／P1／P2。

同场 Fabric 复验使用来源提交 `45eb73f`，目录为：

`artifacts/f1-known-world-following/20261005T1647399858283Z-move-stop-800-resume-2-0-d067/`

tick 852 的 rev11 正确进入 `NEEDS_INFORMATION`，pending revision 为 11，正式请求 42 个空气事实，路线、身体 owner 和输入均为空。tick 853 事实到齐后，pending 才清除并接纳 `request-13-direct`，修订响应为 2 tick。全场 20 个修订全部生效，响应 P95 为 4 tick；规划比为 4/20，稳定样本 27，恢复和安全违规均为 0，最终距离为 0.950978 格。

独立复审重算了 1,019 帧和 11 组分段哈希。`satisfied_idle_stopping_ticks` 为空，合法制动期间仍有身体责任，释放后 handoff 为 `QUIESCENT`。终态正常取消，source、host、time 和 cleanup 均通过。该场正式签署，下一场按 fail-fast 进入 `normal_cancel_2_0`。
