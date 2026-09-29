# 导航协调第二十轮整改实施计划

> **执行要求：** 实施时使用 `superpowers:executing-plans`，按任务顺序逐项完成。每项先写失败检查，再修改生产代码；每项单独提交并保留原始失败证据。

日期：2026-09-29

状态：任务 0 至 7 已完成。第二十轮整改的 S4 结构门槛已经关闭；S5 继续保持打开，等待完整连续高度速度带、形状和随机迟到 Fabric 矩阵达到可靠性门槛。在 S5 关闭前不新增运动、地形和战斗能力。

**目标：** 修复已验证空中动作在“中断后又晚一帧”时永久卡死的问题，让生命周期事件表真正约束事件入口，并用阶段进展监视器防止控制器在持续运动中循环而逃过无界等待检查。

**总体做法：** 事件表负责判断事件能否进入业务逻辑，转移表继续独占任务状态写入。动作执行者根据真实生效窗口和输入账本有界地结束等待；会话只消费有类型的动作结果和交接证据。模拟器同时检查位置进展与动作阶段进展，避免“身体一直在动”掩盖控制流程没有前进。

**技术范围：** Python 3.11、现有 1.21 运动计算器、正式 `Runtime → RuntimeNavigationDriver → NavigationSession → ExecutionSupervisor` 调用链、现有 Fabric 1.21 接入。不增加依赖，不新增线程或进程。

关联文档：[协调架构](../architecture/navigation-coordination-v1.md)、[协调强化计划](navigation-coordination-hardening-plan.md)、[协调验收](../acceptance/navigation-coordination-refactor.md)、[连续高度验收](../acceptance/continuous-height-ground-movement.md)、[D040](../decisions/0040-reopen-navigation-coordination-gates.md)。

## 1. 本轮确认的问题

### 1.1 必须修复

1. 已验证下落在第 36 至 45 帧收到改目标或取消，之后再出现一帧输入晚到，会永久停在 `executing` 或 `stopping / recovery_unresolved`。192 个组合中有 80 个无法结束。
2. `SESSION_EVENT_TABLE` 虽然会返回 `HANDLE / IGNORE / REJECT`，但会话中的 6 个调用都丢弃返回值。它目前只是记录，不是准入门槛。
3. 每条命令固定晚一帧时，一格下降会在恢复和落地确认之间形成 6 帧循环。身体一直移动，现有 I4 静止检测看不出来。
4. `STOPPING → EXECUTING / PLANNING` 只凭转移动作发生，没有要求调用方提供对应的交接证据。
5. 正式运行若出现表外转移，异常会直接传给父层。身体可能仍由空中动作持有，不能因为任务状态错误而释放控制责任。

### 1.2 需要澄清或补证据

1. 当前代码复跑冻结种子是 198/200 成功、2/200 有界安全失败；现行文档中的 192/200 是上一次历史结果。两组都要保留，不能用新结果覆盖旧证据。
2. “下一整格每条命令固定晚一帧也必须完成”与 B10 的正式契约冲突。正式契约只允许首条命令在已证明窗口内晚一帧；动作开始后的缺帧属于执行故障。
3. 半砖和楼梯首条路线作废，初步判断来自模拟器先规划、后灌入第一次正式观察的顺序。需要修正测试初始化，并用 Fabric 对照确认是否存在真实问题。
4. 起跳前最后约 4 帧移除落点支撑是否还能靠潜行停住，必须由 Fabric 确认物理边界。

### 1.3 本轮不做

- 不把所有已验证动作改成能承受每一条命令固定晚一帧。若以后需要这种能力，应另行设计输入流水线或整段命令授权。
- 不把统一支撑标准写成固定的 `0.8`。坑边允许安全悬出部分身体，释放条件继续依据当前动作、风险约束、已知几何和中性输入尾迹。
- 不新增攀爬、游泳、主动进入 Crawl 或战斗策略。
- 不借本轮重写整个 `NavigationSession`。只迁出本轮已出现两次以上的控制者协议和有界收尾责任。

## 2. 长期边界

### 2.1 两张表各自负责什么

| 组件 | 负责 | 不负责 |
|---|---|---|
| 事件表 | 根据当前任务状态判断事件是处理、忽略还是拒绝 | 不直接选择下一状态 |
| 转移表 | 根据当前状态和有类型的转移动作给出唯一下一状态 | 不判断异步结果是否过时 |

