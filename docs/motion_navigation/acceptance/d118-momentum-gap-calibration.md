# D118-0 带动量连续跨隙校准验收

日期：2026-10-18。状态：**FAILED／STOPPED。** C0—C3 的实现和组件检查已经完成；最新 Windows 朝南 smoke 触发物理一致性门槛，本记录不签署 D118-0。

## 1. 验收范围

本轮只校准计算器、输入实际生效 tick 和潜行 fallback。它不启用生产四格跨隙、连续两跳或 prepared 自动后继。

## 2. 前置探索证据

以下数字来自三方在项目 1.21 运动计算器上的探索，尚未成为本项目签署结果：

- 冻结的停稳四格跳候选中，允许输入流首条晚一 tick 的候选为 `0／79`；
- 长平台停稳助跑候选中，对同一时序分支容忍的候选为 `0／451`；
- 带动量连续入口候选中，对同一时序分支容忍的候选为 `530／530`；
- 现行第一跳约以 `1.42 b/s` 落地，目标区域约为 `[1.3, 1.7]`；
- 后续四格跳探索需要约 `5 b/s` 的入口，落地点约为 `[1.86, 2.30]`；
- 高速动量落地后，neutral 与 back 不能可靠停在一格平台，sneak 可以；sneak 若到 `L+2` 才生效，探索中只有 `158／601` 能停住。

这些数字必须由项目测试重放。它们只适用于冻结模板，不能解释为对 Minecraft 全部技巧的穷举。

## 3. 正式门槛

### 3.1 输入和时间线

- 每条输入的 Runtime receipt、物理 sidecar 和 F-TICK request sequence 唯一对应；
- observation、decision、receipt、sampled、applied 和 post-state 的 movement tick 完整；
- 后台交付抖动、输入流起点 0／1 tick 和流中段偏离分别报告；
- 落地 tick `L` 能从 authoritative post-state 唯一确定；
- 后继未获批时，潜行 fallback 最迟在 `L+1` 实际生效。

### 3.2 物理一致性

- 每轴位置误差 `<= 0.001` 格；
- 每轴速度误差 `<= 0.0001` 格／tick；
- 着地、离地、碰撞、疾跑、潜行和姿态逐项一致；
- 走出边缘最后一个着地 tick 的起跳结果与计算器一致；
- 落地即跳的实际生效 tick 与计算器允许分支一致。

### 3.3 安全和余量

- neutral 与 back 负对照不能被误报为安全；
- sneak 正例在四方向都把身体留在中间平台；
- 计算器与实机误差后，第二跳入口和 fallback 均保留正余量；
- 零伤害、零 UNKNOWN 当空气、零额外输入出口、零 owner 空档；
- INVALID 运行不计入通过。

### 3.4 运行规模

- 四族各一场朝南 smoke 全部有效；
- smoke 通过后，四方向各 3 次；
- 固定输入清单和输入哈希在所有运行中不变。

## 4. 结果表

