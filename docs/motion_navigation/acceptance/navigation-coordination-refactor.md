# 导航协调重构验收

日期：2026-09-28

状态：S0 至 S4 已完成。第二十轮整改已补齐事件准入、有界收尾、阶段进展、交接证据和统一控制者协议，并通过专项 Fabric 对照，S4 已重新关闭。S5 仍然打开。原固定矩阵和冻结种子扫描证明的是“结果属于登记范围”，不是全部可达任务成功。完整连续高度能力矩阵仍按原文档单独关闭。

关联：[原阶段计划](../stages/navigation-coordination-refactor-plan.md)、[强化计划](../stages/navigation-coordination-hardening-plan.md)、[第二十轮整改计划](../stages/navigation-coordination-review20-remediation-plan.md)、[架构](../architecture/navigation-coordination-v1.md)、[D039](../decisions/0039-shared-navigation-coordination-and-closed-loop-gates.md)、[D040](../decisions/0040-reopen-navigation-coordination-gates.md)、[D041](../decisions/0041-enforce-event-admission-and-bounded-motion-finalization.md)和[缺陷台账](defect-ledger.md)。

## 1. 基线与已知失败

基线：公开 `8898cf9`，本地源码 `df3e093`。原型来自第三方 `eae4419`，仅解包到本地临时目录运行，没有替换生产代码。14 项中 9 项符合预期、5 项失败，逐项与原始矩阵一致。耗时约数秒，只说明该机器上原型成本低。

| 场景 | 基线结果 | 后续要求 |
|---|---|---|
| 平地、偏移起点 | 2/2 成功 | 保持成功 |
| 上下半砖、20% 输入晚一帧 | 2/2 成功 | 保持成功 |
| 连续四级整格下降 | 成功 | 保持成功和连续性 |
| 2 格直接下落、20% 晚一帧 | 2/2 成功 | 保持成功 |
| 5 格下落，额度 2 点 | 成功，伤害 2 点 | 保持成功及额度一致 |
| 空中取消 | 安全取消 | 保持取消终态及落地责任 |

空中取消的预期结果为安全取消，单独报告，不计作可达目标完成。

| 失败场景 | 基线表现 | 关闭步骤 |
|---|---|---|
| `goal_on_current_support` | `planning_internal_error` | S0a |
| `direct_drop_2_goal_revised_during_probe` | 同上 | S0a，并经 S1 交接回归 |
| `far_landing_L_walkway` | 探边超时后无主坠落 11 格、伤害 8 点，额度为 0 | S1 安全结束；S3 成功通过 |
| `far_landing_L_goal_revised_at_30` | 改目标后无主坠落 11 格、伤害 8 点 | S1 |
| `terrace_two_ledges` | 信息等待超时 | S3 |

以固定提交中的 [原始矩阵](https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/eae4419731fc7ae6cf4ae43e511b4117ecbd996f/reviews/2026-09-28-refactor-plan/matrix-8898cf9.txt)和实际逐项结果为准。正式工具必须输出机器统计，不能沿用人工分组推算。

已知失败登记只用于 S0 确认复现，不纳入正例通过数。某阶段关闭后，其对应登记必须删除；不能长期使用 expected failure 掩盖未关闭的 P0。

S0 修正了原型的晚到回执：应用样本使用实际接受的请求身份，租约按客户端采样次数消耗；同一 tick 到达多个请求时，旧请求被新请求覆盖。修正后 14 场为 **8 个正例通过、5 个上述原型已知失败、1 个新增失败**。`direct_drop_2_20pct_late` 原型报告成功；正式模型中探边取证后入口横向偏移，进入 `releasing` 后持续等待至 400 tick 上限，结果为 `needs_information / landing_visual_evidence_missing`，I4 在运动 tick 130 报无界等待。该项单列为 `formal_receipt_new_failure`，仍需后续协调修复和 Fabric 对照。首轮错误队列模型的记录保留在 `.tmp/coordination-first-pass/`，不能用作生产缺陷证据；修正后完整轨迹在 `.tmp/coordination-smoke-s0-frozen/direct_drop_2_20pct_late.json`。

