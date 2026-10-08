# F2-TS 普通地面有界末段序列验收

日期：2026-10-08。状态：正式隔离 worker 交付门槛失败，已停止；原控制区间量尺已由 D087 纠正。

本验收判断四件事：求解器是否真正解决多 tick末段，结果是否在正式异步链中保持身份和安全，成本是否进入 terminal edge，以及新能力是否在 Windows 控制期限内运行。

## 1. 冻结基线

| 集合 | 当前结果 | F2-TS 门槛 |
|---|---:|---:|
| 三项 F2-SS oracle | `0/3` 完成，均有界失败 | `3/3` 正式物理完成 |
| remaining9 | `6/9` | 不低于 `6/9` |
| remaining42 | `42/42` | 保持 `42/42` |
| v8 | `1747/1800`，三项旧成功退步 | 零旧成功退步 |
| F2-SS 短控制性能 | P95/P99/max `5.6667/5.9748/16.5341 ms` | 不退步 |
| v7／大集合／D061／Fabric | F2-SS 停止后未运行 | 前层通过后运行 |

三个 oracle 的输入与失败记录来自 F2-SS，不改写原证据。

## 1.1 当前正式结果

| 门槛 | 结果 | 判定 |
|---|---:|---|
| 正式链聚焦检查 | `175/175` | 通过 |
| phase 稳态 P95 | `3.0441 ms` | 通过，门槛 `<50 ms` |
| beam 稳态 P95 | `371.8800 ms` | 通过，门槛 `<500 ms` |
| 真实 worker 线程／进程隔离 | 控制 PID `10304`，搜索 PID `45828` | 通过 |
| 第一个 F2-SS oracle | `0/1`，`no_safe_ground_candidate` | 失败 |
| 原混合控制区间 P95／P99／max | `23.2694／42.7075／42.7075 ms` | 量尺混合，不用于 production 判定 |
| remaining9／remaining42 | 未运行 | 前层失败后停止 |
| 安全事件／伤害／来源泄漏 | `0／0／0` | 通过 |

旧的 `3/3`、`9/9` 和 `42/42` 记录来自干净 Windows 提交 `34ee7369`，但使用测试用 Inline worker。它在控制线程的 `poll_available()` 内直接执行搜索，而且搜索期间模拟运动 tick不推进。因此这些原始文件只保留历史，不再作为正式 worker、交付期限或控制性能证据。

beam 不能从即时 phase 结果直接执行。它必须先提交新 revision，并由 FixedRoute 实际发送 10 tick中性准备；序列安装时再用 `InputApplicationLedger` 核对这 10 条输入。提前量不足或准备记录缺失会拒绝安装。

正式停止证据来自干净 Windows 提交 `a8cb3bfb`，位于 `evidence/motion_navigation/F2TS-bounded-ground-terminal-v1/isolated-worker-gate-failure/`。第一个 oracle 已失败，所以第 8 节剩余集合与第 9—11 节均未运行。

[D087](../decisions/0087-correct-terminal-search-timing-and-bound-delivery.md) 后续确认这里的 `control_ms` 同时包含生产控制、模拟后端和完整夹具。原数字保持不变，但不能直接与 production `8／15／30 ms` 比较。F2-TS 的失败结论仍成立，因为正式 beam 没有在 20 tick／1 秒内交付；性能和取消整改转入独立 [F2-TP](F2TP-terminal-search-performance-and-delivery.md)。

## 2. 独立 holdout

holdout 必须在查看三个 oracle 的可行命令串前冻结，并保存机器可读清单与生成种子。至少覆盖：

- 单轴和双轴位置误差；
- 三种速度方向；
- 四种方位和多种 yaw；
- normal、late1 和窗口外启动；
- 前缀 0 到 N 后逐点松键；
- 窄 completion、墙边和支撑边缘；
- 相关／无关世界变化；
- 目标、路线和工作世代变化；
- phase 可解、只能 beam 解、无解、缺信息和预算耗尽。

每项报告：typed 结果、成本、求解层、轨迹、每前缀尾迹、依赖、最终 completion 和稳定哈希。安全违规、UNKNOWN 当安全、身份错配和结果不稳定必须为 `0`。

## 3. phase search 正确性

phase search 只允许 D086 的 target、brake、组合和 neutral。测试必须证明：

1. 阶段数按 `1 → 2 → 3`；
2. 阶段持续 tick按递增顺序枚举，总长度不超过已冻结的 `maximum_ticks`，且范围为 `1..64`；
3. 相同输入得到相同候选顺序和结果；
4. 坐标旋转／镜像后的结果满足对应变换；
5. 不读取场景 ID、种子、方位名或绝对坐标；
6. normal 与 late1 都满足硬安全和 completion；
7. 每个前缀的 neutral tail 都安全并有完整依赖；
8. 部分证明不能发布 `SOLVED`。

变异至少包括：删除每个保留模板、交换固定顺序、漏 late1、漏中间前缀、缩短 neutral tail、遗漏依赖、按墙钟选第一个结果。每个变异都必须失败或被独立性能／成本门槛发现。

## 4. beam fallback 正确性

beam 必须满足：

- phase 已有合格结果时不运行；
- 每层保留数不超过 `512`；
- 输入集合与普通地面合法输入一致；
- 前沿评分不能越过硬安全、缺信息和完整证明；
- 胜者重新执行完整 normal／late1 和逐前缀尾迹复核；
- 节点、深度或墙钟预算耗尽返回 `BUDGET_EXHAUSTED`；
- 不公开部分前沿，不把预算耗尽写成无路；
- 距执行少于 `11 tick` 时不能接纳 beam 结果。

