# AGENTS.md

本文件适用于整个仓库，只作为项目入口。它说明先读什么、当前做到哪里、必须遵守哪些工程边界，以及怎样运行当前检查。具体架构、阶段范围、验收结果和设计变更分别维护在对应目录中，不在这里复制一份“大方案”。

## 1. 项目目标

项目最终要做一个能像联机 2P 玩家一样常驻真实 Minecraft 服务器的机器人伙伴。它应能听从指令，逐步具备移动、战斗、挖矿和建筑等玩家能力。

当前先建设玩家级具身执行底座：把结构化意图变成合法、连续、可中断、可恢复、可评测的行动。先建立可靠的规则基线，再考虑模仿学习或强化学习。LLM 不承担约 20 Hz 的逐帧控制。

## 2. 运动／导航文档入口

运动与导航重构只维护下面四类文档：

| 目录 | 保存什么 | 什么时候更新 |
|---|---|---|
| [architecture](docs/motion_navigation/architecture/) | 模块职责、接口、状态归属和长期不变量 | 职责或公共接口发生变化时 |
| [stages](docs/motion_navigation/stages/) | 当前阶段、交付范围、已完成事项和进入下一阶段的条件 | 阶段开始、范围变化或结束时 |
| [acceptance](docs/motion_navigation/acceptance/) | 场景、命令、指标、实测结果和证据边界 | 每次正式验收时 |
| [decisions](docs/motion_navigation/decisions/) | 为什么改变设计、实施顺序或验收门槛 | 出现需要长期保留的取舍时 |

开始任务时，先读最新的 `stages` 文档和对应的 `acceptance` 文档，再按任务读取相关 `architecture` 和 `decisions`。不要默认加载旧实验日志，也不要把旧工作日志中的“当前状态”当成现行要求。

代码、测试和原始运行证据是实现状态的事实来源。文档与事实冲突时，先查清原因并修正文档，不能为了符合文档而改写或删除失败证据。

## 3. 当前阶段

B01“三版参照与统一比较口径”至 B10“动作求解与连续执行”已经完成。B10-C 已完成规划接纳、八行接续矩阵和默认后台协调链，并完成三方审查整改。C1-A“固定可见目标的单次近战”、C1-B“移动目标连续近战”和 C1-C“真实受击后的动作责任与路线恢复”均已完成正式 Fabric 验收：

