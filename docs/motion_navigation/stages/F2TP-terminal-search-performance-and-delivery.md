# F2-TP：末段搜索性能与后台交付方案

日期：2026-10-08。状态：已实施到 TP3；production 门槛失败，已按 fail-fast 停止。

**目标：** 让普通地面末段搜索能在正式常驻 worker 中按期限计算、取消和交付，并用正确的三段量尺证明控制线程没有被搜索或模拟后端拖慢。

**依据：** [D087](../decisions/0087-correct-terminal-search-timing-and-bound-delivery.md)、[D086](../decisions/0086-solve-bounded-ground-terminal-sequences.md)、[F2-TS 阶段](F2TS-bounded-ground-terminal-sequences.md)和 [F2-TS 验收](../acceptance/F2TS-bounded-ground-terminal-sequences.md)。

## 1. 当前事实

- F2-TS 的 typed 工作、FixedRoute 身体责任、当前状态恢复、anchor phase／input projection 身份和有界恢复期限已经保留；聚焦检查为 `175/175`。
- 旧 Inline worker 的 oracle `3/3`、remaining9 `9/9` 和 remaining42 `42/42` 不再是正式门槛证据。
- 干净 Windows `a8cb3bfb` 的第一个正式 oracle 没有在 `20 tick／1 秒` 内收到 beam 结果，任务有界失败。
- 独立审计显示正式 beam 在该入口约需 `16 秒`。现有搜索不适合通过单纯延长期限进入在线链。
- 归档的 `control_ms` 混合了生产控制、模拟后端和完整夹具时间。它不能直接用于生产 `8／15／30 ms` 门槛。

因此本阶段先处理计算与交付。F2-TS 继续记为未通过。

## 2. 范围

本阶段只修改或新增：

- 正式帧的 `production／backend／full` 时间记录；
- 地面末段请求的真实 deadline、取消和世代作废检查；
- Motion worker readiness、常驻和预热；
- beam 的逐 tick normal／late1 状态、硬安全早筛和稳定去重；
- completion 邻域候选的完整尾迹／canonical proof；
- 求解通过后的有界提前触发与执行窗口；
- 对应脚本、测试、证据和四类文档。

不修改 Session 状态、F8、任务恢复预算、completion 几何、普通地面十八项候选、严格动作或 F2-GP。

## 3. TP0：先修量尺

### 3.1 冻结旧记录

保留 `a8cb3bfb` 的原始 `control_ms`、PID、失败原因和轨迹。新增元数据明确标记它是 `mixed_interval`，不修改原 JSON 数字。

### 3.2 三段直接计时

正式 runner 对每帧直接记录：

- `production_prepare_ms`；
- `backend_step_ms`；
- `full_frame_ms`。

三者各自有开始和结束时间戳。测试用可控时钟证明：只增加 backend 延迟时，backend 和 full 增加，production 不变；只增加生产逻辑时，production 和 full 增加，backend 不变。

性能报告同时给 P50／P95／P99／max、样本数、冷启动／稳态标签和测量代码指纹。

### 3.3 TP0 完成条件

- 错误把混合时间作为 production 的测试会失败；
- 不允许用相减生成 production；
- 原 `control_ms` 被清楚标成历史混合量尺；
- 没有改变机器人行为。

## 4. TP1：真实 deadline、取消和世代作废

### 4.1 请求契约

每个地面末段请求增加：

- 同一台 Windows 主机上的单调时钟绝对 deadline；
- cancel token／work generation；
- 请求建立、入队和允许执行的时间戳。

deadline 从提交前开始计算，覆盖排队、计算和 IPC 交付，不能在 worker 取件时重新计时。

### 4.2 搜索检查点

worker 在以下位置检查 deadline、取消和世代：

- 取到请求后；
- 每个 phase 候选前；
- 每个 beam 节点扩展前；
- 完整尾迹／canonical proof 前后；
- 发布结果前。

返回值分别为 typed `TIMEOUT`、`CANCELLED` 和 `STALE`。取消之后的部分候选不能发布，旧结果也不能进入 inbox。

### 4.3 常驻 worker

Runtime 启动一个常驻 Motion worker。worker 初始化规则和计算器，执行一次预热，并通过 readiness handshake 报告 `READY`。正式 oracle 只在 READY 后开始。

worker 死亡、预热失败、ready 超时和运行时异常都有 typed 结果。控制线程不回退到 Inline 搜索。