例如，终态收到旧规划结果时，事件表返回 `IGNORE`，调用方必须立即结束且不能改路线、风险或状态。执行中收到控制器报告时，事件表返回 `HANDLE`；报告内容再决定继续执行、停止、失败或重规划。

### 2.2 中断后的身体责任

- 改目标只替换任务目标，不撤销已经生效或可能已经生效的空中动作。
- 取消会停止继续追求原目标，但原动作仍负责安全落地。
- 等待输入回执只能持续到该命令最后可能生效的 tick。超过这个 tick 后，必须根据账本归类为已应用、明确丢失或仍有歧义。
- 有歧义时保持保护输入，并在已有恢复期限内交给父层；不能无限等待，也不能凭空宣布已经释放。

### 2.3 什么算动作阶段有进展

以下任一变化才算控制流程前进：

- 动作索引增加；
- 控制阶段按合法顺序变化；
- 等待的命令获得新应用记录；
- 身体进入该阶段声明的出口范围；
- 产生新的、有依据的重试进展证据。

单纯位置发生往返变化不算阶段进展。这样可以抓到“恢复—落地确认—恢复”的循环。

## 3. 实施任务

### 任务 0：冻结复现与建立缺陷台账

**文件：**

- 新建 `docs/motion_navigation/acceptance/defect-ledger.md`
- 新建 `tests/sim/manifests/navigation-coordination-interrupt-late.json`
- 新建 `tests/sim/run_navigation_interrupt_matrix.py`
- 修改 `tests/sim/manifests/navigation-coordination-smoke.json`

**交付：**

- [x] 把审查方的 192 个“中断 × 动作阶段 × 晚到”组合等价固化到正式清单：2 格和 5 格下落、改目标和取消、中断帧 30 至 45，以及无额外晚到／中断后第 1 或第 2 tick 晚到。
- [x] 增加“每条命令固定晚一帧”的一格下降场景，用来验证有界安全结果，不把它预设为成功正例。
- [x] 台账记录本轮 P1、生命周期事件表失效、阶段循环、交接证据缺失、模拟初始化偏差和 Fabric 待确认边界。每项写明首次发现者、复现入口、计划修复任务和同类搜索范围。
- [x] 运行复现，确认修复前仍得到 80/192 个非终态结果，并保存机器摘要。

**完成条件：** 后续每个修复都有稳定失败场景；没有只靠第三方脚本才能复现的问题。

**验证：**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m tests.sim.run_navigation_interrupt_matrix --manifest tests/sim/manifests/navigation-coordination-interrupt-late.json --output-root .tmp/review20-baseline
```

### 任务 1：让事件表成为真正的入口门槛

**文件：**

- 修改 `mc2p/motion_nav/navigation_lifecycle.py`
- 修改 `mc2p/motion_nav/navigation_session.py`
- 修改 `tests/motion_nav/test_navigation_lifecycle.py`
- 修改 `tests/motion_nav/test_navigation_session.py`

**接口：**

- `NavigationLifecycle.admit_event(event: NavigationSessionEvent) -> SessionEventPolicy`
- `NavigationSession._admit_command_event(event: NavigationSessionEvent) -> None`
- `NavigationSession._admit_async_event(event: NavigationSessionEvent) -> bool`

**规则：**

- [x] 先写测试：`HANDLE` 才能继续原分支；`IGNORE` 不得修改请求、路线、风险账本或任务状态；`REJECT` 不得执行事件对应的业务动作。
- [x] 同步命令事件被拒绝时，向调用方返回契约错误，且会话保持原状。
- [x] 异步结果被忽略时安静丢弃。异步结果若出现表中明确拒绝的组合，进入有类型的内部失败流程，监督者继续持有身体直到取得可释放证据。
- [x] 替换现有 6 个裸 `record()` 调用。增加 AST 门禁，禁止把 `admit_event()` 当作独立表达式丢弃结果。
- [x] 保留转移表作为唯一状态写入口；事件表不直接返回下一状态。

**完成条件：** G1-2 为 0；终态旧结果不能改变任何长期状态；合法的 `CONTROLLER_REPORT` 仍可根据载荷进入不同转移动作。

**验证：**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_lifecycle tests.motion_nav.test_navigation_session -v
```

### 任务 2：给输入应用等待和空中收尾设置上界

**文件：**

