# D128 smoke-run-5

状态：**INVALID_FIXTURE_RUN**。

正式 offline preflight、构建和启动均通过，Minecraft／Fabric 已启动。首个 movement trial 尚未提交时，后继窗口虽然已平移参考点，但 `minimum_feet_y`／`maximum_feet_y` 仍停留在冻结夹具的 64 层，真实夹具位于 0 层，因此预重放再次以 `successor_entry` 拒绝。

该运行完成 0／3，`sessions=[]`，不消耗 smoke attempt，也不产生产品结论。D129 允许只修共享 Fabric 夹具，并在新的 append-only 目录 `smoke-run-6` 重试。
