# Task 5A-R R1：证明分类修正

基线：`5294449598db56b4735f165a7e044b0c5b9d2ead`。平台：项目 Windows `.venv\python.exe`。

范围只包括 R1：安全支撑／安全停车、未闭合风险、资源信息、尾迹上限、实际终速和显式停车输入。没有开始 R2／R3，没有修改 `mc2p` 或正式文档。

## RED 证据

所有测试先在旧实现或只加入前置合同后的实现上单独运行。失败原因均对应尚未实现的行为。

| 行为 | RED 摘要 |
|---|---|
| 显式 `stop_input` 合同 | `TypeError: TrajectorySearchRequest.__init__() got an unexpected keyword argument 'stop_input'`；1 项错误 |
| 1／12 支撑 | `AssertionError: True is not false`；旧目标检查错误接受 `support_fraction=1/12` |
| 未关闭风险 | 期望 `CANDIDATE_REJECTED`，实际 `VERIFIED_CANDIDATE` |
| 条件资源 | 期望 `NEEDS_INFORMATION`，实际 `FOUND` |
| 非零终速 | 期望 `FOUND`，实际 `NO_TRAJECTORY_IN_BUDGET` |
| 尾迹上限 | 期望候选级 `CANDIDATE_REJECTED`，实际请求级 `NO_TRAJECTORY_IN_BUDGET` |
| 尾迹停车输入 | 声明 yaw=1 的中性输入，实际尾迹自行构造 yaw=0 的输入 |
| 未关闭风险独立原因 | 直接组件测试读取 `CandidateRejection.UNRECOVERED_RISK` 时得到 `AttributeError`；1 项错误 |

合同 RED 之后先加入必填 keyword-only `stop_input` 及 `UNPROVEN_RESOURCES` 类型。合同两项转绿后，重新运行两项尾迹测试，取得上表所列的行为 RED，避免前置合同缺失遮蔽真正失败。

`scenarios.py` 原本不在初始文件清单内，但现有代表夹具必须构造 `TrajectorySearchRequest`。父任务补充授权把它纳入 R1，要求所有构造点显式传入 `stop_input`，不允许默认值。

## GREEN 与最终验证

R1 最小实现如下：

- 安全支撑统一要求 `on_ground`、公开支撑查询为 `FEASIBLE`、支撑比例至少 0.15。
- 安全停车在安全支撑上继续要求水平速度为零、不低于允许高度，并保持旧的更强限制：停车尾迹不能比正常候选增加伤害。
- `_risks()` 到循环结束仍有未恢复区间时，以候选级 `UNRECOVERED_RISK` 拒绝，不生成 proof。
- 尾迹上限只产生候选级 `TAIL_NOT_SETTLED`；请求共同 physics step 预算仍保持请求级结果。
- 非空资源下限在搜索前返回 `NEEDS_INFORMATION/unproven_resources`。
- 目标检查不再要求先停零速，向 `GoalState.accepts()` 传入实际水平速度。安全停车尾迹仍由 scanner 独立证明。
- `TrajectorySearchRequest.stop_input` 是无默认值的 keyword-only 字段。它必须零移动、非跳跃、非潜行、非疾跑，并属于当前 `supported_inputs`。scanner 不再自行构造中性输入。

R1 后 Gap1 两个时序分支都保持 `(last_abandon, first_committed, recovered) = (2, 3, 17)`；结果仍为 FOUND，计数仍为 nodes 251、physics steps 1061、tail ticks 436。

Windows 验证：

| 范围 | 结果 |
|---|---|
| 三份 TP 合同／承诺／搜索 | 78／78，41.305s，OK |
| 指定 physics／rollout／gap solver／continuation 回归 | 62／62，16.108s，OK |
| standalone export | 12／12，28.054s，OK |
| 修改文件 `py_compile` | 退出 0 |
| `git diff --check` | 退出 0；只有 Windows 行尾提示 |

自审确认没有修改 `mc2p`、R2／R3 或正式文档。`scenarios.py` 是父任务为必填 `stop_input` 明确补充授权的唯一范围扩展。当前合同只有一层 `supported_inputs`；“每个累计输入层都包含 stop_input”由 R2 新增输入层时继续落实，本轮没有提前实现层结构。

## 唯一审查修正轮

R1 首提交 `ce9b2dcf490c0be32da8e3d5646e379a07829a8d` 保留不重写。审查没有批准进入 R2，要求先补齐请求级和搜索级覆盖，并按 D096 修正停车尾迹伤害上限。

先增加五项检查：请求级 1／12 支撑及旧规则变异、候选 `TAIL_NOT_SETTLED` 后继续枚举、非零终速目标通过但停车尾迹 `FINAL_STOP_UNSAFE`、任务伤害余额内／外、Gap1 逐支精确 `2／3／17`。前四类覆盖中的三类与 Gap1 在代码修改前已经通过；伤害余额测试真实 RED 为“期望 VERIFIED_CANDIDATE，实际 CANDIDATE_REJECTED”。

唯一代码修正把 scanner 传给 `_tail()` 的上限从正常候选伤害改为 `task_damage_budget.maximum_expected_damage_points`。测试中的正常候选伤害为0、停车尾迹伤害为1：余额1时转为 VERIFIED，余额0.5时仍以 FINAL_STOP_UNSAFE 拒绝且无 proof。

唯一修正提交为 `104318a00e1af8302e986e1b6b465a99e6a7d71f`。可提交证据保存在 `evidence/motion_navigation/trajectory-proto-5ar-v1/`。最终回归结果以该目录的 `verification.md` 为准。本轮没有开始 R2。
