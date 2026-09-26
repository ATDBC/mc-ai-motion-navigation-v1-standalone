# B12-A Attack Evidence and Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每次攻击拥有独立、可重放的证据记录，并让门禁拒绝、输入失败和确认超时分别消耗自己的重试额度。

**Architecture:** 新增纯数据契约和纯重试归约函数。`MeleeStrikeDriver` 负责一项攻击尝试的命令、回执和观察证据；`MovingMeleeDriver` 只消费终态结果并维护任务级统计。现有 Runtime、唯一输入出口和观察协议保持不变。

**Tech Stack:** Python 3.11、`dataclasses`、`StrEnum`、现有 `unittest`、Minecraft 1.21 独立 Fabric 实机入口。

**Spec:** `docs/motion_navigation/architecture/B12-attack-evidence-v1.md`

## Global Constraints

- 正式运行只使用合法结构化观察，不能增加服务端特权伤害来源。
- Runtime 仍是唯一后端推进者和唯一输入出口。
- 生命下降和死亡不能单独证明机器人命中。
- 各类预算耗尽返回任务结果，不能因为预算耗尽封存 Runtime。
- 不改写 C1-A、C1-B、C1-C 的历史原始记录。
- 本计划不实现 B12-B 的部分观察、横移和后撤。

## Review Focus

- 攻击提交后恰好发生环境伤害：只记录目标状态变化，不授予确认命中。
- 攻击意图输给同帧其他操作：不算攻击提交，不消耗失败额度。
- 目标在确认窗口内修订或换世界：旧证据不能用于新目标。
- 门禁拒绝后出现确认超时：两类失败不能合并成同一个连续计数。
- 已有一次失败后成功命中，再发生同类失败：新的失败序列从一次开始，历史总数仍保留。

---

### Task 1: 攻击尝试和重试契约

**Files:**
- Create: `mc2p/skills/attack_evidence.py`
- Create: `tests/test_attack_evidence.py`

**Interfaces:**
- Produces: `AttackAttemptKeyV1`、`AttackAttemptPhase`、`AttackAttemptOutcome`、`AttackEvidenceGrade`、`AttackAttemptReportV1`、`AttackRetryLedgerV1`、`AttackTaskOutcome` 和 `advance_attack_retry(...)`。
- Consumes: 任务、目标、命令与观察中已经存在的标识和数值，不读取 Runtime。

- [ ] **Step 1: 写失败检查**

覆盖契约拒绝非法序号和终态缺证据，覆盖三类失败分别累计、命中重置连续失败但保留历史总数，以及仲裁延后不消耗额度。

- [ ] **Step 2: 运行检查并确认失败**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_attack_evidence -v
```

Expected: 因 `mc2p.skills.attack_evidence` 尚不存在而失败。

- [ ] **Step 3: 实现最小纯契约**

`AttackAttemptOutcome` 的终态固定为：`DEFERRED_BY_ARBITRATION`、`GATE_REJECTED`、`INPUT_FAILED`、`CONFIRMATION_TIMEOUT`、`OBSERVATION_INTERRUPTED`、`COMMAND_CORRELATED_HIT`、`TARGET_DEAD_UNATTRIBUTED`、`TARGET_REVISED` 和 `CANCELLED`。来源未确认的生命下降记录在证据等级和布尔事实中，不单独结束仍在确认期限内的尝试。

`advance_attack_retry` 返回新的不可变账本。历史总数始终累计；只有同类连续失败达到冻结上限时才返回对应的 `AttackTaskOutcome`。命中清零三类连续计数。

- [ ] **Step 4: 运行检查并确认通过**

运行 Step 2 的命令。Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add mc2p/skills/attack_evidence.py tests/test_attack_evidence.py docs/motion_navigation/stages/B12A-attack-evidence-implementation-plan.md
git commit -m "feat(b12): add typed attack evidence contracts"
```

### Task 2: 单次攻击接入证据记录

**Files:**
- Modify: `mc2p/skills/fixed_melee.py`
- Modify: `mc2p/skills/melee_strike_driver.py`
- Modify: `tests/test_melee_strike_driver.py`
- Modify: `tests/test_fixed_melee_driver.py`

**Interfaces:**
- Consumes: Task 1 的攻击尝试类型。
- Produces: `MeleeStrikeDriver.attempt_report`，以及构造参数 `attempt_sequence: int = 1`。

- [ ] **Step 1: 写失败检查**

增加以下行为检查：生命下降但受伤动画没有新变化时不确认命中；死亡但没有命令关联证据时返回 `TARGET_DEAD_UNATTRIBUTED`；门禁拒绝、回执缺失、确认超时和目标修订得到不同终态；仲裁未选中攻击时返回 `DEFERRED_BY_ARBITRATION`；成功命中保存意图、动作请求序号、提交观察序号和证据等级。

- [ ] **Step 2: 运行检查并确认失败**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_melee_strike_driver tests.test_fixed_melee_driver -v
```

Expected: 新的报告接口不存在，且生命下降测试仍会被旧逻辑误判为命中。

- [ ] **Step 3: 收紧纯判断并接入尝试记录**

从 `decide_fixed_melee` 删除“生命下降即可完成”的分支。`MeleeStrikeDriver` 在攻击提案、仲裁、回执、确认、取消和目标修订时更新当前尝试，并在终态写入一次 `attack_attempt` 任务事件。已有 `MeleeStrikeOutcome` 暂时保留为兼容视图，其值由新的有类型结果产生。

- [ ] **Step 4: 运行检查并确认通过**

运行 Step 2 的命令。Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add mc2p/skills/fixed_melee.py mc2p/skills/melee_strike_driver.py tests/test_melee_strike_driver.py tests/test_fixed_melee_driver.py
git commit -m "feat(b12): bind hit evidence to one attack attempt"
```

