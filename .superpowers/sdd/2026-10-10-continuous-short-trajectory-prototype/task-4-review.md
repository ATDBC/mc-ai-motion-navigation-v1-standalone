# Task 4 独立审查

日期：2026-10-10。审查提交：`588c6baaf783ca9d629b58ce49b15b210b628010`；比较基线：`77e05bda30c723da12a6e1505a03083e70faf396`。

首轮结论：发现 **2 项 P2 和 1 项 P3**。两项 P2 均位于提交校验，建议修正后进入 Task 5。没有发现 P0／P1 安全缺陷。审查没有修改生产或原型代码；本报告是组件审查，不是 P0 验收。

限定复审结论：`2732e39a` 已关闭上述三项问题，可以进入 Task 5；详细依据见文末复审节。首轮失败与伤害误报的核查过程保留，不改写为首轮通过。

已阅读 task-4 brief/report、D095、TP architecture/stage/acceptance、实施计划、Task 3 contracts/report/review，以及正式 `motion_solver._release_recovery_evidence` 与计算器的落地更新。

## P2：当前提交校验被未来边界缺少应用证据阻塞

位置：`experiments/motion_navigation/trajectory_proto/commitment.py:282`，相关比较为 `:293`。

`validate_commitment(..., boundary=1)` 会调用 `_check_evidence()` 检查整条轨迹的所有边界，再把整组 `boundary_inputs` 与旧证明比较。传入的 `boundary` 除了检查取值范围，没有参与应用事实检查。

已完成扫描的一格跨隙证明中，当前边界 1 的在途 Jump 和账本身份保持不变，只将未来边界 20 或末尾边界 33 的应用事实设为 `None`，结果仍是：

```text
submission1_missing_boundary 20 NEEDS_INFORMATION missing_input_application
submission1_missing_boundary 33 NEEDS_INFORMATION missing_input_application
```

当前输入本可按已验证证明提交，却要等待未来边界的应用事实。将来滚动执行时，这会造成不必要等待，可能错过当前承诺前窗口。过去边界 0 的无关缺证据也产生同样结果。

建议：扫描时仍完整验证整条候选；提交校验只核对每个分支的当前 `boundary` 对应的不可撤回输入与证明，保留完整证明哈希、请求身份、世界依赖和共享候选检查。当前边界缺事实仍返回 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`；未来无关边界缺事实不能阻塞当前提交。增加“当前已知、未来未知”的反例，并验证改成全表校验时该测试失败。

## P2：已知请求过期会被缺输入事实改判为 NEEDS_INFORMATION

位置：`experiments/motion_navigation/trajectory_proto/commitment.py:280`。

请求、锚点、目标和世界身份的过期检查位于应用事实检查之后。如果 `input_ledger_id` 仍相同，任意边界缺事实都会先返回 `NEEDS_INFORMATION`，甚至不检查当前请求是否已经是另一项请求。

复现使用上述有效证明，改为 `request_id="new"` 并令未来边界 20 缺事实，得到 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`。旧证明已经不属于当前请求，按 D095 应是 `STALE/REQUEST_IDENTITY`。目标修订、入口重锚与缺事实同时出现时有相同问题。虽然这条路径仍拒绝执行，但调用者会被误导去等待回执，而不是退场旧证明。

建议：先检查明确的请求、锚点、目标、世界和账本身份过期，再对仍有效的当前提交边界检查缺事实。补“过期与缺事实同时发生”的组合断言，锁定 `STALE` 及对应 typed reason。

## P3：删除全部伤害计算时，现有 15 项组件测试仍然全绿

位置：`tests/motion_nav/test_trajectory_proto_commitment.py:65`。

所有现有正例均为零预计伤害，额度反例也没有覆盖受伤候选。将 `_damage()` 在内存中替换为永远返回 0，现有 15 项测试仍为零失败、零错误。因此，这组测试不能发现整条轨迹只取最大落距、尾迹重置前缀伤害或完全跳过伤害计数等退化。

这次独立审查补充的真实两次落地检查表明当前实现确实会累计：地面顶面依次为 y=9／5／1，40 tick 候选在第 14 和 30 tick 两次落地，各次落地前累计落距为 `3.3462703824114044`；额度 0／1 均 `CANDIDATE_REJECTED/DAMAGE_ALLOWANCE`，额度 2 为 `VERIFIED_CANDIDATE`，累计伤害 2 点。

建议把这类行为反例纳入正式原型测试，同时补停车尾迹保留已走前缀伤害的反例。它是测试覆盖缺口，本次没有据此认定当前伤害算法错误。

## 其他审查结果

