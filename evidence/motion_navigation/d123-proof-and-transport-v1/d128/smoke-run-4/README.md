# D128 smoke-run-4

状态：**INVALID_FIXTURE_RUN**。

正式 offline preflight、构建和启动均通过，Minecraft／Fabric 已启动。首个 movement trial 尚未提交时，harness 用过期常量组装后继入口：冻结 proof 的局部终点为 `2.0502355877734346`，夹具写死为 `2.05023666079027`；精确入口窗口只容许 `1e-9`，因此预重放以 `successor_entry` 拒绝。

该运行完成 0／3，`sessions=[]`，不消耗 smoke attempt，也不产生产品结论。D129 允许只修共享 Fabric 夹具，并在新的 append-only 目录 `smoke-run-5` 重试。`manifest.json` 绑定四个 launcher 原始文件及从 source run 原字节复制的结果、失败栈、Runtime trace、physics tick、fixture command 和 teleport 记录。
