# F2-RH Runtime worker 生命周期与控制热路径验收

日期：2026-10-08。状态：生命周期门槛通过；正式性能门槛失败。

本验收关闭两个问题：共享 Motion worker 是否真的由 Runtime 持有；production 超限是否被一份可解释、可复现且不改变行为的 profile 收敛。

它不验收 TP4、oracle、remaining9／42、F2-GP、大集合、D061 或 Fabric。

## 1. RED 基线

| 项目 | 当前结果 | 判定 |
|---|---:|---|
| phase P95 | `3.8161 ms` | 通过 |
| beam worst-case | `125.9636 ms` | 通过 `<500 ms` |
| production P95/P99/max | `10.2973/22.9633/24.9259 ms` | P95、P99 失败 |
| full max | `44.8677 ms` | 通过 `<50 ms` |
| 正式 worker 所有者 | Session 首次需要时可创建，Session 可关闭 | RED |
| READY 后死亡 | readiness 可能保留 READY | RED |
| cancel queue 满 | 布尔 `False` | RED，调用方没有 typed 处置 |
| 发布前最后 drain | 缺少 | RED |
| production 分段 | 只有总区间 | RED |

来源为干净 Windows `a61a76e6` 和 `evidence/motion_navigation/F2TP-terminal-search-performance-v1/tp3-gate-failure/`。原文件不改写。

## 2. Runtime 所有权

必须用正式 `PlayerRuntimeV1 → RuntimeNavigationDriver → NavigationSession → MotionRouteCoordinator` 路径证明：

1. Runtime 只创建一个 Motion worker；
2. Driver 只借用并注入；
3. Session 不创建、不预热、不关闭 worker；
4. successor／continuation 复用同一个 PID；
5. Session 结束只取消自己的身份；
6. Runtime 关闭后 worker 进程结束；
7. worker 不可用时没有 Inline 搜索。

反例至少包括：Session 首次路线时偷偷创建进程、Session close 关闭共享进程、第二个任务更换 PID、无 worker 时同步搜索。每个反例都必须失败。

## 3. readiness 与 health

固定矩阵：

| 场景 | 预期 |
|---|---|
| 正常初始化与预热 | `STARTING → READY`，记录 PID 和 ready 时间 |
| 初始化抛错 | `INITIALIZATION_FAILED` |
| 预热抛错 | `INITIALIZATION_FAILED` |
| READY 前退出 | `DEAD` 或初始化失败，不得 READY |
| READY 后退出 | 下一次查询为 `DEAD` |
| 已关闭 | `CLOSED` |
| 非导航 Runtime 仍活着但 worker 失败 | Runtime 状态与导航能力状态分别报告 |

导航开始时 worker 不是 READY，必须返回明确的能力不可用结果，并且不注册持久输入源。

## 4. cancel、stale 和发布边界

至少覆盖：

- cancel 控制队列正常接收；
- cancel 控制队列已满；
- 本地退休后旧 `SOLVED` 到达；
- deadline 在最后一个 beam 节点后到期；
- generation 在完整 replay 后作废；
- result put 前一刻取消；
- worker 在取消待重试期间死亡。

断言：

- 每次 cancel 都返回 typed 状态；
- 背压不会被当作成功，也不会阻塞控制线程；
- 本地退休立即剥夺接纳资格；
- worker 发布前最后 drain controls；
- 最后作废只能发布 typed `TIMEOUT／CANCELLED／STALE`，不能发布 `SOLVED`；
- 所有待重试和退休记录有容量与期限。

删除最后 drain、把 typed 状态改回 bool、忽略背压和允许本地退休结果接纳的错误副本都必须被发现。

## 5. FIFO 阻塞矩阵

运行下面六组确定性组合：

1. 排队旧 ground revision → 当前 ground；
2. 运行中旧 ground revision → 当前 ground；
3. 排队旧 air → 当前 ground；
4. 运行中旧 air → 当前 ground；
5. 排队旧 ground → 当前 air；
6. 运行中旧 ground → 当前 air。

每组分别注入 cancel、stale generation 和 deadline。记录旧工作停止检查点、新工作开始时间、物理 step 和结果身份。

门槛：

