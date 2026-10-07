# 动作接口加固与驱动器生命周期验收

日期：2026-10-07。状态：通过。本轮按 D073 验收，历史 M1 仍按原冻结门槛记为未通过。

## 1. 来源和口径

- 计划起点：`39374a6`；其生产代码来自 `bf20f0e`。
- 当前行为和量尺来源：`069b24a`，完整正逆及五组在该提交的干净 Windows 工作区重新采集。后续交付提交仅更新文档与证据。
- 首轮行为和性能来源：`4f3bf8a`。首轮之后曾只增加文档、证据和非语义尾部空行清理；最终复审随后重开两个 P2，并以七个独立修复提交解决。原采集保留，不再冒充修复后的正式行为证据。D058/D061 本轮沿用原来源，依据见第 6 节。
- 公开对照：`5124bd72`；本轮没有推送或发布。
- 行为对照：`evidence/motion_navigation/redesign-m1/round2/`。
- 紧凑证据：`evidence/motion_navigation/action-spec-hardening-v1/`；文件哈希见 `final-manifest.json`。

首次 v3 量尺把登记完整性校验也算成了实现选择，报告了 4 个 ControllerFamily 位置。`metrics-baseline.json` 保留这一原始结果。更正后的 `metrics-baseline-corrected.json` 使用起点提交的文件，区分执行器的 3 处选择和登记的 1 处校验。新的登记校验验证适配器类型、三个必需操作和同帧停止能力，没有删除完整性要求。

驱动器字符串数也按实际 AST 更正：只统计驱动器自身 `self.state` / `self._state` 的流程比较与直接写入，共 29 个位置。方案原来的 36 是预估数；映射中的显示值、继任者的其他生命周期和枚举定义不并入这个数。最终要求仍是自身流程字符串为零。

## 2. 结构结果

| 项目 | 起点 | 最终 | 判定 |
|---|---:|---:|---|
| 直接动作分支，保留 v2 口径 | 42 | 42 | 通过 |
| 执行器控制器代理分支 | 3 | 0 | 通过 |
| 求解策略代理分支 | 1 | 1 | 保留并登记 |
| BodyCommitment 能力判断 | 2 | 2 | 保留 |
| 未分类位置 | 0 | 0 | 通过 |
| 驱动器自身流程字符串位置 | 29 | 0 | 通过 |
| 构造之外的驱动器状态写入方法 | 6 | 1 | `_set_state` |
| Session 行数 | 4,994 | 4,994 | 只报告 |
| 运动导航包生产行数 | 38,431 | 38,495 | 只报告 |
| 七个协调模块行数及包内占比 | 10,680 / 27.79% | 10,680 / 27.74% | 只报告 |

剩余策略分支是 `motion_coordination.py` 中的 `MotionSolveKind.JUMP_GAP` 求解策略选择，本轮未增加或迁移它。量尺能发现别名、模块属性链、match/case、改名枚举和私有枚举。既有生命周期、事实与交接枚举明确区分；未知分类同时进入代理分支和未分类清单，不能静默略过。

`ControllerFamily` 已从生产代码移除。新控制器测试使用独立的 `NewAction`、`NewController` 和 `NewResult`，覆盖创建、结果解释、入口限制、步行同帧接续、完成、取消、输入失联和同帧停止。探针没有继承下降，也没有复用 AIR 控制器。

下降实现只运行时导入低层前提契约与落点证据；没有接纳器、Session、协调 owner 或私有帮助函数导入。旧公开前提类型仍可从 facade 导入，并与低层类型保持同一对象。

## 3. 首轮 Windows 行为和性能（4f3bf8a）

| 检查 | 实测 | 判定 |
|---|---|---|
| 完整正序 | 1547/1547，零失败、零错误、零预期失败 | 通过 |
| 完整逆序 | 1547/1547，零失败、零错误、零预期失败 | 通过 |
| 场景深度哈希 | 两次运行前后都不变 | 通过 |
| 产品 | 2,000 项，完成 1718/2,000，异常与安全事件均为 0 | 与 M1 round2 逐项一致 |
| 协调 | 1,448/1,448 | 与 M1 round2 逐项一致 |
| 扩充故障 | 16/16 | 与 M1 round2 逐项一致 |
| 正式跟随 | 10/10 | 与 M1 round2 逐项一致 |
| 世界变化 | 19/19 | 与 M1 round2 逐项一致 |
| D058 路线复核 | 最慢组 P95 0.2843 ms | 通过 |
| D058 完整准备 | 保留 3413；P95 4.1006、P99 5.0921、最大 10.8163 ms | 通过 |
| D061 长会话准备 | 保留 4096；P95 4.8539、P99 5.4822、最大 9.1149 ms | 通过 |
| D061 完整控制路径 | 4096 帧，最大 38.6045 ms，期限错失 0 | 通过 |
| D061 GC、pacing、trace、身份、行为、安全及终态 | 原正式门槛全部通过 | 通过 |

