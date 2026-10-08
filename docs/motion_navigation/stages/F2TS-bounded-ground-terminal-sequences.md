# F2-TS：普通地面有界末段序列方案

日期：2026-10-08。状态：正式隔离 worker 门槛失败，已按 fail-fast 停止。

**目标：** 用现有 1.21 计算器在后台求出一段可中断的多 tick地面末段，使机器人能进入窄 completion，同时保持普通地面安全、异步身份和控制期限不变。

**依据：** [D086](../decisions/0086-solve-bounded-ground-terminal-sequences.md)、[F2-SS 停止点](F2SS-single-ground-candidate-source.md)、[F2-SS 验收](../acceptance/F2SS-single-ground-candidate-source.md)和 [D080](../decisions/0080-share-ground-tracking-policy-for-terminal-approach.md)。

## 1. 当前事实与范围

F2-SS 已保留单一普通地面候选报告，短性能门槛也通过。F2-TS 实施前冻结了三个 v8 旧成功退步：

1. `f2r/clutter/0.1/7/22/product`；
2. `f2r/clutter/0.2/1/18/product`；
3. `f2r/clutter/0.2/8/22/product`。

三项在旧实现中停在 completion 外 `0.0061—0.0229` 格，零伤害、零安全事件，且输入来源释放。它们作为 oracle，只用于判断新求解器是否真的解决多 tick末段，不用于设计场景专用模板。Inline worker 曾让三项表面上完成，但正式隔离 worker 的第一项已经失败，因此不能宣称批次 5 通过；旧 F2-SS 结果仍按原证据保留。

本阶段只支持：

- `STANDING + WALK`；
- 已知世界；
- 已经有合法地面路线与 `GroundCompletionRegion`；
- 当前身体仍在安全支撑上；
- 最终若松键，可以通过已证明的中性尾迹安全停下。

## 2. 预计修改范围

预计新增：

- `mc2p/motion_nav/ground_terminal_search.py`：纯 phase／beam 求解、类型化请求与 `GroundTerminalSequence`；
- `tests/motion_nav/test_ground_terminal_search.py`：holdout、变异、normal／late1、安全尾迹与预算；
- `tests/motion_nav/test_ground_terminal_formal_chain.py`：正式 worker、身份、执行与过期结果；
- `scripts/benchmark_ground_terminal_search.py`：Windows phase／beam 性能与控制线程零搜索检查。

预计修改：

- `motion_worker.py`：在同一 worker 协议中增加地面末段 typed job/result；
- `motion_coordination.py`：复用现有 work identity、inbox、期限和退休规则；
- `fixed_route.py`：继续拥有身体，安装并逐 tick执行有效序列；
- `action_route_executor.py`：传递正式锚点、账本和序列窗口，不增加动作专用生命周期；
- `ground_terminal_approach.py`、`known_map_planner.py`、`route_admission.py`：行为门槛通过后再接 F2-GP terminal edge 与成本；
- 对应四类文档与证据索引。

明确不改：

- Session 状态集合、F8 和任务恢复预算；
- Runtime、MotorGateway 和唯一输入出口；
- goal/completion 几何；
- 普通地面十八项候选和评分；
- 严格动作的求解、入口、生效窗口、落地责任和伤害额度；
- 探索、动态避障、路线平滑和新动作。

## 3. 批次 0：冻结 RED、holdout 和独立 oracle

### 3.1 三项 RED

先在未改生产代码时重放三个 F2-SS 场景。保存：

- 进入末段时的正式 `StateAnchor`；
- goal、route 和 work identity；
- completion、依赖与规则版本；
- 每 tick观察、输入、正式候选报告和失败原因；
- 最终位置、速度和离 completion 的有符号距离。

三项必须继续得到当前有界失败，不能先修改预期结果。

### 3.2 独立 holdout

在看三个 RED 的成功序列前冻结一组独立输入。至少覆盖：

- X、Z 和双轴偏差；
- 正向速度、反向速度和横向速度；
- 四种世界方位与不同 yaw；
- 窄长区域、近方形区域和贴墙区域；
- normal 与 late1 启动；
- 某个前缀后立即松键；
- 相关世界格变化、目标修订和路线修订；
- 预算耗尽、无解和缺信息。

