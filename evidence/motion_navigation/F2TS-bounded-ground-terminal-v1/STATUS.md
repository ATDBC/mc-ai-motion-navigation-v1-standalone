# F2-TS 证据状态

当前门槛结果是 **未通过**。以 `isolated-worker-gate-failure/` 为准。

`formal-oracles/`、`formal-remaining9/` 和 `formal-remaining42/` 是 2026-10-08 的历史运行。它们使用 `InlineMotionWorker`：搜索在控制线程的 `poll_available()` 中同步执行，而且搜索期间模拟运动 tick 不推进。因此这些文件保留用于追溯，但不能证明后台交付、执行窗口或控制性能，也不能用于关闭 F2-TS。

干净 Windows 提交 `a8cb3bfb` 使用真实 `PlannerWorker` 与 `MotionSolverWorker`。第一个 oracle 已有界失败，项目随即按 fail-fast 停止，没有运行其余 oracle、remaining9、remaining42、F2-GP、大集合、D061 或 Fabric。