- [阶段记录](docs/motion_navigation/stages/B01-reference-baselines.md)
- [验收结果](docs/motion_navigation/acceptance/B01-reference-baselines.md)
- [B02 阶段记录](docs/motion_navigation/stages/B02-unified-data-geometry.md)
- [B02 验收结果](docs/motion_navigation/acceptance/B02-world-knowledge-geometry-core.md)
- [B03 阶段记录](docs/motion_navigation/stages/B03-fixed-route-walk.md)
- [B03 验收记录](docs/motion_navigation/acceptance/B03-fixed-route-walk.md)
- [B04 阶段记录](docs/motion_navigation/stages/B04-known-map-background-planning.md)
- [B04 验收记录](docs/motion_navigation/acceptance/B04-known-map-background-planning.md)
- [B05 阶段记录](docs/motion_navigation/stages/B05-minimal-height-transition.md)
- [B05 验收结果](docs/motion_navigation/acceptance/B05-minimal-height-transition.md)
- [B06 验收结果](docs/motion_navigation/acceptance/B06-version-contracts-ordinary-materials.md)
- [B07 阶段记录](docs/motion_navigation/stages/B07-support-surfaces-shapes-steps.md)
- [B08 阶段记录](docs/motion_navigation/stages/B08-ground-modes-low-clearance.md)
- [B07 验收结果](docs/motion_navigation/acceptance/B07-support-surfaces-shapes-steps.md)
- [B08 验收结果](docs/motion_navigation/acceptance/B08-ground-modes-low-clearance.md)
- [B09 阶段记录](docs/motion_navigation/stages/B09-parameterized-air-transitions.md)
- [B09 验收结果](docs/motion_navigation/acceptance/B09-parameterized-air-transitions.md)
- [B09-R 阶段记录](docs/motion_navigation/stages/B09R-physics-calculator.md)
- [B09-R 验收结果](docs/motion_navigation/acceptance/B09R-physics-calculator.md)
- [B10-A 阶段记录](docs/motion_navigation/stages/B10A-online-motion-foundation.md)
- [B10-A 验收结果](docs/motion_navigation/acceptance/B10A-online-motion-foundation.md)
- [B10–B15 修订计划](docs/motion_navigation/stages/B10-B15-revised-delivery.md)
- [B10-B 阶段记录](docs/motion_navigation/stages/B10B-single-action-solving.md)
- [B10-B 验收结果](docs/motion_navigation/acceptance/B10B-single-action-solving.md)
- [B10-C 阶段记录](docs/motion_navigation/stages/B10C-planning-continuous-execution.md)
- [B10 验收计划](docs/motion_navigation/acceptance/B10-motion-solving-continuous-execution.md)
- [C1 阶段记录](docs/motion_navigation/stages/C1-fixed-visible-melee.md)
- [C1-A 验收结果](docs/motion_navigation/acceptance/C1A-fixed-visible-melee.md)
- [C1-B 阶段记录](docs/motion_navigation/stages/C1B-moving-target-melee.md)
- [C1-B 验收结果](docs/motion_navigation/acceptance/C1B-moving-target-melee.md)
- [C1-C 阶段记录](docs/motion_navigation/stages/C1C-external-motion-recovery.md)
- [C1-C 验收计划](docs/motion_navigation/acceptance/C1C-external-motion-recovery.md)
- [导航层实时原型验收](docs/motion_navigation/acceptance/B02-navigation-layer-live-prototype.md)
- [当前架构](docs/motion_navigation/architecture/mc_motion_navigation_architecture_v1.md)
- [导航层观察原型](docs/motion_navigation/architecture/navigation-layer-observation-v1.md)
- [固定路线步行架构](docs/motion_navigation/architecture/fixed-route-walk-v1.md)
- [已知图规划与后台隔离架构](docs/motion_navigation/architecture/known-map-background-planning-v1.md)
- [一格上升动作架构](docs/motion_navigation/architecture/jump-up-v1.md)
- [方块运动特征](docs/motion_navigation/architecture/block-motion-traits-v1.md)
- [目标状态](docs/motion_navigation/architecture/goal-state-v1.md)
- [动作转换](docs/motion_navigation/architecture/movement-transitions-v1.md)
- [地面移动模式](docs/motion_navigation/architecture/ground-modes-v1.md)
- [支撑面与小台阶](docs/motion_navigation/architecture/support-surfaces-v1.md)
- [空中转换](docs/motion_navigation/architecture/air-transitions-v1.md)
- [运动计算器](docs/motion_navigation/architecture/physics-calculator-1_21-v1.md)
- [B10 动作求解与连续执行](docs/motion_navigation/architecture/B10-motion-solving-continuous-execution-v1.md)
- [C1 战斗纵切片](docs/motion_navigation/architecture/C1-combat-vertical-slice-v1.md)
- [C1-B 移动目标连续近战](docs/motion_navigation/architecture/C1B-moving-target-melee-v1.md)
- [C1-C 外部运动与受击恢复](docs/motion_navigation/architecture/C1C-external-motion-recovery-v1.md)
- [C1-C 使用受控的原版攻击触发受击](docs/motion_navigation/decisions/0018-controlled-disturbance-for-c1c-acceptance.md)
- [B10 后先做战斗纵切片](docs/motion_navigation/decisions/0013-combat-vertical-slice-after-b10.md)
- [B10 审查整改](docs/motion_navigation/decisions/0014-b10-audit-hardening.md)
- [交战记忆与攻击能力边界](docs/motion_navigation/decisions/0015-engagement-awareness-and-attack-capability.md)
- [C1-B 目标事实、生命和固定种子](docs/motion_navigation/decisions/0016-c1b-target-vitality-and-seeds.md)
- [统一处理外部运动](docs/motion_navigation/decisions/0017-unified-external-motion-recovery.md)
- [运动导航代码边界](docs/motion_navigation/architecture/package-boundaries-v1.md)
- [交付顺序决定](docs/motion_navigation/decisions/0001-incremental-delivery-order.md)
- [三版参照决定](docs/motion_navigation/decisions/0002-three-reference-versions.md)
- [B02 Fabric 验收范围](docs/motion_navigation/decisions/0005-b02-fabric-acceptance-scope.md)
- [后续 Fabric 实机范围](docs/motion_navigation/decisions/0006-fabric-only-live-development.md)
- [B03 绝对验收决定](docs/motion_navigation/decisions/0007-b03-absolute-acceptance.md)
- [B04 已知图首版决定](docs/motion_navigation/decisions/0008-b04-known-graph-first.md)
- [先完成已知地形移动](docs/motion_navigation/decisions/0009-known-world-mobility-before-exploration.md)
- [B08 Crawl 与证据取样](docs/motion_navigation/decisions/0010-b08-evidence-and-observed-crawl.md)
- [先做运动计算器](docs/motion_navigation/decisions/0011-b09r-calculator-before-continuous-handoff.md)
- [B10 分步接入动作求解](docs/motion_navigation/decisions/0012-b10-staged-motion-solving.md)

