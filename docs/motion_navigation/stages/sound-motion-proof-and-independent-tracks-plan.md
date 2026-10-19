# 可靠运动证明与独立轨道实施计划

日期：2026-10-13。状态：**T1 在 A3 性能门槛失败并停止；T2 在 T2-2 失败并停止；T3 失败并停止。**

**目标：** 用有代码依据的最小省略重新开始证明瘦身，同时独立验证 receiver／局部快照和真实 Fabric tick 顺序。三条轨道全部通过后，才讨论连续两跳。

**依据：** [D105](../decisions/0105-restart-sound-proof-slimming-and-decouple-tracks.md)、[可靠稀疏运动证明](../architecture/sound-sparse-motion-proof-v1.md)、[D104 失败记录](../acceptance/key-boundary-motion-proof-and-continuity.md)。

## 全局约束

- Windows 是正式性能平台；Linux 只作补充检查。
- D098、D101、D102／D103 和 D104 的原证据与失败不改写。
- 少算前先给出代码等价关系或保守物理上界；没有计算不能默认安全。
- 每条轨道使用独立 index、源码哈希、命令和止损结果。
- T1、T2、T3 可以独立推进。一个轨道失败，不伪造另一个轨道的失败或通过。
- 不建立第二套 worker、receiver、registry、账本或身体 owner。
- UNKNOWN、伤害、依赖、输入窗口和身体释放条件不降低。
- candidate、executing、safe-tail 三种责任用途始终保留。

## T1：可靠的证明瘦身

### T1-A0：冻结输入和反例

只增加或更新隔离检查、量尺和证据工具，不修改 `mc2p` 正式链。

1. 冻结 `measurement._set_d(18)` 的 18 个正式 D 样本及旧扫描器结果。
2. 单列 `_set_a(4)` 和 `_set_c(6)` 的 10 个补充样本。
3. 保留 D104 的未采样 `UNSAFE→SAFE` 反例。
4. 新增至少一个包含两个分离风险区间的旧扫描器样本。
5. 冻结 UNKNOWN、UNSUPPORTED、依赖顺序、空中转着地和尾迹变异样本。

**通过条件：** 两次旧扫描结果逐项一致；18 个正式样本和 10 个补充样本使用不同清单和分母；反例确实能否定 D104 候选；没有正式行为修改。

**止损：** oracle、样本身份或查询顺序不能稳定重算时停止 T1，不实现方案 A。

### T1-A1：实现方案 A

在 D101 隔离路径实现以下唯一省略：风险已打开，且当前主轨迹边界明确满足 `on_ground == false` 时，不生成该边界的假想停车尾迹。

所有着地边界保持旧顺序：先运行原 tail，再由旧 `_risks` 在原条件下查询支撑。首轮不实现“已知不安全支撑跳过”，不按输入变化、动作名称、固定 tick 或相邻采样点选择边界。

输出保留全部风险区间。多个风险区间必须逐个输出，不能合并或返回不支持。实际保存的安全尾迹继续包含可执行输入、端点、伤害、依赖和长度上限。

**通过条件：** D104 反例不再漏报；正式 18 个样本的风险开闭与旧扫描器逐项一致；补充 10 个样本单列一致；多风险区间完整保留；主轨迹和输入不变。

**止损：** 任一风险开闭、关键边界、UNKNOWN、UNSUPPORTED、依赖顺序或尾迹可执行性不一致时停止 T1。不能通过增加动作特判继续。

### T1-A2：完整性和否决权审查

独立审查以下两项不同结论：

1. 风险打开后的空中边界不能按旧 `_risks` 关闭风险，因此省略该假想尾迹不会改变风险区间；
2. 旧扫描器可能被这条不会执行的假想尾迹否决，方案 A 有意取消这部分否决权。

结果只允许写“风险开闭等价”和“有意缩小空中假想尾迹否决权”。不得写全结果等价。所有被省略位置必须可由主轨迹 `on_ground == false` 复算。

**通过条件：** 代码关系、反例和属性检查共同覆盖上述两项；变异空中判定、着地查询顺序、多个风险区间或 UNKNOWN 处理时检查会失败。

**止损：** 无法证明某类省略只发生在风险已打开的空中边界时，方案 A 失败。不得先跑性能来决定是否接受。

### T1-A3：Windows M0

只有 T1-A2 通过后，才使用 D101 原命令和口径运行 Windows M0：204 场、4,710 个决定，完整决定 P95≤8 ms、P99≤15 ms、最大值<30 ms。不得减少样本或改变 nearest-rank 口径。

**通过条件：** 安全分类、输入、风险区间、关键边界和可执行尾迹通过；安全违规为 0；旧成功退步为 0；三个性能门槛全部达到。

