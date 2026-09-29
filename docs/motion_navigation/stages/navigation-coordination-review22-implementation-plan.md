# 导航协调第二十二轮整改实施计划

状态：已完成。下面的勾选项记录本轮实际执行范围。

**目标：** 让输入歧义、动作等待和停止交接都有统一且有界的生命周期，并以至少 1000 个正式路径随机序列重新关闭导航协调 S5。

**设计依据：** [导航协调第二十二轮整改方案](navigation-coordination-review22-remediation-plan.md)与[D042](../decisions/0042-own-navigation-obligations-and-centralize-handoff.md)。

**实施方式：** 当前 `refactor/motion-navigation-v1` 分支内顺序执行。每项先运行失败测试，再修改生产代码。连续高度 M3 不在本轮重做。

## 文件职责

| 文件 | 本轮职责 |
|---|---|
| `mc2p/motion_nav/online_motion.py` | 根据输入记录和当前锚点形成有类型的输入责任评估 |
| `mc2p/motion_nav/route_body_controller.py` | 路线停止时使用责任评估和地面尾段证明决定是否释放 |
| `mc2p/motion_nav/probe_body_controller.py` | 探边停止时使用责任评估和稳定站位决定是否释放 |
| `mc2p/motion_nav/retry_ledger.py` | 保存带所有者的等待，并按所有者统一结束 |
| `mc2p/motion_nav/navigation_handoff.py` | 长期拥有待提交目标、停止请求和停止后的去向 |
| `mc2p/motion_nav/navigation_session.py` | 调用上述公共契约，不再自行旁路停止交接 |
| `tests/sim/event_sequences.py` | 生成可重复事件，按真实场景选择落点，并在终态后停发事件 |
| `tests/sim/run_navigation_event_sequences.py` | 运行固定随机门槛，保存分类和缩减反例 |
| `tests/sim/manifests/navigation-coordination-event-sequences.json` | 冻结至少 1000 个场景与种子组合 |

## 任务 1：冻结第二十二轮三个失败族

**修改：**

- `tests/motion_nav/test_input_responsibility.py`
- `tests/motion_nav/test_retry_ledger.py`
- `tests/motion_nav/test_navigation_event_sequences.py`
- `tests/motion_nav/test_navigation_supervised_interruptions.py`
- 新增 `tests/motion_nav/test_navigation_handoff.py`

- [x] 加入“丢回执后 1 至 10 帧改目标或取消”的正式路径参数化测试，覆盖两格下落、两级台地和 L 形走道。
- [x] 加入 `STOPPING` 期间普通 `BEGIN_PLANNING` 不能绕过交接的测试。
- [x] 加入探边完成、替换、取消、失败和关闭后等待记录归零的测试。
- [x] 运行这些测试，确认当前代码分别出现永久等待、非法转换和等待泄漏。

**完成条件：** 三个问题都有稳定失败检查；测试不读取会话私有状态来决定通过，只使用公共结果、账本只读视图和不变量监视器。

## 任务 2：输入歧义形成有界的当前锚点责任

**修改：**

- `mc2p/motion_nav/online_motion.py`
- `mc2p/motion_nav/route_body_controller.py`
- `mc2p/motion_nav/probe_body_controller.py`
- `mc2p/motion_nav/execution_supervisor.py`
- `tests/motion_nav/test_input_responsibility.py`
- `tests/motion_nav/test_execution_supervisor.py`

- [x] 在 `online_motion.py` 增加不可变的 `InputResponsibilityAssessment`，保存状态、阻塞序列和最晚可能生效 tick。
- [x] 增加 `assess_input_responsibility(ledger, anchor, previous_sequence_floor)`。它保留 `AMBIGUOUS` 历史，但能识别“已越过所有可能生效 tick 的当前锚点”。
- [x] 路线控制者只有在自身已到终态、当前锚点有效、释放后的地面尾段得到证明时，才把该评估转成 `QUIESCENT`。
- [x] 探边控制者只有在停止站位成立、当前锚点有效时，才允许同样的释放。
- [x] 没有有效锚点、仍有在途输入或身体尾段不安全时继续保留控制权；现有停止期限负责给出有界失败。
- [x] 运行输入责任、监督者和“丢回执后改目标”测试。

**完成条件：** 账本仍如实保存历史歧义；旧控制者能从更新且安全的身体锚点结束责任；没有安全锚点时不会提前释放。

## 任务 3：等待绑定动作所有者

**修改：**