宽度 `513`、跳过最终完整复核、提前量 `10 tick`、返回最优部分结果和把 timeout 改成无路的错误副本必须失败。

## 5. `GroundTerminalSequence` 完整性

每个可执行结果逐字段核对：

- anchor 与 movement tick；
- 完整 work identity；
- goal ID/revision；
- route ID/revision 与动作索引；
- execution window；
- 从 anchor 到窗口的准备前缀及 normal／late1预计入口；
- normal／late1 命令、轨迹和实际成本；
- 每个前缀的 neutral tail；
- completion、姿态、模式、支撑和终速；
- 全部世界依赖与规则版本；
- phase／beam 来源和稳定 sequence ID。

删掉任一字段，或让 sequence ID 不包含会改变物理／资格的字段，构造与接纳检查必须失败。

## 6. 正式异步链

必须走现有 motion worker、队列、inbox 和工作生命周期。断言：

- 控制线程只提交、轮询和接纳，不调用搜索；
- worker 异常转为 typed `INTERNAL_ERROR`，进程不静默死亡；
- 旧 anchor、旧 work identity、旧 goal、旧 route 和过期窗口结果全部退休；
- 同一用途的新修订替代旧修订，不覆盖其他动作工作；
- 结果未返回时 FixedRoute 仍是 body owner；
- 未来入口没有正式准备前缀时不得提交求解，准备输入与预测不符时旧结果失效；
- 没有新增 Session waiting 状态或无 owner 输入；
- 队列满、worker 死亡和 inbox 饱和均有界返回。

至少注入：返回前改目标、改路线、改变依赖、晚一 tick交付、窗口后交付、乱序结果和重复结果。

## 7. 逐 tick执行与偏离

执行时每帧核对观察与已证明分支。至少覆盖：

- normal 启动；
- late1 启动；
- 窗口外启动；
- 每个命令位置丢一次输入；
- 每个命令位置改一次目标／路线；
- 依赖在执行前和执行中变化；
- 外力使身体离开预测容差；
- 取消发生在第一条命令前、中间和最后一条命令后。

偏离后只能执行对应前缀的安全尾迹或现有更保守停止规则。不能跳到另一个分支、继续后续命令或直接释放身体。安全落地／停稳后才能退休 owner。

## 8. 三项 oracle 与分层回归

独立门槛通过后，依次运行：

1. `f2r/clutter/0.1/7/22/product`；
2. `f2r/clutter/0.2/1/18/product`；
3. `f2r/clutter/0.2/8/22/product`；
4. remaining9；
5. remaining42。

三个 oracle 必须在正式 1.21 物理下停进原 completion，并满足原终速、姿态、支撑和模式。不能只比较旧终态标签。

三项 `3/3`，remaining9 `≥6/9`，remaining42 `42/42`。旧成功退步、安全事件、伤害超额、来源泄漏、无人负责输入和终态仍持有输入全部为 `0`。

## 9. F2-GP terminal edge 与成本

行为层通过后，测试同一份 `GroundTerminalSequence` 是否贯穿：

1. terminal edge；
2. A* 的 `g` 成本；
3. terminal witness；
4. RouteAdmitter；
5. ActionRoute；
6. 世界变化复核；
7. 正式执行和完成报告。

需要移动的序列成本为实际 tick 且大于零。已经满足 completion 才能返回零 tick。删除末段成本应改变最优路线并被固定场景发现。

宽泛入口状态不得生成可执行序列。预算耗尽的末段记为未决，不能直接删除并报告 `NO_KNOWN_ROUTE`。

## 10. Windows 性能与容量

Windows 是正式平台。分别报告 phase-only、beam fallback、worker 交付和控制生产路径。

硬门槛：

- phase search P95 `<50 ms`；
- beam fallback P95 `<500 ms`；
- beam 最大宽度 `≤512`；
- beam 进入执行前的提前量 `≥11 tick`；
- 控制线程 phase／beam 调用次数 `0`；
- 控制生产 P95 `≤8 ms`；
- 控制生产 P99 `≤15 ms`；
- 控制生产 max `<30 ms`；
- D061 完整夹具 max `<50 ms`；
- deadline miss `0`。

报告必须区分排队、计算、交付、接纳与控制时间，不用相减推算生产耗时。phase 和 beam 都要在冷启动与稳态下报告。

## 11. 大集合与 Fabric

前面通过后，依次运行 v9、v8、v7、F2／F2-R、五组行为、协调集合、完整正序／逆序、D061 和 Fabric。

Fabric 走 profile 4、Runtime、NavigationSession、现有 worker、RouteAdmitter、ActionRouteExecutor、FixedRoute 和真实输入账本。至少包含四方位、双轴、normal、实际 late1、中途偏离、目标／路线／世界变化、beam 提前量边界和一个严格动作负对照。

严格动作的命令、轨迹、窗口、落地责任和伤害额度必须与冻结基线逐项一致。

## 12. fail-fast 与关闭

验收顺序为 holdout → 变异 → 性能 → 三项 oracle → remaining9 → remaining42 → F2-GP → 大集合 → D061 → Fabric。任一层失败立即停止。

F2-TS 只有第 2—11 节全部通过才能关闭。通过只表示普通地面窄 completion 有了通用、有界、可中断的末段序列；不会自动关闭 F2-SS、F2-GP、F2-SG、F2-S 或路线优化器。