逐项一致使用原来的场景身份和签名规则，比较结果、原因、作业、输入和轨迹；本轮没有改变归一化规则。两项性能检查在其他正式集合结束后独立运行，使用默认正式参数，没有 test-mode 或缩短样本。

首次候选完整检查为 1,565/1,565，其中新测试模块重复发现了 23 个导入的 fixture 检查。该候选不作为最终签署。随后一轮旧 HEAD 检查因复审问题主动停止，原日志保留在 `.tmp/action-hardening-forward-before-final-review.log`。修正导入方式和当时的复审问题后，在首轮采集 HEAD 重新跑了上述正逆检查。后续两个 P2 和最新运行见第 6 节。失败的 RED 检查、测试夹具修正和复审处理见交接报告，未删除或改写历史 M1 证据。

## 4. 正式命令

在 PowerShell 中从仓库根目录调用项目环境：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python scripts/run_motion_navigation_checks.py --output .tmp/action-hardening-forward-final.json
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python .tmp/action_hardening_verify.py
```

串行验证入口依次调用下面的脚本；子进程使用同一环境的 `sys.executable`。每项实际命令、退出码及每组对比保存在 `validation-commands.json`，性能目录另保留原始 `COMMAND.txt`。

```text
scripts/run_motion_navigation_checks.py --reverse --output .tmp/action-hardening-reverse-final.json
scripts/navigation_s0r_evidence.py --set <faults|world_changes|follow|coordination|product> --output .tmp/action-hardening-<set> --workers 4
scripts/benchmark_d058_route_revalidation.py --output .tmp/action-hardening-d058
scripts/benchmark_d061_long_session.py --output .tmp/action-hardening-d061
scripts/navigation_design_metrics.py --output .tmp/hardening-metrics-final.json
```

直接检查覆盖量尺、导入边界、适配器、动作契约、下降、跨隙、重锚、入口、输入责任、驱动器状态、I/O、交互、继任者和身体收尾。直接组合检查为 76/76；最后的量尺与契约补充检查为 20/20，完整正逆覆盖全部最终检查。这些检查对应首轮范围；最终复审重开的问题和最终检查另列于第 6 节。

根代理的提交范围复核另发现三个文件有多余尾部空行。此前只检查未提交的 diff，没有覆盖整段提交。最终仅删除 `action_requirements.py` 两行、`landing_evidence.py` 两行和驱动器状态测试一行；三个文件的 AST 均与采集版本相同。相关直接检查 61/61，通过 `git diff --check 39374a6..HEAD`。五组、性能和原始来源哈希保持原记录，不因格式清理重跑或回写。

```text
-m unittest tests.motion_nav.test_action_spec_boundaries tests.motion_nav.test_action_preconditions tests.motion_nav.test_runtime_navigation_driver_state tests.motion_nav.test_runtime_backend_io tests.motion_nav.test_runtime_navigation_verified_handoff tests.motion_nav.test_b11_world_change_navigation -q
```

## 5. 证据边界

- Windows 是本轮正式平台；本轮没有新 Linux 或 Fabric 结论。
- 测试新控制器只证明扩展接口，不代表新增正式运动能力。
- D061 紧凑文件保留全部计时样本、统计、门槛与 Gen2 事件；其余 GC 明细留在原始目录，并记录原文件哈希。
- Session、监督者、规划协调器及唯一输入出口的职责不变。
- 历史 M1 仍未通过。下一步只设计终点接近，不自动实施 M2—M5、统一复核或统一异步作业。

## 6. 最终复审后的修复和重新采集

最终复审发现两个 P2：量尺漏掉跨文件小写枚举与重导出；近战等直接消费者仍使用字符串或给只读状态赋值。修复分别保存为 `d8e223d`、`5b50cd1`、`c61f51d`、`5271550`、`21da29c`、`c0685cd` 和 `069b24a`。第一次重新采集在 `d8e223d` 上主动停止，日志留在 `.tmp/action-hardening-p2-forward-d8e223d-interrupted.log`，不作为最终证据。范围复审关闭后，从干净的 `069b24a` 重新执行正式集合。

量尺只静态读取仓库内 Python 模块，不执行 import。支持模块级 Enum/StrEnum/IntEnum 等声明和成员、import/from-import 别名，以及模块/函数中单 Name 的 Assign/AnnAssign；可解析的赋值右侧只支持 Name 或 Attribute。有限类型/成员别名链和重导出递归解析，带循环保护和缓存。每个函数有独立局部表；动态表达式、重复赋值和分支赋值只记录来源，不分析路径。分类使用原始枚举身份，别名不会把能力或交接结果重复记成未知。闭包、对象字段、容器传播、调用返回值和任意控制流不解析；getattr 等表达式只显露可识别的枚举/模块来源。

把所有属性比较直接计入未知曾产生 525 个无关位置，涉及坐标、帧/路线身份与输入值。该诊断未作为正式量尺。当前实仓仍是直接位置 42、执行器代理 0、求解策略代理 1、BodyCommitment 能力 2、未分类 0。210 条 `derived_checks` 经双次实仓测量稳定。来源全为明确能力、中性状态或协议身份时继续原分类；含未知/实现或无法解析模块来源的闭值比较进入代理和未分类。既有 MotionSolveKind 派生的参数数值上限只列 derived constraint，不重复计策略分支。通用规划职责、输入优先级、战斗生命周期和成功条件运算符都在职责表说明。`JAVA_1_21_RULESET` 与 math.pi/inf 按完整根符号身份记录 70 条 `neutral_value_checks`（含安全派生），没有按业务成员名排除。量尺独立三次中位耗时为 1.3800→1.6338 秒，绝对增加 0.2538 秒；量尺不进入实时控制路径。

近战生产比较改为枚举。五个旧测试改为 FakeNavigationSession 的失败入口或 COMPLETE 报告，再由 `_sync_report()` 同步；来源释放、命中、重新接近与目标刷新断言仍保留。C1 的迟到旧结果负例只通过已有 `_set_state` 注入已退休驱动器的枚举结果。Fabric 的动态 expected 先转换为枚举，JSON/日志使用 `.value`。全仓静态检查遍历 `mc2p`、`scripts`、`tests`，手工 rg 核对其余读写和序列化；历史 PointGoalDriver、其他生命周期以及已序列化字符串明确排除。

| 最终检查 | 结果 |
|---|---|
| 定向检查 | 142/142；其中固定近战 14/14、移动近战 43/43、量尺 30/30、全仓静态 2/2 |
| Windows 完整正序 | 1568/1568 |
| Windows 完整逆序 | 1568/1568 |
| 场景深度哈希 | 正逆前后不变 |
| 故障 / 世界变化 / 跟随 / 协调 / 产品 | 16 / 19 / 10 / 1448 / 2000，全部与 M1 round2 逐项一致 |
| 产品完成 | 1718/2000，异常与安全事件为 0 |

本轮五组全部重新采集，避免共享故障输出边界影响遗漏。正式命令为 PowerShell 下运行项目 conda 环境的 `python .tmp/action_hardening_p2_verify.py`；它依次执行完整正逆、五组和定向检查，并 fail fast。每项实际命令、退出码与耗时见 `review-p2/validation-commands.json`。

D058/D061 沿用 `4f3bf8a` 的正式 Windows 性能。两份 benchmark、RuntimeNavigationDriver、Runtime 与跟随测量夹具的 Git blob 未改变；测量路径中仅两个低层文件删除尾部空行，AST 相同。近战/Fabric 消费者和故障输出不进入这两份测量路径。该边界与原性能哈希见 `review-p2/performance-reuse.json`，不宣称在 `069b24a` 重新采集性能。

当前量尺见根目录 `metrics-final.json`，旧版本按原字节另存 `history/metrics-final-4f3bf8a.json`。原正逆、五组和性能文件保持原字节，最新行为位于 `review-p2/`。`final-manifest.json` 指向当前来源，哈希基于实际提交 blob 字节校验。历史 M1 的未通过结论、原失败和基线均不改写。本轮仍没有新 Fabric 或运动能力。
