# D105 成本、量尺与 tick 归因修正计划

日期：2026-10-14。状态：**部分完成并停止。** 决定见 [D106](../decisions/0106-repair-d105-cost-measurement-and-tick-diagnostics.md)。

## 目标

在不改变运动证明内容和安全门槛的前提下，去掉重复摘要；把调用计数与性能计时拆开；按 UNKNOWN 保守失败规则重新验证局部快照；查清 T3 的 B10 中止原因。

连续两跳、方案 B、T2-3 载荷删除和新动作不在本计划范围内。

## R0：冻结修正前基线

1. 引用 D105 的 T1、T2、T3 原 index，不复制或改写原证据。
2. 固定当前源码哈希、Windows M0 结果、T2 spawn 时间线和 T3 原始批次。
3. 为四条修正轨道建立独立目录和状态。

**通过条件：** 所有旧证据可重算；新目录没有伪造通过标记。

## R1：T1-R 摘要只算一次

状态：**FAILED／STOPPED。** 功能、安全和摘要一致性通过；Windows M0 P99 为 16.5732 ms，未达到 15 ms 门槛。方案 B 不实施。

主要文件：

- `experiments/motion_navigation/trajectory_proto/physics.py`
- `experiments/motion_navigation/trajectory_proto/sound_sparse_proof.py`
- `tests/motion_nav/test_sound_sparse_motion_proof_d105.py`
- `scripts/verify_sound_sparse_motion_proof_d105.py`
- `scripts/measure_sound_sparse_motion_d105.py`

步骤：

1. 先写 RED：一次正常扫描只能执行一次完整证明摘要；公共验证入口遇到篡改仍拒绝。
2. 把结构检查与外部摘要检查拆开。扫描器计算摘要后调用结构检查；公共验证器仍检查摘要。
3. 缓存 dataclass 类型的字段表，证明规范字节和 SHA-256 必须与 D105 完全一致。
4. 复跑 D、A、C 样本、短暂风险反例、多风险区间和变异检查。
5. 在 Windows 上按原 204 场、4,710 个决定运行 M0。

**门槛：** 200／200 正例、4／4 负例、安全违规 0；P95≤8 ms、P99≤15 ms、最大值<30 ms；摘要逐字节不变。

**止损：** 任何结构、安全或摘要差异立即停止 R1；只有性能失败时保存结果并停止，不进入方案 B。

## R2：G2 量尺拆分

状态：**PASS。** 调用计数和无 profile 性能计时已经分成两种正式模式，配对业务结果一致。

主要文件：

- `tests/sim/d102_prepared_motion.py`
- 相关 D102／G2 量尺测试和脚本

步骤：

1. 写 RED：性能批次启用 `sys.setprofile` 时必须拒绝；计数批次不得声明生产耗时。
2. 计数模式只输出调用数和调用来源。
3. 性能模式关闭 profile，只保留显式阶段计时。
4. 使用相同 seed 运行配对场景，核对终态、输入和安全结果一致。

**门槛：** 两种模式的业务结果逐项一致；性能记录明确 `profile_enabled=false`；调用记录明确 `timing_valid=false`。

## R3：T2-2-R 局部快照

状态：**PASS。** 现有生产链已经以 UNKNOWN 和正式依赖复核保守失败；本轮只补验证，没有新增包络算法或生产代码。

主要文件：

- `mc2p/motion_nav/world_snapshot.py`
- `mc2p/motion_nav/motion_worker.py`
- `mc2p/motion_nav/prepared_motion_validation.py`
- 对应 receiver、快照和正式链测试

步骤：

1. 补可信投影与完整构造逐字段等价测试。
2. 补快照边界反例：边界外空气、实体方块、UNKNOWN 和依赖变化都不能产生假 SAFE。
3. 从已选状态、轨迹与现有有界余量构造局部 bounds；越界统一返回需要信息。
4. 比较完整快照与局部快照：查询都在局部范围内时结果相同；超出时只允许有类型的需要信息。
5. 分别记录快照封存、worker 接收、求解、规范化、序列化、receiver 发布和首次消费时间。

**安全门槛：** 假 SAFE 为 0；实际依赖漏记为 0；依赖变化后的旧证明接纳为 0；关闭后资源归零。

**效率指标：** 快照格数、首次命中率、补取次数、请求到首次消费总时长。首轮只报告，不以“最大模板包络”作为通过条件。

**止损：** 任一越界查询被当作空气、任何实际依赖未进入证明，或正式世界变化没有使证明过期时停止 R3。

## R4：T3-R 离线归因和条件式实机

状态：**FAILED／STOPPED。** R4-A 已定位 B10 手工探针的第二本账问题；两轮限定修正后，第二次正式批次的 40／40 F-TICK 时间线均完整且全部为下一 movement tick，来源也正常注销。批次随后在 40 个 validation 之后的旧 B10 R4 移动入口夹具失败：东向低速带 `(0.5, 1.0)` 的准备器一次观察跨两个 movement tick 后错过窄入口窗口，80 tick 内没有恢复。该路径位于动作求解前，与 Runtime 账本修正无关；身体全程安全。两轮额度已经用完，不再追加第三轮。

### R4-A：现有 trace

读取 `artifacts/fabric-deployment/20261013T131224340162Z-90a78bfa/`，输出：

- 第 23 个试次缺失的 movement tick；
- 客户端采样、回执、Python 输入账本和 coast 检查各自看到的范围；
- ordered source 未注销前最后一个 owner 和终止路径。

### R4-B：无 sidecar 对照

只有 R4-A 不能确定原因时，才运行一次固定 seed、四方向各 10 次的 B10 诊断批次。关闭 F-TICK sidecar，其余代码、场景和输入不变。该结果不计入 T3。

### R4-C：限定修复

- B10 对照失败：先写正式链 RED，再修输入采样／账本或清理路径；
- B10 对照通过：sidecar 改为有界内存缓冲，不在 movement tick 内 flush；
- 每条原因最多两轮限定修正。

### R4-D：正式 T3

原因修复后，重新运行四方向各 10 次。40／40 时间线必须完整；输入、回执、安全落地和来源释放全部通过。next／later 只如实统计，不把 later 自动算作失败。

**止损：** 无法在两轮内得到稳定根因或修复时停止 R4，不继续增加诊断分支。

## 汇合结果

R2、R3 通过；R1 和 R4 未通过。汇合条件没有满足，不另写连续两跳计划。下一阶段如继续，应先单独处理 B10 R4 带速接近，并拆清准备链中快照、worker、序列化、receiver 和首次消费的时间；不能把本轮的 40／40 F-TICK 写成整体通过。