- 修改 `mc2p/motion_nav/motion_candidate.py`
- 修改 `mc2p/motion_nav/action_route_executor.py`
- 修改 `mc2p/motion_nav/navigation_session.py`
- 修改 `tests/motion_nav/test_b10_motion_candidate.py`
- 修改 `tests/motion_nav/test_navigation_supervised_interruptions.py`

**交付：**

- [x] 先写失败测试：下落中改目标或取消，随后晚一帧，必须在有限 tick 内到达成功、取消或有类型安全失败。
- [x] 执行器保存正在等待命令的最晚合法生效 tick。当前正式锚点超过该 tick 后，不再返回无限期的 `awaiting_application`。
- [x] 使用现有输入账本区分 `APPLIED`、明确丢失和歧义。明确丢失进入 `INPUT_LOST`；歧义进入有期限的保护收尾。
- [x] 取消安全落地后返回 `CANCELLED`；改目标安全落地后允许后继路线接管；期限耗尽返回有类型的“身体仍保留”结果。
- [x] 期限归动作执行者或现有重试／等待账本所有，不在 `NavigationSession` 增加布尔保持标志。

**完成条件：** 192 个组合中非终态为 0；所有空中帧都有身体负责人；不会因晚到回执重复执行不可逆命令。

**验证：**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_navigation_supervised_interruptions -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m tests.sim.run_navigation_interrupt_matrix --manifest tests/sim/manifests/navigation-coordination-interrupt-late.json --output-root .tmp/review20-after-bounded-finalization
```

### 任务 3：增加动作阶段进展与循环检测

**文件：**

- 修改 `mc2p/motion_nav/body_control.py`
- 修改 `mc2p/motion_nav/action_route_executor.py`
- 修改 `mc2p/motion_nav/landing_edge_probe.py`
- 修改 `mc2p/motion_nav/navigation_session.py`
- 修改 `tests/sim/monitor.py`
- 修改 `tests/sim/runner.py`
- 修改 `tests/motion_nav/test_navigation_closed_loop.py`

**接口：**

- 新增不可变的 `BodyControlProgress`，至少包含控制者身份、只用于诊断的阶段标签、单调进展代次、动作索引和最近确认的应用 tick。阶段标签不能决定流程。
- `NavigationSessionDiagnostics` 只读暴露当前进展；监视器不得读取控制器私有字段。

**交付：**

- [x] 先用“每条命令固定晚一帧”的 6 帧循环证明现有 I4 漏报。
- [x] 控制者仅在动作索引增加、新输入得到确认、身体进入声明的出口范围等真实进展时更新代次。阶段在恢复与落地确认之间往返、坐标往返、重复发同一请求和重复观察都不更新。
- [x] I4 同时检查位置无进展与阶段无进展。阶段超过自身期限后必须报告违规，即使身体仍在移动。
- [x] 正式控制流程在同一阶段用尽现有恢复额度时，返回 `INPUT_LOST` 或对应有类型结果，不继续循环。

**完成条件：** 原 6 帧循环要么完成，要么有界安全结束；监视器能在期限处报告它，不能等到总任务超时。

**验证：**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_closed_loop tests.motion_nav.test_navigation_supervised_interruptions -v
```

### 任务 4：交接必须携带证据，非法转移不能释放身体

**文件：**

- 修改 `mc2p/motion_nav/body_control.py`
- 修改 `mc2p/motion_nav/navigation_lifecycle.py`
- 修改 `mc2p/motion_nav/navigation_session.py`
- 修改 `mc2p/skills/navigation_session_driver.py`
- 修改 `tests/motion_nav/test_navigation_lifecycle.py`
- 修改 `tests/motion_nav/test_runtime_navigation_verified_handoff.py`

**规则：**

- [x] `RESUME_EXECUTION_AFTER_HANDOFF` 只接受新路线已赢得仲裁的 `TRANSFERABLE` 证据。
- [x] `REPLAN_AFTER_HANDOFF` 只接受旧控制者已经 `QUIESCENT` 的证据。若证据为 `RETAIN`，保持当前身体负责人，不执行该转移。
- [x] 交接证据必须属于当前世界会话、当前观察和当前控制者；旧证据不能复用。
- [x] 测试和模拟中的表外转移继续抛出 `ContractViolation`，便于尽早发现程序错误。
- [x] 正式 Runtime 捕获这类内部错误后产生有类型失败，要求监督者安全收尾；不能直接销毁 Runtime 或释放空中身体。

