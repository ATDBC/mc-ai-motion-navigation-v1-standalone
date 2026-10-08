# F2-RH：Runtime worker 生命周期与控制热路径方案

日期：2026-10-08。状态：已实施到 RH5；按原复合门槛阶段未通过。生命周期硬门槛已通过，性能余量按 [D089](../decisions/0089-grade-fail-fast-by-risk-and-bound-task-expansion.md) 登记为债务。

**目标：** 先把共享 Motion worker 的所有权、健康和取消交付修正确，再用只读分段计时找到 production 超限的真实热点。若热点确认为普通地面验证器，只做一轮语义不变优化。

**依据：** [D088](../decisions/0088-own-motion-worker-at-runtime-and-profile-control-hotpath.md)、[D087](../decisions/0087-correct-terminal-search-timing-and-bound-delivery.md)和 [F2-TP 验收](../acceptance/F2TP-terminal-search-performance-and-delivery.md)。

## 1. 当前事实

- F2-TP TP3 的 phase P95 为 `3.8161 ms`，beam 最坏为 `125.9636 ms`，搜索进程和控制进程已经隔离。
- 五个非 oracle 正式链共 `142` 个控制帧。production P95／P99／max 为 `10.2973／22.9633／24.9259 ms`，P95、P99 失败。
- full max 为 `44.8677 ms`，通过 `<50 ms`；backend 已单列。
- `NavigationSession` 仍会在第一次需要运动协调时创建并预热 worker，并保存关闭它的所有权。
- worker 的初始化失败、READY 后死亡、cancel 队列满和结果发布前作废还没有完整的正式契约。
- production 还没有细分到足以确定优化对象。

F2-TP 因此停在 TP3。F2-RH 不改变 F2-TP 的历史结果。

## 2. 范围

本阶段允许修改：

- `PlayerRuntimeV1` 与 `RuntimeNavigationDriver` 的 worker 持有和注入；
- `NavigationSession`／`MotionRouteCoordinator` 对借用 worker 的使用；
- `MotionSolverWorker` readiness、health、cancel 和结果发布边界；
- 生产控制路径的只读分段计时；
- profile 明确指向后的一轮普通地面验证器局部优化；
- 对应测试、脚本、证据和四类文档。

本阶段不改候选集合、安全规则、completion、规划搜索、严格动作和 F2-GP。

## 3. RH0：冻结失败和行为参照

保留 TP3 原证据目录和源码指纹。新增实现前冻结：

- 五个非 oracle 场景的终态、原因、逐帧输入、路线／工作身份、依赖和轨迹签名；
- 十八候选集合与稳定顺序；
- `0／1／2 tick` 前缀数量；
- 每个候选的 neutral-tail 上限和依赖；
- worker PID、READY、phase／beam 统计和取消结果。

完成条件：测试能发现候选减少、停止尾迹缩短、UNKNOWN 放宽、依赖减少和 Session 临时创建 worker。

## 4. RH1：把 worker 生命周期移到 Runtime

### 4.1 所有权

- Runtime 创建并关闭唯一共享 Motion worker；
- Runtime 保存 typed health 和 readiness；
- Driver 在 start 前取得借用端口并注入 Session；
- Session 把同一端口交给 coordinator；
- Session、successor、continuation 只退休自己的工作，不关闭进程。

正式 Session 如果没有注入 worker，却接纳了需要运动协调的路线，返回 typed `motion_worker_unavailable`。它不能懒启动，也不能回退 Inline。

### 4.2 初始化与死亡

覆盖：

- 正常启动、预热、READY；
- 初始化异常；
- 预热异常；
- READY 前死亡；
- READY 后死亡；
- Runtime 正常关闭；
- 连续 Session 复用同一 PID。

health 查询必须检查当前进程，不只读取历史 readiness。失败时 Driver 不注册或继续使用导航输入源。

### 4.3 RH1 完成条件

- 正式调用链只创建一个 worker；
- Session 源码没有创建、预热或关闭 worker 的路径；
- 两个连续导航任务复用 PID；
- 初始化失败和进程死亡都有 typed 结果；
- worker 不可用时控制线程搜索调用数仍为 `0`；
- 原身体收尾与输入来源释放规则不变。

RH1 不通过时停止，不进入 profile。

## 5. RH2：取消、交付和 FIFO 阻塞