S0a 继续核对 Java 的 `ClientBehaviorInput.tick`：潜行时客户端把前进和横移采样轴乘以 0.3，再交给物理计算器。原 S0 模拟把离散按键值 1 直接交给计算器，探边速度因此偏快。校正后，每 tick 回执保留浮点采样值；租约耗尽也保留最后已接纳请求身份及中性样本。旧清单保存在 `tests/sim/manifests/navigation-coordination-s0-before-sneak.json`，旧结果不覆盖。

新冻结清单 `tests/sim/manifests/navigation-coordination-smoke.json` 仍是原 14 个场景，结果为 **7 个正例、3 个原已知失败、4 个输入采样校正后新增失败、0 个未登记差异**。`goal_on_current_support` 和 `direct_drop_2_goal_revised_during_probe` 已成功，撤销原来的已知失败登记。新增四项分别为 `direct_drop_2`、`direct_drop_5_budget_2`、`direct_drop_2_cancel_in_air` 和 `direct_drop_2_20pct_late`，均在运动 tick 61 以 `landing_edge_probe_timed_out` 失败。空中取消事件在原定 tick 40 未触发，因为身体尚未进入空中；清单保留这一原定时刻，并单独记录事件未触发。新增失败暂由 S1 保证安全收尾，S2/S3 处理期限与取证。L 形两项仍有 I1/I2/I3 坠落违规，`terrace_two_ledges` 仍失败。逐项结果和浮点采样轨迹在 `.tmp/coordination-s0a-calibrated-frozen/`；独立公开版复跑在 `.tmp/coordination-export-s0a-calibrated-frozen/`。这些结论来自计算器模拟，尚未经 Fabric 对照。

S1 清单保留同一 14 场、同一事件时刻和潜行采样规则。正式结果为 **8 个正例、2 个已登记有界失败、4 个潜行校准后失败、0 个未登记差异**。`far_landing_L_goal_revised_at_30` 从坠落失败变为无伤害到达修订目标；`far_landing_L_walkway` 从 11 格无主坠落变为无伤害的类型化取证失败。`terrace_two_ledges` 仍有界失败，四项直接下落的取证超时未被延长或伪装成成功。原定 tick 40 空中取消仍未触发；另用身体实际离地时的正式路径测试覆盖取消和关闭，并继续观察来源释放后的尾迹。S1 监视器已读取监督者的同帧交接结论、获胜意图和实际提交序列；I5、I7、I8 尚缺后续阶段的正式事件，不能视作端到端通过。

S2 沿用同一 14 场清单，结果仍为 **8 个正例、2 个已登记有界失败、4 个潜行校准后取证失败、0 个未登记差异**。机器清单及逐帧轨迹在 `.tmp/coordination-s2-frozen-v2/`；摘要记录本次未提交源码的内容哈希与脏状态。新增正式路径证据包括：近边 5 格下落的 2 点风险从保留、实际提交、应用转为承诺及落地后的 2 点生命损失核对；空中把额度由 2 降到 0，旧授权继续落地并完成零新增风险路线；旧空中命令缺一帧应用回执同时改目标，保持来源和落地责任，收尾期限耗尽仍不释放，最后给出 `input_lost`；并发失血使观察损失超出预计，账本锁住后续风险并触发 I3。组件测试覆盖恢复生命后再次受伤、20→None→17 的确定损失下界、首次缺样后的第二次下落、容量触顶和拒绝的第 3／第 13 次失败不计为获准重试。I5 现读取任务账本的获准次数和真实进展事件；I7、I8 仍是覆盖缺口。健康下降缺精确伤害来源，混合伤害按保守损失处理；缺样本不伪称完整结算。

## 2. 每个运动 tick 检查的不变量

本表固定 I1 至 I9 编号，替代原型注释中与方案表不一致的编号。