**止损：** 完整性或安全失败时结束 T1。若只有性能失败，保存原始结果并停止本轮；方案 B 只作为下一份决定的候选，不在本计划实施。

### T1-B：未授权的未来选项

本阶段不编写、不运行 T1-B。未来若授权，它只能单向证明 `SAFE`，不适用时回退方案 A，并生成可执行安全尾迹。已知不安全支撑跳过也必须另作证明，不能混入 T1-A 的限定修正。

## T2：信任边界和局部快照

T2 不以 T1-A 的算法通过为开始条件。每个小步单独验证并止损。

### T2-0：冻结 receiver 和生命周期基线

冻结消息大小、receiver 耗时、快照格数、registry 容量、关闭后资源数，以及 request、generation、owner、输入、规则、窗口、伤害、世界会话、依赖和发布竞态的拒绝矩阵。

**止损：** 基线不能稳定复算，或现有身体责任无法区分 candidate、executing、safe-tail 时停止 T2。

### T2-1：收窄本地 receiver 的重复审计

同一发行物启动的本地 worker 继续使用现有可信边界。receiver 保留消息类型、schema、大小、摘要和全部父侧权威事实检查。递归对象图复核、构造器重放和完整规范重算只有在构造端测试与离线审计已有等价覆盖时才可移出热路径。

**通过条件：** 拒绝矩阵保持通过；损坏、超限、错身份和竞态仍拒绝；receiver 工作量下降；外部或插件 worker 不复用这条可信边界。

**止损：** 任一权威事实只能依赖被删除的递归检查才能发现时停止并回退该小步。

### T2-2：验证 detached 局部快照

父侧按动作模板的最大包络生成独立局部值。越界查询返回 `NEEDS_WORLD`；返回结果只携带实际依赖，执行前仍与正式世界核对。

**通过条件：** 大世界与等价局部快照结果一致；边界外 UNKNOWN 不当空气；依赖变化使结果过期；快照大小和查询有明确上限。

**止损：** 出现结果差异、漏依赖或越界默认安全时停止并回退该小步。

### T2-3：等待 T1 格式稳定后删除重复载荷

只有 T1 的正式证明格式已经通过并冻结，才允许删除完整 bundle、重复规范 bytes、snapshot lease、返回队列、专用返回线程和 registry 中的重复载荷。

candidate、executing、safe-tail 永不作为重复载荷删除。close 仍只能撤销尚未生效的 candidate；executing 和 safe-tail 必须在身体责任解决后释放。

**通过条件：** 身份、安全、容量和关闭门槛全部通过；worker、receiver、feeder 和 registry 在正式关闭后归零；正式提交到首次可用的时间线完整。

**止损：** T1 格式未稳定时保持未运行。任何责任丢失、提前释放、资源泄漏或正式延迟回归都停止 T2，不影响已通过的 T2-1／T2-2 事实。

## T3：只读 F-TICK

T3 独立复用 B10 Runtime＋Fabric 跨隙路径，固定 seed 21001，四方向各 10 次。它不依赖 D102 G3，不修改客户端调度和输入协议。

每次记录并联结：客户端 observation、Python observation 接收、决定发送、客户端 frame 接收、输入采样、movement tick 应用、首条正式回执。跨进程时间戳不直接相减，只用 episode、generation、observation sequence 和 request sequence 关联。

结果分类为 `NEXT_MOVEMENT_TICK`、`LATER_MOVEMENT_TICK` 或 `INCOMPLETE`。缺失、重复、错身份、缓存溢出和未封存都属于 `INCOMPLETE`。

**通过条件：** 40／40 时间线可复算；四方向各 10 次；B10 输入、回执、安全落地和来源释放不退步；如实报告 next／later 分布。

**止损：** 任一时间线不完整或 B10 回归时停止 T3。只保存只读结果，不在同一批次修客户端或追加重跑。

## 三轨汇合条件

连续两跳必须同时引用：

1. T1 通过的方案 A index、源码哈希和 Windows M0；
2. T2 完整通过的 receiver、局部快照、载荷和生命周期 index；
3. T3 的 40／40 F-TICK index。

缺少任一项都不创建连续两跳 predecessor。满足三项后仍需另立阶段，先跑单跳对照，再跑有安全停车点的两跳，最后才测试落地即起跳。本计划不授权这些运行。

## 证据目录

根目录使用 `evidence/motion_navigation/sound-motion-proof-d105-v1/`，下设 `t1/`、`t2/`、`t3/`。每条轨道保存自己的 index、源码哈希、命令、正式平台、原始结果和失败指针。未运行步骤不创建通过标记；一个轨道的 index 不能声明另一个轨道已通过。

## 实施结果

### T1：A0—A2 通过，A3 性能失败

