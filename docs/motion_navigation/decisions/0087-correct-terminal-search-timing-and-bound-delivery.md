# D087：纠正末段搜索量尺并先收敛后台交付

日期：2026-10-08。状态：已决定并实施到 TP3；production 门槛失败，F2-TP 与 F2-TS 保持未通过。

## 要解决的问题

F2-TS 第一次使用真实 `PlannerWorker／MotionSolverWorker` 时，末段 beam 没有在现行 `20 tick／1 秒` 工作期限内返回。任务以 `no_safe_ground_candidate` 有界结束，搜索进程和控制进程已经隔离，零安全事件、零伤害且输入来源正常释放。

这份失败证据说明真实后台交付不可用，但当时对性能数据的解释有偏差。

归档中的 `control_ms` 同时包含生产控制、模拟后端推进和完整夹具开销。把它的 `23.2694／42.7075／42.7075 ms` 直接与生产控制 `8／15／30 ms` 门槛比较，会把不同范围的时间混在一起。现有记录因此不能证明生产控制超限，也不能反过来证明生产控制通过。

真正已经确认的阻塞有两个：

1. 正式 beam 在实际 oracle 上约需 `16 秒`，远超现行工作期限，也远超可用于在线执行的量级；
2. worker 没有在搜索内部持续处理真实 deadline、取消和世代作废。过期任务会继续占用搜索进程，新的任务只能等待旧计算结束。

旧 `InlineMotionWorker` 的 `3/3`、remaining9 `9/9` 和 remaining42 `42/42` 继续判为无效门槛证据。它在控制线程内同步搜索，而且搜索期间模拟 tick 不推进，绕过了本决定要解决的交付问题。

## 决定

新增独立阶段 F2-TP。先修性能量尺、取消和后台交付，再重跑 oracle。F2-TP 不接 F2-GP，也不改变普通地面、严格动作或 completion 的语义。

### 1. 恢复三段时间量尺

正式记录必须把一次控制帧拆成三段：

- `production`：Runtime、导航和控制器在控制线程上生成本帧结果的时间；
- `backend`：模拟后端、物理环境或测试夹具推进所花的时间；
- `full`：从正式帧输入开始到整帧处理结束的端到端时间。

`production`、`backend` 和 `full` 都直接打时间戳。不能用两段时间相减推算第三段。

原 `8／15／30 ms` 只约束 `production`：

- P95 `≤8 ms`；
- P99 `≤15 ms`；
- max `<30 ms`。

`full` 的门槛是 max `<50 ms`。`backend` 单独报告，不套用生产控制门槛。现有混合 `control_ms` 保留为历史原始证据，不改名冒充新的三段量尺。

### 2. 先让过期工作真正停下来

地面末段请求必须携带真实单调时钟 deadline。worker 在开始工作前、每次 phase／beam 扩展前、完整重放前和发布结果前检查：

- deadline 是否已经到期；
- 请求是否被取消；
- work generation 是否已经作废。

到期返回 typed `TIMEOUT`。取消返回 typed `CANCELLED`。世代作废返回 typed `STALE`。三者都不能改写成“没有安全候选”或“没有路线”。

取消和世代水位使用现有 worker 生命周期的一条有界控制通道，不新建第二套工作状态机。旧工作一旦失效，必须在下一次搜索检查点停止扩展，也不能把结果发布到当前 inbox。

### 3. beam 改为逐 tick 增量扩展

beam 的一个搜索节点同时保存 normal 和 late1 两个预测状态。一次扩展只让两个分支各推进一个玩家 tick。

每次扩展先检查：

- 碰撞、支撑、危险接触、UNKNOWN 和姿态等硬安全；
- normal 与 late1 是否仍完整；
- deadline、取消和世代；
- 稳定状态键是否已经出现过。

不安全或重复状态立即丢弃。去重键必须同时包含 normal／late1 的位置、速度、姿态、支撑、输入相位和会改变后续结果的规则身份。遍历和同分顺序固定，墙钟先后不能改变已完成搜索的答案。

当前实现为大量前沿节点反复运行最长约 `30 tick` 的中性尾迹和 canonical proof。这是实际 oracle 上约 `16 秒` 的主要成本。F2-TP 只让已经进入 completion 邻域的少量候选运行完整尾迹和 canonical proof。beam 前沿评分只负责排序，不能当作安全证明。最终发布的候选仍须通过 D086 的 normal／late1、逐前缀安全尾迹和完整依赖检查。

### 4. Runtime 持有常驻、已就绪的 worker

Motion worker 随 Runtime 启动，完成进程创建、规则加载、计算器初始化和一次不发布结果的预热，然后报告 typed `READY`。

正式任务不会为每个末段请求重建进程。Runtime 在 worker 未就绪、已死亡或预热失败时返回明确状态，不能在控制线程临时执行搜索。冷启动与稳态分别计时，正式 oracle 只在 readiness 成立后开始。

### 5. 求解最坏时间通过后，再把触发点前移

只有 Windows 正式 worker 的 beam 最坏时间 `<500 ms` 后，才能调整末段请求的触发时机。