| 编号 | 检查内容 | 证据 |
|---|---|---|
| I1 | 已授权移动及收尾始终有明确身体负责人；取消、超时和依赖变化不使负责人消失 | 当前控制者身份、责任状态、停止事件、实际应用记录 |
| I2 | 授权范围内不出现可避免的无主离地和坠落 | 每 tick 支撑、身体轨迹、恢复许可；不能只看最终位置 |
| I3 | 风险保留与承诺不超过当时授权；声明可恢复扰动内实际运动伤害不超额度 | 风险账本事件、下落及生命变化、策略修订 |
| I4 | 无进展任务有期限和类型化结果；取证耗尽不撤销收尾责任 | 等待起点、运动 tick、单调时钟、收尾状态 |
| I5 | 原因交替、目标更新和重复回执不能绕过重试上限 | attempt_id、进度证据、每原因／本轮／任务总次数 |
| I6 | 本帧导航移动只有一个负责人，最终输入对应获胜意图及其生效窗口 | 提案、仲裁、提交、实际应用四段身份关联 |
| I7 | 所需证明和动作启动门槛真实成立；未知事实不按空气放行 | 路线证明、入口、获取结果、风险及世界依赖 |
| I8 | 缺信息、计算超时、未支持和无路分开；无路结论有声明搜索范围的穷尽证据 | 类型化规划结果及后备选择记录 |
| I9 | 合法取消、目标满足、迟到和世界变化不变成契约异常；不错误报告完成 | 正式任务结果、GoalState 检查、Runtime 状态 |

外力把身体推入不可恢复状态时，不要求必然零伤害；必须冻结扰动适用域，报告无法保证的结果，并保持可用控制责任。故意输入非法契约可被拒绝，不把这种拒绝算成 I9 缺陷。每个监视器都要用故意违反的记录验证能报错。

## 3. 固定矩阵与扰动扫描

固定矩阵包括原型 14 项和下列场景族；每次提交运行最小代表集，目标小于 60 秒。阶段关闭前，每个适用的“场景族 × 扰动类型”冻结 100 个种子，建议种子范围 `280001..280100`；具体起点、时刻及排除原因在运行前写入机器清单并计算哈希。

| 场景族 | 重点条件 |
|---|---|
| 平地与目标对齐 | 目标已满足、同支撑面不同位置、仍在移动、朝向不符 |
| 小高差 | 半砖、土径、已授权积雪；宽场地与一格窄道；起点偏移 |
| 连续下降与多个坎 | 2／4／8 级台阶、远处第一落点、两道和三道坎 |
| 探边与相邻危险 | L 形、旁边坑、身后坑、平台旁墙、上部可见但下部不可见 |
| 风险和旧结果 | 两次有伤害下落、额度调低、取消后晚回执、旧候选回包 |
| 跨模块调用 | 正式战斗驱动追击、放置中目标变化、外力恢复与导航交接 |

扰动必须覆盖：固定晚 1 tick、20% 冻结随机晚 1 tick、额外客户端 tick、丢应用样本、缺锚点、观察断档、有限外力、方块增删、目标修订、取消、正常关闭、后台迟到及死亡。仅支持的组合进入正例；超出证明窗口的扰动验证安全恢复或类型化拒绝，不能要求继续完成。

控制者 × 中断 × 阶段至少包含路线执行器和探边，中断为改目标、取消、超时、依赖变化、INPUT_LOST、缺锚点和关闭；阶段为接近、边缘、已提交未应用、离边、空中和落地后。物理上不适用的组合在清单中写明原因。

任务结束后继续物理推进至少 20 tick，未稳定时继续至收尾观察上限；到上限仍不稳定即失败。成功率以运行前冻结的正例为分母，求解超时不得删样本。内部允许恢复后成功仍可计任务成功，但须报告首次表现、恢复次数和代价。

## 4. 性能与连续性

- 继承连续高度验收的控制耗时 P95 ≤ 8 ms、P99 ≤ 15 ms、最大 < 30 ms；使用正式控制链计时，保留失败帧。
- 继承现有命令生效窗口、采样到实际应用与日志阻塞门槛。模拟墙钟不得代替 Fabric 输入时序。
- 原本允许带速交接的正例，不得为监督者交接新增停稳或空输入帧。连续高度的 1.3 倍参照、直接下落动作参照加 3 tick 保留。
- 分别报告信息获取、回到入口和收尾、后台求解、动作执行、全任务时间；完整端到端时间不能删掉取证成本。
- 重试记录、事件队列和证据缓存必须有容量与满载行为；触顶不能阻塞控制线程或删除未解决的风险承诺。任务总重试上限不能因进度重置。
- 同步替身主要测控制语义；另用实际进程测试观察线程非阻塞、晚结果拒绝、队列替代范围和 worker 死亡。

