# TP Task 5A-R 实施计划

日期：2026-10-10。状态：已结束。R1 通过，R2 未通过，R3 未开始。

依据：[D096](../decisions/0096-repair-trajectory-proof-and-use-stable-motion-primitives.md)。

## 1. 实施原则

- 只修改 `experiments/motion_navigation/trajectory_proto/`、对应测试、TP 文档和独立证据。
- `git diff -- mc2p` 必须为空。
- 每项行为先写 RED，确认按预期失败后再修改实现。
- R1、R2、R3 分别提交和独立复核。
- 每一步不过门槛就停止，不进入下一步。

## 2. R1：修正证明

修改：

- `commitment.py`：统一安全停住判断；拒绝未关闭风险；尾迹停不下改成候选拒绝；
- `contracts.py`：增加 `UNPROVEN_RESOURCES`、`UNRECOVERED_RISK`、`TAIL_NOT_SETTLED` 的类型结果，并登记显式 `stop_input`；R1 只检查它存在于当前 `supported_inputs`；
- `reference_search.py`：非空资源目标提前返回；目标支撑使用统一下限；
- 三个现有测试模块：补 RED、固定 Gap1 的 2／3／17 边界。

R1 必须保存：

- 1／12 支撑错误 FOUND 的 RED；
- 非空资源错误 FOUND 的 RED；
- 未关闭风险组件 RED；
- 尾迹上限误伤整个请求的 RED；
- 非零终速满足目标，但独立停车尾迹不安全时仍拒绝的 RED；
- `stop_input` 缺失、不是中性输入或没有出现在当前 `supported_inputs` 时的合同 RED；R2 建立输入层后，再检查它存在于每一层；
- RED 转绿后的相同命令输出。

通过条件：专项检查全绿，Gap1 原风险边界保持 2／3／17，原三个代表空资源结果仍有效。

## 3. R2：稳定动作元前端

新增或修改：

- `primitive_search.py`：累计输入层、动作元身份、共享前缀推进和候选枚举；
- `reference_search.py`：保留公共入口和结果类型，改为调用动作元前端；
- `contracts.py`：原型最小终速下限；
- `scenarios.py`：冻结跨隙、转弯、疾跑和两项连续终态夹具；
- `test_trajectory_proto_primitive_matrix.py`：矩阵、单调性、确定性和反例。

动作元前端必须做到：

- A3 已有动作元在 A5、A15 中逐项保持；
- A5、A15 必须以前一层为严格有序前缀；新输入只追加可用动作元；全部层共享一份请求总预算；
- 两支同步推进，不按单支状态去重；
- 直接终态和制动终态分开；
- 非零终态路线不包含先停稳再起步；
- 返回执行输入不包含只为证明取消安全而生成的停车尾迹；
- 每个正常候选仍由原承诺扫描器验证。

R2 只判断稳健性和计数预算，不用墙钟决定结果。正式矩阵使用 D096 第 4.3 节逐项列出的正例和负例，不以“三方矩阵”等外部简称代替仓库内定义。

## 4. R3：降低成本和正式 Windows 测量

新增 `measure.py`，只负责墙钟测量和 JSON 输出。生产搜索代码不得导入计时模块。

先测原始 R2，定位成本。只对实测热点使用 D096 第 6 节允许的优化。缓存限制在单个请求内，请求结束后释放。

正式证据写入 `evidence/motion_navigation/trajectory-proto-5ar-v1/`，至少包括：

- Windows 环境和源码提交；
- 每场冷启动与稳态分位数，正例与负例分别验收；
- 正例和负例的节点、step、扫描和缓存数据；
- 结果与证明哈希；
- 距首个承诺边界的开环余量，仅作报告；
- R1—R3 的 RED、变异和复核摘要。

通过门槛以 D096 第 5 节为准。没有“有条件进入 5B”。

## 5. 验证命令

每步至少运行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search -v
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

R2 起增加：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_primitive_matrix -v
```

R3 完成后再运行完整 Windows 正序和逆序检查。原型阶段不运行新的 Fabric。

## 6. 提交与复核

| 提交 | 内容 | 复核重点 |
|---|---|---|
| R1 | 证明修正 | 是否仍遗漏承诺或误证资源 |
| R2 | 动作元前端 | 单调性是否由构造保证；连续终态是否真实非零速 |
| R3 | 性能优化与证据 | Windows 门槛、负例成本、确定性和代码范围 |

独立复核发现新的证明完整性问题时，直接停止，不进入下一轮修正。

## 7. 实际结果

R1 按本计划完成并通过。R2 候选提交为 `cfd36acd27f03099ed3f6e1fb9434a2dd3553419`；候选在原冻结检查中为 TP 94／94、相关回归62／62、standalone 12／12，但它把 G、J、A 绑定为同一 forward／sprint／yaw，遗漏审查反例，因此未签署。

唯一限定修正先让审查反例转绿，再重跑原22正／6负矩阵。`turn_90`、`jump_up_after_turn`、`jump_up_after_turn_continue` 三项正例均返回 `NO_TRAJECTORY_IN_BUDGET/physics_step_budget`，共同计数都是1,375 nodes、65,536 physics steps、32,769 completed candidates、0次完整扫描。没有放大预算、改场景或进入性能优化。

R2 未通过。R3 没有创建 `measure.py`，没有运行预热、20次测量或正式 Windows 性能门槛。TP 按D096结束，不进入5B。失败修正只以diff和结构化证据保留；`cfd36acd`仍是候选，不是通过版本。