**完成条件：** 所有从 `STOPPING` 返回执行或规划的调用都有可核对的 `HandoffEvidence`；非法转移计数为 0；故意注入非法转移时身体责任仍连续。

**验证：**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_lifecycle tests.motion_nav.test_runtime_navigation_verified_handoff -v
```

### 任务 5：监督者只依赖统一的控制者协议

**文件：**

- 修改 `mc2p/motion_nav/body_control.py`
- 新建 `mc2p/motion_nav/route_body_controller.py`
- 新建 `mc2p/motion_nav/probe_body_controller.py`
- 修改 `mc2p/motion_nav/execution_supervisor.py`
- 修改 `tests/motion_nav/test_execution_supervisor.py`

**接口：**

- 定义最小 `BodyController` 协议：控制者身份、`decide`、`request_stop`、`safe_to_release`。
- 路线控制和探边控制通过薄适配器实现同一协议；动作内部状态仍归各自模块所有。

**交付：**

- [x] 先写结构测试，证明监督者不再导入 `ActionRouteExecutor`、`LandingEdgeProbe` 和 `MotionRouteCoordinator` 的具体类型。
- [x] 迁移现有两类控制者，不改变控制输出、风险授权和仲裁顺序。
- [x] 把 `_last_support_fraction` 移到监督者的交接／诊断证据中；把 `_replacement_failure_reason` 改成规划所有者持有的有类型结果。会话不再保存这两个临时字段。
- [x] 不添加通用插件系统、注册表或配置开关。

**完成条件：** G1-3、G1-4 为 0；以后新增攀爬或游泳控制者时无需给监督者增加类型分支。

**验证：**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_execution_supervisor -v
```

### 任务 6：修正模拟初始化与验收口径

**文件：**

- 修改 `tests/sim/runner.py`
- 修改相关模拟场景构造文件
- 修改 `docs/motion_navigation/acceptance/continuous-height-ground-movement.md`
- 修改 `docs/motion_navigation/acceptance/navigation-coordination-refactor.md`
- 修改 `docs/motion_navigation/stages/navigation-coordination-hardening-plan.md`
- 新建 `docs/motion_navigation/decisions/0041-enforce-event-admission-and-bounded-motion-finalization.md`

**交付：**

- [x] 模拟器在首次规划前建立第一份正式观察，或明确把预装世界作为同一观察的一部分，避免首帧无条件改变 63 个依赖格。
- [x] 重新跑半砖和楼梯场景，记录首路线是否仍会无扰动作废。若仍发生，保留为真实缺陷；若消失，记为测试初始化偏差。
- [x] 把连续高度验收第 44 行改为：已验证动作要求首条命令晚一帧时仍在独立证明窗口内完成；动作中途晚到或缺失必须有界恢复或安全失败。
- [x] 保留 20% 随机逐命令晚到作为鲁棒性分布，所有失败继续留在分母。
- [x] 保留历史 192/200；新增带代码提交和清单哈希的当前复验结果。不能把临时目录中的 198/200 直接写成正式结论。
- [x] D041 记录事件表、转移表、交接证据和有界收尾的长期分工。

**完成条件：** 文档中不再同时要求“首条晚一帧”和“每条固定晚一帧都必须成功”；历史结果与当前结果能清楚区分。

**验证：**

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python tests/sim/run_navigation_matrix.py --manifest tests/sim/manifests/navigation-coordination-smoke.json --output .tmp/review20-smoke
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python tests/sim/run_navigation_seed_scan.py --manifest tests/sim/manifests/navigation-coordination-s5-seeds.json --output .tmp/review20-seeds
```

### 任务 7：专项 Fabric 对照和 S4／S5 复验

**文件：**

- 新建 `scripts/run_navigation_coordination_review20_matrix.py`
- 修改 `scripts/continuous_height_runtime.py`
- 修改 `docs/motion_navigation/acceptance/navigation-coordination-refactor.md`
- 完成后再更新 `AGENTS.md`

**实机场景：**

- [x] 2 格和 5 格下落：在接近、边缘、已提交未应用、离边、空中和落地后分别改目标或取消；取消场景叠加一个晚到客户端 tick。正式批次 `20260929T022643553090Z-705d59f8` 为 24/24。
- [x] 一格下降：首条命令实测晚一帧后完成；每条已验证输入都晚一帧时，以 `input_lost` 有界失败，8 个实际生效样本均晚 1 tick。
- [x] 起跳前移除落点支撑：覆盖离地前 1 至 4 帧。4 帧提前量两次均能用潜行守在原平台；3 帧处于客户端采样相位边界，实测同时出现过零伤害停住和已经离地；2／1 帧已越过可停止点。晚于边界的场景使用独立兜底平台验证失败能有界结束，兜底平台不参加路线或目标选择。
- [x] 半砖和楼梯：正式代表矩阵没有无扰动作废首路线，12/12 通过。

**复验顺序：**

1. 直接相关组件测试；
2. 192 组合中断矩阵；
3. 固定 14 场；
4. 冻结 200 种子扫描；
5. `tests/motion_nav` 全量；
6. 真实后台进程门禁；
7. Fabric 专项对照；
8. 完整连续高度矩阵。

全量组件检查使用：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
```