## 5. Fabric 门槛

先在模拟中关闭对应失败，再运行隔离的 Fabric 场景。每个关键模拟场景有同布局、起点、目标和风险约束的 Fabric 对照。

必测：L 形远处落点正常执行、探边超时、探边中改目标与取消、两道／三道坎、已满足目标、带速路线替换、首条命令晚一 tick、空中失联及依赖变化。事件按真实动作阶段触发，不能用“试次开始前取消”代替边缘取消。

连续高度完整矩阵继续按[原验收](continuous-height-ground-movement.md)第 3 至第 7 节的次数、速度带和形状执行。B10 正式导航入口、C1-B 活动目标、B11 事务和 B12-B 瞄准移动均需跨模块回归。简化模拟尚不包含实体、流体和透明规则；不以模拟通过代替这些实机边界。

每条失败记录必须有最后有效观察、身体状态、控制者、在途命令、风险账本和下一步处置。无法归因到具体一帧的失败不能以“时序波动”关闭。

## 6. 命令与交付物

当前可用检查，仓库根目录 PowerShell：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_segmented_trace tests.test_standalone_java_gates -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
git diff --check
```

S0 新增的正式工具入口：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_closed_loop -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m tests.sim.run_navigation_matrix --manifest tests/sim/manifests/navigation-coordination-smoke.json --output .tmp/coordination-smoke
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m tests.sim.run_navigation_matrix --manifest tests/sim/manifests/navigation-coordination-scan.json --output .tmp/coordination-scan
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m tests.sim.run_navigation_seed_scan --manifest tests/sim/manifests/navigation-coordination-s5-seeds.json --output .tmp/coordination-s5-seeds
```

运行器保存源码提交、场景清单哈希、种子、协议版本、每 tick 观察／回执／事件、实际结果和不变量列表。输出到已有目录应拒绝覆盖。固定矩阵的 unittest 包装必须被现有 motion_nav discover 自动发现；数据清单需显式加入公开导出。

`navigation-coordination-scan.json` 在 S0 只冻结同一 14 场，用于验证扫描入口；它不是第 3 节要求的 100 种子扰动扫描。每项结果有独立 JSON 和逐帧 JSONL，异常仍保存已执行帧；已知失败与新增失败分别计数，任何未登记结果使进程非零退出。S2 已补 I5 的失败尝试、获准重试与进展事件；I7 的动作入口证明和 I8 的搜索穷尽仍无正式事件，结果中的 `coverage_gaps` 明示这一点。I9 只检查 Walk 且无资源下限的本批目标；超出范围的配置会拒绝。

公开版在独立目录先跑测试再做哈希校验，补齐原型依赖的夹具和新的 JSON 清单。远端只更新整理后的 main，评审分支保留。

## 7. 当前进度

