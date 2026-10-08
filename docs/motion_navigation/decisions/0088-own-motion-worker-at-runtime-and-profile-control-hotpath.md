# D088：由 Runtime 持有运动 worker，并按实测收敛控制热路径

日期：2026-10-08。状态：生命周期已实施；按原复合门槛，性能余量未通过并停止。后续风险解释由 [D089](0089-grade-fail-fast-by-risk-and-bound-task-expansion.md) 修订。

实施结果：Runtime 已成为共享 Motion worker 的唯一生命周期所有者。Session 不再创建、预热、转移或关闭 worker。健康、取消背压、两秒重试期限、每次非阻塞发布前作废和 air／ground cooperative stop 已落地。真实 air solve／revalidation 会在入口后的检查点停止，并让 FIFO 中的后续 ground 工作继续。生命周期聚焦检查为 `98/98`，独立复审无未关闭 P0／P1／P2。只读 profile 确认普通地面验证器是稳定帧最大的可控开销；唯一一轮形状查询复用没有把 production 降到门槛内。干净 Windows `963d5b49` 的 production P95／P99／max 为 `10.2680／23.9535／24.9818 ms`，full max 为 `45.3178 ms`。项目按本决定的止损规则停在 RH5，没有进入 TP4 或 oracle。

## 要解决的问题

F2-TP 已把末段 beam 从约 `16 秒` 降到最坏 `125.9636 ms`，也证明了搜索运行在独立进程中。TP3 仍没有通过：五个非 oracle 正式链场景共 `142` 个控制帧，production P95／P99／max 为 `10.2973／22.9633／24.9259 ms`。P95 和 P99 超过 `8／15 ms`。

独立复核发现，继续直接优化验证器并不稳妥。当前还有两类问题。

第一类是 worker 生命周期没有真正落在 Runtime：

- `NavigationSession` 在第一次接纳需要运动求解的路线时才创建并预热 `MotionSolverWorker`；
- Session 还保存“是否拥有 worker”，并可能在会话结束时关闭进程；
- worker 已经报告 `READY` 后死亡，当前 readiness 仍可能继续显示 `READY`；
- cancel 控制队列满时只返回 `False`，调用方可能继续等待一项实际上没有送达的取消；
- 搜索算完到写结果之间没有最后一次取消／作废检查；
- 同一 FIFO 中的旧 revision、空中动作和地面末段工作缺少正式的互相阻塞测试。

这与 D087 的“Runtime 持有常驻、已预热 worker”不一致。它也可能把进程创建、预热或错误收尾带进少数控制帧。

第二类是 production 只有总时间，没有足够的分段数据。现在还不能严谨回答时间主要花在观察接入、状态锚定、Session 推进、普通地面十八候选验证、运动协调、快照、结果收件箱、Runtime 仲裁、结果采纳、输入账本还是 trace。

因此，先修 worker 生命周期，再只读测量控制热路径。只有 profile 指向普通地面验证器后，才允许进行一轮不改变语义的局部优化。

## 决定

新增独立阶段 **F2-RH**。F2-TP 保持停在 TP3。F2-RH 通过之前，不运行 TP4、oracle、remaining9／42、F2-GP、大集合、D061 或 Fabric。

### 1. Runtime 是 worker 的唯一生命周期所有者

正式 `PlayerRuntimeV1` 持有一个共享 `MotionSolverWorker`。Runtime 负责：

- 创建进程；
- 完成规则、输入投影和 1.21 计算器预热；
- 保存 typed readiness／health；
- 在 Runtime 关闭时关闭进程。

`RuntimeNavigationDriver` 从 Runtime 取得已预热的借用端口，并在导航开始前注入 `NavigationSession`。Session 再把同一个端口交给 `MotionRouteCoordinator`。

Session 不再：

- 在路线接纳期间创建或预热 worker；
- 用 `_owns_motion_worker` 转移进程所有权；
- 在 Session、successor 或 continuation 关闭时关闭共享 worker。

Session 结束时只取消属于自己的工作身份、清理自己的结果路由和释放身体责任。不同任务连续运行时继续复用同一 worker PID。

测试可以显式注入 fake worker。正式路径不能因为 worker 不可用而回退到 Inline 搜索。

### 2. readiness 和 health 必须反映真实进程状态

worker 初始化和预热的结果用类型表达，至少区分：

- `STARTING`；
- `READY`；
- `INITIALIZATION_FAILED`；
- `DEAD`；
- `CLOSED`。

初始化或预热失败不能伪装成普通无解，也不能只抛一个没有归属的契约异常。Runtime 可以继续报告自己的通用控制状态，但导航开始必须得到明确的 `motion_worker_unavailable` 结果。

worker 在 `READY` 后退出时，下一次 health 查询必须返回 `DEAD`。不能因为历史上曾经 ready 就继续报告可用。PID、预热完成时间、死亡时间和失败类型只用于诊断，不参与路线选择。

### 3. 取消和最后交付不能静默丢失

`cancel()` 不再只返回布尔值。结果至少区分：

- 已接收；
- 工作已经结束；
- 控制队列背压；
- worker 不可用。

控制队列满时，调用方必须保留本地退休标记，并在后续帧有界重试发送取消。旧结果即使到达，也先被本地身份门槛拒绝。队列背压不能让调用方假定取消成功，也不能阻塞控制线程等待空位。

