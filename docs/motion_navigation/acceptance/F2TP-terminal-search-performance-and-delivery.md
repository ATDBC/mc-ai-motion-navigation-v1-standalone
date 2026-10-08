# F2-TP 末段搜索性能与后台交付验收

日期：2026-10-08。状态：TP3 production 门槛未通过，已停止。

本验收先判断搜索能否及时停止和交付，再判断三个 oracle 能否完成。它不验收 F2-GP、路线优化器、大集合、D061 或 Fabric。

## 1. 当前 RED

| 项目 | 当前事实 | 本阶段判定 |
|---|---|---|
| 正式 worker 隔离 | control PID `10304`，worker PID `45828` | 已证明隔离，继续保留 |
| 第一个 oracle | `no_safe_ground_candidate`，20 tick／1 秒内没有 beam 结果 | RED |
| beam 实际耗时 | 审计约 `16 秒` | RED，目标 worst-case `<500 ms` |
| 旧 Inline oracle／remaining | `3/3`、`9/9`、`42/42` | 无效门槛证据 |
| 旧 `control_ms` | P95/P99/max `23.2694／42.7075／42.7075 ms` | 混合区间，不与 production 门槛比较 |
| 安全事件／伤害／来源泄漏 | `0／0／0` | 保持 |

原始记录位于 `evidence/motion_navigation/F2TS-bounded-ground-terminal-v1/isolated-worker-gate-failure/`。旧文件不改写。

## 2. 量尺验收

正式 runner 每帧必须直接输出：

- `production_prepare_ms`；
- `backend_step_ms`；
- `full_frame_ms`。

验收要求：

1. 三段都有独立开始／结束时间戳；
2. 不用相减推导任何一段；
3. 可控 backend 延迟只改变 backend 与 full；
4. 可控 production 延迟只改变 production 与 full；
5. 旧 `control_ms` 明确标注为 `mixed_interval`；
6. 报告包含样本数、P50/P95/P99/max、冷启动／稳态和测量代码指纹。

性能门槛：

| 区间 | 门槛 |
|---|---:|
| production P95 | `≤8 ms` |
| production P99 | `≤15 ms` |
| production max | `<30 ms` |
| full max | `<50 ms` |
| backend | 只报告，不套 production 门槛 |

## 3. deadline、取消和作废验收

至少覆盖：

- 入队前已过期；
- 排队中到期；
- phase 中取消；
- beam 前、中、后段取消；
- 完整 replay 中取消；
- goal／route／work generation 更新；
- 结果写出前作废；
- 乱序、重复和延迟 IPC。

每项断言：

- 到期为 typed `TIMEOUT`；
- 主动取消为 typed `CANCELLED`；
- 世代作废为 typed `STALE`；
- 旧候选不发布、不进入 inbox、不被当前任务接纳；
- worker 在下一个搜索检查点停止物理扩展；
- 新工作不会被已经作废的长搜索长期阻塞；
- 队列、取消通道、inbox 和历史容量不增长失控。

把任一终态改成 `NO_PROVED_SEQUENCE`、`NO_SAFE_GROUND_CANDIDATE` 或 `NO_KNOWN_ROUTE` 的错误副本必须失败。

## 4. readiness 与 prewarm 验收

Runtime 级常驻 worker 必须覆盖：

- 正常启动与 `READY`；
- prewarm 完成后才开放正式请求；
- 未 ready、ready 超时、worker 死亡和初始化异常；
- 多个连续任务复用同一 worker PID；
- 取消旧任务后执行新任务；
- Runtime 关闭后进程和输入来源都释放。

正式 oracle 不计入进程创建和首次规则加载。冷启动成本单独报告。控制线程不得回退到 Inline 搜索。

## 5. 增量 beam 正确性

测试必须证明：

1. 一个扩展只推进 normal／late1 各一个玩家 tick；
2. 任一分支出现碰撞、支撑不足、危险接触、UNKNOWN、姿态或资源失败，节点立即淘汰；
3. normal／late1 联合状态稳定去重；
4. 重复运行、旋转和镜像得到对应结果；
5. beam width 始终 `≤512`；
6. 前沿排序不等于安全证明；
7. 只有进入 completion 邻域的有界候选执行完整尾迹与 canonical proof；
8. 最终候选仍对 normal／late1 的全部执行前缀保存安全尾迹和依赖；
9. deadline、取消和世代在每个扩展与 replay 前后生效。

错误副本至少包括：只保存 normal、一次推进多 tick、先评分后硬安全、去重忽略速度／姿态／相位、跳过最终 replay、让所有前沿节点跑 30 tick尾迹、依赖墙钟先后选胜者。每项都必须被发现。

## 6. 搜索性能和 profile

先在 Windows 正式常驻 worker 上跑性能，不跑 oracle。

硬门槛：

- phase P95 `<50 ms`；
- beam worst-case `<500 ms`；
- control-thread search calls `0`；
- deadline miss `0`；
- 本文第 2 节的 production／full 门槛全部通过。

每个请求保存：