- **在途输入和时间边界。** 每个边界的输入必须等于共同候选从该位置开始的前缀；尾迹先消费这些输入，再追加中性输入。现有一格跨隙实际得到 0／1／13，状态 1 仍着地，状态 2 才离地。除上述提交校验外，没有发现已证实的 off-by-one。
- **风险区间。** 已承诺期间即便空中出现安全松键尾迹，也要等真实边界状态着地才关闭。两个独立跨隙保留 0／1／13 和 24／25／37。成功候选的最后状态要求着地，最后尾迹要求安全，因此当前成功路径不会静默丢失未关闭区间。
- **累计伤害。** `_damage()` 已逐次结算落地，不是只取整条轨迹最大值。独立的真实两次落地检查累计为 2 点，额度 1 时拒绝。尾迹使用“已走前缀＋尾迹”重算，没有直接重置余额。现有正式原型测试未覆盖受伤路径，见 P3。
- **两时序分支。** API 只有一条共同输入序列，所有声明入口均重放它；分支预算不足不会删掉第二支。独立把第二支入口改到缺少安全落点的位置时返回 `NEEDS_INFORMATION/UNKNOWN_WORLD` 且无 proof，没有选择第一支的有利结果。当前代码不生成晚到调度，调用者提供两支已解析入口和在途事实，这是 Task 4 已声明的边界；它不证明 P2 的真实输入账本重锚。
- **预算。** 候选逐支重放和每个边界尾迹分别计节点，全部 step 尝试共用物理预算，尾迹另有 1—80 tick 上限。现有边界反例的计数不越过预算；没有新增队列、worker、墙钟决定或无界缓存。校验输入结构仍发生在部分预算检查之前，本次未发现能绕过计数执行物理 step 的路径。
- **哈希和依赖。** 哈希包含请求、共同输入、各分支状态／尾迹／区间、逐边界输入事实、世界事实、ruleset、预算、选项与计数。计算器和所有实际重放尾迹的依赖均合入证明。绑定支撑变 AIR 或 UNKNOWN 会使旧证明 `STALE/WORLD_DEPENDENCY`；远处未绑定格变化允许继续沿用同一证明。
- **geometry revision。** 扫描要求请求与快照 revision 一致；提交时按绑定事实检查，允许全局 revision 因未绑定格变化而增加。忽略全局 revision 的这处差异符合 brief 的“只验证已绑定事实”，不作为缺陷。
- **结果分类。** UNKNOWN、计数耗尽与单候选拒绝未混写为请求级 BLOCKED／FOUND。已知依赖变 UNKNOWN 在提交时归入 STALE 是旧证明过期，符合范围。P2 所述的过期与缺事实优先级需要修正。

## 测试是否能发现关键逻辑删除

在独立 Python 进程中仅用 `unittest.mock.patch` 做内存变异，没有改写文件。运行现有 15 项组件测试：

| 内存变异 | 失败 | 错误 | 判断 |
|---|---:|---:|---|
| `_tail()` 忽略全部在途输入 | 5 | 0 | 会发现 |
| `_risks()` 永远返回空区间 | 2 | 2 | 会发现 |
| `_damage()` 永远返回 0 | 0 | 0 | 不会发现，伤害安全逻辑存在覆盖缺口 |

API 缺失 RED、在途输入行为反例、落地关闭和双区间断言能够验证各自逻辑；它们不能代替受伤候选与累计额度的行为反例。

## 已排除的伤害误报

审查中曾怀疑“落地 tick 的下降没有计入 fall_distance”会低估伤害，并向父任务发出过初步 P1。进一步读取本地 Minecraft 1.21 named JAR 的字节码后，已撤回该判断。

`Entity.fall(double, boolean, BlockState, BlockPos)` 的着地分支直接用已有 `fallDistance` 调用 `Block.onLandedUpon`，随后 `onLanding()`；只有未着地且高度变化为负的分支才累加落距。因此，总位移 3.3 格不能直接替代游戏在落地前记录的 `fallDistance`。当前计算器和 `_damage()` 沿用这项语义，不能据此要求增加最后一段位移。

核查工具为项目 `.venv/Library/lib/jvm/bin/javap.exe -c -p`，读取：

```text
.gradle/caches/fabric-loom/minecraftMaven/net/minecraft/minecraft-merged/1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2/minecraft-merged-1.21-net.fabricmc.yarn.1_21.1.21+build.9-v2.jar
```

该核查没有修改 JAR 或源码。初步误报不计入最终问题列表。

## 独立验证

正式 Windows 项目解释器执行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment -v
```

结果：`Ran 15 tests in 0.443s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

结果：`Ran 93 tests in 50.303s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

结果：`Ran 12 tests in 25.250s / OK`。

`git diff 77e05bda 588c6baa --check` 返回 0。额外反例和内存变异通过 PowerShell here-string 管道传给同一 Windows 解释器。

P2 的最小复现：

```python
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.commitment import scan_commitment, validate_commitment
from tests.motion_nav.test_trajectory_proto_commitment import scan_request, ledger, world, GAP_INPUTS, JUMP

req = scan_request(GAP_INPUTS)
proof = scan_commitment(req, GAP_INPUTS, world(gap=True),
                        ledger(req, GAP_INPUTS, inflight=(JUMP,))).proof