worker 在发布结果前最后执行一次 control drain，并再次检查 deadline、cancel 和 stale generation。如果此时工作已经失效，只能发布对应 typed 终态，不能发布先前算出的 `SOLVED`。

空中与地面工作都使用同一组 cooperative stop 检查点。旧 revision 排在 FIFO 前面时，取消后必须在下一个检查点退出；不能继续占住 worker，挡住当前 revision。

### 4. 生命周期先于性能优化

顺序固定为：

1. Runtime／Driver 持有、预热和注入共享 worker；
2. readiness、死亡、取消背压和发布前最后检查；
3. 旧 revision 与 air／ground FIFO 阻塞场景；
4. 只读分段计时；
5. 根据 profile 决定是否执行唯一一轮局部优化；
6. 重新跑 production／full 门槛。

生命周期门槛没通过时，不做验证器优化。这样可以避免把进程懒启动或错误排队造成的尖峰误当成物理计算热点。

### 5. 只读拆分 production 时间

同一个正式控制帧至少直接记录下面的区间：

- 观察接入 `ingest`；
- 状态锚定 `anchor`；
- `session.propose`；
- `FixedRoute` 十八候选验证器；
- motion coordinator；
- snapshot 推进；
- motion result inbox；
- Runtime 仲裁；
- Driver 采纳结果；
- 输入账本；
- trace 写入。

每段都用控制进程的单调时钟直接计时。允许同时保存 inclusive 和 exclusive 时间，但报告必须说明口径。总 production 还要报告没有落入已知分段的 residual，避免分项相加后仍藏着一段无法解释的时间。

计时只写诊断，不能改变候选顺序、预算、deadline、工作身份或控制结果。启用和关闭计时后，同一冻结输入的终态、原因、输入、轨迹和依赖必须逐项一致。

### 6. 只允许一轮验证器语义不变优化

profile 如果确认普通地面验证器是主要热点，允许进行一轮局部优化。允许的手段只有：

- 同一帧复用已经建立的验证 context 和 `WorldQueryCache`；
- 复用已查询的几何、材质和支撑结果；
- 同一候选只计算一次完整 neutral tail；
- 依赖先局部收集，最后稳定合并；
- 减少不必要的临时对象和重复不可变容器复制。

下面的语义保持不变：

- 十八个普通地面候选一个都不能删除；
- `0／1／2 tick` 可能生效前缀全部保留；
- 最长 `30 tick` 的中性停止尾迹保留；
- UNKNOWN、碰撞、危险接触、姿态、支撑和 completion 规则不变；
- normal／late1、路线适配和世界依赖不变；
- 不能按场景、方向、种子或固定坐标缓存答案。

profile 若显示主要耗时不在验证器，本阶段停止并记录结果。不能把授权转用到 Session 生命周期、候选裁剪或后台化控制逻辑。

### 7. 性能门槛不变

Windows 正式门槛仍为：

- production P95 `≤8 ms`；
- production P99 `≤15 ms`；
- production max `<30 ms`；
- full max `<50 ms`。

backend 单独报告。不能用 full 通过替代 production，也不能删除慢帧、减少候选或改成异步后只统计提交时间。

F2-RH 通过后，才返回 F2-TP TP4，实施有界前移触发，再运行正式 oracle。F2-RH 本身不运行 oracle。

## 明确不做

- 不改变 completion 或目标区域；
- 不减少十八候选、前缀分支或停止尾迹；
- 不修改 F8、任务恢复预算或 Session 生命周期状态；
- 不新建第二个 motion worker 或第二套工作身份；
- 不给 air／ground 建立彼此不一致的取消语义；
- 不在控制线程执行搜索、replay 或 canonical proof；
- 不运行 TP4、oracle、remaining9／42、F2-GP、大集合、D061 或 Fabric；
- 不回写 F2-TP TP3 的失败记录。

## 与既有决定的关系

本决定细化 [D087](0087-correct-terminal-search-timing-and-bound-delivery.md) 的 Runtime worker 所有权和 TP3 production 收敛方式。D087 的 deadline、逐 tick beam、normal／late1、安全尾迹、500 ms 搜索门槛和三段量尺继续有效。

实施顺序见 [F2-RH 阶段](../stages/F2RH-runtime-worker-and-control-hotpath.md)，验收见 [F2-RH 验收](../acceptance/F2RH-runtime-worker-and-control-hotpath.md)。

## D089 后续解释

本决定中的 `8/15 ms` 继续作为容量目标保留，RH5 的失败和当时未运行项目不回写。D089 将期限与容量余量分开：本批五个非 oracle 场景没有观察到安全事件或 deadline miss，full max 通过该批 50 ms 门槛，production max 容量上限也通过；P95/P99 未达改记为性能债。这些结果不能外推到未运行的动作和场景。

这项性能债不自动阻塞无关产品主线，也不允许继续在本阶段叠加第二轮优化。TP4 和 oracle 没有因此自动获得授权；如要继续，必须由用户明确授权，重新冻结产品目标、受影响热路径和最多两轮修正，并说明如何处理原 `8/15 ms` 复合门槛与同一热路径的性能债。