- submit、queue、worker start、phase start/end；
- 每个 beam layer 的 start/end；
- replay／canonical proof start/end；
- result write、IPC receive、admit、cancel／stale 时间；
- 各层生成、硬安全淘汰、去重、保留节点数；
- 总物理 step、replay step、完整 proof 数；
- control／worker PID、ready 和 prewarm 时间。

profile 必须能回答时间主要花在扩展、去重、物理 step、完整尾迹、canonical proof、排队还是 IPC。只有总耗时没有分项时，本节不通过。

## 7. 有界前移和执行窗口

第 6 节通过后，冻结前移公式并测试：

- 请求最早提交 tick；
- 最坏搜索时间换算的 tick；
- queue／IPC P99；
- 一 tick交付余量；
- 准备前缀长度；
- normal／late1 execution window。

至少注入：准备输入正常、首条晚一 tick、准备期丢一条输入、目标／路线／世代变化、相关世界变化、worker 晚于窗口、取消和外力偏离。

结果只有在准备输入、anchor、goal、route、generation、依赖和窗口全部匹配时才能安装。失败必须有界，并由 FixedRoute 继续承担身体责任。

## 8. 正式 oracle

前面全部通过后，依次运行：

1. `f2r/clutter/0.1/7/22/product`；
2. `f2r/clutter/0.2/1/18/product`；
3. `f2r/clutter/0.2/8/22/product`；
4. remaining9；
5. remaining42。

门槛：

- 三项 oracle `3/3` 正式物理完成；
- remaining9 `≥6/9`；
- remaining42 `42/42`；
- 旧成功退步、安全事件、伤害超额、无人负责输入、来源泄漏和终态持有输入均为 `0`；
- 每项结果都来自真实常驻 worker，不允许 Inline 替代；
- 每项都保存 deadline、节点／step／replay／IPC 和三段时间。

## 9. fail-fast

顺序为：量尺 → deadline／取消／作废 → readiness → 增量 beam → 性能/profile → 有界前移 → oracle。

前一层失败立即停止。remaining9／42 不能在 oracle 失败时继续运行。本阶段不接 F2-GP，不运行大集合、D061 或 Fabric。

## 10. 关闭条件

第 2—8 节全部通过后，F2-TP 才能关闭。关闭不会自动把 F2-TS 标为通过；它只允许 F2-TS 从正式 oracle 之后继续原验收顺序。

## 11. 2026-10-08 Windows 实施结果

正式来源提交为 `a61a76e6a245cd2f4b808ba3eed581a6dc4722e6`，运行前工作树干净。

| 检查 | 结果 | 判定 |
|---|---:|---|
| 聚焦检查 | `183/183` | 通过 |
| worker readiness | `READY`，预热 `338.2108 ms` | 通过，正式请求在 READY 后开始 |
| phase | P95 `3.8161 ms`，max `4.1583 ms` | 通过 `<50 ms` |
| beam | P95 `125.8381 ms`，max `125.9636 ms` | 通过 worst-case `<500 ms` |
| 进程隔离 | control `5456`，worker `17532` | 通过 |
| 排队前取消／作废／超时 | typed 结果一致，beam 节点 `0` | 通过 |
| production | P95/P99/max `10.2973/22.9633/24.9259 ms` | **失败**，P95 和 P99 超限 |
| backend | P95/P99/max `21.8194/30.2270/32.7892 ms` | 只报告 |
| full | P95/P99/max `32.2344/39.6086/44.8677 ms` | 通过 max `<50 ms` |

生产控制样本来自五个不属于 oracle 的固定 clutter 正式链场景，共 `142` 帧，五项任务全部完成，安全事件为 `0`。三段时间均直接计时。旧混合 `control_ms` 没有拿来判 production。

本轮没有运行第 7、8 节。三个 oracle、remaining9 和 remaining42 没有新结果；旧 Inline 结果继续无效。完整紧凑证据和命令位于 `evidence/motion_navigation/F2TP-terminal-search-performance-v1/tp3-gate-failure/`。

独立复核还确认：正式 worker 的生命周期仍部分留在 Session，取消背压和结果发布前的最后作废检查没有形成完整门槛；production 也缺少足以指导优化的分段。后续验收移到 [F2-RH](F2RH-runtime-worker-and-control-hotpath.md)。F2-RH 不改写本页 TP3 失败，且通过前不得运行第 7、8 节。

## 12. D089 风险分级

本页 TP3 失败、未运行项目和原始数字保持不变。F2-RH 后续已经补齐 Runtime worker 所有权、取消、发布和 air／ground cooperative stop，并确认零安全事件和零 deadline miss。

按 [D089](../decisions/0089-grade-fail-fast-by-risk-and-bound-task-expansion.md)，production P95/P99 未达 `8/15 ms` 作为性能债保留，不再自动阻塞无关产品工作。beam 上界、真实期限、取消、旧世代作废、控制线程隔离和安全规则仍是硬门槛。TP4 与 oracle 需要用户明确授权，并要说明如何处理原复合门槛和同一热路径的性能债；不能把风险重新分类解释成已经通过本页未运行的验收。
