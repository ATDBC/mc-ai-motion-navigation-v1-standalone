# R1 RED／GREEN 记录

## 首轮 R1

首轮测试在实现前分别证明：

| 行为 | RED |
|---|---|
| 显式 `stop_input` | 构造器不接受该字段，`TypeError` |
| 1／12 支撑 | 旧目标检查返回 accepted=true |
| 未关闭风险 | 旧扫描返回 `VERIFIED_CANDIDATE` |
| 条件资源 | 旧搜索错误返回 `FOUND` |
| 非零终速 | 旧搜索错误返回 `NO_TRAJECTORY_IN_BUDGET` |
| 尾迹上限 | 旧扫描错误返回请求级 `NO_TRAJECTORY_IN_BUDGET` |
| 停车输入 | 声明 yaw=1，旧尾迹自行构造 yaw=0 |
| 未恢复风险原因 | `UNRECOVERED_RISK` 尚不存在，`AttributeError` |

首轮 GREEN 后，三份 TP 测试为 78／78。Gap1 两个时序分支都保留 `(last_abandon, first_committed, recovered) = (2, 3, 17)`。

## 唯一审查修正轮

先增加以下测试，再修改生产代码：

1. 请求级 1／12 支撑反例：现行规则非 FOUND；只读注入旧的“任意正支撑”规则后为 FOUND，两支目标检查的支撑比例都低于 0.15。
2. 首个可检查候选返回候选级 `TAIL_NOT_SETTLED` 后，参考搜索继续枚举，并由后续不同候选返回 FOUND。
3. 非零终速状态满足目标检查，但独立停车扫描返回 `CANDIDATE_REJECTED/FINAL_STOP_UNSAFE`，请求不得 FOUND。
4. Gap1 每个时序分支直接断言 `2/3/17`。

上述四项在修改伤害上限前已经通过，说明它们封住既有正确行为，没有把覆盖缺口伪装成实现失败。

伤害余额测试是真实 RED。测试用固定的 1 点停车尾迹隔离分类逻辑：正常候选伤害为 0，停车尾迹伤害为 1。任务余额为 1 时，旧 scanner 把正常候选伤害 0 传给尾迹，因此错误返回 `CANDIDATE_REJECTED`：

```text
FAIL: test_stop_tail_damage_uses_remaining_task_allowance
expected VERIFIED_CANDIDATE, got CANDIDATE_REJECTED
Ran 1 test in 0.003s
FAILED (failures=1)
```

最小修正只把 `_tail()` 的伤害上限改为 `task_damage_budget.maximum_expected_damage_points`。同一测试随后通过：任务余额 1 接受 1 点停车尾迹；任务余额 0.5 返回 `CANDIDATE_REJECTED/FINAL_STOP_UNSAFE`，且不生成 proof。

```text
test_stop_tail_damage_uses_remaining_task_allowance ... ok
Ran 1 test in 0.005s
OK
```

## R2 动作元搜索

R2 先写测试再修改生产代码，RED 依次为：

| 行为 | RED |
|---|---|
| 累计输入层合同 | `ImportError: cannot import name 'InputTier'` |
| 结果计数和获胜层 | 三代表读取 `winning_tier` 时得到 `AttributeError` |
| 冻结动作元矩阵 | `ImportError: cannot import name 'primitive_fixture'` |
| 疾跑层真实覆盖 | `flat_sprint` 期望 A5，旧目标带实际由 A3 找到 |

合同 GREEN 后，A3、A5、A15 必须严格以前一层为有序前缀。每层都包含请求声明的 `stop_input`，最后一层与 `supported_inputs` 完全一致。最小终速默认0，必须有限、非负且不超过 `GoalState` 最大终速。

自审时另补一项真实 RED：A15 中的仅转视角占位输入被“所有新增项都必须移动”的检查发现。GREEN 后，A15 在 A5 后追加五组朝向 Walk／Jump，共15个真实输入；每个步态都有同朝向起跳配对。

动作元身份的自审 RED 发现原结构没有显式登记转向值和 B 保持 tick；GREEN 后身份包含输入层、步态索引、移动 yaw、G／A／B tick 与是否起跳。计数自审 RED 还确认，共同预算在 scanner 内耗尽时不能把未完成扫描计入 `commitment_scans`；计数现只在 scanner 正常返回后增加。

搜索 GREEN 后，动作元按输入层稳定枚举 `G(0..20) → 可选J1 → A(0..16) → B(k)`。每个网格点先检查直接终态，再检查正常停车输入的各个前缀。B 进入返回的正常路线，但证明用的 `StopTail` 仍由 scanner 单独生成。两个时序分支同步执行同一输入。前缀模拟、后续输入层、目标检查和 scanner 共用一个 `CountedPhysics`。

三代表从 A3 扩到 A5／A15 后，仍由 A3 返回同一执行输入、逐支终态和计数。`flat_sprint` 单独记录了选择目标带的边界：20 tick 冻结模板内，A3 最远安全停车 z 为 `6.962757644806954`，A5 为 `9.970127011683733`。目标带固定为 `7.5—8.3`；A3 返回 `SEARCH_EXHAUSTED`，A5 用含 sprint 的输入返回 FOUND。没有调整 P0 预算。

两个连续终态都以真实非零水平速度退出；把旧的“只接受零速”判断注入搜索后不再 FOUND。把最终安全停车尾迹混入正常输入后，逐支最小终速检查拒绝该终态。

## R2 独立 G／J／A 审查与停止

审查反例先在 `cfd36acd` 上得到 RED：`gap_start_1_width_2/A5` 的 `Walk×1 → SprintJump×1 → Walk×5 → Neutral×14` 不在动作元枚举中。原因是候选把 G、J、A 的 forward／sprint／yaw 强制绑定为相同值。

限定修正独立登记并枚举 G、J、A。反例转绿：直接 scanner 和两支目标检查通过；收窄终点后的自由搜索由 A5 返回同一输入，计数为859 nodes、23,900 physics steps、384 tail ticks、11,565 completed candidates和1次完整扫描。

随后按 fail-fast 重跑原冻结矩阵。`turn_90`、`jump_up_after_turn`、`jump_up_after_turn_continue` 三项均变为 `NO_TRAJECTORY_IN_BUDGET/physics_step_budget`，每项为1,375 nodes、65,536 physics steps、32,769 completed candidates、0次完整扫描。这是真实门槛失败，不是允许负例。没有调整场景、枚举顺序或 `4096／65536／40／2` 预算。

按D096退出规则，R2不通过，R3不开始，TP结束。限定修正没有提交；完整diff和结构化结果保留在本目录。候选源码恢复到 `cfd36acd`，其旧94项通过数字保留，但不改写为R2通过。