facts = ledger(req, GAP_INPUTS, inflight=(JUMP,), absent=20)
print(validate_commitment(proof, req, world(gap=True), facts, boundary=1))
print(validate_commitment(proof, replace(req, request_id="new"),
                          world(gap=True), facts, boundary=1))
```

未运行完整 motion_nav、参考搜索、P0 冻结矩阵、性能量尺、闭环或 Fabric。通过的旧组件检查不能抵消上述可复现缺陷。

## 限定修正复审：2732e39a

日期：2026-10-10。复审提交：`2732e39a`；比较基线：`1964afd6`。本轮只核对上述两项 P2 和一项 P3 的修正及相邻契约，没有修改生产或原型代码。

结论：**三项均已关闭，未发现新的 P0／P1／P2，可以进入 Task 5。** 这是 Task 4 组件交付的复审结论，不是 TP-P0 整阶段通过。

### 两项 P2 已关闭

`commitment.py:286`—`:304` 现在先检查请求、世界、锚点、目标、账本、证明哈希和已绑定依赖，明确过期立即返回对应 `STALE`。之后才检查当前提交边界的应用事实，不会因缺回执去等待一个已失效证明。

`commitment.py:308` 按指定 `boundary` 从所有声明分支取当前事实。`commitment.py:311` 对任一分支的已知事实差异返回 `STALE/INPUT_LEDGER`，随后才将当前缺事实归为 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`。它没有只检查 ON_TIME，没有重新扫描或选择另一条候选。

独立重放的结果：

| 条件，当前提交 boundary=1 | 结果 |
|---|---|
| 两支当前 Jump 相同，过去 boundary=0 缺事实 | VERIFIED_CANDIDATE，零节点／物理 step／尾迹 tick |
| 两支当前 Jump 相同，未来 boundary=20 缺事实 | VERIFIED_CANDIDATE，零节点／物理 step／尾迹 tick |
| 两支当前 Jump 相同，末尾 boundary=33 缺事实 | VERIFIED_CANDIDATE，零节点／物理 step／尾迹 tick |
| ON_TIME 当前缺事实，LATE_ONE_TICK 当前已知为不同命令 | STALE/INPUT_LEDGER，无 proof |
| 证明哈希损坏，当前缺事实 | STALE/REQUEST_IDENTITY，无 proof |
| 请求已更换，未来 boundary=20 缺事实 | STALE/REQUEST_IDENTITY，无 proof |

新增测试也逐项覆盖请求、世界、目标、锚点、账本及绑定支撑过期与当前缺事实同时发生的分类，分别保留对应 typed reason。每支当前事实变为 `()` 或 `(NEUTRAL,)` 均返回 `STALE/INPUT_LEDGER`，零物理计算。

### P3 已关闭

`test_trajectory_proto_commitment.py:336`、`:352`、`:368` 新增真实计算器行为检查。测试不是人为填写 fall counter：单次落地预计 1 点，额度 0 拒绝、额度 1 接受；两次落地预计 2 点，额度 0／1 拒绝、额度 2 接受。第 14 和 30 tick 的落地状态真实清零落距。

停车尾迹检查同时保留已走前缀伤害：第一次落地边界的纯地面尾迹仍计 1 点；第二次下落中的尾迹和最后边界计 2 点。补充这些断言后，删除伤害计算、只取最大落距和重置尾迹前缀伤害均能被发现。

### 独立内存变异

仅用独立 Python 进程的 `unittest.mock.patch` 或内存重建函数运行现有 21 项组件测试，未改写代码文件：

| 内存变异 | 失败 | 错误 | 证明什么 |
|---|---:|---:|---|
| `_damage()` 永远返回 0 | 3 | 0 | 非零伤害和额度反例有效 |
| `_damage()` 只取整条轨迹最大落距 | 2 | 0 | 多次落地累计反例有效 |
| `_tail()` 只保留当前入口，丢弃已走前缀 | 1 | 0 | 尾迹保留已发生伤害的反例有效 |
| 提交校验只读取 ON_TIME 当前边界 | 2 | 0 | 所有时序分支都会被检查 |
| 恢复全表应用事实检查，并放在身份检查之前 | 7 | 1 | 旧全表阻塞和分类问题会被发现 |
| 只将当前缺事实判断放到 STALE 之前 | 5 | 0 | 过期优先断言有效 |

全表变异的一项错误来自不同已知命令被旧 `_check_evidence()` 抛成 `ContractViolation`；新增断言要求该合法账本变化返回 typed `STALE/INPUT_LEDGER`。变异结果如实保留，不把错误写成断言失败。

### 本轮验证

正式 Windows 项目解释器执行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment -v
```

结果：`Ran 21 tests in 0.915s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

结果：`Ran 99 tests in 50.598s / OK`。`git diff 1964afd6 2732e39a --check` 返回 0。

本轮未重新运行独立公开导出模块、完整 motion_nav、参考搜索、P0 冻结矩阵、性能量尺、闭环或 Fabric。修正没有改变公开导出配置或脚本，组合中的合同检查仍核对原型排除。未将这次组件复审扩大为整阶段验收。