### Task 3: 移动近战使用分类重试额度

**Files:**
- Modify: `mc2p/skills/moving_melee.py`
- Modify: `mc2p/skills/moving_melee_driver.py`
- Modify: `tests/test_moving_melee_driver.py`

**Interfaces:**
- Consumes: `MeleeStrikeDriver.attempt_report` 和 `advance_attack_retry(...)`。
- Produces: `MovingMeleeDriver.attack_retry_report`、有类型的 `task_outcome`，以及仍兼容现有状态和说明文字的移动近战报告。

- [ ] **Step 1: 写失败检查**

覆盖门禁拒绝和确认超时交替出现时不会共享额度；相同失败连续两次得到对应的有类型任务结果；成功命中清除连续失败；历史总数不清零；额度耗尽时 Runtime 仍为 `READY`；外力恢复和重新接近不改变攻击失败计数。

- [ ] **Step 2: 运行检查并确认失败**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_moving_melee_driver -v
```

Expected: 旧 `_unconfirmed_strikes` 把门禁拒绝和确认超时合并，至少一项新增检查失败。

- [ ] **Step 3: 替换混合计数**

删除 `_unconfirmed_strikes`。每个子攻击完成后，用其 `AttackAttemptOutcome` 更新不可变账本。任务流程只根据 `AttackTaskOutcome` 决定是否进入 `NEEDS_TASK_DECISION`；`reason` 只用于展示和历史兼容，不再决定额度归属。

- [ ] **Step 4: 运行检查并确认通过**

运行 Step 2 的命令。Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add mc2p/skills/moving_melee.py mc2p/skills/moving_melee_driver.py tests/test_moving_melee_driver.py
git commit -m "feat(b12): separate melee retry budgets"
```

### Task 4: 证据重放和跨阶段回归

**Files:**
- Create: `mc2p/skills/attack_evidence_replay.py`
- Create: `tests/test_attack_evidence_replay.py`
- Modify: `tests/test_c1_melee_evidence.py`
- Modify: `tests/test_c1_moving_melee_evidence.py`

**Interfaces:**
- Consumes: 正式 trace 中的攻击意图、仲裁、回执、观察和 `attack_attempt` 事件。
- Produces: `replay_attack_attempt(records, key) -> AttackAttemptReportV1`，供验收脚本和公开证据检查使用。

- [ ] **Step 1: 写失败检查**

用固定事件夹具证明在线结果可由原始事实重放；删除关键回执或观察后，重放必须返回证据不完整，不能沿用在线结论；健康下降夹具不得重放成命令关联命中。

- [ ] **Step 2: 运行检查并确认失败**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_attack_evidence_replay tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence -v
```

Expected: 重放模块不存在。

- [ ] **Step 3: 实现有界重放**

重放只读取单个尝试编号关联的有限事件，不扫描或修改世界。缺少命令、回执或后续观察时返回 `OBSERVATION_INTERRUPTED` 和 `NONE` 证据等级。

- [ ] **Step 4: 运行检查并确认通过**

运行 Step 2 的命令。Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add mc2p/skills/attack_evidence_replay.py tests/test_attack_evidence_replay.py tests/test_c1_melee_evidence.py tests/test_c1_moving_melee_evidence.py
git commit -m "test(b12): replay per-attack evidence"
```

### Task 5: B12-A 回归、实机入口和文档结果

**Files:**
- Create or Modify: `scripts/b12_attack_evidence_runtime.py`
- Modify: `docs/motion_navigation/stages/B12-partial-observation-combat.md`
- Modify: `docs/motion_navigation/acceptance/B12A-attack-evidence-retry.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: Tasks 1–4 的正式攻击尝试、重试账本和重放接口。
- Produces: 固定种子的 Fabric 正例、边界和反例记录，以及 B12-A 是否关闭的明确结论。

- [ ] **Step 1: 增加验收入口检查**

先为场景注册、固定种子、结果分组和失败保留规则写组件检查。验收入口必须把有效正例超时留在分母中，并分别输出门禁拒绝、输入失败和确认超时。

- [ ] **Step 2: 运行全部直接相关检查**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_attack_evidence tests.test_melee_strike_driver tests.test_fixed_melee_driver tests.test_moving_melee_driver tests.test_attack_evidence_replay tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence -v
```

Expected: PASS。

- [ ] **Step 3: 运行项目回归**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_segmented_trace tests.test_standalone_java_gates -v
```

Expected: PASS。

- [ ] **Step 4: 运行 Fabric 验收或如实记录未运行原因**

按验收文档执行 40 个正例及冻结的边界和反例。若游戏环境当时不可用，阶段状态保持“组件实现完成，等待 Fabric 验收”，不能关闭 B12-A。

- [ ] **Step 5: 更新文档并提交**

只写实际完成的检查、批次、数量和证据边界。运行：

```powershell
git diff --check
git add scripts/b12_attack_evidence_runtime.py docs/motion_navigation/stages/B12-partial-observation-combat.md docs/motion_navigation/acceptance/B12A-attack-evidence-retry.md AGENTS.md
git commit -m "test(b12): validate attack evidence and retry semantics"
```
