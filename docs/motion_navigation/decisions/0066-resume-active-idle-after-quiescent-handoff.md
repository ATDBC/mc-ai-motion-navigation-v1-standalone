# D066：持续目标安全收尾后回到活动待命

日期：2026-10-06。状态：代码、正式链检查和摘要门槛已完成；独立复审没有未关闭的 P0／P1／P2。随后单场 Fabric 复验在新的 pending goal／旧局部入口边界失败，D066 的生命周期修正本身没有被推翻；后续处理见 [D067](0067-pending-goal-blocks-stale-direct-activation.md)。

## 1. 要解决的问题

D065 后的 `move_stop_800_resume_2_0` 满足原有全部 gate，但逐帧记录暴露了一个生命周期缺陷。

目标停止后，路线先用 6 帧完成合法制动。tick 925 时已经同时满足：

- handoff 为 `QUIESCENT`；
- 路线、控制者、身体活动和等待全部为空；
- 持续目标正式为 `SATISFIED`；
- 任务没有取消或终结。

Session 却在后续 94 帧一直保持 `STOPPING / goal_state_satisfied`。原因是 `_retain_satisfied_goal()` 清理身体责任后调用通用 `_continue_execution()`，而该函数为了防止绕过未完成交接，遇到 `STOPPING` 只改 reason，不改变状态。

这没有造成输入泄漏，但违反持续任务的状态定义，并会让无界等待监视器继续计时。原摘要没有检查这条条件，因此把该批写成了 `passed=true`。

## 2. 决定

增加明确的生命周期动作 `RESUME_ACTIVE_IDLE_AFTER_HANDOFF`。它只允许：

- 当前状态是 `STOPPING`；
- 当前帧的正式 handoff 为 `QUIESCENT`；
- 持续目标仍然满足；
- 路线已经安全退役，Session 等待已经结束。

动作的目标状态固定为 `EXECUTING`，表示任务仍活动但当前没有身体控制工作。它不能接受 `RETAIN` 或 `TRANSFERABLE`，也不能代替取消、重规划或路线接替。

普通 `_continue_execution()` 继续禁止直接离开 `STOPPING`。原来的身体责任门槛保持不变。

## 3. 验收门槛

增加两层检查：

1. 正式 Session 检查先进入 `STOPPING`，再提供当前帧 `QUIESCENT` 证据；满足的持续目标必须回到 `EXECUTING`，且没有身体 owner 或等待。
2. Fabric 摘要拒绝这种逐帧状态：目标满足、策略为 `KEEP_ACTIVE_ON_REACH`、尚未请求结束、handoff 已 `QUIESCENT`、路线／控制者／等待均为空，但 Session 仍是 `STOPPING`。正常取消帧使用持久化的 typed `cancellation_requested` 排除，不能按场景名或 reason 豁免。

生命周期检查同时覆盖拒绝边界：没有 handoff、`RETAIN`、`TRANSFERABLE` 和非 `STOPPING` 状态都不能使用这个动作；身体仍有在途输入或释放尾迹时，原 supervisor 继续保留责任。

旧 D065 目录保留。它的原有 gate 结果仍然有效，但不能作为 F1-D 场景通过证据。修复形成干净提交并通过独立复审后，只重跑同一场。

直接相关的生命周期、持续目标、F1 摘要和正式链检查为 51/51。完整 motion_nav 为 1,466/1,471；失败仍是此前冻结的同 5 项，名称和断言没有变化，本轮没有新增失败。

## 4. 后续实机结果

D066 后的同场复验保存在：

`artifacts/f1-known-world-following/20261005T1607326063433Z-move-stop-800-resume-2-0-d066/`

场景在 tick 852 接受 rev11 后进入 `FAILED/same_support_local_path_unavailable`。当时仍有 42 个缺失格，rev11 最终 unanswered，稳定窗口尚未开始。原始 `scenario-result.json` 保持 `passed=false`。

这次失败发生在恢复移动阶段，没有再次出现 D066 修正的“满足目标后停在 STOPPING”现象。它暴露的是另一条共享边界：pending goal 已经要求补事实，旧 local direct 入口却仍在同帧执行。该问题按 D067 单独处理，不能把 D066 批次追认为通过。
