# D128 smoke-run-6

状态：**INVALID_FIXTURE_RUN**。

正式 offline preflight、构建和启动通过，完整窗口平移后的 proof replay 也通过。首个 movement trial 尚未提交时，harness 用实时 `entry_state` 重建 proof，却保留冻结 proof 的 anchor observation／movement tick 身份，触发 `verified motion anchor tick does not match entry`。

该运行完成 0／3，`sessions=[]`，不消耗 smoke attempt，也不产生产品结论。D129 允许只修共享 Fabric 夹具，并在新的 append-only 目录 `smoke-run-7` 重试。