T1-A0、T1-A1、T1-A2 已通过独立审查。正式 D 样本为 18／18，补充 A 和 C 分别为 4／4、6／6。D104 的短暂 `UNSAFE→SAFE` 反例得到风险区间 `(4,5,6)`；两个分离风险区间、着地 tail 后查询支撑、UNKNOWN／UNSUPPORTED 和四项变异检查也通过。签署范围仍只是风险开闭等价，以及有意缩小空中假想尾迹的否决权。

Windows T1-A3 M0 按原口径完成 204 场、4,710 个决定。200／200 正例完成，4／4 负例分类正确，安全违规为 0。结果如下：

| 指标 | 结果 | 门槛 | 判断 |
|---|---:|---:|---|
| P50 | 0.0722 ms | 记录 | — |
| P95 | 0.1317 ms | ≤8 ms | 通过 |
| P99 | 23.7115 ms | ≤15 ms | **失败** |
| 最大值 | 30.4564 ms | <30 ms | **失败** |

因此 T1 在 A3 **FAILED／STOPPED**。方案 B 未实现，正式 worker 迁移未运行。T1 证据位于：

- `evidence/motion_navigation/sound-motion-proof-d105-v1/t1/index.json`
- `evidence/motion_navigation/sound-motion-proof-d105-v1/t1/verification.json`
- `evidence/motion_navigation/sound-motion-proof-d105-v1/t1/m0/`

### T2：T2-0／T2-1 通过，T2-2 停止

T2-0 冻结了 774,509 bytes 消息、6,496 格快照、拒绝矩阵、三种身体责任和关闭资源基线，94 项基线检查通过。

T2-1 的可信本地 receiver 保留父侧身份、窗口、伤害、依赖和发布竞态检查。10 个样本的完整审计中位数为 1.218995350 s，可信路径中位数为 16.950750 ms，下降 98.609449%。真实 spawn 试验的三段时间为：

| 时间段 | 结果 |
|---|---:|
| 提交到父侧收到 worker 结果 | 300.444 ms |
| 父侧收到结果到 receiver 发布 | 29.624 ms |
| receiver 发布到首次消费 | 0.353 ms |

真实 spawn 结束后 candidate、executing、safe-tail、lease、worker、receiver、returner、feeder、队列和未完成作业全部为 0。

T2-2 检查发现，现有请求只带已选轨迹状态、已选世界依赖和固定两格余量。动作规格没有最大可达包络字段，请求也没有动作模板或落地区域。父侧因此无法从动作模板推出最终 detached 快照边界。补齐这一点需要实现尚不存在的多模板保守物理包络，超出本阶段范围。T2-2 **FAILED／STOPPED**，没有生产修改；T2-3 未运行。

T2 证据位于：

- `evidence/motion_navigation/sound-motion-proof-d105-v1/t2/index.json`
- `evidence/motion_navigation/sound-motion-proof-d105-v1/t2/t2-0-baseline.json`
- `evidence/motion_navigation/sound-motion-proof-d105-v1/t2/t2-1-result.json`
- `evidence/motion_navigation/sound-motion-proof-d105-v1/t2/t2-2-stop.json`
- `evidence/motion_navigation/sound-motion-proof-d105-v1/t2/receiver-timing/`
- `evidence/motion_navigation/sound-motion-proof-d105-v1/t2/spawn-probe/`

### T3：单次 Fabric 批次失败

T3 按固定 seed 21001 运行一次，没有修正后重跑。预定四方向各 10 次，共 40 个时间线。结果为 22 个 `NEXT_MOVEMENT_TICK`、0 个 `LATER_MOVEMENT_TICK`、18 个 `INCOMPLETE`。

南向 10 次、东向 10 次和北向前 2 次得到完整 next-tick 时间线。第 23 个预定试次在 command 16 进入 `RECOVERING`，原因为 `coast_input_unobserved_retain_landing`。B10 随即失败，后续北向 8 次和西向 10 次没有产生试次记录，ordered source 也没有正常注销。sidecar 正常封存 1,818 个事件，丢弃为 0。

T3 **FAILED／STOPPED**。正式证据位于 `evidence/motion_navigation/sound-motion-proof-d105-v1/t3/`；原始 Fabric 批次位于 `artifacts/fabric-deployment/20261013T131224340162Z-90a78bfa/`。

### 收尾

三条轨道按各自门槛独立止损。T1 的完整性通过不抵消性能失败；T2-0／T2-1 的通过不抵消 T2-2；22 个完整 next-tick 时间线也不抵消 T3 的 B10 回归和 18 个不完整结果。

三轨没有全部通过。连续两跳、零等待交接、方案 B 和正式 worker 迁移均未授权、未运行。D104 及更早失败记录保持不变。
