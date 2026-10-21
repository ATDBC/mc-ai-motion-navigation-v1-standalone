# D122-2 拒绝事务隔离的第五次限定修正

基于 `dee97944`，生产与测试修正提交为 `eaf6db3a`。没有进入 D122-3。

通用拒绝现在只清理“本次 `execute` 已实际安装”的时序成员。Executor 的布尔标记只活在一次同步调用内：开始为 false，`stageWindowed` 或 `armDefault` 成功后才变为 true，并在 `finally` 和 reset 中清零。pending、fallback 和它们的生命周期仍只由 `ClientTimingSchedule` 持有。

动态 RED 覆盖两种同 sequence 冲突：重复请求不能删除已存在的 pending；重复父请求不能删除已存在的 fallback，拒绝响应继续等待父默认 8 个真实采样和 exhaustion。stale 与 episode 拒绝沿用相同的“本次未安装，不能清理”规则。新增 mutation 会故意忽略事务标记，动态检查能够检出。

最终相邻检查 94／94，Java mutation 20／20，Fabric overlay `compileJava --rerun-tasks` 通过。

未运行完整 Windows 分片、公开快照、性能验收、D122-3 或 Fabric。
