# 可靠运动证明与独立轨道验收

日期：2026-10-13。当前状态：**T1 在 A3 性能门槛失败并停止；T2 在 T2-2 失败并停止；T3 失败并停止。**

## 1. 历史基线

- D104 K0 的 18 个 D 样本可重复。
- D104 K1 因未采样边界可隐藏 `UNSAFE→SAFE` 风险而失败，候选已撤回。
- D104 的 Windows M0 是无效候选诊断，不构成 K2。
- D098 P3、D101 M0、D102／D103 G2 的失败保持不变。

本页不追加或改写上述证据。D105 使用新的证据根目录和三个独立 index。

## 2. 共同门槛

- 少算前必须有代码等价关系或保守物理上界；未计算不能记为安全。
- UNKNOWN 不当空气；UNSUPPORTED 使用独立类型。
- 伤害不超过任务额度，依赖变化使旧结果失效。
- 旧 generation、owner、输入、窗口、规则、世界会话或依赖错配均拒绝。
- candidate 可以取消；executing 和 safe-tail 只有在责任解决后释放。
- 无界搜索和无界结果为 0。
- 正式 Windows 分位数从原始样本按 nearest-rank 重算。

## 3. T1 验收

### 3.1 样本边界

| 集合 | 数量 | 用途 | 计入口径 |
|---|---:|---|---|
| D | 18 | 正式风险开闭与性能前置 | 正式分母 |
| A | 4 | 冻结输入补充检查 | 单列，不并入 D |
| C | 6 | 冻结输入补充检查 | 单列，不并入 D |

A／C 共 10 个补充样本。任何报告都必须分别列出 D、A、C 的通过数和失败项，不能写成“28／28”代替正式 18 个 D 样本。

### 3.2 方案 A 的允许差异

唯一允许省略的是：风险已经打开，且当前主轨迹边界明确满足 `on_ground == false` 时，不运行该边界的假想停车尾迹。

着地边界必须先运行旧 tail，再按旧 `_risks` 的条件查询支撑。提前支撑查询、已知不安全支撑跳过、按输入变化采样、按动作名或固定 tick 采样都不在允许范围。

允许的结论是“风险开闭等价”。以下内容必须与旧扫描器逐项一致：

- 风险区间数量和顺序；
- 每个区间的最后可放弃、承诺和恢复边界；
- 多个分离风险区间；
- 主轨迹、输入和时序分支；
- 着地查询顺序、UNKNOWN、UNSUPPORTED 和实际依赖；
- 实际保存安全尾迹的可执行输入、端点、伤害和长度上限。

旧扫描器可能因被省略的空中假想尾迹拒绝候选。该差异要单列为“缩小假想尾迹否决权”，不能记成全结果等价。被省略尾迹不得产生安全事实或恢复输入。

### 3.3 完整性反例

至少包含：

- D104 的单边界短暂 `UNSAFE→SAFE`；
- 两个分离风险区间；
- 风险打开后连续空中边界；
- 空中转着地并恢复安全支撑；
- 着地尾迹安全但当前支撑不安全；
- 主轨迹、尾迹和支撑查询中的 UNKNOWN；
- 当前能力无法处理的 UNSUPPORTED；
- 空中判定、查询顺序和依赖收集变异。

任一反例失败即停止 T1，不运行 Windows M0。

正式审查结果为 D 18／18、A 4／4、C 6／6。D104 的短暂 `UNSAFE→SAFE` 反例得到预期风险区间 `(4,5,6)`；两个分离风险区间分别得到 `(2,3,4)` 和 `(6,7,8)`。着地查询顺序、UNKNOWN／UNSUPPORTED 矩阵和四项变异检查全部通过。T1-A0、T1-A1、T1-A2 因此签署为通过。证据：`evidence/motion_navigation/sound-motion-proof-d105-v1/t1/verification.json`。

### 3.4 Windows M0

完整性通过后，按 D101 原口径运行 204 场、4,710 个决定。

| 指标 | 门槛 |
|---|---:|
| 正例 | 200／200 |
| 四类负例 | 4／4 |
| 安全违规 | 0 |
| 旧成功退步 | 0 |
| 完整决定 P95 | ≤8 ms |
| 完整决定 P99 | ≤15 ms |
| 完整决定最大值 | <30 ms |

