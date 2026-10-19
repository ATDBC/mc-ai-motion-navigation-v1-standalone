# R2 独立 G／J／A 限定修正失败

日期：2026-10-10。基线候选：`cfd36acd27f03099ed3f6e1fb9434a2dd3553419`。

审查指出，候选实现把地面推进 G、起跳 J 和空中控制 A 绑定为同一组 forward／sprint／yaw，遗漏合法混合动作元。限定修正先增加反例，再让三段独立枚举。

审查反例 `gap_start_1_width_2/A5` 使用 `Walk×1 → SprintJump×1 → Walk×5 → Neutral×14`。修正后该21 tick候选可以枚举。两支终点都是 `(0.5, 1.0, 3.341460582123732)`，水平速度为0。直接 scanner 返回 `VERIFIED_CANDIDATE`：50 nodes、427 physics steps、384 tail ticks；两支目标检查均通过，支撑比例均为1，风险区间均为 `0／1／13`。收窄到该终点后，自由搜索由 A5 返回同一输入：859 nodes、23,900 physics steps、384 tail ticks、11,565 completed candidates、1次完整扫描。

随后立即重跑冻结矩阵。三个 A15 正例均在原 `4096／65536／40／2` 预算内耗尽 physics step：

| 场景 | 状态／原因 | nodes | physics steps | completed candidates | scans |
|---|---|---:|---:|---:|---:|
| `turn_90` | `NO_TRAJECTORY_IN_BUDGET/physics_step_budget` | 1,375 | 65,536 | 32,769 | 0 |
| `jump_up_after_turn` | `NO_TRAJECTORY_IN_BUDGET/physics_step_budget` | 1,375 | 65,536 | 32,769 | 0 |
| `jump_up_after_turn_continue` | `NO_TRAJECTORY_IN_BUDGET/physics_step_budget` | 1,375 | 65,536 | 32,769 | 0 |

按 D096 的退出规则，本轮不调整场景、预算或枚举顺序，不进入 R3。未提交修正的完整 diff 保存在 `r2-independent-gja-failed.diff`。工作树随后恢复到 `cfd36acd` 的候选代码；该候选仍只是可复现的 R2 候选，不能回写成 R2 通过。