| 交付 | 实现 | 组件 | 正式路径模拟 | Fabric 代表 | 完整矩阵 |
|---|---|---|---|---|---|
| 第三方原型 | 临时原型 | 非正式门禁 | 已复跑，5 项失败 | 不适用 | 未完成 |
| S0 正式工具 | 已实现并审查 | 15/15；真实 worker 10/10 | 原回执模型 14 场：8 正例、5 原型已知失败、1 新增失败 | 不适用 | 扫描未完成 |
| S0a 目标检查与采样校准 | 已实现并审查 | motion_nav 582/582 | 校准后 14 场：7 正例、3 原已知失败、4 新失败、0 未登记差异 | 未运行 | 扫描未完成 |
| S1 身体监督与安全交接 | 已实现并本地回归 | motion_nav 602/602 | 14 场：8 正例、2 已知有界失败、4 校准后取证失败；无未登记差异 | 未运行 | 扫描未完成 |
| S2 任务重试与风险 | 已实现并本地回归 | motion_nav 638/638 | 14 场：8／2／0／4／0；增加失联、预算下调和实测超额正式路径 | 未运行 | 100 种子扫描未完成 |
| S3 动作边界门槛 | 已实现 | 已通过 | 远处落点、两道和三道坎通过 | 代表场景通过 | 本轮门槛关闭 |
| S4 状态所有者与转移 | 第二十轮整改已完成 | motion_nav 723/723；事件准入、交接证据和阶段停滞专项通过 | 192 个中断×阶段×晚到组合全部到达终态 | 中断 24/24；连续高度 12 个代表场景和 7 个强化场景符合冻结口径 | 已关闭 |
| S5 综合验收 | 进行中 | 后台进程门禁通过 | 当前固定场景 13 成功、1 安全取消；当前种子扫描 198 成功、2 安全失败、0 意外 | 本轮专项已通过 | 完整速度带、形状和随机迟到矩阵未关闭 |

方案本身不关闭任何 P0/P1，也不撤回历史失败。每步完成后更新本表和阶段记录；新的通过必须附正式链路及源码身份。

S0 本地命令结果：`tests.motion_nav.test_navigation_closed_loop -v` 为 15/15；现有 `test_planner_worker` 与 `test_b10_motion_worker` 真实进程测试为 10/10，覆盖请求替代、晚结果、非阻塞和进程死亡；`test_standalone_export` 为 6/6；完整 motion_nav 为 565/565。正式清单命令耗时 3.47 秒，计数为 8／5／1／0，机器汇总见 `.tmp/coordination-smoke-s0-frozen/summary.json`。独立公开目录 `.tmp/navigation-s0-standalone-frozen/` 内同一清单为 8／5／1／0；哈希验证 `STANDALONE_EXPORT_OK files=625`。这些是计算器模拟和进程工具证据，不替代 Fabric。

S0a 本地命令结果：`python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v` 为 582/582（72.325 秒）；`python -m tests.sim.run_navigation_matrix --manifest tests/sim/manifests/navigation-coordination-smoke.json --output .tmp/coordination-s0a-calibrated-frozen` 为 7／3／0／4／0（正例／原已知／回执模型新增／采样校准新增／未登记）。独立公开目录 `.tmp/navigation-s0a-standalone-calibrated/` 同清单计数一致，哈希验证 `STANDALONE_EXPORT_OK files=628`。测试还覆盖同支撑面位置、带速、朝向、未知支撑、重规划、取消后旧结果、在途旧输入、历史歧义、潜行速度与中性租约样本。

S1 本地命令结果：`python -m unittest discover -s tests/motion_nav -p 'test_*.py' -q` 为 602/602（76.944 秒），`tests.motion_nav.test_navigation_closed_loop`、`test_navigation_route_handoff`、`test_navigation_supervised_interruptions`、`test_execution_supervisor` 的独立公开版专项为 36/36。正式清单和独立公开版清单均为 8／2／0／4／0，未登记差异为 0。源码未提交，因此矩阵摘要除 HEAD 外还记录 `git_dirty`、参与执行的 Python 文件数量和内容 SHA-256；独立导出目录不冒充主仓库 HEAD。清单输出分别在 `.tmp/coordination-s1-frozen-v4/` 和 `.tmp/coordination-export-s1-frozen-v4/`，导出目录为 `.tmp/navigation-s1-standalone-v4/`。哈希验证及具体源码摘要以这两份 `summary.json` 为准。独立导出只包含清单允许的 635 个文件，临时运行结果写在导出目录外。上述均是本地模拟和组件检查，不替代 Fabric 对照、100 种子扰动或 S2 至 S5 的门槛。

S2 本地命令结果：`python -m unittest discover -s tests/motion_nav -p 'test_*.py' -q` 为 638/638（78.996 秒）；`python -m tests.sim.run_navigation_matrix --manifest tests/sim/manifests/navigation-coordination-smoke.json --output .tmp/coordination-s2-frozen-v2` 为 8／2／0／4／0，未登记差异 0，完整逐帧轨迹和未提交源码身份在该目录。独立公开目录的账本、监视器、闭环及监督专项为 63/63；同一矩阵结果在 `.tmp/coordination-export-s2-frozen-v1/`，计数相同。公开导出哈希验证为 `STANDALONE_EXPORT_OK files=638`。这仍是本地计算器模拟，四项取证失败未关闭；未运行新 Fabric 或 100 种子扫描，也未进入 S3。

