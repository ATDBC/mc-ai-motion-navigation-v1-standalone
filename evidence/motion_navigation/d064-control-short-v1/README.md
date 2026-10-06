# D064 控制路径短参数回归

这份低成本回归来自干净提交 `546e6e668d30ab22fa84df57d92fe3630dbf6f7b`。它使用 D061 原有工具和短参数 `warmup=5 / retained=20..50 / post-gen2=5`，用于检查 D064 最终代码有没有立即破坏既有控制期限。它不能替代 D061 v4 长 Session 证据。

- prepare P95／P99／最大值：3.9066／3.9725／3.9725 ms；
- 正式控制路径 P95／P99／最大值：21.5256／23.2795／23.2795 ms；
- deadline miss：0；最小余量：26.7205 ms；
- 首个 retained gen2：ordinal 25，后续样本完整；
- 18 项 gate 全部通过。

此前用 `warmup=1 / retained=8..16 / post-gen2=2` 做过一次过短诊断。该次没有 retained gen2，`gen2_coverage=false`，整体退出 1。`summary.json` 保留它的命令、配置和 gate，不能把它写成通过。最终结果只采用上面的冻结短参数运行。

公开证据只保存统计、门槛、来源哈希和 trace 交付摘要，不复制短跑 trace 或逐帧原始数组。长期结论继续引用 `evidence/motion_navigation/d061-long-session-v4-formal/`。
