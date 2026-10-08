# F2-SC 普通地面安全尾迹与完成判断验收

日期：2026-10-08。状态：聚焦门槛止损，阶段未通过。

本验收回答三个问题：当前输入是否始终可以安全退出，机器人是否真的已经完成任务，以及一／两 tick许可是否只改善控制质量而不改变逐 tick执行语义。

## 1. 冻结基线

| 项目 | 当前事实 | 本阶段要求 |
|---|---|---|
| F2-PC RED | 保留 | 不改写 |
| F2-PC 定向组合 | `46/46` | 只作历史局部证据 |
| F2S-C-04 首次正式结果 | `no_safe_ground_candidate` | 保留 |
| 补齐正式一 tick尾迹后的结果 | `fixed_route_stalled` | 保留 |
| 剩余 9 项、大集合、D061、Fabric | 未运行 | 主例通过后才运行 |

F2-PC 继续记为未通过。F2-SC 新建自己的 RED、错误副本和证据，不把旧失败重命名成新阶段成功。

## 2. 硬安全尾迹

每个候选都要保存并检查所有可能前缀：

| 许可 | 必须检查的方向输入前缀 | 随后输入 |
|---|---|---|
| 1 tick | 0、1 tick | 中性直到现有停止条件或尾迹上限 |
| 2 tick | 0、1、2 tick | 中性直到现有停止条件或尾迹上限 |

每条轨迹都要满足已知安全支撑、路线走廊、净空、碰撞、危险接触、姿态和伤害约束。尾迹可以停在 completion 外，但不能离开安全走廊。

删掉 `0` tick、中间前缀或中性尾迹中的任何一种检查，都必须让至少一个晚到、丢失、坑边或墙边错误副本失败。

## 3. 完成判定

必须同时满足：

1. 当前身体位置在 completion 内；
2. 当前模式和姿态满足目标；
3. 当前终速满足目标；
4. 当前状态施加中性输入后的完整尾迹仍在 completion 内。

至少覆盖：

- 当前位置在外、预测会进入：未完成；
- 当前位置在内、速度会带出：未完成；
- 当前位置、模式、姿态和速度满足，但尾迹擦出边界：未完成；
- 所有条件与中性尾迹都满足：完成。

完成判定不能读取候选评分、reason 或“预计很快到达”。

## 4. 控制时长和逐 tick rollout

验证：

- 一／两 tick先分别通过硬安全；
- 两者都安全时，停止点更接近 completion且没有越过制动位置的候选胜出；
- 两 tick会过冲而一 tick质量更好时选择一 tick；
- 两者都不安全时不发送方向输入；
- rollout 每次只推进一个玩家 tick；
- `control_ticks` 只表示许可长度，不直接等于动作成本；
- policy、decision、intent、Runtime、账本和 rollout 记录一致。

把两 tick许可错误地作为一次两 tick状态推进，必须改变状态、成本或轨迹断言并被检出。

## 5. 超调、制动和进展

至少覆盖：

- 正常接近后制动；
- 轻微超调后一次安全反向修正；
- 反向修正仍需满足全部可能前缀；
- 制动期间位置短暂不改善，但预计停止点在改善；
- 反复往返且停止点不改善；
- 无位移、无速度改善和无 completion 距离改善。

前四类不能误报停滞。后两类必须在冻结 tick上限内返回 typed 停滞，不能永久运行。

## 6. 晚到、丢失和重锚

逐项核对 requested／latest／actual tick 和输入账本：

- 输入没有生效时，对应 `0` tick前缀；
- 只生效一 tick时，对应中间前缀；
- 过期输入被压下，不延后补发；
- 下一份正式观察到达后重新计算；
- 新决定不继承未证明的额外许可；
- 业务终态后只允许已有身体责任按规则收尾。

不能用“最终仍安全”代替生效前缀与账本逐项一致。

## 7. 严格动作负对照

Sprint、Sneak／探边、Crawl、Swim、Climb、JumpGap、JumpUp、ControlledDrop、离地和连续高度严格入口不得使用 F2-SC 的普通地面谓词替代自身证明。

负对照要确认它们原来的入口、轨迹、落地、伤害额度、生效窗口和责任交接没有变化。

## 8. 错误副本门槛

至少冻结以下错误副本：

1. 安全尾迹必须停进 completion；
2. 漏掉 0 tick前缀；
3. 漏掉两 tick许可的中间前缀；
4. 完成判定不检查中性尾迹；
5. 预测会进入就提前完成；
6. 两 tick许可让 rollout 前进两 tick；
7. 许可长度直接作为成本；
8. 超调后禁止反向修正；
9. 每帧距离不降就立即停滞；
10. 无进展没有上限；
11. 晚到输入延后补发；
12. 严格动作复用普通地面规则；
13. 用 reason、场景 ID、固定距离或权重特判。

