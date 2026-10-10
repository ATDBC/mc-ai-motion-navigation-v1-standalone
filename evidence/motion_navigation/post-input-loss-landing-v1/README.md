# D094：安全落地后的目标恢复证据

状态：已完成并签署。Windows 完整正序、逆序各 1705／1705；旧集合零成功退步；D058／D061 通过；Fabric 四方向 4／4。

- `red/focused.txt`：修复前两个真实行为断言失败。
- `red/fixed/`：源树 d9ad674e 两个落点族各种子 1—8，均为 3／8。
- `red/cancel-priority.txt`：第二轮原因优先级的正式链 RED，取消曾误报 INPUT_LOST。
- `green/fixed/`：两个落点族各 8／8。
- `green/random100/`：六族各一百种子的输入、索引和压缩原始轨迹。
- `green/paired100.json`：对 D093 同输入的逐项比较，122 项新增完成、478 项运动不变、旧成功退步 0。
- `green/affected-tests-final.txt`：最终受影响五模块 73／73。
- `provenance.json`：矩阵源码与最终源码同时记录；第二轮仅改变取消、修订、未接受请求的原因优先级，600 项无这些注入事件，控制行为不变。
- `regressions/`：v7、F2 528、v8、v9 的冻结对照结果。
- `performance/`：D058、D061 的紧凑摘要和压缩原始 JSON。
- `fabric/`：四方向计划、结果摘要、逐试次记录和压缩逐帧结构化轨迹。

D093 目录、原验收正文和所有原始结果保持原字节。当前测试随 D094 契约更新，不能把新通过回写成历史通过。完整门槛见 docs/motion_navigation/acceptance/post-input-loss-landing-recovery.md。

模拟中的第 3 号严格空中命令被延迟后，以 `SUPERSEDED` 结束，没有实际应用，因此模拟只证明输入失联后能够恢复。Fabric 四方向均取得 `applied_outside_window`、`actual_offset=2` 和着地 `INPUT_LOST` 恢复记录，随后补走完成；对应实机门槛已经签署。
