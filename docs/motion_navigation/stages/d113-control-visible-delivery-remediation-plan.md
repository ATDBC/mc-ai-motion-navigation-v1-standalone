# D113 控制可见延迟与交付抖动整改计划

日期：2026-10-16。决定见 [D113](../decisions/0113-unify-control-visible-delivery-and-repair-jitter.md)。

当前状态：**FAILED／STOPPED。** 已完成授权实现、本地门禁和单场定向；唯一正式批次在首个 10-tick 场景等待观察时超时，未完成矩阵。没有追加修正或重跑。

定向场景的两个作业实际提交到首次控制可见均为 5 tick，任务完成、零伤害、来源显式释放。正式批次完成 baseline 20／20、2-tick 8／8、5-tick 8／8；8 个 late1 明确记录为后端不支持。首个 10-tick 作业只记录到 submit／receiver／publish，尚未首次控制可见，不能算作精确 10-tick 注入或安全有界终态。其余 7 个 10-tick、4 个 never-return 和后续 R4 没有运行。

已记录的 755 个正式生产协调帧完整 received→dispatch 的 P95／P99／最大值为 4.1105／4.4411／4.6834 ms，关联问题为 0。这只证明已记录部分的性能，不关闭完整交付门槛。当前缺少安全终态的 10-tick 场景和未运行的 never-return 仍阻止签署。D110-SOLVE-01 保持打开。

Windows 源树正逆各 191 模块、2387 项，独立公开快照正逆各 190 模块、2363 项，均零失败、错误、超时、跳过和清理失败。源树 Java 13／13、快照纯 Java 3／3，矩阵前后 verify 各 2386 文件。正式结果、环境中断和原始证据边界见[验收](../acceptance/d113-control-visible-delivery-remediation.md)及 `evidence/motion_navigation/d113-control-visible-v1/`。

## 1. 交付目标

本阶段把 D112 的延迟和性能量尺统一到生产控制路径，修复一 tick 交付抖动和 jump-up 中性停稳误拒绝，再用一个定向 Fabric 场景决定是否进入唯一正式批次。

## 2. 修改范围

预计修改：

- `mc2p/motion_nav/motion_coordination.py`
- `mc2p/motion_nav/motion_solver.py`
- `mc2p/motion_nav/motion_worker.py`
- `scripts/analyze_d112_delivery_baseline.py` 或新的 D113 分析器
- `scripts/b10_gap_solver_runtime.py`
- `deployment/fabric-observation-probe/.../DeploymentObservationProbe.java`
- D102、D112、B10 和 Java 对应测试
- D113 决定、计划、验收和新证据目录

不修改 Session 状态、动作搜索算法、窗口公式、重试上限或伤害门槛。

## 3. 实施顺序

### D113-0：冻结 RED 和只读基线

先增加失败检查：

1. 诊断区分 `first_control_visible_tick` 与 `result_take_tick`；
2. 混合 trace 中，生产协调器帧、同步探针和其他夹具步骤不能串组；
3. REVALIDATE 比现有估计晚一 tick时必须上调，比它快时不得下调；
4. jump-up `[4,4,5]` 与 `[4,5,5]` 必须复现；
5. 主动碰墙、侧碰后仍滑动、离地、UNKNOWN 和视角变化继续拒绝；
6. D102 测试只能取得本次 generation 的新 job；
7. never-return 与普通容量不足必须得到不同细分原因；
8. 客户端脱敏诊断不得输出异常消息或完整路径。

用 D112 最终原始 trace 只读生成 D113 基线。源文件哈希和重算脚本版本必须写入结果，不能覆盖 D112 证据。

### D113-1：修交付估计和 jump-up 准备预测

按 TDD 实施：

- 将合法 REVALIDATE 的实际交付时间作为只增不减的估计；
- 允许纯中性、固定视角、始终着地且侧碰后水平速度归零的准备前缀完成预测；
- 完整记录准备轨迹、依赖和资源，再运行原动作重验证；
- 不增加窗口、重试次数或误差容限。

先通过三个最小序列，再运行 180 个抖动场景和现有固定 33 场。

### D113-2：统一诊断、分析和 Fabric 注入端点

- 新增首次控制可见事件；旧 consume/take 事件保留；
- 记录 job anchor、实际 submit、receiver、publish、首次可见和 take tick；
- 分析器按 episode、observation sequence、control sequence 和 identity 唯一关联；
- Fabric 夹具按目标首次可见 tick 注入，并核对实际值；
- 真实 worker 尚未完成、控制 tick 跳过或旧 generation 泄漏时，场景记为注入无效。

性能报告分成生产协调器、同步探针和其他夹具三组。无法唯一关联时不签署生产性能。

### D113-3：补客户端定位、测试屏障和超时原因

- 保留 transport cause；
- 输出受控的异常类型与项目源码 `文件名:行号`；
- D102 最多等待五个 receiver cycle，直到取得本次 generation 的新 job；
- never-return 继续保留两个未完成槽上限，最终报告 `motion_solver_request_expired`；
- 修正“最多五 tick”但实际只描述四 tick 预热动作的注释。

### D113-4：定向 Fabric 场景

只运行一个 5-tick 场景：

- 实际首次控制可见延迟必须正好为 5；
- 若客户端失败，必须得到脱敏代码位置；
- 机器人零伤害、Runtime 不失败、来源有界释放；
- 注入无效或异常无法定位时停止，不进入正式批次。

允许根据该单场结果做一轮夹具或客户端修正。不能用提高准备上限来通过。

### D113-5：唯一正式批次

定向场景通过后运行一次完整批次：

- baseline 20；
- 2／5／10 tick 各 8；
- never-return 4；
- late1 继续按后端实际支持情况显式记录。

要求：

- 2／5 tick 正例完成；
- 10 tick 完成或安全有界；
- never-return 不起跳、不离边、零伤害、有界结束；
- 所有 identity 链、目标延迟与实际延迟可复算；
- 生产控制准备满足 8／15／30 ms；
- 同步探针只报告；
- 中途失败仍生成部分统计和原始失败位置。

## 4. 检查顺序

每个生产修正都先跑对应 RED，再跑直接与相邻检查。进入 Fabric 前依次完成：

1. D113 专项和三种变异检查；
2. 受影响与相邻模块；
3. Windows 源树正序、逆序分片；
4. 独立公开快照正序、逆序；
5. Java、导出和快照 verify。

## 5. 停止规则

- 任一修正需要增加 Session 状态、扩大重试次数或放宽安全门槛时停止；
- 180 个抖动场景仍有失败时，不运行 Fabric；
- 定向 Fabric 注入无法证明实际 5 tick 时，不运行完整批次；
- 一轮定向修正后仍无法定位客户端异常时，保留证据并停止；
- 正式批次只运行一次，不为通过门槛追加运行。

D112 保持 FAILED／STOPPED。D110-SOLVE-01 只在 D113 正式批次全部通过后关闭。