### 5.1 cancel 背压

取消结果改为 typed 状态。控制队列满时：

1. 当前 owner 先保存本地退休标记；
2. 旧结果不再有接纳资格；
3. 取消请求留在有界待发送集合，后续帧非阻塞重试；
4. worker 不可用时返回明确状态；
5. 新工作不能无限堆在仍未取消的旧工作后面。

不能阻塞控制线程等待 queue 空位，也不能把 `False` 当作已经取消。

### 5.2 发布前最后检查

worker 在 `results.put` 前最后 drain control，并复核 deadline、cancel 和 generation。最后一刻取消或作废时，不得发布 `SOLVED`。

### 5.3 固定阻塞矩阵

至少覆盖：

| 前一个工作 | 后一个工作 | 注入 | 预期 |
|---|---|---|---|
| 旧 ground revision | 新 ground revision | 排队前取消 | 旧工作不扩展，新工作开始 |
| 运行中的旧 ground revision | 新 ground revision | beam 中取消 | 下一个检查点退出，新工作有界开始 |
| 旧 air | 当前 ground | air 运行中作废 | air 有界退出，ground 不饿死 |
| 旧 ground | 当前 air | ground 运行中作废 | ground 有界退出，air 不饿死 |
| 已算完待发布 | 新 revision | 发布前作废 | 只能得到 typed `STALE` |
| cancel queue 满 | 任意新工作 | 连续取消 | 返回背压，旧结果不能接纳，队列保持有界 |

两个仍然有效的工作可以按 FIFO 执行，但每项都受已有 deadline 和 `<500 ms` 搜索上限约束。这里不引入动作优先级调度器。

### 5.4 RH2 完成条件

- 所有工作类型使用 cooperative stop；
- 旧 revision 和不同动作族不能无限阻塞当前工作；
- cancel、request、result、inbox 和本地退休集合容量有上限；
- 删除最后 drain、忽略 cancel 背压、让 gap job 不检查取消的错误副本都会失败。

## 6. RH3：只读分段计时与 profile

### 6.1 分段

在控制线程记录：

1. `runtime.navigation_ingest`；
2. `driver.anchor`；
3. `session.propose`；
4. `fixed_route.verifier`；
5. `motion_coordinator`；
6. `snapshot`；
7. `motion_inbox`；
8. `runtime.arbitrate`；
9. `driver.adopt`；
10. `input_ledger`；
11. `trace`。

每段记录调用次数、P50／P95／P99／max 和 inclusive／exclusive 口径。production 总区间与这些分段之间的差额作为 `unattributed` 报告。

### 6.2 只读门槛

- 开关计时前后的业务签名逐项一致；
- 计时不能改变 worker 调度、候选排序或墙钟预算；
- 可控时钟能分别命中每一段；
- 嵌套分段不会重复加总成错误的 production；
- trace 失败仍走原有失败策略，不被计时器吞掉。

### 6.3 profile 输入

使用 RH0 的五个固定场景，并覆盖：

- 普通路线推进；
- 目标修订；
- snapshot／规划等待；
- motion 结果到达和 coordinator 消费；
- Runtime 仲裁、账本与 trace；
- 有 worker 但当前帧没有 motion 工作。

先保存未优化 profile。只有 `fixed_route.verifier` 明确是 production 的主要热点，才进入 RH4。

## 7. RH4：唯一一轮语义不变优化

允许：

- 按帧建立并复用验证 context／query cache；
- 复用 geometry、material、support 查询；
- 同一候选复用完整 neutral-tail 结果；
- 一次稳定合并世界依赖；
- 减少重复对象和容器复制。

禁止：

- 减少或重排十八候选；
- 删除 `0／1／2 tick` 分支；
- 缩短 `30 tick` 尾迹；
- 放宽 UNKNOWN、碰撞、危险、支撑、姿态或 completion；
- 按场景特判；
- 把验证移到后台后只统计提交时间。

实现后先跑聚焦正确性和变异检查。任一业务签名不同即撤销该优化，不进入正式性能门槛。

本阶段只允许这一轮。若仍超时，记录 profile 和失败，另写决定；不能继续叠加第二轮局部优化。

## 8. RH5：Windows 正式门槛

在干净提交上运行和 TP3 同口径的非 oracle 正式链。报告总 production、所有分段、unattributed、backend 和 full。

硬门槛：