功能、安全或完整性失败时，T1 结束。只有这些项目全部通过、单独的性能门槛失败时，才记录“方案 A 安全但性能不足”。方案 B 仍保持未实施，不能在同一阶段继续。

正式结果为 204／204 场完成、4,710 个决定。200／200 正例完成，4／4 负例分类正确，安全违规为 0。T1-A0、T1-A1、T1-A2 已先通过独立审查，因此这次 M0 是正式 T1-A3，不是无效候选诊断。

| 指标 | 结果 | 门槛 | 判断 |
|---|---:|---:|---|
| P50 | 0.0722 ms | 记录 | — |
| P95 | 0.1317 ms | ≤8 ms | 通过 |
| P99 | 23.7115 ms | ≤15 ms | **失败** |
| 最大值 | 30.4564 ms | <30 ms | **失败** |

T1 因 P99 和最大值失败而 **FAILED／STOPPED**。安全与功能结果保留，但不能签署 T1 整体通过。证据为 `evidence/motion_navigation/sound-motion-proof-d105-v1/t1/index.json`、`t1/verification.json` 和 `t1/m0/`。

### 3.5 方案 B

当前状态固定为**未授权、未实现、未运行**。T1-A3 只因性能失败，满足“可以另行讨论方案 B”的条件，但本阶段没有因此自动取得实施授权。未来若另行授权，验收必须同时证明：

1. 只能单向证明 `SAFE`，不适用时回退方案 A；
2. 每个 SAFE 结论都带可执行安全尾迹，不能只有分类标签。

## 4. T2 验收

T2-1／T2-2 可以在 T1 未通过时独立取证。T1 正式格式稳定前，T2-3 保持未运行。

| 范围 | 必须保留的事实 | 失败条件 |
|---|---|---|
| receiver | 消息边界、摘要、request、generation、owner、输入、规则、窗口、伤害、会话、依赖、发布竞态 | 任一错配被接纳 |
| 局部快照 | 最大包络、越界 `NEEDS_WORLD`、实际依赖、正式世界复核 | 缺格当空气、漏依赖、结果不一致 |
| registry／关闭 | candidate、executing、safe-tail 分开；关闭后资源归零 | 责任提前释放、泄漏、容量超限 |

完整 bundle、规范 bytes、snapshot lease、返回队列、专用返回线程和 registry 重复载荷只有在 T1 格式稳定后才能删除。candidate、executing、safe-tail 不属于可删除项。

T2 任一小步失败时只停止 T2 后续，不改写已经通过的 T1 或 T3。若 T2-3 未运行，T2 整体不能作为连续两跳的通过前置。

### 4.1 T2-0 结果

T2-0 **PASS**。基线包含 774,509 bytes 消息、673,452 bytes 规范 bundle、6,496 格／1,514,939 bytes 规范快照，以及 request、generation、owner、路线／动作、账本、世界、输入、规则、窗口、伤害、依赖、取消和发布竞态拒绝矩阵。94 项基线检查通过。candidate、executing、safe-tail 三种用途可区分，关闭后全部资源为 0。

证据：`evidence/motion_navigation/sound-motion-proof-d105-v1/t2/t2-0-baseline.json`。

### 4.2 T2-1 结果

T2-1 **PASS**。10 个 Windows 样本的 receiver 结果如下：

| 指标 | 结果 |
|---|---:|
| 完整审计中位数 | 1.218995350 s |
| 可信本地 receiver 中位数 | 16.950750 ms |
| 中位数下降 | 98.609449% |

真实 spawn 时间线完整。提交到父侧收到 worker 结果为 300.444 ms，父侧收到结果到 receiver 发布为 29.624 ms，发布到首次消费为 0.353 ms。试验结束后 candidate、executing、safe-tail、activation、lease、bytes、worker、receiver、returner、feeder、队列和未完成作业全部为 0。

证据：`evidence/motion_navigation/sound-motion-proof-d105-v1/t2/t2-1-result.json`、`t2/receiver-timing/` 和 `t2/spawn-probe/`。

### 4.3 T2-2／T2-3 结果

