# D114 正式导航驱动夹具修复计划

日期：2026-10-17。决定见 [D114](../decisions/0114-run-b10-through-the-formal-navigation-driver.md)。

当前状态：**PASSED。** D113 保持 FAILED／STOPPED；本阶段从 `d077629f` 建立独立工作树，没有修改 D113 历史证据。唯一一次 Fabric 命令的原始失败和之后的只读量尺修正均已公开。

## 1. 交付目标

修正 B10 协调器夹具绕过正式导航驱动的问题，让组件和 Fabric 执行同一条控制循环。先在组件后端证明 baseline、延迟和失联场景不会复用过期意图，再运行一次新的 D114 正式批次。

本轮同时处理三项与代码行为无关但会妨碍验收的问题：D102 测试屏障、误删的失败记录、打包版入口文档。

## 2. 范围

预计修改：

- `scripts/b10_gap_solver_runtime.py`
- `mc2p/skills/navigation_session_driver.py`，仅在证据需要时增加只读 prepared proposal 接口，或增加复用共同初始化的正式低层请求入口
- `tests/motion_nav/test_d114_b10_formal_driver.py`
- `tests/motion_nav/test_d102_action_handoff.py`
- 公开导出清单和对应测试
- packaging 的 AGENTS／README
- D114 决定、计划、验收和证据目录

不修改：

- 运动求解器、动作证明和物理计算；
- 5 tick 准备上限、重试额度和伤害门槛；
- Session 状态和生命周期规则；
- D113 原始证据与历史结论；
- 路线内交付估计策略。

## 3. 实施顺序

### D114-0：计划、历史和测试基础

1. 签署 D114 决定、计划和验收骨架；
2. 从冻结 Git blob 原字节恢复 TP 5A-R 的三份失败记录；
3. 把三份文件加入导出 `required_paths`，增加缺失即失败的检查；
4. 更新 packaging 入口到 D113；
5. 修正 D102 两阶段准备测试：最多等待五个 receiver 周期，直到当前 identity／generation 的新作业真正提交；不批量放宽单阶段测试。

### D114-1：先建立真实 RED

从 B10 脚本提取可独立调用的单场函数，但先保留旧控制循环，通过 CalculatorBackend 重现：

- 10-tick 或 never-return 会重复选择旧会话意图；
- 预先植入手工租约时，该租约仍可能参与选择；
- Runtime 最终出现由旧租约造成的 deadline failure。

若组件无法重现至少一个直接故障签名，先修量尺，不实施驱动迁移。

### D114-2：迁移到正式驱动

单场函数统一使用 driver 的 prepare／Runtime control／driver adopt。等待帧不另开分支。

场景接管前撤销 manual source 的旧意图。driver 负责会话源的登记、停止和释放。异常路径必须 discard prepared frame 并保留原异常。

先尝试用 `driver.start(goal_state)` 精确表达旧目标。组件要核对路线动作、目标完成和冻结断言没有变化。如果 `goal_state=None` 的低层请求语义无法等价表达，才增加正式 driver 的窄入口 `start_request()`；它只负责接收已有请求，并必须复用 `start()` 的绑定和失败清理。它不得成为第二套调度接口，也不允许脚本写 driver 私有字段。

证据若需要读取本帧提案，只增加生命周期受限的只读 `prepared_proposal` 属性。

### D114-3：组件可执行矩阵和变异检查

核心矩阵至少 16 场：四方向 × baseline／5 tick／10 tick／never-return。另加手工租约污染、异常清理和 typed 失败。

门槛：

- baseline、5 tick 完成，零伤害，identity 链完整；
- 10 tick 完成或 typed 安全有界，Runtime READY，不出现旧租约期限失败；
- never-return 报 `motion_solver_request_expired`，不起跳、不离边、零伤害；
- 所有场景没有旧 manual/session intent 获执行资格；
- driver 的 prepare 必须与 adopt 或 discard 一一配对；
- 来源按真实状态显式释放。

变异检查覆盖等待帧撤销、手工源撤销、adopt、延迟端点、释放事实和等待帧记录。

### D114-4：本地集成门槛

按 fail-fast 顺序运行：

1. D102、导出和 D114 直接检查；
2. D112／D113／B10 相邻检查；
3. Windows 源树正序、逆序完整分片；
4. 独立公开快照正序、逆序完整分片；
5. Java、导出和快照 verify。

进入完整检查前，锁定生产字节和语义哈希。完整检查只在最终候选上运行，避免反复消耗数小时。

### D114-5：唯一正式 Fabric 批次

本地门槛全部通过后，只运行一次正式 B10 命令。其中协调器矩阵为 56 项：

- baseline 20；
- 2／5／10 tick 各 8；
- never-return 4；
- late1 继续按后端能力明确记录，8 项不支持记录属于这 56 项的一部分。

同一次正式命令还必须运行或复核既有 40 项 validation、40 项 turn validation、10 项默认协调链、60 项 R4 和 24 项 typed negative。若命令在协调器矩阵中途停止，后续未运行项要如实记录，不能用此前历史结果补成当次通过。

完整报告等待帧、旧来源选择、动作期限、交付端点、identity、伤害、终态和释放。中途失败仍保存部分证据，不追加运行来改善结果。

## 4. 分提交和复核

1. `docs(d114)`：决定、计划、验收骨架；
2. `test(d102)`：有界等待当前作业；
3. `docs(export)`：恢复失败记录和导出门禁；
4. `docs(packaging)`：同步公开入口；
5. `fix(b10)`：正式驱动单场循环；
6. `test(b10)`：组件矩阵和变异；
7. `docs(d114)`：正式结果和证据索引。

每个行为提交由独立子 agent 复核。多个 agent 不同时修改同一组文件。

## 5. 停止规则

- RED 不成立：停在量尺修正，不改生产或夹具行为；
- 需要改变生产运动语义：停止并另立决定；
- 组件矩阵不绿：不跑完整检查或 Fabric；
- 完整检查失败：保留结果，限定两轮修正；
- Fabric 正式批次只运行一次；
- D113 不因 D114 的结果改写。

## 6. 实际结果

- 正式驱动迁移、组件矩阵、旧来源变异和异常清理均通过；
- 唯一一次 Fabric 命令完整运行，没有追加运行；
- 命令因“坐标不变但一帧未着地”被错误计作离开支撑而记录 failed；
- 修正量尺后，对同一份原始 trace 只读重算得到 10-tick 8／8 安全有界；
- baseline 20／20、2／5-tick 16／16、never-return 4／4，零伤害，来源全部显式释放；
- 生产控制准备 1211 帧 P95／P99／最大为 2.9600／5.4988／5.8878 ms；
- D110-SOLVE-01、连续两跳、零等待交接和新动作仍不在本阶段范围内。