- 已失效工作在下一个 cooperative checkpoint 停止；
- 排队但未开始的失效工作生成 beam／replay step 为 `0`；
- 当前工作不会被失效工作无限挡住；
- 两项仍有效时保持 FIFO，且单项仍受已有 `<500 ms` 搜索上限；
- air／ground 的 stop 语义一致；
- 结果顺序不能让旧身份进入当前 inbox。

## 6. 只读计时正确性

正式帧必须直接记录：

- ingest；
- anchor；
- session.propose；
- FixedRoute verifier；
- coordinator；
- snapshot；
- inbox；
- Runtime arbitrate；
- adopt；
- ledger；
- trace；
- unattributed residual；
- production／backend／full。

验收要求：

1. 每段使用单调时钟直接计时；
2. 嵌套段同时报告 inclusive／exclusive 或明确选择一种口径；
3. production 总量与已知分段和 residual 能对上；
4. backend 不混入 production；
5. 启用／禁用计时后，冻结行为签名逐项一致；
6. 计时结果不参与候选、deadline 或接纳判断；
7. trace 仍按原失败规则处理。

变异：把 backend 包进 production、重复累加嵌套分段、漏记 residual、计时器改变候选顺序，均须失败。

## 7. profile 判定

先在未优化实现上保存 profile。报告按场景和帧类型列出样本数、P50／P95／P99／max、调用次数和占 production 比例。

只有同时满足下面条件，才授权验证器优化：

- `fixed_route.verifier` 是超限帧的主要可控耗时；
- worker 创建、预热、死亡检查和 cancel 重试已经不在 Session 热路径；
- unattributed 没有大到足以改变根因判断；
- 结论在重复运行和正逆场景顺序下保持一致。

如果根因在其他分段，记录 RED 并停止。不能把本阶段的一轮优化授权挪到其他模块。

## 8. 验证器语义不变检查

若进入优化，前后逐项比较：

- 十八候选身份、顺序和数量；
- `0／1／2 tick` 前缀；
- normal／late1 轨迹；
- 每项完整 neutral tail，最长仍为 `30 tick`；
- UNKNOWN、碰撞、危险接触、姿态、支撑和 completion 结果；
- tracking／stopped endpoint；
- 世界依赖；
- 最终命令和拒绝原因。

专项变异至少包括：删除一个候选、跳过一个 prefix、把尾迹缩短一 tick、复用错误世界 cache、漏一个依赖、把 UNKNOWN 当空气、只验证 normal。每项必须失败。

允许的性能变化只来自重复查询、重复尾迹、依赖合并和对象构造减少。

## 9. Windows 正式性能门槛

正式提交必须干净。使用和 TP3 相同的五个非 oracle 场景和直接三段计时，报告所有分段。

| 指标 | 门槛 |
|---|---:|
| production P95 | `≤8 ms` |
| production P99 | `≤15 ms` |
| production max | `<30 ms` |
| full max | `<50 ms` |
| control-thread search calls | `0` |
| deadline miss | `0` |
| 安全事件 | `0` |
| 伤害额度超出 | `0` |
| 无人负责输入／来源泄漏 | `0` |

backend 只报告。预热和冷启动单列，不能删样本。worker 生命周期、FIFO、行为签名和验证器变异必须同时通过。

## 10. fail-fast 与证据边界

顺序为：RH0 基线 → Runtime 所有权 → health／cancel／FIFO → 只读 profile → 一轮优化 → 正式门槛。

前一层失败立即停止。profile 不支持优化时停止。一轮优化后仍超时也停止。

本验收通过后，只允许返回 F2-TP TP4。它不提供 oracle、remaining9／42、D061 或 Fabric 结论。

## 11. 结果记录模板

实施后在这里记录：

- 来源提交和工作树状态；
- Windows、Python、CPU 与进程启动方式；
- Runtime／worker PID、health 和预热时间；
- 生命周期、cancel 和 FIFO 矩阵结果；
- profile 原始分段和 root cause；
- 唯一优化的代码范围与语义变异结果；
- production／backend／full 最终样本；
- 未运行项目和停止原因。

## 12. 正式结果