- `mc2p/motion_nav/retry_ledger.py`
- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/execution_supervisor.py`
- `tests/motion_nav/test_retry_ledger.py`
- `tests/motion_nav/test_navigation_supervised_interruptions.py`

- [x] 为 `WaitToken` 增加 `owner_id`，并让 `begin_wait` 明确接收所有者。
- [x] 增加 `end_owner_waits(owner_id)` 和只读的活动等待摘要，供生产退场和测试检查使用。
- [x] 给信息等待、恢复等待和探边取证等待分配稳定所有者；同一动作重入不能重复占容量。
- [x] 把探边正常完成、替换、取消、失败、超时和关闭统一接到一个退场方法。该方法结束探边所有等待后再清除控制者。
- [x] 静态搜索风险记录、重试尝试和提交序列，记录各自所有者与结束条件；发现同类泄漏时在本任务一并补测试和修正。
- [x] 运行等待账本和频繁改目标测试。

**完成条件：** 任意动作退场后，不存在属于它的活动等待；容量满载仍保留既有责任并返回有类型结果。

## 任务 4：停止后的单一交接入口

**新增：**

- `mc2p/motion_nav/navigation_handoff.py`
- `tests/motion_nav/test_navigation_handoff.py`

**修改：**

- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/navigation_lifecycle.py`
- `tests/motion_nav/test_navigation_lifecycle.py`
- `tests/motion_nav/test_navigation_route_handoff.py`

- [x] 在 `navigation_handoff.py` 定义不可变的待提交目标、停止请求和交接结果类型。
- [x] `NavigationHandoffCoordinator` 只允许一个当前修订；更高修订可以替换尚未提交的旧修订。
- [x] 目标修订先保存，再请求旧控制者停止。旧控制者没有给出当前帧交接证据前，不修改正式请求和任务状态。
- [x] 交接证据到达后，由协调器一次返回“重新规划、等信息、失败、完成或取消”。会话原子应用结果。
- [x] `_advance_planning()` 在非 `PLANNING` 状态直接返回，不能自行离开 `STOPPING`。
- [x] 清点所有 `_transition()` 调用。`STOPPING` 中只允许 `REPLAN_AFTER_HANDOFF`、`RESUME_EXECUTION_AFTER_HANDOFF` 和有类型终态。
- [x] 增加 AST 或等价静态门禁，阻止普通规划和执行入口重新旁路协调器。
- [x] 运行生命周期、路线交接、目标频繁修订和非法转换测试。

**完成条件：** `STOPPING` 不再由普通业务分支直接离开；目标修订没有半提交状态；非法转换为零。

## 任务 5：修正随机事件生成器并冻结门槛

**修改：**

- `tests/sim/event_sequences.py`
- `tests/sim/run_navigation_event_sequences.py`
- `tests/sim/manifests/navigation-coordination-event-sequences.json`
- `tests/motion_nav/test_navigation_event_sequences.py`
- `config/motion-navigation/standalone-export-v1.json`

- [x] 生成器改用有放回抽样，允许频繁改目标、连续外力和多次回执缺失。
- [x] 终态且身体控制权释放后停止派发事件。
- [x] 每个场景提供自己的落点支撑集合；移除事件只修改当前场景真实依赖。
- [x] 结果按事件类型、场景、任务终态和不变量分类。
- [x] 缩减后的反例写入独立结果文件，并能直接转为固定回归序列。
- [x] 清单覆盖两格下落、五格下落、L 形走道和两级台地，总数至少 1000。
- [x] 运行清单，要求 0 个不变量违规、0 个永久等待、0 个公开接口异常。

**完成条件：** 至少 1000 个冻结序列全部落在允许的任务结果内；任何失败都保存最短反例，不从分母删除。

## 任务 6：回归、文档和阶段判定

**修改：**

- `docs/motion_navigation/architecture/navigation-coordination-v1.md`
- `docs/motion_navigation/acceptance/navigation-coordination-refactor.md`
- `docs/motion_navigation/acceptance/defect-ledger.md`
- `docs/motion_navigation/decisions/0042-own-navigation-obligations-and-centralize-handoff.md`
- `docs/motion_navigation/stages/navigation-coordination-review22-remediation-plan.md`
- `AGENTS.md`

- [x] 运行第二十二轮三个固定复现族。
- [x] 运行固定 14 场、冻结 200 种子和 192 个中断组合。
- [x] 运行至少 1000 个随机事件序列。
- [x] 运行完整 `tests/motion_nav`、分段证据、Java 门禁和公开真实证据检查。
- [x] 运行 `git diff --check`，确认只包含本轮文件。
- [x] 如全部门槛通过，把 NC-022-01 至 03 标为已修复并关闭导航协调 S5；否则如实保留打开状态。

**完成条件：** 文档与实际结果一致；连续高度 M3 结论不变；导航协调 S5 只在所有门槛通过后关闭。