holdout 断言安全、完成、身份、稳定成本和固定遍历顺序，不把某个具体命令串写成唯一答案。

### 3.3 变异门禁

每个保留的阶段模板必须有独立作用。临时删除模板后，至少一个冻结 holdout 应出现以下变化之一：

- 原可解变成不可解；
- 只能进入 beam fallback；
- 预计成本超过冻结上限。

没有任何证据需要的模板直接删除，不保留“以后可能有用”的分支。交换模板顺序、只验证 normal、漏一个前缀尾迹、把预算耗尽当无解，也必须被检查发现。

## 4. 批次 1：冻结请求、结果和状态身份

新增类型化 `GroundTerminalSolveRequest`。至少包含：

- `StateAnchor`；
- `AsyncWorkIdentity`；
- goal ID/revision；
- route ID/revision 和动作索引；
- `CandidateExecutionWindow`；
- 从 anchor 到执行窗口的有界准备前缀；没有经过当前普通地面证明的未来入口不能提交；
- completion 与允许姿态／模式／终速；
- 路线走廊和当前进度；
- 有界物理世界快照与规则版本；
- phase、beam、总深度和节点预算；`maximum_ticks` 只能取 `1..64`，并在运行 oracle 前冻结。

新增类型化结果：

- `SOLVED`；
- `ALREADY_SATISFIED`；
- `NEEDS_INFORMATION`；
- `UNSUPPORTED_ENTRY`；
- `NO_PROVED_SEQUENCE`；
- `BUDGET_EXHAUSTED`；
- `INSUFFICIENT_LEAD`；
- `STALE`；
- `INTERNAL_ERROR`。

`BUDGET_EXHAUSTED` 只说明没有算完。调用方不得将它改成 `NO_KNOWN_ROUTE` 或 `NO_PROVED_SEQUENCE`。

`GroundTerminalSequence` 必须保存 D086 规定的准备前缀、正常／late1预计入口与轨迹、每前缀安全尾迹、全部依赖、完成结论和成本。构造器拒绝缺少任一启动分支、前缀尾迹、任务身份或路线身份的结果。

## 5. 批次 2：实现固定顺序的 phase search

phase search 只使用以下通用意图：

- `TARGET_X`、`TARGET_Z`；
- `BRAKE_X`、`BRAKE_Z`；
- 两轴意图的合法组合；
- `NEUTRAL`。

每个意图根据当前预测状态、目标区域和当前 movement yaw 投影成合法九键输入。算法不读取样例 ID、种子、绝对方位名或固定世界坐标。

按下面顺序枚举：

1. 一阶段；
2. 两阶段；
3. 三阶段；
4. 每个阶段持续时间按 `1..remaining_ticks` 递增枚举，序列总长不超过 `maximum_ticks`；
5. 每层内部按冻结的 primitive、组合和持续 tick顺序。

每个候选都同时推演 normal 与 late1。每执行一个前缀，再追加中性输入直到停稳或达到有界尾迹上限。只有两条启动轨迹的所有前缀都安全，并且完整序列都满足最终 completion，候选才进入稳定排序。

排序只使用：完成 tick、控制变化次数、最终 completion 余量、固定 primitive 身份和完整命令字典序。墙钟完成先后不参与选择。

## 6. 批次 3：增加 beam=512 的后台兜底

phase search 没有结果时才进入 beam。每一深度只保留最多 `512` 个状态。扩展输入仍来自同一组合法普通地面按键与 neutral。

beam 的稳定评分只用于保留搜索前沿，至少包含：

- 硬安全先决条件；
- 到 completion 的几何下界；
- 速度与可停车余量；
- 路线走廊偏差；
- 已用 tick与控制变化数；
- 稳定命令身份。

任何 `UNSAFE`、`NEEDS_INFORMATION` 或不完整 normal／late1分支不得留在前沿。搜索结束后仍要对胜者执行完整前缀中性尾迹复核，beam 评分本身不是安全证明。