**关闭条件：**

- G1-1、G1-2、G1-3、G1-4 和 G2 全部通过；
- 192 组合无非终态结果；
- 阶段循环监视器有故意违规测试，并在全部正式模拟中零违规；
- 非法生命周期事件和非法状态转移不会释放身体；
- 固定矩阵 0 个意外结果；种子扫描按成功、有界安全失败和意外结果报告；
- Fabric 已确认本轮三类真实时序边界；
- S4 已重新关闭。S5 还要等待完整连续高度矩阵，不随 S4 自动关闭。

### 任务 7 结果

正式连续高度批次为 `20260929T031538442603Z-29ff17ad`。12 个代表场景和 7 个强化场景全部符合冻结口径。该批次同时通过正式 V3 观察、输入回执、时间归因、零图像、源码冻结和清理检查。

落点支撑移除的判断按机器人真正收到新事实的时间执行。提前 4 帧时，机器人还来得及潜行守住边缘，并以 `landing_support_missing`、零伤害结束。提前 3 帧是离散采样边界；同样的服务端操作可能在离地前或离地后进入控制器。提前 2／1 帧时已经无法靠新输入改变本次离边。测试因此把“至少提前 4 帧”冻结为本轮已证明的可停止范围，不把 3 帧写成确定保证。

为观察越过停止点后的正式收尾，测试在原落点下方设置 3×3 兜底平台。它不写入路线目标，也不改变零伤害任务额度。机器人落到这里仍记为任务失败和额度超出；它只防止测试客户端掉入虚空，使执行器有机会报告终态。

本轮保留三个中间批次：

- `20260929T030430760951Z-f1b35de3`：提前 3 帧移除后坠入虚空，暴露原夹具无法记录越过停止点后的终态；
- `20260929T030943190921Z-f54be202`：19 个场景行为均有界，旧门禁仍错误要求 1 至 4 帧全部零伤害停住；
- `20260929T031242902817Z-937bc6ef`：提前 3 帧这次零伤害停住，证明该点受采样相位影响，不能写成单一确定结果。

完整运动导航回归随后发现：落点支撑变化会先终止路线，但当时仍持有身体的探边控制器没有收到停止请求。它进入 `READY` 后会一直保留控制权。`2512b47` 已在同一个依赖拒绝入口同时停止路线和探边；对应正式链路模拟现在以 `landing_support_missing`、零伤害结束。这个缺陷登记为 NC-020-08，不能因专项 Fabric 已通过而省略全量回归。

## 4. 提交顺序

每个提交只包含一个可验收行为，建议顺序如下：

1. `test: freeze review-20 navigation failures`
2. `fix: enforce navigation event admission`
3. `fix: bound pending motion finalization`
4. `test: detect stalled controller phases`
5. `fix: require evidence for navigation handoff`
6. `refactor: decouple execution supervisor controllers`
7. `docs: align navigation timing and lifecycle gates`
8. `test: record review-20 fabric evidence`

每次提交前运行直接相关测试和 `git diff --check`。最终再运行 AGENTS.md 中的全套检查。不得把两个未跟踪的 `docs/reference` 文件顺带加入提交。

## 5. 复审重点

实现完成后，复审优先检查以下五类边界：

1. 空中动作中断后，下一帧输入晚到或应用记录缺失；
2. 终态收到旧规划、旧接纳、旧控制器和旧信息结果；
3. 身体持续往返运动，但动作阶段和应用证据没有前进；
4. `STOPPING` 在没有新鲜交接证据时尝试恢复执行或重规划；
5. 正式 Runtime 内部出错时，任务失败与身体收尾是否分开处理。