H1 至 H6 本地结果：`tests/motion_nav` 为 685/685。真实规划进程、动作求解进程和候选接纳检查为 43/43。状态复活探针运行 38 次，没有发现终态后的非法状态写入。风险容量、探边收尾、目标修改、动作入口重新锚定和落点支撑移除都已有正式路径组件回归。

固定清单 `navigation-coordination-smoke.json` 的 14 个结果都属于预先登记范围，但任务层结果为 **12 个成功、2 个有界安全终止、0 个意外结果**。两个安全终止分别来自取消和输入失联场景，不能加入任务成功数。

`navigation-coordination-s5-seeds.json` 固定种子 `280001..280100`。方向性预测余量修正后的复验目录为 `.tmp/h7-seeds-directional-margin-v1/`。200 次运行中有 **192 个任务成功、8 个有界安全失败、0 个意外结果**。其中：

- 半砖上下、20% 概率晚一帧：100/100 最终完成；100 次都发生一次合法恢复，因此首次成功率为 0；
- 两格下降、20% 概率晚一帧：92/100 完成；其余为 3 次探边取证超时和 5 次输入失联；
- 旧冻结白名单覆盖这两类安全失败。原来唯一未声明的 `no_safe_ground_candidate` 来自尾侧预测余量与身后上层方块的虚拟重叠。修正后同一种子正常完成，前侧和侧面余量、真实碰撞及坑边支撑门槛没有放宽。

这些结果说明协调层能在高频迟到下继续执行或给出有界结果，也说明未声明结果已经清零。但仍不能把 192/200 写成完整能力通过。S5 不能因为 0 个意外结果就忽略 8 个任务失败。

代表性 Fabric 证据如下：

| 批次 | 结果 | 说明 |
|---|---|---|
| `20260928T090344946323Z-dd21bb67` | 12/12 | 连续高度代表场景 |
| `20260928T091106094609Z-d3013639` | 40/40、10/10、60/60、40/40 | B10 验证、协调、带速和转向；另有 24 个移动反例 |
| `20260928T092004980212Z-49a4d3fa` | 30/30 | C1-B 活动目标连续近战 |
| `20260928T092410836590Z-d2beeab3` | 60/60、90 次确认放置、24/24 | B11 正例、放置确认、边界与反例 |
| `20260928T092856428742Z-42567a82` | 34/34、8/8 | B12-B 正例与边界；26 个转头帧中 21 帧同时移动 |

B10 第一次运行在外层 300 秒命令期限结束，未形成算法失败；使用 600 秒外层期限重跑后完成。C1-B 第一次运行发现移动目标连续修订时，未获仲裁的待接替路线无法更新；修正为只替换尚未接管身体的候选后重跑 30/30。两项失败均保留，没有从统计中删除。

强化后的专项 Fabric 批次 `20260928T135808548919Z-587c5904` 继续通过原代表矩阵 12/12，并新增：

- `runup-step-down`：先走四格，再下一整格并继续到目标；路线为 `WalkSegment → ControlledDropSegment → WalkSegment`，52 tick，零伤害；
- `landing-support-removed`：在首条不可逆下落输入前移除落点支撑，以 `landing_support_missing` 失败，机器人留在上层，零伤害；
- `fixed-one-tick-late-drop`：输入回执确认首条已验证移动相对请求窗口精确晚 1 tick，动作仍完成，42 tick，零伤害。

本批没有完成连续高度文档要求的全部速度带、方块形状和随机迟到组合。冻结种子扫描也仍有 8 次有界任务失败。因此专项证据通过，S5 和完整连续高度矩阵仍未关闭。

## 8. 第二十轮协调整改的本地证据

