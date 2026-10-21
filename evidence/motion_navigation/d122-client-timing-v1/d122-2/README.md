# D122-2 Java 采样门禁与安全默认

基于 `9cda6c03` 实施，生产与测试提交为 `3be9aaba`、`0752ce9e`、`6569c97f` 和 `775cafd2`。本切片只完成 Java 客户端合同；没有进入 D122-3，也没有运行 Fabric。

客户端时序状态只有一个 owner：`ClientTimingSchedule` 持有完整 pending 请求和一个默认输入。Executor 不另存 pending 生命周期，只在 schedule 选中窗口请求时尝试原子激活。激活成功后才清旧默认；激活失败继续使用旧默认。后继请求可以携带自己的默认，它只在后继实际激活成功后安装。

v3 部署会话协商 `mc2p.client_action.v2`，v2 会话保持 action v1。v1 decoder 仍要求旧键集合完全一致。v2 拒绝超界 tick、反向窗口、递归默认和额度不足。窗口化请求在本阶段只支持 movement＋look；`operation` 非空会在排队前得到 `windowed_operation_unsupported`，避免用旧朝向执行或错过 player-tick 采样边界。

`prepareForPlayerTick` 在原版读取 sneak 前选择当前、pending 或 default，并同步覆盖 sneak 的 true／false。`Input.tick` 每拍只消费一次。默认从绝对 tick 开始，完整采样声明时长；wall 或父响应期限不能截短。`default_exhausted` 在 duration 结束后的下一采样边界报告。窗口 missed、pending 激活失败、合法 cancel、普通替换和 reset 都走 schedule 的唯一清理入口。

检查结果：

- 相邻 Python／Java 动态检查 89／89 通过；
- Fabric overlay `compileJava --rerun-tasks` 通过；
- 11／11 Java 变异被检查发现；
- action decoder 上限保持 16 KiB；timing event 队列最多 64 项，溢出立即失败；
- `red-before-java.txt` 保存实现前 decoder、部署会话和 schedule 的真实编译失败。

证据文件：

- `java-adjacent.txt`：89 项动态检查；
- `compile-java.txt`：正式 overlay 编译；
- `mutations.json` 和 `mutation-run.txt`：11 种变异；
- `red-before-java.txt`：TDD RED。

未运行完整 Windows 分片、独立公开快照、D122-3 组件三态、性能签署或 Fabric。正式 v2 host/probe 构造点仍留 D122-3；本结果不能宣布端到端能力完成。