三版参照承担不同的比较用途，没有任何一版被指定为新架构底座。当前已接入 Walk、Sprint、Crouch、合法预置后的 Crawl、一格跨隙和相邻一格零伤害下降，并保留 B05 的一格上升。B09-R 只计算给定输入的后果；B10 已建立状态锚点、真实输入应用账本、命令投影、单动作求解、规划接纳和连续执行。`JumpGap` 默认走后台求解与回执执行链。主动进入 Crawl、攀爬、游泳和特殊地面仍未授权。

## 4. 工程要求

### 不要过度工程化

- 只实现当前阶段实际需要的最小闭环，不提前生成完整架构的空壳。
- 没有实测瓶颈时，不引入额外线程、进程、缓存、框架、插件系统或通用抽象层。
- 不为假想需求增加开关、兼容分支或配置项。新抽象至少要解决当前两个明确重复点，或形成清楚的状态归属和测试边界。
- 优先复用已经验证的游戏接入、观测、动作执行和证据工具；复用前仍要通过新接口的验收。
- 测试应验证行为、接口或失败边界，不编写只复述实现的低价值测试。

### 保持可维护

- 每类长期状态只有一个拥有者，其他模块通过明确接口读取或发送事件。
- 名称直接表达用途。不要用 `reason` 字符串、散落布尔开关或全局变量暗中决定权限和生命周期。
- 模块保持小而完整。一次修改围绕一个可验收行为，不同时改感知权限、路径评分和底层控制。
- 公共契约变更时，同步检查生产者、消费者、测试和四类文档。
- 删除过时代码前先确认没有现行入口和证据依赖；不得删除用户世界、模型、轨迹、检查点、演示数据或失败结果。

### 保持可扩展

- 新增跟随主要扩展目标管理；新增跳跃主要扩展移动能力和动作执行；新增地形主要扩展知识、几何和能力判断。
- 更换寻路算法应主要替换后台规划内部；优化转弯应主要修改局部跟踪。
- 模块可以互相提供数据，但不得为一项新能力复制目标、地图、路线、动作许可或输入状态。
- 先稳定接口和不变量，再优化算法内部。若一次功能必须修改大多数模块，先检查职责是否放错位置。

## 5. 项目级边界

- 正式机器人使用合法的结构化观察。未知空间不能当作空气；测试全知信息必须通过独立 `TEST_ORACLE` 入口，不能进入正式 actor。
- 正式运行默认无头、零图像。`pov_debug` 只用于人工明确要求的诊断，不改变正式行为语义。
- 后续阶段的正式实现与实机验收只使用独立 Fabric。公共观察、动作和运动核心仍保持后端无关；现有 CraftGround 代码保留，但不构成阶段门槛，除非新的设计决定明确恢复。
- 只有动作仲裁后的唯一输入出口可以写玩家控制。动作发出不等于成功，必须由后续观测确认。
- CraftGround 和独立 Fabric 后端使用同一套观察与动作语义。不能直接修改世界、库存或容器来冒充玩家行为。
- 不擅自在外部服务器实验，不静默升级依赖，不恢复无关 stash，不覆盖他人工作区修改。
- 安全、权限和失败分类不能被路径收益抵消。未实现、缺少信息、计算未完成、明确阻塞和超时必须分别表达。
- 默认使用自然、直接的中文编写方案和记录。先说结论，再说明原因、证据和边界。

## 6. 环境与当前检查命令

- 工作区：`D:\My_project\mc_ai`
- 项目环境：`D:\My_project\mc_ai\.venv`
- Python 通过 `D:\Miniforge3\Scripts\conda.exe run` 调用，不依赖系统 Python 或全局 PATH。
- 版本锁定为 Python 3.11、OpenJDK 21、Minecraft 1.21、CraftGround 2.7.4 / runtime 0.1.0、Fabric Loader 0.15.11、Fabric API 0.100.6+1.21。不得静默升级。

在仓库根目录运行运动／导航专项检查：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
```

检查三版公开快照、版本注册表和场景注册表：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/motion_navigation_references.py verify --snapshot-root output/github-navigation-code-share-v2
```

检查归一化工具依赖的分段证据读取：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_segmented_trace -v
```

提交前运行 `git diff --check`，并确认暂存区只包含本次任务文件。后续阶段增加新的正式检查时，把完整命令写入对应 `acceptance` 文档；这里只保留当前入口需要的命令，不累积历史命令。

## 7. 工作方式

- 开始修改前先运行 `git status --short --branch`，保留与当前任务无关的修改。
- 先复现问题或写出失败检查，再实施最小修正；完成后运行直接相关检查。
- 计划不等于实现，组件测试不等于实机通过，有限场景成功不等于能力已经普遍可靠。
- 只提交当前任务文件。除非用户明确要求，不推送、不发布、不启动训练或外部服务器实验。
- 非必要不使用子 agent。使用时必须划清文件所有权，避免多个 agent 同时修改共享文件。
