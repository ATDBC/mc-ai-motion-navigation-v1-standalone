# Motion Navigation M0-M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成 M0 的量尺和不变量收尾，并用受控下降验证最小 `ActionSpec` 扩展边界。

**Architecture:** 动作实现只拥有动作自身的前提、风险、身体承诺、求解描述和执行规则。作业身份、身体责任、等待落地、重锚和 Session 生命周期继续由现有协调 owner 管理。M1 不建设 M2—M5 空壳。

**Tech Stack:** Python 3.11、unittest、现有 Fabric/Minecraft 1.21 契约、Windows PowerShell。

**Spec:** `docs/motion_navigation/stages/motion-navigation-middle-layer-M0-M1-plan.md`

## Global Constraints

- 正式平台是 Windows；Linux 不是阶段门槛。
- 不新增线程、进程、生产双轨开关、动态插件或长期状态的第二 owner。
- 安全相关动作声明必须显式；缺失时拒绝登记。
- S0R-C-01 的等待落地和重锚不能迁入具体动作实现。
- 每个生产修改先写失败检查并确认正确失败。
- 每个提交先跑直接检查；完整集合只在批次末运行。
- 不推送远端。

## Review Focus

- 新动作漏写身体承诺时必须拒绝启动，不能落入“地面安全”默认值。
- 空中收到 `NEEDS_STATE` 时仍由原控制者负责落地，落地后从正式观察重锚。
- `ControlledDropSpec` 不得保存每次执行状态或作业身份。
- 类型分支不能通过字典、字符串或放进通用 `actions` 文件逃过量尺。
- 规划 worker 的控制侧不能执行规划，也不能调用阻塞队列 API。

---

### Task 1: 冻结 M0 决定和量尺入口

**Files:**
- Modify: `AGENTS.md`
- Create: `scripts/navigation_design_metrics.py`
- Create: `tests/motion_nav/test_navigation_design_metrics.py`
- Create: `evidence/motion_navigation/redesign-m0/`

**Interfaces:**
- Produces: `measure(root: Path) -> dict` 与稳定 JSON 报告。

- [x] 先写量尺组件测试，覆盖类型判断、字典映射、字符串别名、未分类位置和具体动作实现排除边界。
- [x] 运行组件测试，确认因脚本尚不存在而失败。
- [x] 实现量尺脚本，生成仓库起点量尺，并在 M0 用 v2 冻结 79/30 正式基线；旧 3897232 数字仅作旧口径对照。
- [x] 更新 AGENTS.md 当前阶段和默认子 agent 协作方式。
- [x] 运行量尺测试和 `git diff --check`。
- [x] 提交 M0 量尺。

### Task 2: 恢复后台非阻塞不变量

**Files:**
- Modify: `tests/motion_nav/test_planner_worker.py`

**Interfaces:**
- Consumes: `PlannerWorker.submit`、`poll_latest` 和现有队列端口。

- [x] 写两个测试：规划不在控制进程执行；控制侧不调用阻塞队列 API。
- [x] 运行错误副本，确认控制侧计算和阻塞轮询分别被对应测试发现。
- [x] 在未修改生产代码上运行测试并确认通过。
- [x] 提交不变量检查。

### Task 3: 逐项删除死路径

**Files:**
- Modify: `mc2p/motion_nav/navigation_session.py`
- Modify: `mc2p/motion_nav/motion_coordination.py`
- Modify: related tests/inventory only when references require it

- [x] 对 `_admit_async_event` 做断言变异并运行直接检查，然后删除。
- [x] 对 `_cell_fact_id` 做断言变异并运行直接检查，然后删除。
- [x] 对 `_upcoming_air_index` 做断言变异并运行直接检查，然后删除。
- [x] 更新清单与量尺，确认三个名字消失。
- [x] 提交删除。

### Task 4: 核对历史种子和驱动器边界

**Files:**
- Modify: `tests/motion_nav/test_motion_baseline_recovery.py`
- Modify: driver/runtime tests selected by the failing boundary
- Modify: `docs/motion_navigation/acceptance/defect-ledger.md`

- [x] 证明 seed 15、69、119 是否提供当前条件触发场景没有的路径；只保留有独立证据的种子。
- [x] 为“Runtime READY 下任务失败”和 `_finish_control_unavailable` 已有业务结论写直接检查。
- [x] 运行检查并分类；若出现状态分裂、终态后输入或无人负责身体，先写失败测试再最小修复。
- [x] 更新缺陷台账，提交边界检查或修复。

### Task 5: 冻结 M0 起点

**Files:**
- Modify: `evidence/motion_navigation/redesign-m0/`
- Modify: `docs/motion_navigation/acceptance/motion-navigation-middle-layer-M0-M1.md`

- [x] 运行直接检查和量尺。
- [x] 运行完整运动导航正序、逆序。
- [x] 运行五组行为集合一轮；若签名工具变化，再连续运行第二轮。
- [x] 保存紧凑索引、哈希、命令和结果，不保存可重建的大型原始轨迹。
- [x] 提交 M0 基线。

### Task 6: 先用失败检查定义最小动作契约

**Files:**
- Create: `mc2p/motion_nav/actions/contracts.py`
- Create: `mc2p/motion_nav/actions/registry.py`
- Create: `mc2p/motion_nav/actions/controlled_drop.py`
- Create: `mc2p/motion_nav/actions/existing.py`
- Create: `tests/motion_nav/test_action_specs.py`