### 4.4 TP1 完成条件

- 排队中、phase 中、beam 中和 replay 中取消都能有界结束；
- 新世代到来后旧任务停止扩展，旧结果不发布；
- deadline 到期返回 `TIMEOUT`，不返回无路或无安全候选；
- 连续提交新 revision 不会让废任务占满 worker；
- 控制线程搜索调用数为 `0`。

## 5. TP2：增量 beam 与早筛

### 5.1 节点内容

一个 beam 节点保存：

- normal 与 late1 的当前 `PhysicsState`；
- 当前 tick深度和上一条控制；
- 两个分支已读取的世界依赖；
- 稳定父节点／命令身份；
- 只用于搜索排序的距离、速度、停车余量和控制变化数。

一次扩展只给两个分支各应用一个 tick输入。任何分支缺失、未知、不安全或超过世界快照范围，整个节点立即淘汰。

### 5.2 去重

同一深度使用 normal／late1 联合状态键去重。键包含会影响后续物理和资格的量化位置、速度、姿态、支撑、yaw／输入投影相位、规则和相关资源。若键相同，按固定成本与命令字典序保留唯一节点。

删除去重、只按 normal 去重、忽略速度／姿态／相位和依赖墙钟完成顺序的错误副本都必须被测试发现。

### 5.3 延后完整证明

前沿节点只执行逐 tick硬安全检查。进入 completion 邻域后，最多对冻结数量的候选运行：

1. normal／late1 完整轨迹重放；
2. 每个已执行前缀的完整中性停止尾迹；
3. canonical proof 和依赖汇总。

候选数量按门槛前冻结，不能在失败后扩大。只有完整证明通过的候选可以发布 `SOLVED`。

### 5.4 TP2 完成条件

- phase 结果与冻结实现逐项一致；
- beam width `≤512`；
- 每个扩展只推进一个玩家 tick；
- normal／late1 任一不安全即淘汰；
- 稳定去重对旋转、镜像和重复运行一致；
- 只有 completion 邻域的有界候选运行完整 replay；
- 最终结果继续满足 D086 的全部安全与身份条件。

## 6. TP3：性能和取消门槛

先运行搜索与 worker 门槛，不运行 oracle。

### 6.1 报告字段

每个请求报告：

- phase 候选数、beam 各层生成／淘汰／去重／保留数；
- 总物理 step、replay step 和 canonical proof 数；
- submit、queue、start、phase、beam layer、replay、result、IPC、admit、cancel 时间戳；
- 冷启动、prewarm 和稳态；
- worker／control PID；
- typed 终态和停止原因。

### 6.2 硬门槛

- phase P95 `<50 ms`；
- 正式 beam 的 worst-case `<500 ms`；
- 已取消／过期工作在下一个搜索检查点停止，不继续跑完整 beam；
- 控制线程搜索调用数 `0`；
- production P95 `≤8 ms`、P99 `≤15 ms`、max `<30 ms`；
- full max `<50 ms`；
- deadline miss `0`；
- 队列、取消通道、inbox 和历史均保持有界。

backend 只报告，不与 `8／15／30 ms` 比较。

任一硬门槛失败就停止，不运行 TP4 或 oracle。

## 7. TP4：有界前移触发

TP3 通过后，根据正式 worker worst-case 计算请求应提前多少 tick。冻结公式至少包含：

- 最坏搜索时间；
- worker 排队／IPC P99；
- 一 tick提交余量；
- 已证明准备前缀可覆盖的最大 tick。

前移后仍由 FixedRoute 持有身体并执行正式准备前缀。每 tick核对实际应用，结果只在对应 normal／late1 入口和窗口内接纳。计算超过准备前缀或窗口上限时返回 typed 未决／超时，不延长身体等待。

TP4 先做纯正式链窗口测试。删除准备前缀、错一个 tick、改目标／路线／世代、取消或更改依赖都必须让旧结果失效。

## 8. TP5：最后才跑 oracle

TP0—TP4 全部通过后，依次运行：

1. 三项 F2-SS oracle；
2. remaining9；
3. remaining42。

门槛沿用 D086：oracle `3/3`，remaining9 不低于 `6/9`，remaining42 `42/42`，零旧成功退步、零安全事件、零伤害超额、零来源泄漏。