所有错误副本都必须由行为断言发现，不能只做源码字符串检查。

## 9. Fail-fast 顺序

1. F2-SC 纯谓词与错误副本；
2. 原 F2-GP 定向检查；
3. F2S-C-04 normal／晚到／丢失；
4. 剩余 9 项和已恢复 42 项；
5. v9、v8、v7、F2、F2-R；
6. 五组行为、严格动作和协调集合；
7. 完整 motion_nav 正序、逆序；
8. D061 和规划性能；
9. Fabric。

任一层失败即停止。若 F2S-C-04 normal 主例仍失败，只允许检查完成谓词和 terminal 进展／停滞谓词。只有晚到或丢失负对照单独失败时，才检查账本前缀。不得调权重或完成区域。

## 10. Windows、性能和 Fabric 门槛

Windows 是正式平台。历史成功退步、新安全事件、伤害额度超出、来源泄漏和终态潜行都必须为 `0`。

性能门槛保持：控制生产 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`；完整夹具最大值 `<50 ms`；默认规划期限 `0.5 s`；deadline miss 为 `0`。另报告每帧安全前缀数、物理 rollout 数、一／两 tick选择比例、反向修正次数和停滞窗口长度。

Fabric 必须走 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter、ActionRouteExecutor 和真实输入账本。至少覆盖四方向窄 completion、晚到、丢失、超调反向修正、完成正反例、世界变化和严格动作对照。

## 11. 关闭判定

只有第 2—10 节全部通过，F2-SC 才能关闭。通过后才能恢复 F2-GP 的 terminal edge、成本和真实状态重锚工作。F2-PC 保持历史未通过，F2-GP、F2-SG、F2-S 和路线优化器仍需各自关闭。

## 12. 本轮验收结果

正式平台是 Windows，源码提交为 `8fa7924f3fdbebde0537739f392dba4965d54381`。

### 12.1 聚焦检查

运行命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_f2sc_safe_tail_completion tests.motion_nav.test_f2pc_ground_precision_control tests.motion_nav.test_f2gp_ground_tracking_policy tests.motion_nav.test_fixed_route_walk tests.motion_nav.test_f2_non_center_ground_route tests.motion_nav.test_goal_region_planning.F2SGFormalChainTests.test_frozen_f2s_c04_reaches_the_other_goal_surface
```

结果为 `54/54`。它覆盖纯 `safe_tail`、纯 `completion`、一／两 tick许可、单玩家 tick rollout、反向修正、普通 FixedRoute 接线和 F2S-C-04 normal 正式模拟链。C04 以 `goal_state_satisfied` 完成，零伤害、零安全违规。

### 12.2 remaining9

使用 `F2ST-terminal-approach-v1/red-manifest.json` 的九个稳定 ID，运行 `f2r_piecewise_evidence.py clutter-formal`。结果如下：

| 结果 | 数量 | 原因 |
|---|---:|---|
| 完成 | 4 | `goal_state_satisfied` |
| 有界失败 | 1 | `fixed_route_has_no_forward_control` |
| 有界失败 | 4 | `no_safe_ground_candidate` |
| 安全事件 | 0 | 无 |

五项失败都完成了验证并安全释放来源。它们不能记为成功。

### 12.3 根因与止损

`f2r/clutter/0.1/7/22/product` 的安全修正可以缩短到 completion 的距离，但不能增加已经走完的路线弧长。现有前进资格没有把这个 terminal 改善算作进展，因此选择中性输入后返回无前进控制。

其余四项在失败帧都有九个候选要求完整 1.21 复核，现有上限只复核前三个。前三个没有被接纳，尚未复核的候选可能包含可用输入。继续处理需要改变候选复核数量或顺序，触发第 9、11 节的止损条件。

本轮没有修改评分权重、completion 区域、UNKNOWN、支撑、材质、Session、通用 A* 或严格动作。remaining42 和第 9 节后续各层均未运行。F2-SC 判为未通过。

原始九项结果、汇总、聚焦检查输出和 SHA256 位于 `evidence/motion_navigation/F2SC-safe-tail-completion-v1/`。

后续验收已移交 [F2-CV](F2CV-closed-ground-candidate-verification.md)。它先用 oracle 和最坏微基准证明完整 `9 × 2` 候选族能在期限内验证，再处理本轮一个 terminal 进展反例和四个复核名额反例。本页的失败结果不改写。
