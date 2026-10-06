# R28 时间盒收尾方案

日期：2026-10-04。状态：已完成。最终状态、发布证据与验证结果见[验收第 20 节](../acceptance/navigation-coordination-convergence.md#20-r28-时间盒收尾)；收尾提交为 `4577e3f`。

## 1. 目标

准确结束 R28，补齐可公开复核的证据，同时不再修改生产行为。

本方案只整理状态、证据和公开清单。它不修控制器、不删除疑似死代码、不改变测试判定，也不借收尾继续执行 R28-5。

## 2. 最终状态

收尾后统一使用下面的表述：

| 范围 | 状态 |
|---|---|
| R28-1 | 按冻结范围完成 |
| R28-4 | 按冻结范围完成 |
| R28-3 | 限定安全与功能交付完成；原完整阶段未通过 |
| R28-5 | 未执行 |
| R28 整体 | 时间盒结束，部分交付通过，未完成项已转交 |

R28 的正式批次为 10／10。阶段结束的原因是时间盒用尽，不是全部门槛通过。

## 3. 工作清单

### A. 修正文档状态

修改：

- `AGENTS.md`；
- `packaging/motion-navigation-standalone/AGENTS.md`；
- `packaging/motion-navigation-standalone/README.md`；
- `docs/motion_navigation/stages/navigation-coordination-convergence-r28-plan.md`；
- `docs/motion_navigation/stages/navigation-coordination-r28-3-async-generation-plan.md`；
- `docs/motion_navigation/acceptance/navigation-coordination-convergence.md`；
- `docs/motion_navigation/decisions/0048-converge-recovery-by-risk-and-product-evidence.md`。

必须写清：

1. 批次已经达到 10／10；
2. R28-5 未执行；
3. 结构净减少、两次统计查看、五 tick 响应、正式跟随和室内接近未通过；
4. 五个晚到下降属于时序敏感变化；
5. 迁移入口覆盖是 40／41，`_wait_for_active_terminal` 尚未证明可达或不可达；
6. 最终代码已经由三方在 Linux 上完成 1,207 项、v7、协调集合和补充故障复跑。

不得删除原数字或把后续复跑回记到旧候选。

### B. 发布精简证据

新增：

- `evidence/motion_navigation/r28-4-budget-v1/`；
- `evidence/motion_navigation/r28-3-generation-v1/`。

每个目录只保存：

- `README.md`：范围、源码身份、证据边界；
- `verification.json`：检查名称、结果、来源路径和是否属于最终代码；
- `comparison-summary.json`：逐层完成数、差异和未签署项；
- 必要的 Fabric 摘要、原始文件哈希与复杂度摘要；
- `SHA256SUMS.txt`。

不提交完整日志、视频、世界、Gradle 缓存或 `.tmp` 目录。现有本地原始证据保持不动。

同时更新 `config/motion-navigation/standalone-export-v1.json`，让公开整理版包含两个目录及必需文件。

### C. 核对公开清单

检查：

1. 证据中的源码提交存在；
2. 摘要数字能从现有 `.tmp/r28-3`、`.tmp/r28-4` 和 Fabric 批次重新取得；
3. 两个目录的校验和覆盖所有文件，但不覆盖 `SHA256SUMS.txt` 自己；
4. 独立整理版导出和校验通过；
5. `git diff --check` 通过。

## 4. 完成条件

- 没有生产 Python／Java 文件变化；
- 没有测试行为或门槛变化；
- R28 所有入口文档使用同一个最终状态；
- 公开整理版能验证 R28-3、R28-4 的精简证据；
- 工作区保留全部历史失败和原始结果。

该方案完成后才能开始正式跟随阶段。