本阶段到此为止。即使这些门槛通过，也只表示 F2-TS 可以继续后续工作；不在 F2-TP 中接入 F2-GP、大集合、D061 或 Fabric。

## 9. fail-fast 与停止条件

出现下面任一情况立即停止：

- 仍用混合时间判断 production；
- deadline 从 worker 取件时才开始计算；
- 取消或旧世代只能等整个搜索结束；
- 为取消另建一套与现有 work identity 并行的生命周期；
- beam 继续对大部分前沿节点运行 30 tick尾迹；
- 去重丢失 normal／late1、速度、姿态或输入相位；
- 部分证明被发布为可执行序列；
- beam worst-case `≥500 ms`；
- 通过放宽工作期限、completion 或安全门槛制造通过；
- worker 未 READY 就运行正式 oracle；
- 生产控制线程执行任何搜索或完整 replay；
- 在本阶段接 F2-GP。

## 10. 完成条件

F2-TP 只有 TP0—TP5 全部通过才能关闭。关闭时必须同时保留：

- 三段时间的原始样本和汇总；
- 完整 worker profile 与节点统计；
- deadline／cancel／stale 的正式链证据；
- readiness／prewarm 证据；
- 三项 oracle、remaining9／42 的机器可读结果；
- 源码、测试和环境指纹。

F2-TP 未通过期间，F2-TS 保持未通过，F2-GP 不得接入。

## 11. 本轮实施结果

TP0—TP2 的实现已经保留：

- 正式帧直接记录 `production_prepare_ms`、`backend_step_ms` 和 `full_frame_ms`，没有用相减推算；旧 `control_ms` 明确标为 `mixed_interval`；
- 地面末段请求在提交前写入真实单调时钟 deadline；worker 能返回 typed `TIMEOUT`、`CANCELLED` 和 `STALE`；
- Motion worker 启动后先预热输入投影和 1.21 计算器，再报告 `READY`；
- beam 节点同时保存 normal／late1 状态，每次只推进一个 tick；不安全节点立即淘汰，完整停止尾迹和 canonical proof 只在 completion 邻域运行；
- 现有 500 ms 上界换算为 11 tick lead，也就是 10 tick准备前缀加一 tick交付余量。没有延长旧 anchor 或工作期限。

干净 Windows `a61a76e6` 的 worker 门槛通过：phase P95 为 `3.8161 ms`、最大 `4.1583 ms`；beam P95 为 `125.8381 ms`、最坏 `125.9636 ms`。控制进程 PID `5456`，worker PID `17532`。排队前的取消、作废和超时都在生成 beam 节点前返回对应 typed 结果；运行中取消也有组件检查。

TP3 的生产控制门槛失败。五个非 oracle 正式链场景共记录 `142` 个控制帧：production P95／P99／max 为 `10.2973／22.9633／24.9259 ms`。前两项超过 `8／15 ms`。full max 为 `44.8677 ms`，仍低于 `50 ms`；backend 继续单列。

项目在这里停止。没有运行 TP4、三个 oracle、remaining9、remaining42、F2-GP、大集合、D061 或 Fabric。失败证据位于 `evidence/motion_navigation/F2TP-terminal-search-performance-v1/tp3-gate-failure/`。下一轮应先定位生产控制时间，不能用已经通过的 beam 性能掩盖控制线程仍超限。

独立复核把下一步拆成 [F2-RH](F2RH-runtime-worker-and-control-hotpath.md)：先让 Runtime 真正持有并预热共享 worker，补齐 health、取消背压、发布前作废和 FIFO 阻塞门槛；随后只读拆分 production，并按 profile 只允许一轮验证器语义不变优化。F2-RH 通过后才返回本方案 TP4。F2-TP 当前状态不变。

## 12. D089 后续安排

TP3 按原复合门槛失败的历史事实不变。F2-RH 后续已经关闭 worker 生命周期硬风险，并确认没有安全事件或 deadline miss。[D089](../decisions/0089-grade-fail-fast-by-risk-and-bound-task-expansion.md) 把 production P95/P99 未达归为性能债。

F2-TP 不会因风险重新分类而自动进入 TP4。只有用户明确授权的新任务可以重新开启 TP4／oracle；该任务要说明产品价值、对同一控制热路径的新增负载、最多两轮修正，以及用什么真实期限证据替代或继续执行原 `8/15 ms` 复合门槛。无关产品工作不再被单独的余量目标阻塞。