第二十轮发现，已验证下落在“中断后又晚一帧”时可能永久停在执行或收尾状态。对应整改在提交 `e5a5d8c` 及其前置提交上完成了本地正式路径复验：

- 生命周期事件先经过有类型准入表，再进入业务处理；状态仍只由转移表改变；
- 输入应用等待、空中收尾和持续无进展阶段都有独立期限；
- 从停止返回执行或规划必须带当前控制者、当前观察和当前世界会话的交接证据；
- 192 个“中断方式 × 动作阶段 × 一帧晚到”组合全部到达终态，没有永久等待；
- `navigation-coordination-smoke.json` 为 13 个任务成功、1 个有界安全取消、0 个意外结果；
- `navigation-coordination-s5-seeds.json` 为 198/200 个任务成功、2/200 个有界安全失败、0 个意外结果。两次失败均为已经声明的输入失联。

本次固定清单哈希分别为 `1995a1581282b99946020688f5a557780b2fc488722603c8a3e87ae79f8e2496` 和 `3821e02f6e18694167565a3818ea67ec5fbda609144d05da93bbe3993abfae81`。结果目录为 `.tmp/review20-smoke-e5a5d8c/` 与 `.tmp/review20-seeds-e5a5d8c/`。

历史 192/200 仍保留在上一节。198/200 是新源码、新初始化基线下的当前复验结果，不覆盖历史证据。半砖和楼梯原先会在第一帧无条件作废路线，原因是测试把 reset 时已经发生的 63 个世界变化带进了首条路线。测试现在把“首次正式观察＋历史测试记忆”作为规划前基线；三类代表路线首帧直接接纳，重试数从 1 降为 0。

以上是计算器闭环。第二十轮专项实机结果见下一节；S4 已据此关闭。完整连续高度矩阵尚未完成，因此 S5 不关闭。

## 9. 第二十轮专项 Fabric 对照

中断矩阵批次 `20260929T022643553090Z-705d59f8` 走正式 `Runtime → RuntimeNavigationDriver → NavigationSession → ExecutionSupervisor` 路径。它覆盖 2 格和 5 格下落、接近／边缘／已提交未应用／离边／空中／落地六个阶段，以及改目标和取消两类中断。取消场景叠加一个晚到客户端 tick。24/24 到达冻结终态，没有永久等待，也没有在空中释放身体。

连续高度正式批次 `20260929T031538442603Z-29ff17ad` 包含 12 个代表场景和 7 个强化场景：

| 场景 | 结果 |
|---|---|
| 连续小高差与四级下降 | 四个方向全部完成；首路线没有被无扰动作废 |
| 直接下落 1／2／3／5 格 | 全部完成；5 格实际伤害 2 点，与额度一致 |
| 助跑后下一整格 | Walk → ControlledDrop → Walk 完成 |
| 移除落点支撑，提前 4 帧 | `landing_support_missing`，留在原平台，零伤害 |
| 移除落点支撑，提前 3 帧 | 客户端采样相位边界；不同批次分别出现零伤害停住和已经离地后的有界失败 |
| 移除落点支撑，提前 2／1 帧 | 已越过停止点；任务明确失败，由测试兜底平台承接，不误报完成 |
| 首条已验证输入晚 1 tick | 完成；回执确认实际晚 1 tick |
| 每条已验证输入都晚 1 tick | 8 个回执样本均晚 1 tick，最终以 `input_lost` 有界失败 |

落点支撑移除场景的任务伤害额度仍为 0。晚于停止边界造成的伤害会记录为额度超出和任务失败，不因测试预期而改写任务授权。下方 3×3 平台只负责阻止测试客户端掉入虚空，不进入路线目标，也不把结果变成成功。

三份失败或旧口径批次继续保留：`20260929T030430760951Z-f1b35de3`、`20260929T030943190921Z-f54be202`、`20260929T031242902817Z-937bc6ef`。它们分别记录无兜底平台、旧门禁错误和 3 帧采样边界，不能删除后只保留最终通过批次。

本轮关闭 S4。S5 继续等待完整连续高度速度带、形状和随机迟到 Fabric 矩阵；当前 200 种子扫描中的有界失败也继续保留在分母中。
