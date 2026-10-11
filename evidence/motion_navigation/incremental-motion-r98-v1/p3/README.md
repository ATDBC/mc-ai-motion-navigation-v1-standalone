# D098 P3 隔离滚动闭环证据

本目录记录 Windows 正式平台上的 P3 修正结果。`closed-loop-matrix.json` 由项目自有 runner 生成，包含 37 个场景、3 轮运行、逐场终态、输入授权来源和全部决定延迟。

这份证据只覆盖实验 owner、实验提交／应用账本和项目 1.21 运动计算器。它没有经过 Runtime、NavigationSession、正式客户端账本或 Fabric。

结论是 **P3 未通过**：1,227 次模拟后端推进全部能追溯到 owner、ledger 和许可或时序证明，无界结果为 0，四项变异都被提交门禁阻止。但 54 个运行结束为 `BODY_RESPONSIBILITY_UNRESOLVED`，6 个晚到运行超过伤害额度；承诺后恢复为 0／27。P95／P99 为 15.8503／19.8099 ms，也超过 8／15 ms。P4 因此未授权。