触发点前移必须保留：

- FixedRoute 已证明的有界准备前缀；
- 明确的 normal／late1 入口；
- 有界 `CandidateExecutionWindow`；
- 当前身体责任；
- 结果到达时对准备输入、锚点、目标、路线、世代和依赖的复核。

不能直接把现行期限从 1 秒放宽到 16 秒，也不能让机器人停在原地等待搜索。计算变快后，才根据实测最坏时间和至少一 tick 交付余量确定需要前移多少 tick。

## 可观测性

每个正式请求至少记录：

- 提交、入队、worker 取件、phase 开始／结束、beam 每层开始／结束、最终重放、结果写出、IPC 到达、接纳、取消和退休时间；
- 请求 deadline、取消原因、作废世代和 typed 终态；
- 每层生成节点数、硬安全淘汰数、去重数、保留数和累计物理 step；
- 进入完整尾迹／canonical proof 的候选数及重放 step；
- IPC 排队与传输时间；
- control PID、worker PID 和 readiness／prewarm 时间。

这些字段用于定位时间花在哪里。它们不参与候选选择。

## 实施顺序

顺序固定为：

1. 修正三段量尺，并保存旧混合量尺的来源说明；
2. 接入真实 deadline、取消、世代作废和常驻 worker readiness；
3. 改造 beam 的逐 tick 双分支状态、硬安全早筛和去重；
4. 将完整尾迹／canonical proof 限于 completion 邻域候选；
5. 先跑 phase／beam 性能、取消和作废门槛；
6. beam 最坏时间 `<500 ms` 后，调整有界准备前缀与执行窗口；
7. 重新运行正式 oracle，再决定是否恢复 F2-TS 后续门槛。

任一前置门槛失败就停止。不能先跑 oracle，再用偶然成功掩盖搜索和交付仍然无界。

## 明确不做

- 不接 F2-GP、A* terminal edge 或路线优化器；
- 不扩大 completion，不降低碰撞、支撑、UNKNOWN、危险接触或停止尾迹要求；
- 不恢复 Inline worker；
- 不通过延长到 16 秒来等待现有 beam；
- 不在控制线程运行 phase、beam、尾迹或 canonical proof；
- 不增加场景 ID、种子、方向或固定坐标特判；
- 不修改 Sprint、Sneak、Crawl、Swim、Climb、JumpGap、JumpUp、ControlledDrop 和连续高度严格动作；
- 不回写 F2-TS、F2-SS、F2-GP 或其他历史阶段结论。

## 与既有决定的关系

本决定修正 [D086](0086-solve-bounded-ground-terminal-sequences.md) 对隔离 worker 性能失败的解释：F2-TS 仍然未通过，但 `23.2694／42.7075／42.7075 ms` 不能直接判为生产控制 `8／15／30 ms` 失败。正式阻塞是 beam 约 `16 秒`、工作期限内没有交付，以及过期工作不能在搜索内部及时停止。

D086 对结果身份、normal／late1、安全尾迹、身体责任和不接 F2-GP 的约束继续有效。实施与验收见 [F2-TP 阶段](../stages/F2TP-terminal-search-performance-and-delivery.md)和 [F2-TP 验收](../acceptance/F2TP-terminal-search-performance-and-delivery.md)。

## 实施后记

干净 Windows `a61a76e6` 已把 beam 最坏时间从约 `16 秒` 降到 `125.9636 ms`，并补上 deadline、取消、世代作废、READY 预热和三段直接计时。这个结果只关闭了搜索与 worker 的主要性能阻塞。

五个非 oracle 正式链的 production P95／P99 为 `10.2973／22.9633 ms`，超过 `8／15 ms`。因此本决定要求的 TP3 整体门槛仍未通过。项目没有前移触发，也没有运行 oracle。下一步必须先定位生产控制耗时；不能放宽门槛，不能把 backend 时间归入 production，也不能用 beam 已通过替代控制线程验收。

独立复核随后发现，Session 仍会在首次需要运动协调时创建、预热并可能关闭 worker，worker 的 READY 后死亡、取消背压和发布前最后作废检查也没有完整收口。因此后续由 [D088](0088-own-motion-worker-at-runtime-and-profile-control-hotpath.md) 和独立 [F2-RH 阶段](../stages/F2RH-runtime-worker-and-control-hotpath.md)处理。F2-TP 继续停在 TP3；F2-RH 通过前不进入 TP4 或 oracle。

## D089 后续解释

TP3 的原失败、未运行项目和证据保持不变。[D089](0089-grade-fail-fast-by-risk-and-bound-task-expansion.md) 将门槛分级后，beam 无界、取消／过期不能早停、控制线程执行搜索、deadline miss 和安全事件仍是硬阻塞。单独的 production P95/P99 容量余量未达改记为性能债，不再自动禁止无关产品工作。

TP4 和 oracle 仍没有自动获得授权。继续 F2-TP 时必须重新冻结范围，并限制为最多两轮修正，不能从旧 fail-fast 链继续扩张。
