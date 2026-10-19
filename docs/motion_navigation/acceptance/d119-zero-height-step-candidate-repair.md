# D119 0 高度 step 候选修复验收

日期：2026-10-19。状态：**COMPLETED。**

关联：[D119](../decisions/0119-repair-zero-height-step-candidate.md)、[实施计划](../stages/d119-zero-height-step-candidate-repair-plan.md)、[D118 验收](d118-momentum-gap-calibration.md)和[缺陷台账](defect-ledger.md)。

## 1. 修复前事实

现有 D118 Windows raw run 的身份链完整：273／273 条命令可以唯一对应 Runtime receipt、physics 和 F-TICK。`fallback-back-south-0` 的独立逐 tick 对照只有 movement tick 246 不一致。

该帧中，计算器漏掉原版的 0 高度 step 候选：

| 项目 | 旧计算器 | Fabric／正确候选 |
|---|---:|---:|
| y | `-0.0784000015258789` | `0.0` |
| z | `29.3` | `29.29013755153609` |
| on_ground | false | true |
| horizontal_collision | true | false |
| vertical_collision | false | true |
| 事件 | `horizontal_collision, left_ground` | `vertical_collision` |

正确候选按相同真实预状态算出的 z 为 `29.29013754582968`，与 Fabric 相差约 `5.7e-9`。因此本阶段不调整验收容差。

## 2. 验收结果

| 门槛 | 结果 | 证据 |
|---|---|---|
| tick 246 RED 先红后绿 | 通过 | 旧实现错误下降 `0.0784000015258789` 格；当前实现对齐 Fabric |
| 0 高度候选不发布 `step_up` | 通过 | `test_d118_tick_246_zero_height_step_preserves_ground_contact` |
| Java `float` 候选规则 | 通过 | 覆盖相等排除、极小正高度和向下碰撞后的原始 `baseY` |
| D118 raw 只读重放 | 通过 | 7 个 trial、273 个 tick，离散差异 0；原 raw 哈希未改 |
| 连续量误差 | 通过 | 最大位置误差 `1.9745570511986443e-6` 格；最大速度误差 `7.923378675389614e-8` 格／tick |
| B09-R physics step／rollout／contracts／validation、B07 和 ground traversal | 通过 | 直接 9 模块 95／95 |
| motion solver／候选 | 通过 | 89／89 |
| D118 组件检查 | 通过 | 27／27 |
| 冻结离线表、控制器与轨迹实验 | 通过 | 67／67；表与输入哈希不变 |
| 历史恢复路径 | 通过 | seed 163 改用已证明的地面后退；独立故障注入继续覆盖空中 `NEEDS_STATE`、落地重锚和 owner 释放 |
| 旧成功退步 | 通过 | 0；没有任务由成功变为失败 |
| Windows 源码树正序／逆序 | 通过 | 各 196／196 模块、2507 项，零失败、错误、超时和跳过 |
| 独立公开快照正序／逆序 | 通过 | 各 195／195 模块、2483 项，零失败、错误、超时和跳过 |
| 公开导出 | 通过 | 2598 个文件；封闭哈希验证通过，D119 当前语义哈希单独发布 |

## 3. 证据边界

- 本阶段使用 D118 已有 raw run 作为只读物理对照，不运行新的 Fabric。
- D118 的失败、INVALID 记录和 `stop.json` 不改写。
- 即使 D119 签署，也只说明这一条计算器规则修好。D118 的入口包络仍未验证，D118-0 不自动恢复。
- D118 full matrix、连续两跳、prepared 自动后继和生产新能力均不属于本验收。
- 本轮没有运行新的 Fabric。实机对照只使用 D118 已封存的 Windows raw。

## 4. 止损记录

未触发停止条件。完整门禁中曾发现两项预期外结果，均已先归因再处理：

1. seed 163 的旧测试固定了错误计算器产生的空中恢复路径。任务本身仍成功。测试已拆分为地面恢复正例和独立空中故障路径，没有放宽成“任一结果都行”。
2. 独立导出仍用旧 F2 语义哈希解释当前 `physics_1_21.py`。D119 新增当前语义哈希，旧证据保持不变。

没有更新冻结表、放宽容差或改写 D118 历史证据。

