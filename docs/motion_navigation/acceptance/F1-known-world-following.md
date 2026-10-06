# F1：已知开阔地正式跟随验收

日期：2026-10-06。状态：F1-A 至 F1-E 已完成检查。正式跟随产品能力和薄调用方边界通过；原“十二个核心文件零修改”门槛失败，F1 整体不写成全部完成。下一步进入独立、行为不变的结构整理。

## 1. 验收范围

本验收只覆盖已知、开阔、普通地面上的单个真实玩家目标。正式链固定为：

`PlayerRuntimeV1 -> RuntimeNavigationDriver -> KnownWorldFollowDriver`

机器人是 `MC2PFollower`，目标玩家是 `MC2PLeader`。两者分别通过自己的 Runtime 输入出口控制。机器人只读取自己的合法 Observation V3。场景不使用旧 `RuleFollower`、`PlaygroundFollower`、传送或直接世界写入。

冻结身份、种子、起点、目标输入节奏、五个场景和判定门槛见 [F1 阶段方案第 4 节](../stages/F1-known-world-following-plan.md#f1-dfabric-代表场景)。每个场景只有一个种子、一次运行。结果只能作为代表性实机证据，不能当作统计证明。

## 2. 当前检查

F1-D 夹具组件检查：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_f1_known_world_following_fabric -v
```

2026-10-05 结果为 **13/13 通过**。检查覆盖双玩家身份、目标输入边界、正式链静态门禁、smoke 汇总、单场 CLI、首条晚一 tick 注入、F1 指标汇总，以及 I15／I16／I17 的正式状态门禁。`git diff --check` 通过。

正式场景入口一次只接受一个冻结场景。它拒绝 `all`，每次要求新的输出目录和新世界：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m scripts.f1_known_world_following_fabric scenario --id straight_2_0 --output D:\My_project\mc_ai\artifacts\f1-known-world-following\<new-batch-id>
```

下面按时间顺序保留各批结果。第 3—7 节记录最初的 smoke 和 `straight_2_0`；第 9 节记录 D058、D059 的组件结果及 D058 后失败；第 10 节记录 D059 修后的新失败。所有原文件保持不变。

## 3. Fabric smoke

第一批 smoke 位于 `artifacts/f1-known-world-following/20261004T1636035336397Z-smoke/`。它的功能链、真实输入、取消和清理成功，但原 `smoke-result.json` 错把结果写成通过。原因是当时的时间证据读取器把 V3 回执按 V2 解析，smoke 汇总也没有把 host 阻塞检查纳入总结果。该批次只保留为失败证据。

修正证据读取和汇总后，新批次 `artifacts/f1-known-world-following/20261004T1648076673611Z-smoke/` 的 11 项门禁全部通过：

- 两个独立真实玩家同时连接；
- 目标玩家有 10 tick 实际移动输入，机器人有 9 tick 实际移动输入；
- 跟随层接受目标修订 2—5；
- 两端时间归属均通过。机器人记录 716 个事件、30 个观察和 29 个区间；目标记录 703 个事件、30 个观察和 29 个区间；错误均为 0；
- 取消后导航来源释放；
- 三个端口和本轮进程全部清理。

smoke 只证明最小正式链可运行，不替代五个正式场景。

## 4. `straight_2_0` 原始结果

原批次位于：

`artifacts/f1-known-world-following/20261004T1655145691341Z-straight-2-0/`

原 `scenario-result.json` 的总结果是 **未通过**。本轮不修改该文件，也不把离线分析结果写回原证据。

### 4.1 已通过的门槛

| 项目 | 结果 | 门槛 | 判定 |
|---|---:|---:|---|
| 稳定窗目标平均速度 | 1.873496 格／秒 | 1.8—2.2 格／秒 | 通过 |
| 修订响应 P95 | 4 tick | 不高于 5 tick | 通过 |
| 最终水平距离 | 0.972323 格 | 不高于 2.5 格 | 通过 |
| 接受／拒绝修订 | 17／0 | 不得拒绝合法修订 | 通过 |
| 结束 | Session 与 driver 均为 `cancelled`，来源已释放，取消尾段 0 tick | 有界结束并释放来源 | 通过 |
| host 与时间证据 | 两端时间归属通过，端口释放，无清理失败 | 全部通过 | 通过 |
| 生命与伤害 | 两个玩家均未死亡、未失血 | 安全违规为 0 | 通过 |

目标玩家有 30 tick 实际移动输入，机器人有 67 tick 实际移动输入。目标停止后机器人最终进入保持区。这些通过项不能抵消下面的能力失败。

### 4.2 未通过的门槛

| 项目 | 结果 | 门槛 | 判定 |
|---|---:|---:|---|
| 稳定窗超出保持区距离平均值 | 1.686361 格 | 不高于 0.75 格 | 未通过 |
| 稳定窗超出保持区距离 P95 | 1.886092 格 | 不高于 1.5 格 | 未通过 |
| 规划提交／接受修订 | 23／17 = 1.352941 | 不高于 1.25 | 未通过 |

稳定窗原始水平距离的平均值为 4.186361 格，P95 为 4.386092 格，最大值为 4.420231 格。任务恢复发生 4 次。

原摘要还记录了 `planning_identity_invalid`。这是 F1-D 夹具的 I15 量尺误报：它曾把执行态没有 owned planning work 也记为违规。正式 monitor 只在 Session 处于 `planning` 时检查 I15。逐帧复核显示，原批次所有 planning 帧都拥有有效且身份匹配的 planning work 和 permit。夹具现已按正式 I15／I16／I17 条件修正；原 `scenario-result.json` 保持不变。即使去掉这项误报，距离和规划频率仍然失败，所以 `straight_2_0` 结论仍是未通过。

## 5. 四次周期性停车的根因

四次 `cancel_braking` 来自同一个正式状态转换。新路线接纳后，Runtime 请求路线前方格的空气事实。下一份 Observation V3 把两格从 `UNKNOWN` 更新为有 `visible_air` 证据的 `AIR`。`apply_observed_blocks()` 会把这种知识变化列入 `changed_cells`。它与活动路线的 dependency 相交后，Session 按通用 `DEPENDENCY_CHANGED` 路径让 `route_executor` 制动，随后废弃旧路线并重新规划。

| 组 | 触发 tick／Observation | 新确认的路线前方空气 | `cancel_braking` | 到正向输入恢复 | 实际距离变化 | 依赖恢复规划 |
|---|---|---|---:|---:|---:|---:|
| 1 | 42／seq 44 | `(0,-60,10)`、`(0,-59,10)` | 3 tick | 5 tick | +0.4354 格 | 1 |
| 2 | 52／seq 54 | `(0,-60,11)`、`(0,-59,11)` | 2 tick | 4 tick | +0.3228 格 | 1 |
| 3 | 62／seq 64 | `(0,-60,12)`、`(0,-59,12)` | 2 tick | 4 tick | +0.2492 格 | 1 |
| 4 | 72／seq 74 | `(0,-60,13)`、`(0,-59,13)` | 2 tick | 4 tick | -0.0309 格 | 1 |

第四组发生时目标已经停止，因此实际距离没有扩大。前三组中，目标继续移动，机器人的制动和等待使距离分别增加 0.4354、0.3228 和 0.2492 格。

这是路线知识基础变化，不是 Minecraft 方块真的变化。场景是静态草地，对应支撑格一直是 `minecraft:grass_block`。两格空气都有正式 `visible_air` 证据，因此也不是观察噪声。四个触发帧的 `tracked_entity` 与同 track id 的可见玩家事实都有效。下一目标 revision 都发生在停车之后，所以目标修订交接不是直接触发源。

四次依赖失效各增加 1 个恢复规划 work，合计 4 个，正好对应 4 次 task recovery。每次恢复附近又有一次正常目标修订，因此从触发前到新路线恢复的窗口内会看到 2 个新增 submission。全批次另外有 2 个额外 work 出现在早期知识获取阶段，不属于这四次停车。

`active_route_dependency_changed` 在代码中表示“路线依赖的知识发生变化”。它同时覆盖物理格变化和 `UNKNOWN -> AIR`。原 reason 没有声称方块一定发生物理变化，但仅看名称容易误解。现有证据没有序列化完整 route dependency 集合；本结论由正式 air request、同一 Observation 重放得到的 `changed_cells`，以及同帧进入 `DEPENDENCY_CHANGED` 制动三项证据共同支持。

原批次另有 9 次 `needs_target_observation`，全部位于 tick 91—99。此时 tracked 玩家事实仍有效，但目标暂时不在 `visible_entities`。这些事件发生在四次停车之后，没有引起新的 dependency recovery 或额外规划，与周期性停车无关。

## 6. 尚未运行的场景

下面四个冻结场景尚未运行，不能根据 `straight_2_0` 或 smoke 推断结果：

- `lateral_2_0`；
- `move_stop_800_resume_2_0`；
- `normal_cancel_2_0`；
- `first_input_late_one_tick_2_0`。

当前不批准连续运行剩余场景。先处理并审查 `straight_2_0` 暴露的通用路线依赖停车，再从新的世界和新的批次目录逐场 fail fast。失败批次继续保留，不覆盖、不重命名，也不从分母删除。

## 7. 证据边界

上述原始目录是当前机器上的本地证据，尚未发布到 Git。它们可能包含世界文件和完整运行日志，不直接提交。正式发布时只提取可复核摘要、清单、必要的分段证据和哈希到新的 `evidence/motion_navigation/f1-known-world-following-v1/`，并继续保留原失败数字。

第 3—7 节不把 `.tmp` 文件当作长期证据，也没有改写原批次。D058 后另建的新批次见第 9 节。F1-D 仍未完成；F1 的正式验收保持打开。

## 8. D058 组件与性能验收

D058 的设计见[决定记录](../decisions/0058-revalidate-ordinary-walk-before-stopping.md)，实施顺序和测试矩阵见[阶段方案 F1-D.1](../stages/F1-known-world-following-plan.md#f1-d1d058-共享前置修复)。组件、正式链、性能门槛和独立复审均已完成，没有未关闭 P0／P1／P2。后续 Fabric 复跑仍未通过，见第 9 节，因此本节不关闭 F1-D。

### 8.1 组件和正式链门槛

最终检查覆盖下面各组：

- WorldKnowledge 的 `UNKNOWN -> AIR` 变化语义保持；
- RouteAdmitter 在最终 ActionRoute 后生成 plan；逐腿 action／fixed route／point／progress 映射；initial connection、terminal tail、D057 跳首边和 Walk→strict→Walk；
- typed DependencyOwner 表和 `position -> owner refs` provenance；退役只读取 owner 的 kind／action／fixed route／recipe ref，不解析 id，也不从 ActionRoute 重猜；
- 实际 GroundMotionProfile／capability identity；`SURFACE_EDGE` 严格重放现有四点 surface edge 查询，`STANDABLE_CONNECTION` 严格重放现有 0.1 格采样和 endpoint region 查询；共同外围只加入材质、trait、identity 与 cache；
- ActiveRouteTracker 的 changed 交集、多 owner 校验、逐 owner 依赖替换、身份和失败关闭；
- ExecutionSupervisor 对 incumbent／pending 的独立处理；
- Session 只消费有类型结果，没有保留第二个依赖判断；
- Step、JumpUp、JumpGap、ControlledDrop、连续高度 Walk、traversal plan 和空中收尾的原回归；
- F1-C 完整固定清单以及运动导航、协调集合和补充故障集合。

测试必须记录实际查询计数。无交集样本的统一几何调用应为 0；单 RouteControl 不超过 2 次；incumbent 与 pending 合计不超过 4 次。预算耗尽样本必须停止，不能顺延旧路线许可。

安全反例必须通过真实 `WorldKnowledge` 更新移除支撑、插入 full cube 和换入不支持材质，不能只传手写 `changed_cells`。另加共享 Walk／strict 依赖、Walk→Step→Walk、D057 边界、initial connection 退役前后和 terminal tail 可映射／不可映射场景。每个 STOP 反例都断言同一帧不再提交 incumbent 正向输入；pending 失败则同帧丢弃，合法 incumbent 仍可前进。

性能使用同一台机器、同一进程的热态组件基准。先预热 100 次，再记录至少 1,000 个受影响样本。无交集墙钟、`SURFACE_EDGE`、`STANDABLE_CONNECTION`、复杂形状、单 incumbent 和 incumbent + pending 四次重放上限分别报告 P95、P99 和最大值，不平均各组分位数。D058 新增重验段 P95 必须不高于 1 ms。正式完整控制准备继续要求 P95≤8 ms、P99≤15 ms、名义最大值<30 ms。组件成绩不能替代实机控制期限。

最终相关组件集合为 **226/226**：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest -q tests.motion_nav.test_d058_validation_plan tests.motion_nav.test_d058_runtime_validation tests.motion_nav.test_d058_route_revalidation_benchmark tests.motion_nav.test_route_body_advance tests.motion_nav.test_navigation_supervised_interruptions tests.motion_nav.test_navigation_closed_loop tests.motion_nav.test_f1_known_world_following
```

正式性能批次是 `evidence/motion_navigation/d058-route-revalidation-v2/`。它使用项目规定的 `conda.exe run --prefix ... --no-capture-output python`，来源提交为 `295fd942`，运行前工作树干净。v1 原字节保留；它来自同一环境并通过门槛，但只记录环境内 Python 路径，不作为项目标准 launcher 的最终证据。

| 组 | P95 / P99 / 最大值（ms） | 查询数 |
|---|---:|---:|
| 非空 changed、无交集 | 0.0056 / 0.0057 / 0.0109 | 0 |
| `SURFACE_EDGE` | 0.2415 / 0.2641 / 0.3750 | 1 |
| `STANDABLE_CONNECTION` | 0.3278 / 0.4145 / 0.5501 | 1 |
| 复杂形状 | 0.2342 / 0.2451 / 0.4087 | 1 |
| 单 incumbent、共享依赖 | 0.1586 / 0.1609 / 0.1833 | 2 |
| incumbent + pending | 0.2777 / 0.2801 / 0.3177 | 2 + 2 |

完整 F1-C 清单捕获 3,513 次 prepare。丢弃前 100 次后保留 3,413 个原始样本，P95／P99／最大值为 **4.5990／7.6267／9.8917 ms**。新增段和完整准备门槛都通过。`performance.json` 保存原始样本、环境、源码与 dirty 状态、每轮 typed 结果和查询数；`COMMAND.txt` 与 `SHA256SUMS` 已复核。

### 8.2 `straight_2_0` 复跑门槛

复跑必须使用新世界和新的只写一次批次目录。原批次 `20261004T1655145691341Z-straight-2-0` 继续保留为失败对照。新批次同时满足下表才算通过：

| 项目 | 门槛 |
|---|---|
| 目标实际平均速度 | 1.8—2.2 格／秒 |
| 稳定窗超出保持区距离平均值／P95 | 不高于 0.75／1.5 格 |
| 修订响应 P95 | 不高于 5 tick；effective、superseded、unanswered 数量守恒 |
| 规划提交／接受修订 | 不高于 1.25 |
| 最终水平距离 | 目标停止后不高于 2.5 格 |
| 原触发位置 | 仍属于 execution proof 且继续被正式请求的格，在观察到达后进入 `changed_cells`；selection-only 格允许保持 `UNKNOWN` 或不出现 |
| 路线校验 | 先按 typed owner/provenance 冻结四格预期；execution proof 格得到身份匹配的 `CONTINUE/REVALIDATED`；selection-only 格若由其他正式来源出现，则得到 `UNAFFECTED/NO_INTERSECTION`、affected 空且零查询 |
| 停车与恢复 | execution proof 格的变化，以及由其他正式来源出现的 selection-only 变化，引起的 `cancel_braking`、task recovery 和额外 planning submission 均为 0；selection-only 格未出现不算缺证据 |
| 安全与结束 | 安全违规 0；取消有界；Session／driver 终态一致；source 释放 |
| host 证据 | 两端 time attribution 通过；端口、进程和清理全部通过 |

任何一项失败都保留原始结果并停止，不接着运行其他四场。`straight_2_0` 通过后，才按 `lateral_2_0`、`move_stop_800_resume_2_0`、`normal_cancel_2_0`、`first_input_late_one_tick_2_0` 逐场运行；每场仍使用新世界和新目录。

随后已按本表运行一次新的 `straight_2_0`。该批仍未通过，结果见第 9 节。上表继续作为 D059 后下一次单场 fail-fast 的完整通过门槛。F1-D 保持打开。

### 8.3 结构结论

F1 初始核心零修改门槛已失败。D057 和 D058 都属于正式跟随暴露出的共享导航前置修复，不能计为跟随层保持十二个核心文件零变化。最终验收分别报告：

1. F1 薄跟随调用方本身的差异；
2. D057、D058 共享修复触及的核心文件和净行数；
3. D058 完成后，后续 F1 提交是否继续保持核心零变化。

这项拆分用于如实说明变化来源，不能把共享修复从总差异中删除，也不能把失败门槛改写成通过。

## 9. D058 后的 `straight_2_0` 失败与 D059 验收计划

新批次位于：

`artifacts/f1-known-world-following/20261004T2147526847180Z-straight-2-0-d058/`

原目录和 `scenario-result.json` 保持不变。总结果是 **未通过**。

### 9.1 实测结果

| 项目 | 结果 | 门槛 | 判定 |
|---|---:|---:|---|
| 稳定窗目标平均速度 | 1.873496 格／秒 | 1.8—2.2 格／秒 | 通过 |
| 修订响应 P95 | 5 tick | 不高于 5 tick | 通过 |
| 最终水平距离 | 0.707793 格 | 不高于 2.5 格 | 通过 |
| 接受／拒绝修订 | 17／0 | 不得拒绝合法修订 | 通过 |
| 稳定窗超出保持区距离平均值 | 2.090606 格 | 不高于 0.75 格 | 未通过 |
| 稳定窗超出保持区距离 P95 | 2.570081 格 | 不高于 1.5 格 | 未通过 |
| 规划提交／接受修订 | 22／17 = 1.294118 | 不高于 1.25 | 未通过 |
| 任务恢复 | 4 | 四个原变化引起的恢复为 0 | 未通过 |

稳定窗原始水平距离平均值为 4.590606 格，P95 为 5.070081 格，最大值和最后一个稳定窗样本均为 5.107350 格。目标停止后机器人最终追入保持区。机器人有 68 tick 实际移动输入，目标玩家有 30 tick 实际移动输入。安全违规为 0；Session 和 driver 最终均为 `cancelled`；来源释放；两端 time attribution、端口释放和清理通过。

目标玩家的实际轨迹与原批次一致。新批次的机器人比原批次晚约两 tick 开始持续前进，稳定窗开始时约多落后 0.43 格；这说明实机后台时序会影响单次距离数字。但原批次和新批次都没有通过距离与规划频率门槛，因此不能把本次失败归为量尺或夹具误差。

### 9.2 四次恢复的直接证据

四份 Observation 分别在场景 tick 42、53、59、70 写入，并首次确认空气格 `(0,-60,10)`、`(0,-60,11)`、`(0,-60,12)`、`(0,-60,13)`；sequence 分别为 45、56、62、73。supervisor 在下一控制帧 tick 43、54、60、71 消费变化，四次 dependency recovery 也在这些消费帧开始。不能把观察写入帧和 Session 消费帧记成同一 tick。

| 组 | 观察写入 | 下一帧消费／恢复开始 | `cancel_braking` | Session 进入依赖重规划 | 恢复计数 |
|---|---|---|---:|---:|---:|
| 1 | tick 42／seq 45／z=10 | tick 43 | tick 43—45 | tick 46 | 1 |
| 2 | tick 53／seq 56／z=11 | tick 54 | tick 54—55 | tick 56 | 2 |
| 3 | tick 59／seq 62／z=12 | tick 60 | tick 60—63 | tick 64 | 3 |
| 4 | tick 70／seq 73／z=13 | tick 71 | tick 71—72 | tick 73 | 4 |

四次恢复只增加 `RetryCause.DEPENDENCY`；execution、information、acquisition 和 planning cause 都没有增加。Session 只有在 supervisor 返回 incumbent `STOP` 时才会把结果映射为 `active_route_dependency_changed` 和 dependency recovery。因此可以确认 D058 正式运行入口被调用，但四次都没有继续原路线。

原批次没有持久化 `ActiveRouteValidation.disposition/reason`、dependency owner、query kind 和 query count。当前不能从这份实机证据直接区分 `NON_RECIPE_OWNER_CHANGED`、引用缺失或其他 typed `STOP`。代码与路线几何显示，最可能的原因是终点站位选择的整包依赖进入最后 Walk，其中未选格成为 `NON_RECIPE` owner。这个判断必须先由 D059 组件红测确认。

### 9.3 D059 的验收顺序

D059 的完整设计见[区分目标站位选择依赖与已选路线执行依赖](../decisions/0059-separate-terminal-selection-and-route-execution-dependencies.md)，阶段顺序见 [F1-D.2](../stages/F1-known-world-following-plan.md#f1-d2区分站位选择依赖与路线执行依赖)。验收按下面顺序执行：

1. typed frame diagnostics 必须绑定 observation sequence，保留 incumbent／pending 原始 `ActiveRouteValidation`。`tests/sim/runner.py` 必须手工新增 `route_validation` JSON 字段；F1-D runner 的整体 `asdict(diagnostics)` 必须由测试确认包含该字段；两种 JSON 都要逐字段读回；
2. 大目标区域红测先复现未选 `UNKNOWN` 格确认为空气时的 `STOP/NON_RECIPE_OWNER_CHANGED`，再读取 typed owner/provenance，对 z=10、11、12、13 逐格区分 selection-only、可重验执行 proof 和必须停止的共享 owner，并把预期处置写入本节；四次正式链组件重放分别接受真实的新 goal revision 和新 route／revision，各自创建 tracker，不能复用同一 tracker；若 typed reason 不同，先修订根因，不进入实现；
3. exact terminal proof 存在时，最终活动执行依赖必须等于 pre-terminal route execution dependencies 与 exact terminal proof dependencies 的并集。只移除本次追加的 terminal selection dependencies；到达终点的 corridor 做同样处理，validation provenance 保留此前所有 SurfaceWalkEdge、node、initial connection 和前腿 owner；
4. 真实 RouteAdmitter builder 必须证明两类共享格仍失败关闭：pre-terminal `NON_RECIPE` 与 exact terminal recipe 共享时保留前者并 `STOP`；exact recipe 与 strict owner 共享时同样 `STOP`。不能用手写 plan 代替；
5. 没有 exact proof 的三个 builder 分支分别覆盖：terminal 与最后图节点重合、ground traversal proof 后另加 tail、第二次 direct query 非 `FEASIBLE` 后 append tail。三者都保留完整 selection dependencies；未被既有 recipe 覆盖的部分进入 `NON_RECIPE`，命中时同帧 `STOP`；
6. `NavigationSession.observation_request()` 与 `WorldKnowledge.set_protection()` 仍包含 exact support／clearance／sweep／0.1 格采样支撑，selection-only 格不再请求或保护；corridor 到达终点与未到终点两种公式都直接断言；
7. 移除已选支撑、堵住已选终点头部或命中共享 strict owner，必须同帧 `STOP` 且无正向输入。`world_session`、`route_id`、`route_revision`、`source_request_id`、`goal_id`、`goal_revision`、`planning_generation`、`work_identity`、`action_index` 逐项错配都必须 `STOP`；`fixed_route_id` 单列检查并同样 `STOP`；
8. 组件按 seq 45、56、62、73 顺序，通过显式合法 `WorldKnowledge` 更新注入四次 `UNKNOWN -> AIR`，并区分写入与下一帧消费。selection-only 格必须得到 `UNAFFECTED/NO_INTERSECTION`、`affected_cells=()`、`queries_used=0`；仍属于执行 proof 的格必须得到身份匹配的 `CONTINUE/REVALIDATED`。共同要求 changed_cells 保留、总 task recovery 为 0、无 `cancel_braking`、同帧 Walk 继续，查询数不超过单控制者 2 次和整帧 4 次；
9. Fabric 只对仍属于 execution proof 且继续被正式请求的格要求 `changed_cells` 和 `CONTINUE/REVALIDATED`。selection-only 格允许保持 `UNKNOWN` 或不出现；若由其他正式观察来源出现，必须 `UNAFFECTED/NO_INTERSECTION`、零查询、零恢复；
10. D058 组件回归、F1-C 完整清单和性能门槛通过，并完成独立复审；
11. 只运行一个新世界、新目录的 `straight_2_0`。它必须同时满足第 8.2 节全部门槛。失败就停止，另外四场继续不运行。

本节只登记失败和下一步验收口径，不把 D058 的组件通过外推为 Fabric 通过，也不改写两个 `straight_2_0` 失败批次。

### 9.4 D059-B：终点选择依赖的根因复现与四次分类

D059-B 已用真实 `WorldKnowledge -> SurfacePlanning -> RouteAdmitter -> ActiveRouteValidationPlan` 链确认第 9.2 节提出的根因。本批只增加组件检查和本节记录，没有修改生产代码，也没有运行 Fabric。

组件使用普通同高石质地面。大 `GoalState` 的第一次站位检查落在 `(3.0, 1, z+0.5)`，其身体净空包含一个尚未观察的格。选择器继续检查后，在 `(2.6, 1, z+0.5)` 找到合法站位。`RouteAdmitter` 为从前一图节点到该已选站位建立了正式 `STANDABLE_CONNECTION` 精确证明。未选站位留下的 `UNKNOWN` 格不属于这份精确证明，也不属于此前的 `SURFACE_EDGE` 证明。

现行 builder 仍把站位选择期间累计的整包依赖并入最后一个 Walk。validation plan 因此把该未选格交给 `NON_RECIPE` owner。随后通过合法 `WorldKnowledge.confirm_air()` 把它从 `UNKNOWN` 更新为 `AIR` 时，独立 `ActiveRouteTracker` 返回：

- disposition：`STOP`；
- reason：`NON_RECIPE_OWNER_CHANGED`；
- affected cells：只有本次确认的未选格；
- geometry query：0 次。

测试没有只根据单个 `NON_RECIPE` owner 推断来源。每个样本还同时确认：该格不在 planner candidate dependencies 中；路线没有 initial connection；末腿存在 exact `STANDABLE_CONNECTION` proof；该格不在末腿 exact proof 和此前 `SURFACE_EDGE` proof 的 dependencies 中；重新执行正式站位选择时，该格出现在 `StandablePointResult.dependencies` 中。因此它只由本次 terminal selection 扫描追加。

这证明 D058 实机的四次停车符合“目标站位选择依赖被误当成已选路线执行依赖”的现行代码路径。它不是几何重验失败，也不是观察噪声。

四次顺序样本分别使用新的 goal revision、新的 route identity 和新的 tracker。组件坐标按同一结构沿 z 方向推进；它们用于冻结 owner 分类，不冒充原 Fabric 坐标的新增实测。

| goal revision | route id / revision | Observation sequence | 晚到格 | selection-only | exact terminal | pre-terminal | shared | 现行结果 | D059-C 修正后期望 |
|---:|---|---:|---|---|---|---|---|---|---|
| 1 | `b619121c87ad28a290636afa` / 1 | 45 | `(3,1,1)` | 是 | 否 | 否 | 否 | `STOP/NON_RECIPE_OWNER_CHANGED` | `UNAFFECTED/NO_INTERSECTION`，0 query |
| 2 | `54c460bb5abc04620fcc552f` / 1 | 56 | `(3,1,2)` | 是 | 否 | 否 | 否 | `STOP/NON_RECIPE_OWNER_CHANGED` | `UNAFFECTED/NO_INTERSECTION`，0 query |
| 3 | `1ec47b940bc81ef03015af69` / 1 | 62 | `(3,1,3)` | 是 | 否 | 否 | 否 | `STOP/NON_RECIPE_OWNER_CHANGED` | `UNAFFECTED/NO_INTERSECTION`，0 query |
| 4 | `992e6f2a9ab65c90edbb1bbc` / 1 | 73 | `(3,1,4)` | 是 | 否 | 否 | 否 | `STOP/NON_RECIPE_OWNER_CHANGED` | `UNAFFECTED/NO_INTERSECTION`，0 query |

每条路线同时含有前腿 `SURFACE_EDGE` recipe 和末腿 `STANDABLE_CONNECTION` recipe。表中的四个晚到格只属于 selection dependency；它们没有被塞进任一 recipe，也没有与其他 owner 共享。D059-C 因而必须只从最终执行依赖移除这些 selection-only 格。此前的路线腿和已选终点精确证明仍须保留。若某个真实格后来查明同时属于执行证明或 strict／non-recipe owner，仍按对应 owner 重验或停止，不能套用本表的 `UNAFFECTED` 结果。

同一 fixture 还增加了一条正式 Session 时间线。它只使用公开的 `start_goal()`、`update_goal()`、`propose()`、`active_route` 和 `diagnostics`，规划结果由既有 `_InlinePlanner` 测试适配器沿正式入口交付。测试不写 Session 或 Supervisor 私有字段。

| goal revision | 命令 | Session 接纳 route 的 sequence | route id / revision | `UNKNOWN -> AIR` 写入／下一次控制消费 | typed 结果 | 累计 dependency recovery |
|---:|---|---:|---|---|---|---:|
| 1 | `start_goal()` 接受 | 2 | `b619121c87ad28a290636afa` / 1 | 45 / 45 | `STOP/NON_RECIPE_OWNER_CHANGED` | 1 |
| 2 | `update_goal()` 返回 true | 46 | `d172cbccb5a1af9d60e9fdb0` / 1 | 56 / 56 | `STOP/NON_RECIPE_OWNER_CHANGED` | 2 |
| 3 | `update_goal()` 返回 true | 57 | `10a6ad000e4b7b59dc565df7` / 1 | 62 / 62 | `STOP/NON_RECIPE_OWNER_CHANGED` | 3 |
| 4 | `update_goal()` 返回 true | 63 | `1553729938a3a56770521184` / 1 | 73 / 73 | `STOP/NON_RECIPE_OWNER_CHANGED` | 4 |

这里的“写入／消费”是两个顺序动作：测试先用对应 `ObservationStamp` 合法写入 `WorldKnowledge`，再由紧随其后的控制调用携带同一 observation sequence 和 `changed_cells` 交给 Session。sequence 全程单调。四个 route identity 和 planning work identity 各不相同，Supervisor 为每条路线产生了匹配的 typed validation，因此没有复用前一条路线的 tracker 状态。

现行结果明确包含四次依赖恢复和四次路线取消。每次取消都返回 neutral movement，Session reason 是 `active_route_dependency_changed`。四轮的其他 recovery cause 均为 0，illegal transition 为 0，damage 为 0。它是 D059-C 修复前的 characterization，也是修复后应由红转绿的前置证据；本节没有把它写成零恢复或已修复。

检查命令与结果：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_d059_terminal_selection_dependencies -v
```

`833d161` 上的结果为 **3/3**。第一项冻结单格根因；第二项冻结四个连续新 goal revision／route identity 的 builder 分类；第三项冻结正式 Session／Supervisor 时间线、typed 停止、取消和依赖恢复。这些是 D059-C 修正前的历史结果，修正后的同组结果见第 9.5 节。本节不改变 F1-D 的打开状态。

### 9.5 D059-C：终点选择依赖与执行依赖分开

D059-C 核心修正已完成组件实现。`RouteAdmitter` 在建立 exact terminal proof 时保存一份私有、不可变的来源记录。记录只含 final action index、进入终点处理前已有的执行依赖，以及 exact proof 的依赖。最终 Walk 和到达终点的 corridor 都使用下面的公式：

`pre-terminal execution dependencies ∪ exact terminal proof dependencies`

因此只删除本次站位扫描新增的 selection dependencies。未到终点的 corridor prefix 不加入 terminal 集合。没有 exact proof 的三个分支保持原行为。

validation plan 直接消费这份构建来源。此前的 `SURFACE_EDGE`、initial connection、strict action 和无法映射的 pre-terminal 依赖仍保留各自 owner。它不会从最终 dependency 集合或 owner id 字符串反推来源。真实 builder 检查确认：

- selection-only 格不再出现在 final action、到达终点的 corridor 或 provenance 中；
- exact support、clearance、sweep 和 0.1 格支撑采样依赖仍在 final action、corridor 和 `STANDABLE_CONNECTION` recipe 中；
- 前腿 `SURFACE_EDGE` recipe 和 D057 initial connection 保留；
- pre-terminal `NON_RECIPE` 与 exact recipe 共享同格时仍返回 `STOP/NON_RECIPE_OWNER_CHANGED`；
- strict action 与 exact recipe 共享同格时仍返回 `STOP/STRICT_OWNER_CHANGED`；
- terminal 与最后图节点重合、第二次 direct query 非 `FEASIBLE`、ground traversal proof 后追加 tail 三个无 exact proof 分支都保留 selection dependency 和 `NON_RECIPE` owner，并同帧停止。ground traversal 样本的同一格也属于 strict owner，所以现行 typed reason 仍是优先级更高的 `STRICT_OWNER_CHANGED`；本轮没有改写这个既有行为；
- corridor prefix 没有带入 selection-only 格，也没有提前带入只属于终点 exact proof 的格。

正式 Session 时间线沿用第 9.4 节同一组 sequence 和合法 `WorldKnowledge` 写入。修正后结果如下：

| goal revision | route 接纳 sequence | `UNKNOWN -> AIR` sequence | 对应 route 的 typed 结果 | affected / query | dependency recovery | 同帧身体结果 |
|---:|---:|---:|---|---|---:|---|
| 1 | 2 | 45 | `UNAFFECTED/NO_INTERSECTION` | 空 / 0 | 0 | Walk 继续 |
| 2 | 46 | 56 | `UNAFFECTED/NO_INTERSECTION` | 空 / 0 | 0 | Walk 继续 |
| 3 | 57 | 62 | `UNAFFECTED/NO_INTERSECTION` | 空 / 0 | 0 | Walk 继续 |
| 4 | 63 | 73 | `UNAFFECTED/NO_INTERSECTION` | 空 / 0 | 0 | Walk 继续 |

`start_goal()` 接受 revision 1，三次 `update_goal()` 都返回 true。四个 route identity 和 planning work identity 各不相同。测试没有伪造接管：因为夹具不提交输入回执，revision 2—4 的新路线先作为 pending route 由各自 tracker 接纳，旧 incumbent 继续产生合法 Walk。断言按 route identity 读取对应 incumbent 或 pending validation。四次变化均没有取消路线，没有增加任何 recovery cause；illegal transition 和 damage 都为 0。

直接检查为 **8/8**：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_d059_terminal_selection_dependencies -v
```

本批没有运行 Fabric，也没有执行 observation request／protection 专项和 D059 性能大集合。这些仍属于 D059-D。F1-D 保持打开；两个既有 `straight_2_0` 失败批次继续保留。

### 9.6 D059-D：消费者、安全、身份和性能

D059-D 已完成组件实现，初始源码提交为 `2b9f2fa`。复审发现 RouteControl 只用 ActiveRoute 生成 validation identity，没有先证明 executor 和 coordinator 确实属于这条路线；修正提交为 `6645780`。本批没有改变 GoalState、规划搜索、恢复预算、跟随阈值或底层控制。

消费者检查沿正式 Session 链生成 observation request，并在下一份正式观察进入时记录 `WorldKnowledge.set_protection()` 的真实参数。结果如下：

- exact terminal proof 存在时，selection-only 格既不请求也不保护；已选终点的支撑、净空、扫掠和 0.1 格采样依赖仍全部保留；
- exact proof 不存在时，完整 terminal selection dependencies 仍被请求和保护，原 `NON_RECIPE` 失败关闭没有缩小；
- bounded corridor 未到终点时不带 terminal selection 或 exact proof；进度到达终点后加入 exact proof，仍不加入 selection-only 格。

身份检查覆盖 `world_session`、`route_id`、`route_revision`、`source_request_id`、`goal_id`、`goal_revision`、`planning_generation`、`work_identity` 和 `action_index`。Supervisor 使用当前 RouteControl 的正式身份调用 tracker，九项中任一错配都得到 `STOP/ROUTE_IDENTITY_CHANGED`。`fixed_route_id` 继续由 `RouteProgressEvidence` 单独检查，错配得到 `STOP/PROGRESS_IDENTITY_MISMATCH`。RouteControl 构造时还要求 tracker 持有同一 ActiveRoute、executor 持有同一 ActionRoute；存在 coordinator 时，其 route 和 executor 也必须分别是同一对象。旧 executor 包装几何相同的新 ActiveRoute、coordinator route 错配和 coordinator executor 错配都会在任何控制输出前抛出 `ContractViolation`。这些诊断没有参与授权。

真实世界安全反例继续成立：移除选定支撑或在选定净空插入完整方块时，incumbent 同帧停止且没有正向输入。pre-terminal `NON_RECIPE`／strict owner 与 exact recipe 共享格时仍停止；三个无 exact proof 分支也保持原失败关闭。第 9.5 节的四次 Session 修订仍分别得到 `UNAFFECTED/NO_INTERSECTION`、affected 空、零 query、零 dependency recovery，旧 Walk 同帧继续。

聚焦检查结果：

| 检查 | 结果 |
|---|---:|
| D059 owner 错配红绿检查与合法 incumbent／pending | 3/3 |
| RouteControl、Session、D058／D059、F1-C 与薄跟随聚焦组合 | 282/282 |
| 性能工具 source identity 与拒绝覆盖 | 1/1 |

完整 motion_nav 运行了 1,408 项，结果为 5 项失败，不能写成完整集合通过。`changed_heading...seed=163`、standalone 旧 CraftGround 闭包、R28 migration 旧恢复入口、R28 shared recovery 旧期望这 4 项在干净 `ced67be` 上同样稳定失败；landing-support 的 I3 只在完整集合出现，单独复跑通过。本批把它们登记为既有或时序问题，没有扩大范围修改。

复审后的正式性能证据在 `evidence/motion_navigation/d059-route-revalidation-v4/`。命令通过项目规定的 conda launcher 运行；`performance.json` 记录来源提交 `6645780`、`git_dirty=false`、空 git status、源码哈希、环境和全部原始样本。source identity 已加入 `mc2p/motion_nav/route_body_controller.py`。`SHA256SUMS` 校验 `COMMAND.txt` 与 `performance.json`。v3 原字节保留，但它没有把本次实际执行边界文件纳入 source identity，因此只作为复审前历史证据。

| 组 | P95 ms | P99 ms | 最大值 ms | 门槛 |
|---|---:|---:|---:|---|
| 非空变化但无交集 | 0.0056 | 0.0057 | 0.0074 | 通过 |
| `SURFACE_EDGE` 一次重验 | 0.2477 | 0.2652 | 0.3107 | 通过 |
| `STANDABLE_CONNECTION` 一次重验 | 0.2951 | 0.3138 | 0.3342 | 通过 |
| 复杂形状一次重验 | 0.2525 | 0.2837 | 0.5128 | 通过 |
| 单 incumbent 两次重验 | 0.1613 | 0.1640 | 0.2134 | 通过 |
| incumbent + pending 四次重验 | 0.2826 | 0.2850 | 0.3211 | 通过 |
| 完整 F1-C prepare，3,413 样本 | 6.3571 | 10.2462 | 13.3886 | 通过 |

六组新增段的 P95 都低于 1 ms；完整 prepare 的 P95、P99 和最大值也分别低于 8、15 和 30 ms。本次单批 prepare 比 v3 慢，但仍分别保留 1.6429、4.7538 和 16.6114 ms 的门槛余量。这里按冻结绝对门槛判定，不把一次运行的波动写成统计结论。

D059-D 组件与性能批本身没有运行 Fabric。复审整改完成后运行的新 `straight_2_0` 仍未通过，见第 10 节。F1-D 继续打开，其余四个实机场景仍不运行。

## 10. D059 修后实机失败与 D060 验收计划

### 10.1 冻结结果

D059 修后的 `straight_2_0` 原始目录为：

`artifacts/f1-known-world-following/20261005T0035326046513Z-straight-2-0-d059/`

命令使用正式 `python -m scripts.f1_known_world_following_fabric scenario --id straight_2_0` 入口。来源提交为 `40cafce8a0f747279aacaa24e41976d36bd1d63d`，启动前 git status 为空。本批保留原字节，不覆盖、不改名，也不把失败样本从分母删除。

| 项目 | 结果 | 门槛 | 判定 |
|---|---:|---:|---|
| 目标稳定速度 mean | 1.873496 格／秒 | 1.8—2.2 | 通过 |
| 修订响应 P95 | 4 tick | ≤5 | 通过 |
| 稳定 excess mean／P95 | 1.887882／2.136090 格 | ≤0.75／≤1.5 | 失败 |
| 最终水平距离 | 0.741732 格 | ≤2.5 | 通过 |
| 规划提交／接受修订 | 25／19，1.315789 | ≤1.25 | 失败 |
| task recovery | 4 | 0 | 失败 |
| 安全违规 | 0 | 0 | 通过 |
| 终态／来源 | `cancelled`／已释放 | 有界／释放 | 通过 |
| host／time／cleanup | 全部通过 | 全部通过 | 通过 |

四次恢复的原始 typed 记录如下：

| tick／Observation sequence | goal revision | route id | affected cells | typed validation |
|---|---:|---|---|---|
| 41／43 | 9 | `ed37db6656912f0f70c7da2e` | `(0,-60,10)`、`(0,-59,10)` | `STOP/NON_RECIPE_OWNER_CHANGED`，0 query |
| 51／53 | 11 | `dc9318ad75bd3279786d03e1` | `(0,-60,11)`、`(0,-59,11)` | 同上 |
| 61／63 | 13 | `39bfcd602098f8e0eb249741` | `(0,-60,12)`、`(0,-59,12)` | 同上 |
| 70／72 | 15 | `9c43371c24ea81b1613fcaf9` | `(0,-60,13)`、`(0,-59,13)` | 同上 |

每次 typed `STOP` 都在同一帧进入 `cancel_braking`，dependency recovery 累计值依次增加到 1、2、3、4。execution、information、acquisition 和 planning recovery cause 没有增加。正式请求在变化写入前包含对应列：Observation 42 后请求 z=8／10，52 后请求 z=9／11，62 后请求 z=8／10／12，71 后请求 z=13。D059 没有在实机把这些格从 effective dependencies 中移除。

### 10.2 直接证据与离线重建

实机 JSON 直接证明 route identity、goal revision、变化格、typed disposition/reason、query count、请求、制动、恢复、距离和规划数字。它没有保存完整 `ActiveRouteValidationPlan` 的 owner/provenance，也没有单列 world protection 参数。

离线分析按顺序读取同批原始 Observation，写入真实 `WorldKnowledge`，再调用正式目标选择、surface route 构建与 `RouteAdmitter`。四组结果一致：

- 目标中心 z 为 9.926195、10.821322、11.803690、12.797093；
- candidate 末节点中心为 z=9.5、10.5、11.5、12.5，selected terminal 与其完全相等；
- 触发格在 `terminal_target.dependencies` 中，不在 candidate、末节点、`SURFACE_EDGE` 或 initial connection dependencies 中；
- 触发格的 provenance 只有 `NON_RECIPE` owner；
- 从最后 Walk 前一点到已选末节点调用现有 `query_standable_connection()`，四组都 `FEASIBLE`，精确依赖不含触发格。

离线重建没有复用原 planning work identity，route hash 也不同，因此只用于确认依赖来源。仓库验收必须用独立、可重复的 fixture 固化这些参数，不能引用 `.tmp` 作为长期证据。

D059-B 的合成样本与实机分支不同。它的 selected terminal 比末图节点偏移 0.1 格，所以进入第二次 direct query 并获得 exact recipe。实机选择器在靠前候选遇到未知净空后退回节点中心，距离为零，现行 builder 跳过了 exact query。

### 10.3 D060 验收顺序

完整方案见 [D060：终点等于末图节点时仍建立精确执行证明](../decisions/0060-prove-terminal-node-execution-without-selection-leakage.md)，阶段安排见 [F1-D.3](../stages/F1-known-world-following-plan.md#f1-d3终点等于末图节点时补精确证明)。按下面顺序实施和验收：

1. **A：红测。** 使用合法、仓库内自足的 WorldKnowledge fixture 复现目标 z=9.926195、start z5、末节点 z9、接纳身体 z=6.284452 的 equality/no-exact 分支；再参数化 z=10—13 四条独立 goal／route identity。生产修改前必须看到 selection-only `NON_RECIPE` 停止。
2. **B：builder。** 仅对普通同高、无 traversal plan、最终 Walk 至少有两个 fixed-route points 的 equality 分支调用现有 `query_standable_connection()`。connection source 必须是 `fixed_route.points[-2]`；recipe 必须绑定同 action index、同 `fixed_route_id` 和最后一腿。只有 `FEASIBLE` 才生成 terminal exact proof。最终 Walk 使用“追加终点选择前已有依赖 ∪ exact terminal dependencies”；到达终点的 full corridor 使用“原 prefix 依赖 ∪ exact terminal dependencies”。
3. **C：消费者与安全。** 四个 selection-only 更新分别得到 `UNAFFECTED/NO_INTERSECTION`、affected 空、0 query、0 recovery、无 `cancel_braking`，同帧 Walk 继续。observation request 和 protection 不含这些格，但仍含 exact support、clearance、sweep 和 0.1 格采样依赖。bounded prefix 不提前带入 terminal exact。早期 action、strict action 和其他 Walk 的独占格不复制到最后 Walk，并按各自 owner 正常退役。
4. **C：反例。** 移除选定支撑、堵住选定净空、pre-terminal `NON_RECIPE`／strict 与 exact 共享、九个 `ActiveRouteValidationIdentity` 字段错配或 `fixed_route_id` 错配，都必须同帧 `STOP` 且无正向输入。ground traversal tail、direct query 非 `FEASIBLE`、body connection 后单 node 等无法绑定最后一腿的情况，继续保留完整选择依赖并失败关闭。
5. **D：回归。** 先跑 D058、D059、route admission、route body、Session、F1-C 与 F1 跟随聚焦集合，再运行完整 motion_nav。前次 1,408 项中的 5 项失败必须逐项对照：相同旧失败如实登记，不能声称完整通过；出现新增失败、签名变化或可单独复现的产品回归时停止并查根因。
6. **D：性能 v5。** 新建 `evidence/motion_navigation/d060-route-revalidation-v5/`，使用标准 conda 命令和干净生产提交。六组重验继续要求新增段 P95≤1 ms；完整 prepare 丢弃前 100 后至少 1,000 样本，P95≤8 ms、P99≤15 ms、max<30 ms。equality exact query 只允许发生在接纳阶段，不能增加逐帧查询。
7. **E：独立复审。** P0／P1／P2 清零后才进入实机。复审必须核对最终 action 的前一点、保守 no-proof 分支、shared owner、九字段身份、fixed route identity、source identity 和证据不可变性。
8. **F：单场 Fabric。** 新世界、新目录只运行 `straight_2_0`。selection-only 格允许不再被请求而保持 `UNKNOWN`；若由其他正式来源出现，必须 `UNAFFECTED/NO_INTERSECTION`、0 query、0 recovery。四条路线身份不得复用。场景还要同时通过第 8.2 节的速度、距离、响应、规划比例、安全、最终保持、终态、来源、host、时间与清理门槛。

任何组件、安全、性能、复审或实机门槛失败都停止。`straight_2_0` 通过前，不运行 `lateral_2_0`、`move_stop_800_resume_2_0`、`normal_cancel_2_0` 或 `first_input_late_one_tick_2_0`。

## 11. D060 组件结果与 D061 性能验收

### 11.1 D060-A 至 D060-C

D060 equality 分支已经用最终 Walk 的 `fixed_route.points[-2]` 重放现有精确连接查询。四组原实机结构得到 terminal exact recipe；selection-only 格不再进入执行依赖。无法绑定最后一腿、direct query 非 `FEASIBLE` 和 ground traversal tail 继续使用 `NON_RECIPE` 并失败关闭。

直接与聚焦检查 250/250。完整 motion_nav 共 1,417 项，仍为前次登记的 5 项失败，没有新增失败或签名变化。D060 的生产和测试提交为 `1e72e5d`。

### 11.2 原性能门槛失败

原组合量尺在一个进程中连续运行十场。结果如下：

| 批次 | 来源 | prepare P95／P99／max | 结论 |
|---|---|---|---|
| v5 | `1e72e5d`，干净 | 4.9077／7.9218／34.7251 ms | max 失败 |
| v6 | `1e72e5d`，干净 | 6.1230／10.4354／51.8439 ms | max 失败 |
| v7 diagnostic | `ce37622`，干净 | 4.9415／7.9353／15.0174 ms | phase wrapper 改变 GC 相位，只算诊断 |
| v8 GC diagnostic | `b5ef893`，干净 | 6.7547／10.5529／48.8184 ms | max 失败；定位到 gen2 |

v8 在与 v5／v6 相同的 captured sample 3242 记录到一次 36.9770 ms 的 generation 2 扫描，collected 为 0。同帧另有 0.0305 ms 的 gen0。扣除 gen2 后约为 11.8414 ms，与相邻重帧一致。六组 route revalidation 的 P95 为 0.0056—0.3514 ms，全部通过 1 ms 门槛。

这些结果不能写成性能通过。v5、v6、v8 原样保留为失败证据；v7 不进入验收分子。

### 11.3 D061 两条正式门槛

完整决定见 [D061：分开验证独立场景和长 Session 性能](../decisions/0061-separate-independent-scenario-and-long-session-performance.md)。

**独立场景证据**继续运行原十场和原业务判定。每场完整关闭后，在下一场开始前、prepare 计时外执行 `run_scenario.cache_clear()` 和 `gc.collect(2)`。所有原始 prepare 样本仍按原顺序聚合；只全局丢弃最前 100 个，记录每场 captured／retained ordinal。门槛保持 P95≤8 ms、P99≤15 ms、max<30 ms，十场业务全部通过；六组 route revalidation P95≤1 ms。

**长单 Session 证据**在独立子进程中运行。它使用一个正式 F1-C world、同一 task／Session／world 和 20 Hz 时间线。先完成 `move_stop_800_resume`，随后继续运行。trace 必须是 `BoundedAsyncTraceWriter` 加 `SegmentedTraceWriter`，容量 2,048；不得使用 `_RecordingTrace`。warmup 为 100，retained 至少 4,096；没有观察到 gen2 时最多延长到 8,192；较晚出现 gen2 后至少再保留 128 个样本。

长 Session 同样要求 P95≤8 ms、P99≤15 ms、max<30 ms，并同时满足原行为、安全、身份、取消、source 释放、trace worker failure=false、dropped=0 和分段证据哈希闭合。场内不得手动 GC、修改 threshold 或扣除 GC 时间。

独立场景正式批次 `d061-independent-scenarios-v1` 来自干净提交 `36986a4`。它保留 3,413 个 prepare 样本，P95／P99／最大值为 **6.8668／10.9606／14.4872 ms**；六组路线重验 P95 为 **0.0060—0.5180 ms**，十场业务全部通过。这一条门槛已通过。

长 Session v1 同样来自 `36986a4`。它保留 8,192 个样本，P95／P99／最大值为 5.2877／5.5120／7.7875 ms；行为、安全、身份、取消、source 释放和 trace 完整性通过。但是 GC 记录只有 127 次 generation 0，没有 generation 1 或 generation 2。旧工具错误地把达到 8,192 个样本写成 `gen2_followup=true`，所以其 `passed=true` 不进入验收。原始目录 `artifacts/d061-long-session-v1/` 保留，不改写。

v2 正式诊断批次 `d061-long-session-v2-diagnostic` 来自干净提交 `179d30c`，按门槛退出 1。它记录 116 次 gen2，全部发生在 prepare 外；99 次在主控制线程，17 次在 trace 线程，耗时最小／P95／最大值为 4.4167／29.3525／33.7916 ms。prepare P95／P99／最大值为 5.1944／5.3794／7.0613 ms。诊断写满 32,768 个槽位并溢出。v2 说明 prepare-only 量尺看不到实际 gen2 停顿，不能签署长期性能。

v3 正式批次固定为 `d061-long-session-v3-formal`。它保留 prepare P95≤8 ms、P99≤15 ms、max<30 ms，同时测量当帧目标更新、`RuntimeNavigationDriver.tick()` 到后端接受输入的完整产品控制路径。每帧产品路径必须小于 50 ms，input deadline miss 必须为 0，minimum slack 必须大于 0。监视器、`_tick_evidence`、响应列表和证据整理不进入计时。

gen2 覆盖改按正式控制帧计算：retained 窗口内，产品控制路径或 trace 线程自然发生的 gen2 都有效；主线程在监视器和证据整理期间触发的 gen2 不算。首个有效 gen2 后至少再完成 128 个正式控制帧。诊断容量至少 65,536；溢出、GC start／stop 不配对或控制路径回调不配对都失败关闭。v3 只运行一次；失败后停止，不刷结果、不运行 Fabric。

### 11.4 D061 正式结果和证据边界

独立场景证据 `evidence/motion_navigation/d061-independent-scenarios-v1/` 已重新校验 SHA 并从原始 ns 重算统计。十场业务全部通过；prepare 保留 3,413 个样本，P95／P99／最大值为 6.8668／10.9606／14.4872 ms。六组路线重验 P95 为 0.0060—0.5180 ms，均低于 1 ms。

长 Session v3 从干净提交 `a6d823b` 只运行一次。prepare 保留 4,096 个样本，P95／P99／最大值为 5.2493／5.3928／10.1588 ms。完整产品控制路径 captured P95／P99／最大值为 11.2243／30.5366／41.2163 ms；retained P95／P99／最大值为 11.2050／30.5680／41.2163 ms。这些墙钟结果有效。

首个有效 retained gen2 位于 ordinal 23，由 trace 线程触发，耗时 14.6725 ms；随后继续完成至少 128 个正式控制帧。GC 共 17,306 次，容量 65,536，无溢出或回调不配对。正式 trace 写入 16,955 条，丢弃 0，worker failure 为 false；11 个分段共 16,956 条记录。行为、安全、身份、取消和 source 释放同时通过。

v3 文件中的 450 ms minimum slack 来自 `500 ms logical deadline - 50 ms simulated tick`。它没有使用控制路径墙钟，不能证明 50 ms 控制周期的 miss 和余量。v3 的 18 个 recorded gate 因此只按历史口径保留，不能关闭现行长 Session 验收。

跟踪证据在 `evidence/motion_navigation/d061-long-session-v1/`、`d061-long-session-v2-diagnostic/`、`d061-long-session-v3-formal/` 和 `d061-long-session-v4-formal/`。其中 v1 仍标为量尺结论无效，v2 仍标为诊断失败，v3 保留有效墙钟但不签署 deadline/slack，v4 是现行正式通过证据。每个目录保存原始 `performance.json`、重算摘要、命令、trace 完成标记、分段清单和各段 SHA-256。完整 trace 分段分别留在本地 `artifacts/`，没有进入 Git 或 standalone；缺少这些大分段时可以复核统计和哈希，不能逐条重放内容。

### 11.5 v4 最终量尺

v4 继续使用相同正式路径、trace、20 Hz、gen2 和行为门槛。每帧以同一对 `perf_counter_ns()` 计算产品控制墙钟，余量为 `50,000,000 ns-duration_ns`；余量小于等于 0 就记一次 deadline miss。控制路径最大值、miss 和 minimum slack 只看 retained 帧，captured 与 retained 分开输出。COMMAND 必须记录实际标准 conda 外层命令。

v4 从干净提交 `4f194da` 只运行一次并通过。prepare captured 4,196、retained 4,096；retained P95／P99／最大值为 **4.3106／4.4078／5.7910 ms**。正式控制路径 captured P95／P99／最大值为 9.9545／26.7095／39.7818 ms；retained P95／P99／最大值为 **9.9315／25.3232／39.7818 ms**。retained deadline miss 为 **0**，最小周期余量为 **10.2182 ms**。

首个有效 retained gen2 位于 ordinal 23，随后继续完成至少 128 个正式控制帧。GC 共 17,304 次，容量 65,536，没有溢出或回调不配对。行为、身份、安全、取消和 source 释放通过；trace 写入 16,955 条、丢弃 0、worker failure 为 false，11 个分段共 16,956 条记录。15 个原始 artifact 哈希、原始 retained ns、18 个 gate、标准 conda COMMAND 和完整分段流已重算或遍历。

正式证据位于 `evidence/motion_navigation/d061-long-session-v4-formal/`，完整大 trace 保留在 `artifacts/d061-long-session-v4-formal/`，没有进入 Git 或 standalone。该性能批完成 D060-D，没有运行 Fabric；随后完成的复审和单场结果见第 12 节。

聚焦回归 11/11、standalone 导出测试 6/6、实际导出与 verify 均通过。`test_public_repository_completeness` 为 2/3：剩余断言禁止导出四个 CraftGround 后端，但用本批修改前的 clean HEAD manifest 重算也会选中同样四个文件，属于既有清单闭包与断言偏差。本批没有借性能证据扩修该范围。

## 12. D060-F 失败与 D062 验收计划

### 12.1 单场结果

D060-F 使用正式 F1 runner、Fabric 1.21、新世界和新目录，只运行一次 `straight_2_0`：

`artifacts/f1-known-world-following/20261005T0408072740471Z-straight-2-0-d060/`

来源提交为干净的 `6ff9ad16ddaba6f7a41d1fc8ab9981ae7adee8e4`。三个端口在启动前为空闲，结束后均释放；旧三个失败目录没有改写。

| 项目 | 结果 | 门槛 | 判定 |
|---|---:|---:|---|
| 目标稳定速度 | 1.867230 格／秒 | 1.8—2.2 | 通过 |
| response P95 | 4 tick | ≤5 | 通过 |
| stable excess mean | 0.649322 格 | ≤0.75 | 通过 |
| stable excess P95 | 1.541509 格 | ≤1.5 | 未通过 |
| planning／accepted | 20／16 = 1.25 | ≤1.25 | 通过 |
| task recovery | 2 | 针对性变化为 0 | 未通过 |
| 最终距离 | 0.673621 格 | ≤2.5 | 通过 |
| 安全、终态和来源 | 0 违规；两端 cancelled；source released | 全部满足 | 通过 |
| host／time／cleanup | 双端通过；无 cleanup failure；端口释放 | 全部满足 | 通过 |

本批失败后已停止，没有运行另外四个场景。

### 12.2 两次 typed STOP

| tick／Observation | route／goal revision | affected cells | typed 结果 |
|---|---|---|---|
| 62／64 | `f1-d/straight_2_0-request-17-local`／13 | `(0,-59,10..12)` | `STOP/PLAN_UNAVAILABLE`，0 query |
| 77／79 | `7ce64f4f805eaecae231e427`／16 | `(0,-59,13)` | `STOP/NON_RECIPE_OWNER_CHANGED`，0 query |

第一条是 same-support local direct Walk。三格属于身体到目标中心的执行扫掠；路线由 Session 手工构造，没有 validation plan。第二条已用原 Observation V3 精确重建，并得到与实机一致的 route id。D057 forward-entry 后最终 Walk 只有一个 graph node；已有 initial `STANDABLE_CONNECTION` proof 精确覆盖最终 fixed leg，但 D060 没有把它复用为 terminal execution，z=13 因此仍是 selection-only `NON_RECIPE`。

D060 不是完全无效。上一 D059 批次在 tick 41、51 的前两次 graph equality 停车已消失。连续前进后，正式链才进入上述 local direct 和 single-node 两条新路径。

### 12.3 P95 边界

27 个稳定样本中，最大和第二大 excess 位于 tick 39、40，分别为 1.707893、1.541509 格。两帧 recovery 都是 0，控制器持续 forward=1，follower 速度约 4.317 格／秒。D059 和 D060 两批在 tick 35—40 的双方位置与速度一致，首次输入和首个位移都在 tick 12。

这说明 P95 失败来自初始信息／规划冷启动后的确定性追赶尾部，不是两次 dependency recovery、单次调度或 D060 控制退步。当前冻结口径下仍判失败。D062 不修改这个口径，也不处理冷启动。

### 12.4 D062 验收范围

D062 的完整方案见[决定记录](../decisions/0062-unify-direct-walk-validation-proof.md)。验收先冻结：

1. local direct Walk 只通过 `RouteAdmitter.admit_local_direct(typed request, frame, profile, capability identity)` 接纳。RouteAdmitter 用 `query_support_surfaces()` 重新取得并核对 request start 的 `SurfaceNodeId` 和身体支撑，再建立唯一 `WALK_LEG` owner、0→1 binding 和 exact `STANDABLE_CONNECTION` recipe；`connection_length_blocks=0`。seq64 等价场景的合法空气确认得到 `CONTINUE/REVALIDATED`、同帧 forward、0 recovery；
2. local query 的 `NEEDS_INFORMATION`、`BLOCKED`、`UNSUPPORTED` 分别映射到 `GOAL_STANDING_POINT_NEEDS_INFORMATION`、`CURRENT_BODY_CANNOT_CONNECT`、`ROUTE_CAPABILITIES_CHANGED`，Session 只消费 typed `AdmissionResult`，不拼支撑、扫掠、依赖或 plan；
3. rev16 保持原 route id 和 fixed-route 结构。现有 `INITIAL_CONNECTION` owner 独占完整两点 fixed leg，action dependencies 等于 recipe dependencies，`retire_after_progress_blocks` 等于完整 connection length；不创建 terminal WalkLeg、recipe 或 owner。z=13 从 action、corridor、effective dependencies、request 和 protection 移除，显式更新时为 `UNAFFECTED/NO_INTERSECTION`、0 query、0 recovery；
4. initial owner 在 progress 小于 connection length 时仍负责依赖；恰好达到或超过时退休。三种边界均有变化格反例，旧身份和倒退进度失败关闭；local WALK_LEG 按自己的 binding 退役；
5. selected support 移除、selected clearance 阻塞、不支持材质、九字段 identity 和 fixed route identity 错配均同帧 STOP，无正向输入；
6. direct query 不可行、endpoint 不同、traversal、strict、高差和多 action owner 退役保持保守；私有 proof context 只能在同次 admission 调用栈内，且受 world、profile、capability、route、action、fixed route 和 endpoint 约束；
7. 聚焦与完整 motion_nav 不新增失败；D061 独立场景和长 Session 两条性能门槛继续通过；
8. 独立复审 P0／P1／P2 清零。

D062 不运行 Fabric。D062 完成后，冷启动／稳定 P95 仍需单独方案；在该问题关闭前，不再次运行 `straight_2_0`，也不运行其余四场。

### 12.5 D062-A 至 D 实施结果

生产提交为 `1edb748`。`NavigationSession` 的 local 路线不再调用 `sweep()`、`query_support()` 或手工构造 plan。它只把 typed request、当前 frame、ground profile 和 capability identity 交给 `RouteAdmitter.admit_local_direct()`，再消费 typed `AdmissionResult`。

RouteAdmitter 用 `query_support_surfaces()` 重新取得并核对 request start 和身体支撑。合法 local direct Walk 生成一个 `STANDABLE_CONNECTION` recipe、一个 `WALK_LEG` owner 和 0→1 binding；connection length 为 0，不创建 initial owner。`NEEDS_INFORMATION`、`BLOCKED`、`UNSUPPORTED` 分别进入第 12.4 节冻结的三个 typed 出口。移除选定支撑或堵住净空时，tracker 同帧 STOP。

rev16 精确结构保持两点 fixed route 和一个 graph node。现有 `INITIAL_CONNECTION` owner 独占整条 fixed leg；action dependencies 等于该 recipe dependencies，退役点等于完整 connection length，没有 terminal leg、recipe 或 owner。z=13 selection-only 依赖从 action、corridor 和 validation provenance 移除。退役前的变化仍重验并停止；进度恰好达到或超过 connection length 后，旧 owner 不再处理该格。

检查结果：

- D062 专项 6/6；
- D058、D059、D060、F1-C、目标保持和 closed-loop 聚焦 219/219；
- D058 route-revalidation 与 D061 long-session 工具的低成本检查 5/5；只运行短参数测试，没有刷新长跑证据；
- 完整 motion_nav 1,427 项中 1,422 项通过，失败仍是此前登记的 5 项：`changed_heading...seed=163`、landing-support I3、standalone 旧 CraftGround 闭包、R28 migration 旧恢复入口、R28 shared recovery 旧期望。失败名称和断言没有变化，本批没有扩范围修改。

D062-E 独立复审仍打开。本轮没有运行 Fabric，也没有改写 D060-F 的失败数字。即使 D062 复审通过，稳定 excess P95 仍为单独的冷启动问题。

### 12.6 D062 复审整改的直接证据

复审发现 local direct 还有一个零长度边界。身体已经位于 GoalState 中心，但速度仍高于目标允许值时，目标尚未正式满足；原实现会尝试建立起点和终点相同的 Walk，并在 0→0 progress binding 上抛出 `ContractViolation`。现在 RouteAdmitter 仍先完成正式 direct query。查询为 `FEASIBLE` 且距离不大于统一 epsilon 时，它返回既有 typed `CANDIDATE_HAS_NO_ACTIONS`，不创建 route、owner 或 validation plan。Session 只消费这个结果，保留 source，发中性 settling，并在下一帧重新判断目标。速度降到允许范围后，正式 Session 以 `goal_state_satisfied` 正常完成。Session 没有重算支撑、扫掠或几何。

seq64 等价正例也改为正式 Session／Supervisor 时间线证据。首帧因 z=10 净空仍为 `UNKNOWN` 进入 `NEEDS_INFORMATION`，当 sequence 64 的合法 `UNKNOWN→AIR` 事实到达后，同一个控制帧由 RouteAdmitter 接纳新的 local exact route。Supervisor 对该 route 返回 `CONTINUE/REVALIDATED`，使用 1 次正式 query，并提交非中性的合法移动。该时间线没有伪造旧 route，dependency recovery 和 task recovery 都是 0。

本次直接检查结果：

- D062 专项 8/8；
- NavigationSession、目标到达、D058、D059、D060 和 D062 聚焦 211/211；
- 完整 motion_nav 1,429 项中 1,424 项通过。失败仍是第 12.5 节登记的同 5 项，名称和断言没有变化。

本次没有实施 D063，也没有运行 Fabric。随后独立复审确认 D062 的 P0／P1／P2 为零；该结论只关闭 D062，不外推为 D063 或 Fabric 通过。

## 13. D063 首次完整目标面信息验收计划

D063 的完整方案见[首次完整请求目标面可确定的信息](../decisions/0063-request-complete-bounded-goal-surface-information.md)。它只能在 D062 完成且独立复审 P0／P1／P2 清零后实施。D062 与 D063 之间不运行 Fabric。

### 13.1 组件门槛

1. D060-F 等价 Observation V3 初始帧中，首次目标面请求一次返回原 25+73=98 个 missing；排序稳定、无重复，全部属于当前 GoalState 的同一次表面几何枚举；
2. 一次合法 Observation V3 回复回答 98 格后，下一控制帧进入现有 planning 流程，不再发第二轮目标面查询；缺格、不可用或乱序回复不能进入 planning；
3. missing 超过 128 时，每帧 payload≤128。完整集合保存在现有 `_snapshot_missing`，按原优先级、节流和分页逐页请求；全部回答前不规划，不新增 owner；
4. `occluded`、`out_of_range`、`unavailable` 保持现有有界信息结果。UNKNOWN 不变成空气，已知 BLOCKED／UNSUPPORTED 不改成等待；
5. 目标 revision 跳到新区域时，新 missing 原子替换旧集合。旧 Observation 只更新 `WorldKnowledge`；旧 notification 或旧否定不能完成新查询；取消、close 或业务终态后不能恢复请求、规划或输入；
6. 目标面集合不包含可能路径方向的额外 UNKNOWN、未展开 graph frontier 或规划搜索边界外事实。候选由 `support_surfaces` 现有形状和碰撞 owner 枚举产生，不使用固定盒子；
7. PlanningCoordinator 的工作身份、双工作上限、期限、候选接纳和 RouteAdmitter 均保持不变。

### 13.2 产品门槛

新的 `straight_2_0` 只能在组件、完整回归、D061 性能和独立复审通过后运行一次。门槛为：

| 项目 | 门槛 |
|---|---:|
| 首个合法 Walk | ≤11 tick |
| 目标面信息请求／回复轮次 | 1 |
| 首个 Walk 前 planning submission | ≤5 |
| 全场 planning／accepted revision | 不劣于 20／16，且比例≤1.25 |
| revision response P95 | ≤5 tick |
| stable excess P95 | ≤1.5 格 |
| 安全、终态、source、host、time、cleanup | 原门槛全部通过 |

稳定窗口和 P95 算法不变。原 D060-F 的 1.541509 格失败继续保留，不能用调整窗口或排除启动样本让 D063 通过。

### 13.3 性能和执行顺序

D061 门槛继续适用：prepare P95≤8 ms、P99≤15 ms、max<30 ms；retained 正式控制路径 max<50 ms、deadline miss=0、minimum slack>0。证据还要记录每帧 query payload、分页轮次和目标面 missing 总量，证明 payload≤128 且目标面收集有界。

实施顺序固定为红测、`support_surfaces` 查询、Session 消费与生命周期、聚焦和完整 motion_nav、D061 性能、独立复审、一次 `straight_2_0`。任一步失败都先停止；不得提前运行 Fabric，也不得运行其余四场。

### 13.4 D063-A 至 D 实施结果

生产实现保持在现有查询和 Session 入口内：

- `query_support_surfaces(..., collect_complete_missing=True)` 在一次候选枚举中继续收集已知支撑候选的净空与支撑 missing；默认调用保持原行为；
- `_surface_for_goal()` 只为目标面查询启用完整 missing；Session 继续使用原 `_snapshot_missing`、请求优先级、五 tick 节流和每帧 128 格分页；
- 事实未完整时保持 `NEEDS_INFORMATION`，不提交 planning work，也不发送移动输入；UNKNOWN 没有获得通行权限。

直接结果如下：

| 检查 | 结果 |
|---|---:|
| 首次目标面 missing | 98 格，一次返回，无重复 |
| 一次合法回复后的额外请求 | 5 格真实 graph frontier；没有第二轮目标面请求 |
| 超过 128 格 | 分页；残差信息优先；完整前不规划 |
| `occluded`／`outside_view` | 保持 UNKNOWN；五 tick 内不重复请求 |
| 目标 revision／取消 | 新 revision 替换旧 missing；晚到回复只更新世界，不能恢复旧任务 |
| 正式 Runtime 回放输入 | D060-F 的 6 格起距、66 tick 移动期、11 tick 目标输入循环 |
| 正式 Runtime 回放首个 Walk | tick 6，达到 ≤11 tick 门槛 |
| 回放目标面等待轮次 | 1 |
| 首个 Walk 前 planning submission | 5 |
| 首个 Walk 前 graph 信息轮次 | 2 |
| 回放 planning／accepted ratio | 18／15=1.2 |
| 回放 revision response P95 | 1 tick |
| 回放稳定窗口 | 27 个样本；P95=1.534463 格，只作诊断 |
| 回放 task recovery | 1，只作诊断 |

D063 专项 8/8；D059—D063 证明合同 39/39；支撑面、表面规划、Session 和 Runtime 的直接相关检查通过。完整 motion_nav 共 1,437 项，其中 1,432 项通过；失败仍是此前登记的同 5 项，名称和断言没有变化。D061 的低成本性能工具检查通过，本轮没有刷新长 Session 证据。

这些是组件与模拟证据。稳定距离和 recovery 没有被写成通过；原 4 格短回放的单个稳定样本也不再作为验收依据。D063-E 独立复审随后清零 P0／P1／P2，并只运行一次新的 `straight_2_0`，结果见第 14 节。

## 14. D063 后的 `straight_2_0` 失败与 D064

### 14.1 原始结果

原始目录：

`artifacts/f1-known-world-following/20261005T1630101063860Z-straight-2-0-d063/`

来源提交为 `6d8baca52ee06cad3941a65a6332c1a6d3bf011e`。运行前工作树干净；预检六项通过。原始结果如下：

| 项目 | 结果 | 判定 |
|---|---:|---|
| target 实测均速 | 1.773723 格／秒 | 失败，低于 1.8 下限 |
| 第一条实际 Walk | 场景 tick 14 | 失败，门槛≤11 |
| planning／accepted revision | 19／17=1.117647 | 通过 |
| revision response P95 | 5 tick | 通过 |
| task recovery | 0 | 通过 |
| stable excess mean | 3.302394 格 | 失败 |
| stable excess P95 | 4.000742 格 | 失败 |
| 最终距离 | 0.978698 格 | 通过 |
| 安全、终态、source、host、time、cleanup | 全部通过 | 通过 |

没有运行另外四个场景。

### 14.2 根因

目标面 98 格已在一次回复中完成，D063 本身生效。首次后台规划在 tick 1 提交，目标于 tick 4 更新，首个结果到 tick 9 才返回 6 格信息需要；tick 12 又返回 33 格，tick 14 才开始 Walk。这个首个结果只展开 1 个节点，长等待主要不是 A* 展开数量。

执行后，tick 26、41、65 的新目标缺少目标面事实。旧路线先进入退场，系统到 tick 29、44、68 才请求各 28 格。稳定窗口内因此出现明显输入空档。目标速度略低不能解释 stable P95 相对 D060 增加约 2.459 格。

### 14.3 D064 验收范围

D064 的完整方案见[普通地面先尝试有证明的局部直达](../decisions/0064-use-proved-ground-direct-before-background-planning.md)。验收重点为：

1. 初始普通同高 direct route 不等待后台首作业；首个 Walk≤11 tick，首个 Walk 前后台提交≤1；
2. 活动普通 Walk 连续三次目标修订且先缺事实时，0 次 `STOPPING`、0 个无输入空档、0 recovery；
3. successor 只有在输入实际获胜后才替换旧 owner；
4. 支撑、净空、材质、世界和身份反例保持安全；
5. 高差、strict、空中、距离超限和已知阻挡回落原后台或安全等待；
6. direct query 与完整控制路径满足 D064 性能门槛；
7. 独立复审 P0／P1／P2 清零后，只运行一次新的 `straight_2_0`。

### 14.4 D064 A—D 实施结果

局部直达现在必须由任务开始时的 typed `GoalPlanningPolicy` 显式选择。默认 `BACKGROUND_PLANNER` 保留所有既有调用方的后台规划；`PROVED_LOCAL_DIRECT_THEN_BACKGROUND` 才允许先尝试有证明直达。策略只由 `GoalRequestLedger` 保存，同一任务的 goal revision 不能改变，same-task continuation 和重锚保留原值。正式跟随只在 `KnownWorldFollowDriver.start()` 声明 direct-first，后续不按 follow、task id、reach policy、goal 大小或 reason 字符串判断权限。

实现结果：

| 检查 | 结果 |
|---|---:|
| 最终 D064／R28／目标策略／正式跟随聚焦集合 | 140/140 |
| 三轮缺事实的正式 Runtime 跟随 | tick 21／61／102 分别接受 rev5／rev9／rev14；每轮 10 格；0 `STOPPING`、0 输入空档、0 recovery、0 planning submission |
| typed policy 修正后的最近完整 motion_nav | 1,442/1,447；仍为冻结的同 5 项失败，无新增失败；本次生命周期补强后未重跑完整集合 |
| direct query，100 次预热后 1,000 次 | P95 1.6753 ms；max 1.6999 ms |
| D061 冻结短参数 prepare | P95 3.9066 ms；P99/max 3.9725 ms |
| D061 冻结短参数正式控制路径 | P95 21.5256 ms；P99/max 23.2795 ms；miss 0；最小余量 26.7205 ms |

最终性能来源是干净提交 `546e6e6`。direct query 满足 P95≤2 ms、max<8 ms，公开证据见 `evidence/motion_navigation/d064-direct-v1/`。D061 短参数的 18 项 gate 全部通过，公开摘要见 `evidence/motion_navigation/d064-control-short-v1/`；它只作低成本回归，长期结论仍引用 D061 v4。此前用 16 个 retained frame 做的过短诊断没有观察到 retained gen2，`gen2_coverage=false`、整体退出 1；该失败摘要保留在同一公开记录中，不能写成通过。

生产与补强提交包括 `da9787d`、`72920ae`、`f71a36c`、`ffbe30a`、`b6894a9` 和 `546e6e6`。`b6894a9` 处理 owned planning、direct route、pending revision 和局部尝试链的生命周期；`546e6e6` 把三轮取证期间的 0 `STOPPING` 要求覆盖到整个场景。

A—D 收口时没有运行 Fabric，也没有把组件和模拟结果写成 F1-D 通过。后续独立复审与单场实机结果见下一节。

### 14.5 D064 独立复审与 `straight_2_0` 结果

独立复审确认没有未关闭的 P0／P1／P2。按 fail-fast 规则，只运行了一次新的 `straight_2_0`：

`artifacts/f1-known-world-following/20261005T2003536561761Z-straight-2-0-d064/`

来源提交为 `7c5c8e1b25d1421878db20ff3d11c6e4f1a96374`，运行前工作树干净，预检全部通过。原始目录、分段轨迹和哈希均已复核。

| 门槛 | 结果 |
|---|---:|
| 首条实际 Walk | tick 4，通过 |
| `STOPPING` | 0，通过 |
| 保持区外中性输入空档 | 0，通过 |
| task recovery | 0，通过 |
| planning submission／accepted revision | 1／11，通过 |
| revision response P95 | 3 tick，通过 |
| Observation payload 最大值 | 98 格，通过 |
| stable excess mean／P95／max | 0／0／0 格，通过 |
| 目标实际均速 | 1.822656 格／秒，通过 |
| 安全、终态、source、host、time、cleanup | 全部通过 |

本次结果不改写 D063 后的失败批次，也不删除更早的失败证据。`straight_2_0` 已满足冻结门槛，因此允许下一步从新世界、新目录单独运行 `lateral_2_0`。

### 14.6 `lateral_2_0` 结果

`lateral_2_0` 使用新世界和新目录单独运行：

`artifacts/f1-known-world-following/20261005T2021024037283Z-lateral-2-0-d064/`

| 门槛 | 结果 |
|---|---:|
| 首条实际 Walk | tick 4，通过 |
| 目标实际均速 | 1.873410 格／秒，通过 |
| stable excess mean／P95／max | 0.133949／0.513506／0.527817 格，通过 |
| revision response P95 | 2 tick，通过 |
| planning submission／accepted revision | 4／13，通过 |
| task recovery | 0，通过 |
| `STOPPING` | 0，通过 |
| Observation payload 最大值 | 98 格，通过 |
| 安全、终态、source、host、time、cleanup | 全部通过 |
| 分段证据与 SHA | 已复核 |

轨迹在 tick 34—47 出现中性输入。逐帧对齐显示，旧路线已经正常完成；这段时间目标仍按旧 revision 满足，或新的 revision 尚未产生。rev9 到达后，下一 tick 就恢复移动。因此这段不属于等待目标面事实时丢失 incumbent，也没有触发 recovery 或 `STOPPING`。

`lateral_2_0` 已满足冻结门槛，因此只批准下一场 `move_stop_800_resume_2_0`。

### 14.7 `move_stop_800_resume_2_0` 失败与修复验证

本场使用新世界和新目录单独运行：

`artifacts/f1-known-world-following/20261005T2031097906144Z-move-stop-800-resume-2-0/`

| 时间或结果 | 证据 |
|---|---|
| pause tick 33—832 | 正常待命；terminal 0、`STOPPING` 0、task recovery 0 |
| tick 853 | rev12 被接受，同帧进入 `FAILED/same_support_local_path_unavailable` |
| 目标事实 | 已形成 42 个 missing，但终态发生在 Observation 请求之前 |
| 修订响应 | rev12 unanswered |
| 释放 | 失败后 source 安全释放 |

这是 P1 产品缺陷，不是夹具、速度或统计口径问题。目标在保持区内满足后，Session 已经没有 incumbent。目标重新移出保持区时，目标面需要更多事实；`update_goal()` 的 missing 分支复用了 ordinary direct eligibility helper。该 helper 在 `incumbent=None` 时返回真，是 initial/direct 入口所需的既有语义，但这里却被误当成“incumbent 可以继续”。同帧旧 `SAME_SUPPORT` request 随后使用新的 goal state，最终产生 `same_support_local_path_unavailable`。

修复只在这一条特殊 missing 分支显式要求 `incumbent_route` 非空。initial direct 和其他 direct eligibility 调用保持原语义。TDD 证据如下：

- Session 公共行为检查在旧逻辑上稳定红：已满足待命、无 incumbent 的任务修订到缺事实目标后仍是 `EXECUTING`；修复后进入 `NEEDS_INFORMATION`，保留 source、pending revision、missing 和 Observation request，不购买 recovery，也不进入失败；
- 新增正式 `Runtime → Driver → Session → Follow` 的 33+800+33 partial-observation 回归。旧逻辑在恢复修订上没有进入 typed `NEEDS_INFORMATION`；修复后会等待信息，本场 missing 集合为 14 格，revision unanswered 为 0，最后正常 cancel、释放 source，安全违规为空。Observation 请求不超过 128 格的通用门槛继续由 D063／D064 专项覆盖；
- 当前 D064 聚焦检查 20/20。实施 agent 先前运行 F1 文件 8/8；根代理增加请求上限等断言后，正式链与 D064 合计 20/20。

当前只完成代码与组件验证，没有重跑 Fabric。修复形成干净提交后，才允许从新世界、新目录重跑 `move_stop_800_resume_2_0`。该场通过前，`normal_cancel_2_0` 和 `first_input_late_one_tick_2_0` 继续不批准；F1-D 和 F1 整体保持打开。

### 14.8 `move_stop_800_resume_2_0` 第二次失败与最终修复

第 14.7 节的首次失败及其证据保持不变。提交 `3999d36` 修正了 `goal_node=None` 时相邻路径误用旧 `SAME_SUPPORT` request 的问题。修复后只从新世界、新目录复跑本场：

`artifacts/f1-known-world-following/20261005T2117132926377Z-move-stop-800-resume-2-0-fix/`

| 时间或结果 | 证据 |
|---|---|
| 来源 | `3999d36` |
| pause | 全程健康 |
| tick 852 | rev11 被接受，同帧进入 `FAILED/same_support_local_path_unavailable` |
| 目标事实 | 终态时仍有 42 个 residual missing |
| 修订响应 | rev11 unanswered |
| 早期观察等待 | 8 次 `needs_target_observation`，均发生在近距离目标区域重叠期间且有界，不是最终原因 |
| 释放 | 失败后 source 安全释放 |

这次实机没有进入 `goal_node=None` 分支。系统已经选出 goal node，随后进入 `SAME_SUPPORT` local 接纳。精确重建得到 typed reason `CURRENT_BODY_CANNOT_CONNECT`：local 接纳把大 `GoalState` 区域中心硬当作终点；中心已经落在当前 surface 外，但目标区域边缘仍有符合支撑、净空和连接规则的合法站位。因此，`3999d36` 修正了第一条失败路径，却没有覆盖这条实际几何路径。

最终修复分两步完成：

1. standable selector 使用 `connection_from=body`，在目标区域与当前 surface 的交集中选择身体可连接的安全终点；
2. 对选中终点再次运行正式 connection query。只有精确重放得到的 dependencies 才进入材质检查、action、validation recipe 和 provenance。

selector 扫描过但未选中的依赖不会冒充执行证明。selector 或精确重放只要不是 `FEASIBLE`，就继续沿现有 typed 出口关闭；实现没有增加后台 fallback，也没有放宽 D059 已冻结的站位选择依赖／路线执行依赖边界。

| 修后检查 | 结果 |
|---|---:|
| D062 | 13/13 |
| D064 | 19/19 |
| D059／D060 | 23/23 |
| F1 800 tick 正式链回归 | 1/1 |
| local direct 性能 | P95 0.7115 ms；max 0.8471 ms |
| 独立复审 | P0／P1／P2 为零 |

当前最终修复只完成代码、测试、性能和独立复审，没有运行第三次 Fabric。修复形成提交后，只批准从新世界、新目录重跑 `move_stop_800_resume_2_0`。该场通过前，`normal_cancel_2_0` 和 `first_input_late_one_tick_2_0` 继续未批准；F1-D 和 F1 整体保持打开。

### 14.9 `move_stop_800_resume_2_0` 第三次失败与 typed 诊断

第 14.7、14.8 节的前两次失败保持原样。endpoint 修复提交后，只从新世界、新目录再次运行本场：

`artifacts/f1-known-world-following/20261005T2205268396025Z-move-stop-800-resume-2-0-endpoint-fix/`

| 时间或结果 | 证据 |
|---|---|
| 来源 | `12a2f3a` |
| pause | 全程健康 |
| tick 851 | rev12 被接受，同帧进入 `FAILED/same_support_local_path_unavailable` |
| 请求身份 | request 14，generation 14 |
| 目标事实 | 终态时仍有 42 个 residual missing |
| 身体路线 | route、incumbent、action 和 route validation 全部为空 |
| 修订响应 | rev12 unanswered |
| 释放 | 失败后 source 安全释放 |

本批证明 endpoint 修复已经进入源码，但没有产生 local route。现有 `same_support_local_path_unavailable` 只能说明 local 接纳失败，不能区分失败发生在哪个阶段。可疑阶段包括 `BODY_SURFACE`、`SURFACE_IDENTITY`、`GOAL_SELECTION`、`EXACT_CONNECTION`、`MATERIAL_CAPABILITY` 和 `ROUTE_BUILD`。这些阶段对应不同事实和修复边界，所以当前证据不足以继续猜测某个算法分支。

为下一次单场运行新增只读 `LocalDirectAdmissionEvidence`：

- phase 使用 enum：`REQUEST`、`BODY_SURFACE`、`SURFACE_IDENTITY`、`GOAL_SELECTION`、`EXACT_CONNECTION`、`MATERIAL_CAPABILITY`、`ROUTE_BUILD`；
- 身份和几何字段包括 request id、goal revision、start／goal node、goal region 和 body position；
- 查询字段包括当前 phase 的 `QueryStatus`、实际 body surface node、selected position、selector status、exact status、missing 数量和 exact dependency 数量；
- 结果字段保存最终 `AdmissionStatus` 和 `AdmissionReason`。

证据为有界、不可变数据。`AdmissionResult` 可选携带它，Session 只保存最后一次 local 尝试；新尝试原子替换，新的非 local request 清理旧值。`NavigationDiagnostics` 只读暴露该证据。Fabric 场景每行通过 `asdict(diagnostics)` 持久化，同时写入当时 `KnownWorldFollowResult.submitted_target_position`。`tests/sim` 可以读取同一字段，但监视器和控制逻辑都不消费它。

这份诊断不参与权限判断、路线接纳、状态转换或失败分类。它只回答下一次失败停在哪个 typed phase、当时使用了哪个 surface／endpoint，以及 exact proof 到达了哪一步。

| 检查 | 结果 |
|---|---:|
| typed phase、字段、JSON 和生命周期聚焦检查 | 34/34 |
| `git diff --check` | 通过 |
| 独立复审 | P0／P1／P2 为零 |

当前没有新的产品修复结论，也没有再次运行 Fabric。typed 诊断形成提交后，只批准从新世界、新目录重跑 `move_stop_800_resume_2_0`，取得精确根因证据。`normal_cancel_2_0` 和 `first_input_late_one_tick_2_0` 继续冻结；F1-D 和 F1 整体保持打开。

### 14.10 typed 实机结果与 D065 量尺修正

typed 诊断后的实机目录为：

`artifacts/f1-known-world-following/20261005T2300158044696Z-move-stop-800-resume-2-0-typed-evidence/`

本批没有再次出现产品终态失败：

- 保存 986 个逐帧样本；整体距离统计另含起始点，共 987 项；16 个目标修订全部生效，拒绝为 0；
- revision response P95 为 3 tick；
- planning submission／accepted revision 为 3／16；
- task recovery、`STOPPING` 和安全违规均为 0；
- 最终距离为 0.963785 格，任务正常取消，输入源正常释放；
- tick 856—859 的 typed evidence 为 `ROUTE_BUILD / ACCEPTED / CANDIDATE_ADMITTED`，selector 和 exact query 均为 `FEASIBLE`，missing 为 0，精确依赖为 12 格。

该批的 `passed` 仍是 `false`。唯一失败的两个 gate 是 target speed 和 stable lag，因为稳定样本数为 0。原场景是“移动 33 tick、暂停 800 tick、再移动 33 tick”，稳定量尺则要求每段先经过 40 tick 预热；两段都不可能进入稳定窗。前三个失败批也同样没有稳定样本，只是此前先被产品失败截断。

D065 不改变 40 tick 预热、速度、距离或通过阈值，只把恢复移动延长到 66 tick。这样恢复段固定产生 27 个稳定样本。场景预检会阻止需要质量 gate 的场景再次出现零样本。原目录和失败结论保持不变，不能追认为通过；干净提交后只重跑修订后的本场。

### 14.11 D065 实机通过原门槛，但暴露持续待命状态缺口

D065 修订后的目录为：

`artifacts/f1-known-world-following/20261005T1525428380773Z-move-stop-800-resume-2-0-d065/`

原有门槛结果如下：

- stable sample 为 27，目标均速为 1.815892 格／秒；
- stable excess lag 的 mean／P95／max 为 0.109873／0.511040／0.538038 格；
- 20 个目标修订全部生效，拒绝和 unanswered 为 0，响应 P95 为 4 tick；
- planning submission／accepted revision 为 2／20；
- task recovery、非法状态转移和安全违规均为 0；
- 最终距离为 0.979653 格；显式取消后的 Session、driver、source、host、time、cleanup 和 11 组分段哈希全部符合要求。

`scenario-result.json` 因而写成 `passed=true`，但独立逐帧复核发现摘要缺少一条生命周期 gate。tick 919—924 仍有路线且 handoff 为 `RETAIN`，属于合法制动；tick 925 起 handoff 已为 `QUIESCENT`，路线、控制者、身体活动和等待全部为空，目标正式满足，但 Session 到 tick 1018 仍保持 `STOPPING`，共 94 帧。

这批不追认为场景通过。D066 增加 `RESUME_ACTIVE_IDLE_AFTER_HANDOFF`：只有当前帧 `QUIESCENT` 证据才能让满足的持续任务从 `STOPPING` 回到 `EXECUTING` 待命。普通 `_continue_execution()` 仍不能绕过交接。Fabric 摘要增加 `satisfied_idle_lifecycle` gate，正式拒绝同类记录；已经请求取消的帧按持久化 typed 标志排除，不能按场景名或 reason 豁免。修复前两个红测分别复现状态卡住和摘要漏判；修复后相邻集合 51/51。完整 motion_nav 1,466/1,471，仍是第 12.5 节冻结的同 5 项失败。独立复审没有未关闭的 P0／P1／P2。形成干净提交后，只重跑同一场。

### 14.12 D066 同场失败与 D067 修正

D066 后只运行了 `move_stop_800_resume_2_0`，目录为：

`artifacts/f1-known-world-following/20261005T1607326063433Z-move-stop-800-resume-2-0-d066/`

场景只保存 854 个统计样本，在 tick 852 接受 rev11 后进入 `FAILED/same_support_local_path_unavailable`。rev11 unanswered，稳定窗口未开始，最终距离 2.994395 格；任务没有购买 recovery，失败后 source、host、time 和 cleanup 正常。原摘要的 terminal、safety、revision response、stable lag、target speed 和 final hold 门槛均未通过，`passed=false` 保持不变。

失败帧同时记录：pending goal revision 为 11、缺失格为 42，typed local evidence 为 `GOAL_SELECTION / BLOCKED / CURRENT_BODY_CANNOT_CONNECT`。它表示旧 local admission 被错误调用，不能证明未知目标面已经阻塞。`update_goal()` 已经正确建立 pending goal 和信息等待，但旧 local activation id 尚未退场；同一帧的 `propose()` 越过 pending owner，用旧节点和新 GoalState 提前作出终态判断。

D067 不再尝试在失败出口重新绑定节点。local 和 ground-direct 的唯一共同激活位置现在都要求 `_pending_goal is None`。pending 期间继续请求事实，现有 incumbent 身体 owner 仍按原监督链运行。事实到达后，只有 `_resume_pending_goal()` 可以从当前观察重算 start／goal，再进入原接纳链。

新增检查用公共 `start_goal → update_goal → 同帧 propose` 时间线覆盖残留 local id 和 ground-direct id。修复前会调用旧 admitter；修复后两种 admitter 均为零调用，Session 保持 `NEEDS_INFORMATION`，正式 Observation 请求非空。只补该请求列出的事实后，下一帧 pending 清除，目标节点分别重算到 z11 和 z14。D062、D064、目标策略、生命周期和 F1 聚焦集合为 86/86。独立复审确认没有未关闭的 P0／P1／P2，也确认 guard 不阻断 incumbent 身体责任。

修复后的实机目录为：

`artifacts/f1-known-world-following/20261005T1647399858283Z-move-stop-800-resume-2-0-d067/`

来源提交为 `45eb73f`。原始证据显示：

- tick 850—851 仍为满足 rev10 的活动待命；tick 852 接受 rev11 后进入 `NEEDS_INFORMATION/goal_surface_requires_information`，pending revision 为 11，request 13 请求 42 个空气事实，路线、owner 和输入均为空；
- tick 853 事实到齐后 pending 清除，才接纳 `request-13-direct` 并恢复前进；rev11 响应为 2 tick，等待期间规划提交数没有增加；
- 20 个修订全部生效，拒绝和 unanswered 均为 0，响应 P95 为 4 tick，规划提交／修订为 4/20；
- stable sample 为 27，目标均速为 1.873496 格／秒；stable excess mean／P95／max 为 0.113513／0.491361／0.539885 格；
- task recovery、非法状态转移和安全违规均为 0，`satisfied_idle_stopping_ticks` 为空，最终距离为 0.950978 格；
- Session 和 driver 正常取消，source、host、time、cleanup 全部通过。

独立复审逐帧核对 1,019 行，并重算 11 个 segmented stream 的数量、字节和 SHA。合法制动帧始终仍有路线或身体责任，释放后 handoff 为 `QUIESCENT`；没有旧 local／direct 抢先启动、旧规划通知借用新 revision、身份错配或输入泄漏。`passed=true` 有原始证据支持，本场正式通过。下一场只批准 `normal_cancel_2_0`。

### 14.13 `normal_cancel_2_0` 实机结果

本场使用来源提交 `ecd8b4f`，从新世界、新目录运行：

`artifacts/f1-known-world-following/20261005T1701295216057Z-normal-cancel-2-0/`

逐帧结果如下：

- tick 43 仍由 rev9、request 10 的 direct route 正常前进；
- tick 44 接受取消并进入 `STOPPING/cancel_braking`。该帧唯一非中性输入是当前 route owner 发出的反向制动；
- tick 45—46 仍保留同一 route、controller 和身体活动，输入已为中性；
- tick 47 清空身体责任并进入 `CANCELLED/known_world_follow_cancelled`，source 同时解绑；
- `cancel_pending=4` 对应取消调用本身及 tick 44—46 的三次停止更新，随后一次 `cancelled`；从请求取消到终态为 3 个控制帧；
- task recovery、安全违规和非法状态转移均为 0，`cancel_tail_ticks=0`。

按冻结口径，本场的距离、速度、规划比和修订响应只记录，gate 为 `null`；取消、安全、活动待命生命周期、终态和 host gate 全部通过。独立复审重算连续 48 条场景记录、11 组分段记录的数量、字节、段 SHA 和 manifest SHA。两端 time report、输入 manifest、客户端和服务器 cleanup 均通过，没有丢记录、线程失败或残留进程。

`passed=true` 有原始证据支持，本场正式通过。下一场只批准 `first_input_late_one_tick_2_0`。

### 14.14 `first_input_late_one_tick_2_0` 首次运行无效与 D068 候选

首次运行目录为：

`artifacts/f1-known-world-following/20261005T1707251056118Z-first-input-late-one-tick-2-0/`

runner 保存的相对偏移是正常 1 tick、注入后 2 tick，因此原摘要写成 `passed=true`。逐帧读取正式输入账本后发现：requested tick 为 9，actual tick 为 10，latest allowed tick 仍为 9，状态应为 `APPLIED_OUTSIDE_WINDOW`。原结果不能关闭晚到门槛，也不能改写。

D068 候选把许可放回动作 owner。只有静止、着地、standing、两点直线、无 traversal 的普通 Walk，可以把一个一 tick 命令的启动窗口声明为 `[t+1, t+2]`。Session 只转发 typed 窗口；Runtime 在仲裁后登记；输入账本负责最终判定。

新的 gate 同时要求：ledger status 为 `APPLIED`、`valid_for_ticks=1`、requested first／last 相同、latest 恰好比 requested 晚 1 tick，并且 actual ticks 严格等于该 latest。扩大窗口或重复应用的反例已经进入检查。

相关检查 111/111 通过；独立复审另跑 39/39 聚焦检查和 24/24 相邻正式链检查，没有未关闭的 P0／P1／P2。

新实机目录为：

`artifacts/f1-known-world-following/20261005T1754012106271Z-first-input-late-one-tick-2-0-d068/`

来源提交为 `cf0aa13`。逐帧和账本证据确认：

- 首条请求 seq7 的 requested first／last 为 9，latest 为 10，actual ticks 为 `[10]`，status 为 `APPLIED`，`valid_for_ticks=1`；
- tick 9 仍是旧请求的 neutral／lease exhausted，tick 10 才应用 request 7；身体开始移动后不再取得启动窗口；
- 186 帧连续，稳定样本 27，目标均速 1.873496 格／秒；响应 P95 为 3 tick；规划提交／修订为 1/11；
- stable excess mean／P95／max 为 0.008421／0.030496／0.196880 格，末距离 1.766775 格；
- task recovery 和安全违规为 0；满足后 `EXECUTING/QUIESCENT` 中性待命 126 帧；终态取消和 source 释放正常；
- 两端 host、time、cleanup 通过；11 条分段流的记录数、字节数、segment SHA、manifest SHA 和完整流 SHA 经独立复算一致。

独立复审没有发现 P0／P1／P2。该场正式通过，F1-D 关闭。F1 整体仍需完成 F1-E。

## 15. F1-E 结构与产品验收

### 15.1 产品结论

五个代表 Fabric 场景全部通过，并分别保留原始目录和独立复审：

| 场景 | 响应 P95 | 规划／修订 | 恢复 | 安全违规 | 结果 |
|---|---:|---:|---:|---:|---|
| `straight_2_0` | 3 tick | 1/11 | 0 | 0 | 通过 |
| `lateral_2_0` | 2 tick | 4/13 | 0 | 0 | 通过 |
| `move_stop_800_resume_2_0` | 4 tick | 4/20 | 0 | 0 | 通过 |
| `normal_cancel_2_0` | 1 tick | 1/9 | 0 | 0 | 通过；能力距离 gate 不适用 |
| `first_input_late_one_tick_2_0` | 3 tick | 1/11 | 0 | 0 | 通过 |

原五 tick 修订响应门槛通过。五场均为代表性单次实机证据，不当作统计成功率。

### 15.2 薄调用方

`KnownWorldFollowDriver` 有 347 个物理行、5 个生命周期状态。它对导航的动作调用只有 `start`、`replace_goal` 和 `release`。实例字段没有规划、路线、接纳、恢复、身体交接、重试或输入账本状态。旧 `RuleFollower` 和 `PlaygroundFollower` 没有进入正式入口。

### 15.3 结构门槛

原“十二个核心文件零修改”门槛未通过：

- 从 F1 开始前 `931ea8b` 到 `f418649`，6 个核心文件增加 2,562 行、删除 163 行，净增 2,399 行；
- 从 D058 完成 `8e9c8a1` 到 `f418649`，仍有 6 个核心文件增加 1,376 行、删除 82 行，净增 1,294 行。

这些变化属于 D059—D068 的共享修复，十二个核心文件中没有跟随专用状态或分支。这说明薄跟随边界成立，但不能让“零修改”门槛变成通过。

### 15.4 完整检查与证据

完整运动导航检查为 **1,469/1,474**。失败仍是第 12.5 节冻结的同 5 项：changed heading seed 163、landing-support I3、公开整理版旧观察来源、R28 migration 旧恢复入口和 R28 shared recovery 旧期望。名称和断言类型没有变化，没有新增失败。

精简证据目录为 `evidence/motion_navigation/f1-known-world-following-v1/`，保存五份已签署摘要的原字节副本、原目录和哈希、产品汇总、结构审计和完整检查结果。逐帧证据继续留在原 artifact 目录。

### 15.5 判定

F1 功能交付通过，原结构门槛未通过。F1 整体保持“功能通过、结构未通过”，不写成全部完成。下一步按 [D069](../decisions/0069-accept-follow-product-and-retain-structure-failure.md) 和[独立结构整理计划](../stages/post-F1-navigation-structure-cleanup-plan.md)只做行为不变删除与归位。