| 检查 | 结果 | 证据 |
|---|---|---|
| 项目计算器 RED | 通过：`79／0`、`1528／530`、fallback `0／0／601／158`；现行一格跨隙出口上限 `0.15 b/s` 与四格接续入口 `> 4.5 b/s` 不重叠 | `test_calculator_replays_frozen_timing_and_fallback_claims` |
| 固定输入与清单哈希 | 通过；smoke 为 7 个试次、273 条命令、SHA `23379a35…a83b6f`；full 为 84 个试次、3276 条命令、SHA `4b6866f1…3c95`；任一 tick 输入变化都会改变哈希 | `test_frozen_manifest_separates_four_families_and_three_timing_kinds`、`test_manifest_hash_detects_any_runtime_input_change` |
| Runtime／physics／F-TICK 唯一归因 | 真实 Java 事件形状的组件契约通过；封存器先重算 manifest 内容哈希，再逐命令核对输入、stream、phase 和 timing kind。保留旧哈希改 manifest，或同步篡改 receipt、applied 和 physics 输入，都会被拒绝 | `test_manifest_document_hash_is_recomputed_before_frozen_lookup`、`test_timeline_join_rejects_duplicate_or_cross_request_sidecar_rows`、`test_frozen_manifest_rejects_jointly_mutated_command_evidence`、`test_wrapper_reads_and_joins_real_sidecar_shapes_before_recording` |
| 每个 trial 独立重建地形 | 通过组件检查；fixture 的 clear／support 行都绑定 trial、origin 和 feet_y，并逐位置等于 `trial_fixture_rows` 的冻结输出。复用同一坐标的不同 trial 不会互相覆盖，改动一个地形格也会被拒绝 | `test_fixture_log_row_binds_each_geometry_phase_to_its_trial`、`test_physics_replay_uses_a_separate_declared_world_per_trial` |
| 逐 tick／开环物理重放 | 真实 physics schema 组件夹具通过；每轴位置、速度、yaw、pitch、着地、两类碰撞、疾跑、潜行、姿态和 contact event 都会报告；数值、离散状态和缺证据 mutation 均被发现 | `test_physics_replay_checks_numeric_and_all_discrete_fields`、`test_physics_replay_marks_missing_trial_fixture_identity_unverified` |
| smoke 声明世界完整性与 fallback | 7 个 south trial 的全部 tick 都能在各自声明世界内重放；neutral／back 保持负例，sneak 为正例。平台余量按中心可位于边界外 0.3 格的真实覆盖语义计算，并检查横向边界 | `test_all_south_trials_replay_in_declared_world_and_freeze_fallbacks` |
| sneak 生效期限 | sneak 正例必须恰好识别一次落地，并在 `L` 或 `L+1` 实际生效；缺落地、缺 sneak 和 `L+2` 均为 typed failure | `test_sneak_fallback_requires_one_landing_and_application_by_l_plus_one`、`test_all_south_trials_replay_in_declared_world_and_freeze_fallbacks` |
| 离地、落地与输入应用 tick | 组件检查按 authoritative `landed` 事件识别 `L`，并报告第二流 jump 与 sneak 的实际 applied tick；真实 tick 待 Fabric | `test_event_timing_uses_authoritative_landing_and_actual_input_tick` |
| 固定输入经过正式 Runtime | 朝南 7 个 variant 全部经过正式 Runtime；request sequence 无重复；四方向变换保持输入不变 | `test_smoke_runtime_sends_every_fixed_tick_through_formal_runtime`、`test_direction_transform_rotates_geometry_without_changing_inputs` |
| 第 0 帧地形事实 | 脚下支撑、所需跑道、间隙和净空不完整或不符时拒绝开始 | `test_tick_zero_fixture_gate_rejects_unknown_world_facts` |
| 独立 Fabric CLI 与源码冻结 | 通过组件检查；首次真实运行结果见下一行 | `test_deployment_cli_requires_both_sidecars_and_is_mutually_exclusive`、`test_deployment_freeze_selects_d118_runtime_and_sidecars` |
| 历史 Windows smoke | **INVALID HARNESS／FAILED。** JDK、锁定资产、独立导出、setup 和早期 F-TICK 关联问题产生的运行全部原样保留，不计入通过，也不被最终运行覆盖 | `evidence/motion_navigation/d118-momentum-calibration-v1/smoke*` |
| 最新朝南 smoke | Fabric probe 通过；273／273 条命令的 Runtime receipt、physics 和 F-TICK 身份逐项对应。7 个 trial 中 6 个与计算器一致；`fallback-back-south-0` 触发物理门槛，D118-0 失败并停止 | `evidence/motion_navigation/d118-momentum-calibration-v1/smoke-ftick-join-reanalysis/`、`evidence/motion_navigation/d118-momentum-calibration-v1/stop.json` |
| 四方向小矩阵 | 未运行；朝南 smoke 已触发停止条件 | — |
| 位置／速度误差 | 6 个 trial 满足逐 tick 对照；`fallback-back-south-0` 从 movement tick 246 起偏离，后续连续量也超过门槛。该 trial 的数值不能与其他 6 个 trial 合并成“整体误差” | `physics-replay.json` |
| 离散状态一致性 | **失败。** movement tick 246 起，着地、水平／垂直碰撞和 contact event 与计算器不同。根因是项目计算器遗漏原版会考虑的 0 高度 step 候选 | `D118-PHYSICS-01`、`physics-replay.json` |
| `L+1` 潜行 fallback | 实机在 `L+1` 应用潜行并安全停住；最终纵向余量只有 `0.000551` 格。该正结果保留，但不能覆盖物理门槛失败，也不足以授权生产 fallback | `fallback-sneak-south-0` |
| 第二跳输入时序 | 按时输入在 `L+1` 生效；late1 在 `L+2` 生效。现有逐 tick Runtime 只能测串行提交，不能把结果写成着地 tick `L` 的预排能力 | `landing-immediate-south-0`、`landing-immediate-late1-south-0` |
| 第二跳入口剩余余量 | typed `unverified`：当前没有冻结可证明安全的入口状态包络，不能用探索矩形代替 | `physics-replay.json` |
| Windows 受影响回归 | D118 直接 26／26；D118、F-TICK、D113、D114 和部署探针相邻集合 104／104 | 本地最终字节检查 |
| 公开快照 | 本次失败收尾未运行新的公开快照门禁 | — |

## 4.1 当前组件结果

以下命令在 Windows 源码树通过：

```powershell
.\.venv\python.exe -m unittest `
  tests.motion_nav.test_d118_momentum_calibration `
  tests.motion_nav.test_d105_ftick `
  tests.motion_nav.test_d113_targeted_probe `
  tests.motion_nav.test_d114_b10_formal_driver `
  tests.test_fabric_deployment_probe -v
```

最终源码树 D118 直接 26 项通过；D118、F-TICK、D113、D114 和部署探针相邻集合共 104 项通过。较早公开仓库完整性与独立快照检查 26 项通过；本次失败收尾没有重跑公开快照。

组件检查通过只说明固定输入、Runtime 出口、部署接线和 C3 分析逻辑能工作。最终 Fabric 证据已经证明身份链完整，也正是这套检查发现了计算器与原版碰撞结果不一致。当前 Runtime 逐 tick 串行提交输入，只能测量下一次可用的 `L+1` 应用；它没有在着地 tick `L` 预排输入，也没有注入真实传输晚到。第二跳入口余量仍为 typed `unverified`。因此 D118-0 明确失败，不授权 D118-1。

## 5. 证据边界

- D117 的 Linux 复核只说明 prepared 组件链成立；
- B09-R 的 Fabric 数据只校准过一格跨隙、下降和地面稳态；
- 本轮只有朝南 smoke；四方向 full matrix 未运行；
- 生产 profile、求解器、规划器、Session 和 prepared 默认路径均未接入；
- 失败根因限定为当前计算器遗漏原版 0 高度 step 候选；不能外推为全部动作物理都不可信；
- D118-0 已停止，D118-1—D118-5 均未授权；恢复工作必须另写决定。

## 6. 保留的失败证据

`evidence/motion_navigation/d118-momentum-calibration-v1/` 下的 `smoke`、`smoke-cached`、`smoke-java21`、`smoke-locked-assets`、`smoke-postprocess-repair`、`smoke-setup-repair` 和最终 `smoke-ftick-join-reanalysis` 均保留。前几项记录夹具和环境问题，最终目录记录身份链通过后的物理门槛失败。两类结果不能互相覆盖。