beam 只在距离预计执行点至少 `11 tick` 时提交。提前量不足返回 typed `INSUFFICIENT_LEAD`／`BUDGET_EXHAUSTED`，继续由已有地面控制负责身体；控制线程不得补算。

## 7. 批次 4：复用 motion worker 并接入正式执行

在现有 `MotionWorkerPort` 中增加地面末段 job/result 联合类型。保留同一进程、固定队列、inbox、worker 存活检查和异常转 typed result 的规则。

求解期间：

- `FixedRouteController` 仍是身体 owner；
- 原闭环继续发出已证明安全的普通地面输入；
- 不新增 Session waiting 状态；
- 控制帧只提交、轮询和核对结果，不运行 phase 或 beam；
- 已无安全推进输入时，沿原规则制动或有界结束，不能为了等结果悬空等待。

接纳结果时核对 anchor、work identity、goal/revision、route/revision、动作索引、窗口、规则和依赖。任一不匹配直接退休。

求解目标在未来 tick 时，还要核对准备前缀的实际应用与预计入口。FixedRoute 可以在求解期间继续推进，但只能沿这段自己已经证明的前缀推进；没有正式前缀时只能从稳定停止入口求解。

执行器一次只提交当前观察对应的下一条命令。normal 或 late1 分支由实际应用账本确定。观察偏离已证明轨迹、命令晚于 late1、依赖变化或目标／路线修订时，使用对应前缀的安全尾迹收尾，再从新锚点求解。

## 8. 批次 5：先过独立门槛，再回归历史三例

顺序固定为：

1. holdout 全部通过；
2. 删除模板和其他错误副本全部被发现；
3. Windows phase search P95 `<50 ms`；
4. Windows beam fallback P95 `<500 ms`；
5. 控制线程搜索调用数 `0`，控制生产帧保持 8／15／30 ms；
6. 三项 F2-SS oracle；
7. remaining9；
8. remaining42。

三项必须 `3/3` 在正式物理下完成，并满足原 completion；不能只恢复旧的“成功”标签。remaining9 不低于 `6/9`，remaining42 保持 `42/42`。所有层的旧成功退步、安全事件、伤害超额、来源泄漏和终态持有输入必须为 `0`。

任一层失败即停，不接 F2-GP。

### 8.1 执行与停止结果

已经保留的实现：

- typed `GroundTerminalSolveJob／Result` 复用现有 motion worker、inbox 和工作生命周期；
- 最终带 completion 的普通 Walk 才进入末段求解，普通 Walk 与严格动作不受影响；
- phase 立即求解；phase 返回 `INSUFFICIENT_LEAD` 时，只有身体稳定着地才会提交带 10 tick正式中性准备的 beam 工作，执行窗口从第 11 tick开始；
- FixedRoute 在等待、准备和逐 tick执行期间始终持有身体。安装序列前会用正式输入账本核对准备输入；
- 旧工作、目标、路线、锚点、窗口或世界依赖结果都会退休；normal／late1 由实际输入账本选择；
- 偏离后只从当前 frame、anchor 和世界验证中性或地面恢复动作。旧前缀尾迹只限定恢复期限，不直接授权输入；
- 安装和执行会核对 anchor phase、input projection 和 sequence anchor 身份；持续外力下的恢复也会有界结束；
- 聚焦正式链检查 `175/175` 通过；真实 MotionSolverWorker 的执行 PID 与控制 PID 不同。

随后查明旧的 `3/3`、`9/9` 和 `42/42` 使用 Inline worker。搜索在 `poll_available()` 中占用控制线程，且模拟 tick不会在搜索期间前进，因此这些结果不是正式异步链证据。

干净 Windows 提交 `a8cb3bfb` 改用真实 Planner／Motion 后台进程。第一个 oracle 即以 `no_safe_ground_candidate` 有界失败：beam 没有在 20 tick／1 秒工作期限内交付。搜索进程 PID `45828`，控制进程 PID `10304`。

后续审计确认，当时的 P95／P99／max `23.2694／42.7075／42.7075 ms` 是混合区间，不能直接与 production `8／15／30 ms` 门槛比较。F2-TS 仍因正式 beam 未交付而失败；量尺修正、约 `16 秒` 的 beam 成本和旧工作早停由独立 [F2-TP](F2TP-terminal-search-performance-and-delivery.md) 处理。

