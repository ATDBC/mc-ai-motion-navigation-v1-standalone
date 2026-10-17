# D115 连续两跳与 D114 收尾实施计划

日期：2026-10-17。决定见 [D115](../decisions/0115-close-d114-and-enable-bounded-two-jump-continuity.md)。

当前状态：**PARTIAL／STOPPED。** A、B 已完成；C 建立正式闭环 RED；D 的协调器局部接线不能完成同帧交接，按止损规则撤回；E、F 未运行。

## 1. 原计划交付

本阶段原计划交付三项结果。实际只完成前两项；第三项在正式代表闭环触发止损：

1. B10 夹具不会在真实传送生效前开始场景，组件和 Fabric 使用同一份 trial 组装与等待帧口径；
2. 新完整求解为当前动作重建交付估计，复核只在该动作内上调；
3. 正式协调器能在第一跳空中准备同一路线的第二次 `JumpGap`，及时交付时不停车接续，迟到或失联时安全停在中间平台。**未完成。**

## 2. 修改范围

预计修改：

- `scripts/b10_gap_solver_runtime.py`
- `mc2p/motion_nav/motion_coordination.py`
- `tests/motion_nav/test_d114_b10_formal_driver.py`
- `tests/motion_nav/test_d113_delivery_adaptation.py`
- 新的连续两跳正式链测试
- 必要的 B10 Fabric 场景和结果分析
- 缺陷台账、入口文档、D115 验收与公开证据

首轮不得修改：

- `navigation_session.py` 的状态、字段或转移；
- `action_route_executor.py` 的公共交接协议；
- worker、inbox、registry 和 Runtime 的公共生命周期；
- 运动求解器、物理规则、安全公差、伤害额度和 UNKNOWN 规则；
- D105—D114 的原始失败证据。

## 3. 实施顺序

### A. 夹具 RED 与收尾

先建立同位姿迟到传送 RED，再实现“观察到命令生效迹象、最少 3 tick、连续 2 帧稳定”和场景起点硬检查。抽出共享 trial builder 和 coordinator summary。等待帧统一为“本帧没有移动意图”。帧数上限显式拒绝。

先只跑 D114 与 B10 probe 定向检查。代表场景稳定后再进入下一切片，不运行 Fabric。

### B. 交付估计 RED 与修正

把现有“后来的完整 SOLVE 也不能缩短估计”的测试改成动作代际语义：新 SOLVE 重建，当前代 REVALIDATE 只上调。覆盖慢 SOLVE、快复核、下一动作快 SOLVE、随后慢复核以及无效／重复结果。

复跑 D113 的 180 个 D／D+1 抖动场景，并增加至少两段严格动作的路线，证明第一段慢交付不会污染后续动作。

### C. 连续两跳 RED

通过正式 planner／admission 构造两个相邻 `JumpGap`。RED 必须走 `MotionRouteCoordinator.decide()`，证明当前生产路径没有在第一跳空中自动提交第二跳准备。不能用测试直接调用 `prepare_upcoming_action()` 代替 RED。

冻结的代表场景覆盖：

- 四个方向；
- 中间平台 2／3／4 格；
- 第二跳交付 0／2／5／10 tick 与永不返回；
- 入口轻微偏差、相关依赖变化、worker 退出和任务取消。

### D. 接通现有准备入口

只在 `MotionRouteCoordinator` 中增加调用条件：当前是已经开始的严格动作、第一条真实回执已经确定分支、存在已验证出口、下一动作也是严格动作、当前没有冲突工作。

调用已有 `prepare_upcoming_action()`。结果继续经过现有 selection、prepared bundle、receiver、registry、监督者和 Runtime 激活链。不得新增后继专用状态或直接安装 worker 返回对象。

### E. 组件闭环与性能

组件记录：

- 第一跳起跳、着地和第二跳起跳 tick；
- 第二跳提交、worker 完成、receiver 发布、控制可见、激活和实际应用 tick；
- 两跳之间的 neutral、制动、反向输入和完整停车；
- route、owner、generation、handle 用途和释放；
- 伤害、支撑、UNKNOWN 和依赖变化。

先跑一组代表场景。只有代表场景达到 `L -> L+1` 且零安全问题，才扩大完整组件矩阵。

### F. 完整门禁与唯一 Fabric

按以下顺序：

1. D114、D113 和连续两跳定向检查；
2. B10、prepared handoff、Runtime activation 相邻检查；
3. Windows 源树正序、逆序完整分片；
4. 独立公开快照正序、逆序；
5. Java、导出和 verify；
6. 一次 Fabric 正式批次。

Fabric 前冻结生产字节、场景、命令、量尺和允许结果。运行后不修改量尺来追求通过；发现夹具错误时保留原结果，并按限定修正规则处理。

## 4. 证据和文档

- D114 原始失败与只读修正保持原字节；
- D110、D111 和 T1 的历史性能失败不回写；
- T3 用组合证据新增关闭记录；
- D110-SOLVE-01 写成由 D112 门槛取代，不写成性能已修复；
- T2-3 写成非阻断跟踪项；
- 公开证据至少保存命令、结果索引、语义哈希、组件矩阵摘要、Windows 门禁和 Fabric 原始关键数据。

## 5. 限定修正

每个行为切片最多两轮修正。代表场景未通过时不跑完整矩阵；组件未通过时不跑 Fabric。范围外问题登记到台账，不顺手扩展本阶段。

## 6. 实际执行

- A 通过：传送等待、起点检查、等待帧、共享汇总和 frame limit 均有组件检查；D114／B10 probe 共 71 项通过。真实 Fabric 传送留给下一次能力批次复核。
- B 通过：新的动作 subject 用完整求解重建交付估计；相同 subject 的求解重试和复核只上调。D113 模块 15 项通过，其中包含 180 个 D／D+1 抖动场景；D102 动作交接 36 项通过。
- C 建立了正式 `PlayerRuntimeV1 -> RuntimeNavigationDriver -> NavigationSession -> planner/admission/executor/coordinator -> CalculatorBackend` RED，路线为 `Walk -> JumpGap -> JumpGap`。
- D 止损：第二跳证明及时登记，真实身体也精确到达入口，但交接批准早于同一控制循环里的动作边界发布。下一帧身体已离开精确入口，证明随后过期。
- E、F 未运行。生产协调器自动准备改动已经撤回；没有修改 Session、监督者或执行器公共协议。

另记录两个未在本轮修正的测试／校验缺口：测试用 `InlineMotionWorker` 不认识 `PreparedMotionJob.request.async_identity`；带后继 Walk 的 prepared proof 会因冻结值校验器不支持 `frozenset[str]` 而拒绝。两项均未混入本轮生产改动。