T2-2 **FAILED／STOPPED**。现有请求没有动作模板最大可达包络，也没有携带动作模板或落地区域。当前“已选轨迹状态＋已选依赖＋固定两格余量”不能证明最终 detached 快照边界。建立多模板保守物理包络属于新的算法，未在本阶段实现。T2-2 没有生产修改。

T2-3 未运行。完整 bundle、规范 bytes、snapshot lease、返回队列、专用返回线程和 registry 重复载荷均未按 T2-3 删除。证据：`evidence/motion_navigation/sound-motion-proof-d105-v1/t2/t2-2-stop.json` 和 `t2/index.json`。

## 5. T3 验收

使用 B10 Runtime＋Fabric 跨隙路径，seed 21001，四方向各 10 次，共 40 次。只读记录以下时间线：

`observation → Python 接收 → 决定发送 → 客户端 frame → 输入采样 → movement tick 应用 → 首条回执`

跨进程墙钟不相减。episode、generation、observation sequence 和 request sequence 必须能把同一试次完整联结。

| 分类 | 含义 | 判定 |
|---|---|---|
| `NEXT_MOVEMENT_TICK` | 首命令落在下一 movement tick | 完整事实 |
| `LATER_MOVEMENT_TICK` | 首命令落在更晚 movement tick | 完整事实，如实计数 |
| `INCOMPLETE` | 缺失、重复、错身份、溢出或未封存 | T3 失败 |

通过要求为 40／40 时间线完整，B10 输入、回执、安全落地和来源释放不退步。T3 不修改调度，不证明绝对 tick 预排程，也不证明连续两跳。

### 5.1 正式结果

T3 只运行一次正式 Fabric 批次，没有修改后重跑。

| 分类 | 数量 |
|---|---:|
| `NEXT_MOVEMENT_TICK` | 22 |
| `LATER_MOVEMENT_TICK` | 0 |
| `INCOMPLETE` | 18 |

南向 10 次、东向 10 次和北向前 2 次完整，全部落在下一 movement tick。第 23 个预定试次在 command 16 触发 `coast_input_unobserved_retain_landing`，B10 进入 `RECOVERING` 并失败。后续 18 个预定试次没有形成完整时间线。ordered source 没有正常注销，因此同时触发来源生命周期失败。

sidecar 已封存，包含 1,818 个事件，丢弃为 0。封存成功只证明已写事件完整保存，不能补足缺失的 18 个试次，也不能抵消 B10 失败。T3 **FAILED／STOPPED**。

证据：`evidence/motion_navigation/sound-motion-proof-d105-v1/t3/index.json`、`t3/summary.json` 和 `t3/timelines.jsonl`。原始批次为 `artifacts/fabric-deployment/20261013T131224340162Z-90a78bfa/`。

## 6. 三轨状态表

| 轨道 | 当前状态 | 通过后能证明什么 | 不能证明什么 |
|---|---|---|---|
| T1 | **FAILED／STOPPED** | A0—A2 完整性通过；A3 安全功能通过 | P99／最大值未过门槛；正式 worker 未迁移 |
| T2 | **PARTIAL／STOPPED** | T2-0、T2-1 通过 | T2-2 失败；T2-3 未运行 |
| T3 | **FAILED／STOPPED** | 22 个 next-tick 时间线 | 18 个不完整；B10 和来源注销失败 |

三条轨道按各自门槛独立止损。T1 的完整性结果不被 T2／T3 失败改写；T2-0／T2-1 的通过也不被 T1 或 T3 改写。三行没有全部通过，因此不能建立连续两跳的新阶段。连续两跳、零等待交接、方案 B、正式 worker 迁移和 Fabric 行为修改均未授权、未运行。

## 7. 证据要求

证据根目录为 `evidence/motion_navigation/sound-motion-proof-d105-v1/`。T1、T2、T3 各自保存 index、源码哈希、命令、平台、原始数据、重算结果和失败指针。失败后不创建后续通过目录；补充样本、诊断数据和正式结果必须分开。

现行根索引分别为 `t1/index.json`、`t2/index.json` 和 `t3/index.json`。D104 及更早阶段的原证据没有改写。