项目已在第一项停止，未运行其余两个 oracle、remaining9、remaining42、F2-GP terminal edge、A* 成本、大集合、D061 和 Fabric。机器可读失败证据位于 `evidence/motion_navigation/F2TS-bounded-ground-terminal-v1/isolated-worker-gate-failure/`。

## 9. 批次 6：接入 F2-GP terminal edge 与 A* 成本

前面全部通过后，才让 `SurfaceTerminalApproachEdge` 持有已证明的 `GroundTerminalSequence` 或它的稳定引用。

要求：

- 只有具体预测出口或请求锚点能创建末段序列；宽泛 `PlannerStateKey` 不得假造入口；
- 序列 `cost_ticks` 进入 A* 的 `g` 值；
- 需要移动的序列成本必须大于零；
- `ALREADY_SATISFIED` 才允许零 tick；
- terminal edge、witness、接纳、ActionRoute、复核和执行使用同一个序列身份；
- 预算耗尽保留为未决，不能变成无路线；
- 大图普通节点不调用 terminal solver。

先跑 F2-GP 原 terminal RED 和 A* 成本变异。删除末段成本、换一份入口、接纳时重算序列、把预算耗尽删边，都必须被测试发现。

## 10. 批次 7：大集合、D061 与 Fabric

按 fail-fast 顺序运行：

1. v9、v8、v7、F2、F2-R；
2. 五组行为、协调集合；
3. 完整 motion_nav 正序／逆序；
4. D061；
5. Fabric。

Fabric 至少覆盖：

- 三个 oracle 对应的几何族，不依赖固定坐标；
- X、Z、双轴和四种方位；
- normal 与实际 late1；
- 序列中途取消、目标修订、路线修订和相关世界变化；
- 每个阶段分别注入一次输入丢失；
- beam 有足够提前量和不足提前量；
- 一个 JumpGap 或 ControlledDrop 严格动作负对照。

逐 tick保存工作身份、候选层级、命令索引、应用 tick、当前分支、预测偏差、尾迹选择、依赖、owner 和终态。

## 11. 性能与容量

正式报告至少包含：

- phase 的候选数、物理 step、P50/P95/P99/max；
- beam 每层生成数、去重数、保留数、最大宽度和总节点；
- worker 提交、排队、计算、交付和接纳时刻；
- 控制线程 phase／beam 调用数；
- 控制生产 P95/P99/max 与 D061 完整夹具最大值。

硬门槛：

- phase P95 `<50 ms`；
- beam P95 `<500 ms`；
- beam width `≤512`；
- beam 提前量 `≥11 tick`；
- 控制线程搜索调用数 `0`；
- 控制生产 P95 `≤8 ms`、P99 `≤15 ms`、max `<30 ms`；
- D061 完整夹具 max `<50 ms`；
- 队列、inbox、结果历史和前缀尾迹都有现行有界容量。

## 12. 止损条件

出现下面任一情况立即停止：

- holdout 只能靠三个已知样例的专用规则通过；
- 需要按场景 ID、种子、方向名或绝对坐标选模板；
- normal 或 late1 任一分支未证明；
- 任一前缀没有安全 neutral tail；
- phase 或 beam 在控制线程运行；
- beam 提前量少于 11 tick仍被接纳；
- 预算耗尽被当成无解；
- 求解期间 FixedRoute 丢失身体责任或新增 Session waiting；
- 旧身份、旧目标、旧路线或过期锚点的结果被接纳；
- 需要扩大 completion、降低安全门槛或改普通评分；
- 严格动作行为改变；
- 前一层出现历史成功退步或新安全事件。

## 13. 完成条件

F2-TS 只有在独立 holdout、变异、性能、三项 oracle、remaining9／42、F2-GP terminal edge、A* 成本、大集合、D061 和 Fabric 全部通过后才能关闭。

F2-TS 通过后，F2-SS 才能从当前停止点继续完成自己尚未运行的门槛。历史 F2-SS、F2-GP、F2-ST、F2-SG 和 F2-S 结论不会自动改写。