**Interfaces:**
- Produces: 固定 `ActionRegistry`；M1 所需的显式安全、前提、求解和执行查询。

- [x] 先写测试：五种动作必须登记；重复登记失败；缺少身体承诺、验证要求、风险或停止声明时失败。
- [x] 运行测试，确认因契约不存在而失败。
- [x] 实现最小契约和五种动作的显式分类；不提供安全宽松默认。
- [x] 写测试确认 spec 无可变执行状态，登记表不接受未知动作。
- [x] 运行直接检查和量尺，提交契约但暂不切换其余生产调用点。

### Task 7: 建立旧分支与新接口的测试侧等价比较

**Files:**
- Modify: `tests/motion_nav/test_action_specs.py`
- Create or modify: minimal fixture extractor under `tests/motion_nav/`

- [x] 从五组场景中抽取实际受控下降实例。
- [x] 在测试侧复制当前旧判断，逐项比较风险、前提、身体承诺、停止保护、求解分类和完成判断。
- [x] 确认至少一个错误 spec 会使等价测试失败。
- [x] 提交测试语料，生产调用点仍不切换。

### Task 8: 迁移受控下降的风险、前提和身体安全

**Files:**
- Modify: `mc2p/motion_nav/navigation_session.py`
- Modify: `mc2p/motion_nav/action_preconditions.py`
- Modify: `mc2p/motion_nav/action_route_executor.py`
- Modify: `mc2p/motion_nav/actions/controlled_drop.py`
- Test: focused risk/precondition/handoff tests

- [x] 按风险、前提、身体安全三个小循环分别先写失败检查。
- [x] 逐项切换到 registry/spec，删除对应旧分支。
- [x] 运行受控下降、信息采集、中断和伤害额度检查。
- [x] 运行量尺，确认没有把新分支藏进通用动作文件。
- [x] 提交。

### Task 9: 迁移求解描述，保留通用落地重锚

**Files:**
- Modify: `mc2p/motion_nav/motion_coordination.py`
- Modify: `mc2p/motion_nav/actions/controlled_drop.py`
- Test: `tests/motion_nav/test_motion_baseline_recovery.py`
- Test: affected motion coordination tests

- [x] 写失败检查，证明 spec 可以描述下降求解，但没有决定等待落地、重锚或重试。
- [x] 迁移下降方向、落点和求解类型查询。
- [x] 保留 `_reanchor_after_landing_action` 及其 owner；不得返回动作专属的等待落地处置。
- [x] 运行 seed 163、条件触发迟到、跨隙和下降直接检查。
- [x] 提交。

### Task 10: 迁移执行与完成判断并删除剩余特判

**Files:**
- Modify: `mc2p/motion_nav/action_route.py`
- Modify: `mc2p/motion_nav/action_route_executor.py`
- Modify: `mc2p/motion_nav/route_admission.py`
- Modify: action spec files and focused tests

- [x] 先写执行器选择、到达判断和未知动作拒绝的失败检查。
- [x] 切换 `ActionRoute` 的固定登记检查。
- [x] 迁移控制器创建和到达判断，删除外部受控下降类型分支。
- [x] 保留规划边到动作段的一个明确转换位置；不要为减少文件计数搬错责任。
- [x] 运行直接检查和量尺，提交。

### Task 11: 扩展探针、M1 文档和最终验收

**Files:**
- Create: `docs/motion_navigation/architecture/action-spec-v1.md`
- Modify: `docs/motion_navigation/acceptance/motion-navigation-middle-layer-M0-M1.md`
- Modify: `docs/motion_navigation/stages/motion-navigation-middle-layer-M0-M1-plan.md`
- Modify: `AGENTS.md`
- Test: `tests/motion_nav/test_action_specs.py`
- Create: `evidence/motion_navigation/redesign-m1/`

- [x] 加一个测试专用动作，只通过自己的实现和登记接入；断言 Session、执行监督者和规划协调器没有动作专用修改。
- [x] 删除测试中的旧逻辑副本，保留最小契约语料。
- [x] 运行批次末完整正序、逆序检查和五组集合。
- [x] 运行 Windows D058、D061 性能检查。
- [x] 连续运行两次最终五组集合，逐项比较 M0 索引。
- [x] 生成最终量尺，逐项判定 M1 门槛和止损条件。
- [x] 更新架构、阶段、验收和 AGENTS.md，提交最终结果。


## 实际交付（2026-10-07 记录）

所有任务已执行并记录实际结果；勾选表示执行完成，不表示 M1 通过。Task 1—5 的提交、RED/GREEN 和正式结果，以及 Task 6—11 的后续提交，统一见验收第 4 节。最终来源 b39da9a，正逆各 1,528、五组两轮逐项一致、D058/D061 通过。M1 因 Session 仅减少 52 行而结构止损，不通过；原 100 行门槛未改，M2 未授权。

Task 3 的零调用结论依赖新版计数代表检查、原 S0-R 完整矩阵和静态零引用。旧 assertion-only 全量运行只证明未出现未捕获异常。Task 7 按裁定覆盖 459 个去重场景，不声称 3,493 场全部有动作级比较；完成判断另以 45 次几何边界比较验收。Task 11 最终删除测试旧副本，保留冻结事实语料。
