# D122-2 接纳顺序限定修正

基于 `35b784c6`，生产与测试修正提交为 `91034d29`。没有进入 D122-3。

`ClientTimingAdmission` 先消费请求的 episode、sequence 和 observation，再检查 D122 时序语义。`safe_default_activation_mismatch` 与 `windowed_operation_unsupported` 都不会安装 lease 或 pending；同一 sequence 的修正 payload 会被 `ClientRequestGate` 拒绝为 duplicate。

`red.txt` 保存新生产类不存在时的动态失败。最终相邻检查 92／92，Java mutation 16／16，Fabric overlay `compileJava --rerun-tasks` 通过。

未运行完整 Windows 分片、公开快照、性能验收、D122-3 或 Fabric。