| 指标 | 门槛 |
|---|---:|
| production P95 | `≤8 ms` |
| production P99 | `≤15 ms` |
| production max | `<30 ms` |
| full max | `<50 ms` |
| control-thread search calls | `0` |
| deadline miss | `0` |
| 安全事件／伤害超额／无人负责输入／来源泄漏 | `0` |

同时复跑 RH0 行为签名、worker 生命周期矩阵、FIFO 阻塞矩阵和候选语义变异检查。

任一项失败就停止。不能运行 F2-TP TP4 或 oracle。

## 9. 完成后去向

RH1—RH5 全部通过后：

1. F2-RH 关闭；
2. 返回 F2-TP TP4，按既有公式实施有界前移触发；
3. TP4 通过后，才依次运行三项 oracle、remaining9 和 remaining42。

F2-RH 通过不等于 F2-TP 或 F2-TS 通过。

## 10. fail-fast

出现下面任一情况立即停止：

- Session 仍创建、预热或关闭正式 worker；
- worker 死亡后仍报告 READY；
- cancel 队列满被静默忽略；
- 结果发布前没有最后一次取消／作废检查；
- 旧 revision 或动作族之间出现无界阻塞；
- profile 计时改变业务结果；
- profile 没指向 verifier 却仍修改 verifier；
- 优化减少候选、前缀、尾迹或安全检查；
- 第一轮优化后性能仍失败却继续第二轮；
- 在本阶段运行 TP4、oracle 或 Fabric。

## 11. 实施结果

RH1—RH3 已完成。Runtime 持有并最终关闭唯一 worker；Driver 在注册输入源前检查健康并注入借用端口；Session 删除了懒创建、所有权转移和关闭路径。READY 后死亡会转成 `DEAD`。cancel 使用类型化结果，背压保留有界待发记录，本地退休立即剥夺旧结果资格。worker 在发布前再次处理 control；air 与 ground 都会读取 cooperative stop。

生命周期与相关正式链聚焦回归为 `98/98`。验证器与计时组合检查为 `28/28`。固定 FIFO 门槛覆盖真实 air solve／revalidation 的中途停止和后续 ground 启动；独立复审没有未关闭 P0／P1／P2。五个冻结的非 oracle 场景为 `5/5` 完成，零安全事件。

只读 profile 的稳定 P95 主要开销为：

- `fixed_route.verifier`：`4.5559 ms`；
- `ingest`：`2.7037 ms`；
- `motion_coordinator` exclusive：`1.8583 ms`；
- 未归因：`0.3286 ms`。

唯一一轮授权优化只给同一验证帧复用不可变 shape query。十八候选、`0／1／2 tick`、30 tick 尾迹、UNKNOWN、支撑、completion 和依赖语义未改。优化后正式门槛结果为：

| 指标 | 结果 | 判定 |
|---|---:|---|
| production P95 | `10.2680 ms` | 失败 |
| production P99 | `23.9535 ms` | 失败 |
| production max | `24.9818 ms` | 通过 |
| full max | `45.3178 ms` | 通过 |

因此 F2-RH 未通过，停止于 RH5。没有运行 TP4、oracle、remaining9／42、F2-GP、大集合、D061 或 Fabric。紧凑证据位于 `evidence/motion_navigation/F2RH-runtime-worker-hotpath-v1/`。

仓库完整入口运行 `1812` 项，仍有 `58` 项失败和 `2` 项错误；共享场景哈希不变。公开导出指纹和既有开放缺陷保持原样，不能把聚焦门槛通过写成仓库全绿。

## 12. D089 后续安排

RH5 原表格和“阶段未通过”保留为历史验收事实。后续排期按风险重新解释：

- worker 生命周期、取消、发布、身体责任、deadline 和安全事件继续作为硬门槛；本批 full-frame 50 ms 结果只证明五个冻结场景；
- production P95/P99 的 `8/15 ms` 是容量目标，本轮未达记为性能债；
- production max 的原容量上限已经通过；
- 不继续在 F2-RH 内追加优化，也不因这项债务阻塞无关产品工作；
- 后续工作若继续增加同一控制热路径的负载，必须重新检查这项债务；
- TP4、oracle 和 F2-TS 不自动恢复。继续前必须由用户明确授权，并说明如何重新核对原 `8/15 ms` 复合门槛和同一热路径的性能债。