来源提交为干净 Windows `963d5b49de4b02f42072b3e40426e3f3b8b67b5d`。Runtime worker 生命周期、健康、取消和发布边界的聚焦正式链为 `98/98`；验证器、计时和组合检查为 `28/28`。两个连续 Session 复用同一 worker；Session close 不关闭它，Runtime close 只关闭一次。不可用 worker 在输入源注册前返回 `motion_worker_unavailable`。

固定 FIFO 门槛包含排队和运行中的 ground／air 交叉顺序，以及 cancel／stale／deadline 三类停止。真实 air solve 和 revalidation 都在入口之后的第三个内部检查点才收到停止；旧工作随后退出，后续 ground 工作得到结果。结果队列使用“重新读取 control，再 `put_nowait`”的有界循环。取消待发、本地退休和 coordinator 重试同时受容量与两秒期限约束。独立复审结论为零个未关闭 P0／P1／P2。

固定五个非 oracle 场景全部完成，零安全事件。profile 使用 exclusive 时间判断归属，inclusive 时间保留嵌套关系，residual 单列。主要分段如下：

| 分段 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|
| FixedRoute verifier | `4.2538` | `4.5559` | `4.7784` | `4.7945` |
| ingest | `2.4949` | `2.7037` | `2.7612` | `2.7757` |
| motion coordinator exclusive | `1.4645` | `1.8583` | `15.7448` | `16.8904` |
| unattributed | `0.2583` | `0.3286` | `0.3680` | `0.3681` |

单位均为毫秒。coordinator 的长尾来自少数后台结果交付帧；稳定帧中 verifier 仍是最大的单项可控开销，因此按 D088 进入了唯一一轮局部优化。

该优化只缓存同一不可变验证世界中完全相同的 shape query。聚焦行为、十八候选和安全语义检查均通过，但性能改善不足：

| 指标 | 结果 | 门槛 | 判定 |
|---|---:|---:|---|
| production P95 | `10.2680 ms` | `≤8 ms` | 失败 |
| production P99 | `23.9535 ms` | `≤15 ms` | 失败 |
| production max | `24.9818 ms` | `<30 ms` | 通过 |
| full max | `45.3178 ms` | `<50 ms` | 通过 |

项目据此按 fail-fast 停止。F2-RH 未关闭，不进入 F2-TP TP4。没有运行 oracle、remaining9／42、F2-GP、大集合、D061 或 Fabric。证据见 `evidence/motion_navigation/F2RH-runtime-worker-hotpath-v1/`；完整逐帧数据可由其中命令和固定输入重建。

仓库完整入口另运行 `1812` 项，得到 `58` 项失败和 `2` 项错误，共享场景哈希前后一致。该入口没有全绿，不能被本阶段聚焦结果覆盖。其中包含源码变化后尚未更新的公开导出指纹，以及项目已经登记的 F2 边缘防坠、旧产品证据和历史动作回归；原始名单保存在紧凑证据中。本阶段没有为使完整入口变绿而改写这些既有失败。

## 13. D089 风险分级

本页原验收结论和数值保持不变。后续工作使用 [D089](../decisions/0089-grade-fail-fast-by-risk-and-bound-task-expansion.md) 的统一口径：

| 项目 | 当前事实 | 后续分类 |
|---|---|---|
| 生命周期、取消、发布和 FIFO | 聚焦检查通过，独立复审无未关闭 P0／P1／P2 | 已关闭硬风险 |
| 安全事件和 deadline miss | 均为 `0` | 硬门槛通过 |
| production max | `24.9818 ms < 30 ms` | 容量目标通过 |
| full max | `45.3178 ms < 50 ms` | 本批五个场景的 full-frame 门槛通过 |
| production P95/P99 | `10.2680/23.9535 ms`，未达 `8/15 ms` | 性能债，原 RH5 仍记失败 |

这次重新分类不把 F2-RH 改成通过，也不补写未运行的 TP4、oracle 或 Fabric。它只说明 P95/P99 余量本身不再自动阻塞无关产品工作。本批 full max 和 deadline 结果不能外推到所有正式动作。以后若同一热路径增加新负载，或者出现真实迟到、窗口外输入、产品退步和安全事件，应重新提升为阻塞项。
