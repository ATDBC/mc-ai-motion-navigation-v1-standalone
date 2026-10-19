# TP 准时／晚一 tick 分支专项审查

日期：2026-10-10。审查来源：`43c6f2640692b9cbacb098a0f699f70e562c7dc5`。环境：项目 Windows／Python 3.11。范围：概念、合同、承诺扫描、相关测试和正式运动求解器的时间语义。本文只记录审查，不修改生产代码、原型代码或测试。

## 1. 结论

**当前 `ON_TIME`／`LATE_ONE_TICK` 没有模拟同一候选真实晚一 tick。它重放的是两个同 tick 的入口状态。** 两支都从各自入口立即应用第一条候选输入，第一条命令实际都进入 `anchor_tick + 1`。`allowed_effect_ticks` 虽然写成下一 tick 和再晚一 tick，但没有进入物理重放。

这是 Task 3／4 的 P1 审查缺陷：当前组件证明可以支持“多个给定入口使用同一候选”，不能支持“该候选已经同时通过准时和首条晚一 tick”。在修复前，不应基于现有两支结果进入 Task 5 的时序覆盖或 P0 签署。隔离原型尚无正式控制消费者，因此本次没有证据说明生产机器人已经因此提交不安全输入。

两个同 tick 状态可以用于表达当前位置的不确定性。但这与候选晚一 tick 开始执行是不同问题，不能用位置偏移代替缺失的一步物理计算。

## 2. 代码如何产生这个问题

| 位置 | 实际行为 | 影响 |
|---|---|---|
| `contracts.py:131–139` | 所有入口必须具有相同 `movement_tick_id`；允许生效 tick 必须是 `(t+1, t+2)` | 正确的晚支候选入口 `tick=t+1` 会被拒绝 |
| `commitment.py:239–243` | 对每个入口直接重放同一 `inputs`，没有等待输入，也没有读取分支生效 tick | 两支第一条输入都在 `t+1` 应用 |
| `physics_1_21.py:537` | 每次成功 `step()` 把完整状态的 tick 增加一 | 时间无法靠一个未参与重放的字段改变 |
| `test_trajectory_proto_contracts.py:46–54` | 两支使用完全相同的 `state`，但声明 `(11, 12)` | 合同测试固定了互相矛盾的时间关系 |
| `test_trajectory_proto_commitment.py:48–52` | 双分支夹具仍是 `(state, state)` | 默认双支轨迹完全相同 |
| 同文件 `256–271` | 只把晚支 `z` 从 `.5` 改到 `.52`，tick、速度、冷却和其他状态不变 | 验证的是入口偏移，无法验证首条输入晚到 |

`BoundaryInputs` 当前只保存候选边界序号和不可撤回输入。`_check_evidence()` 检查输入是否等于候选的对应切片，没有绝对 movement tick、控制序号或等待期间输入。请求里的 `input_ledger_id` 和完整证明哈希会绑定调用者给出的数据，但不能证明调用者给出的两个入口确实来自同一锚点的不同应用时间。

## 3. 只读实验证据

使用现有跨隙夹具，入口 `tick=10`、`z=.5`、`vz=.1`，候选为现有 `GAP_INPUTS`。用项目真实 `step()` 得到：

```text
accepted_duplicate_states: VERIFIED_CANDIDATE
declared_effect_ticks: (11, 12)
simulated_first_command_ticks: (11, 11)
branches_are_identical: True

real_neutral_prelude:
  tick=11, position=(0.5, 1.0, 0.6)
  velocity=(0.0, -0.0784000015258789, 0.05460000000000001)
real_late_first_command:
  tick=12, position=(0.5, 1.0, 0.7526000090740741)
  velocity=(0.0, -0.0784000015258789, 0.08331960495444446)
scanner_late_first_command:
  tick=11, position=(0.5, 1.0, 0.6980000090740741)
  velocity=(0.0, -0.0784000015258789, 0.10810800495444446)

real_late_entry_rejected:
  ContractViolation: entry hypotheses must share anchor tick and physics rules
```

中性输入仍会让现有速度产生位移，并改变速度、冷却、支撑和可能的碰撞。即便入口静止且等待前后位置相同，完整状态的 movement tick 仍相差一。只把 tick 加一也不等于真实等待。

补充扫描了同一跨隙夹具的 79 个入口 z 值 `.41–1.19`，没有找到“准时接受、真实中性晚支拒绝”的安全反例。本次明确证明的是时间模型和状态演化不正确，不把这个有限扫描写成实机安全事故或晚支普遍失败。

本次复跑：

```powershell
& 'D:/Miniforge3/Scripts/conda.exe' run --prefix 'D:/My_project/mc_ai/.venv' --no-capture-output python -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment -q
```

结果为 `Ran 37 tests in 33.818s / OK`。通过的是现有组件检查；时间错位仍能在同一代码上复现。

## 4. 正式求解器已经使用的正确时间关系

`motion_solver.py:456` 对 `VerifiedMotionStartVariant` 明确要求：

```python
entry_state.movement_tick_id + 1 == start_tick
```

`_prove_delayed_starts()` 从真实锚点出发，在候选开始前先执行中性等待输入。它调用真实 `step()`，推进到 `start_tick - 1`，记录这段等待触碰到的世界依赖，并检查等待期间仍着地、没有横向碰撞且高度不变。随后 `_replay_verified_commands()` 从推进后的完整状态重放同一不可变命令列表。

这个正式路径为 TP 提供了可复用的时间原则。TP 不需要导入其私有函数，也不能默认把所有输入失联解释为中性。正式路径中的中性等待来自该动作启动窗口的既有前提；TP 的等待输入必须来自其自己的已知账本事实或明确的夹具前提。

## 5. 最小正确合同

设共同观察锚点为 `S_t`，共享候选为 `C=(c0,c1,...)`，`p` 是晚到期间已知会应用的一 tick 输入。这里的“等待输入”就是候选尚未开始时，客户端实际仍会使用的输入。

| 分支 | 候选开始前状态 | 等待输入 | 第一条候选输入生效 tick |
|---|---|---|---|
| ON_TIME | `S_t` | 空 | `t+1` |
| LATE_ONE_TICK | `step(S_t, p).next_state`，tick 为 `t+1` | `(p,)` | `t+2` |

必须同时满足：

1. 两支共享同一原始锚点、世界、目标、伤害余额和账本身份。
2. 每支候选入口满足 `entry_state.movement_tick_id + 1 == allowed_effect_tick`。
3. 晚支完整入口能由共同锚点和已知等待输入重放得到；不是手工偏移位置或只改 tick。
4. 两支都执行同一 `C`，保持相同命令顺序、长度、输入值和 yaw；不允许各支挑选另一条候选。等待输入单独存放，不塞入共享候选前缀。
5. 等待过程的 UNKNOWN、碰撞、支撑、伤害和依赖也经过检查。缺等待输入证据返回 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`，不能自动补中性。

可以采用两个等价入口方案，但应只选一个拥有者：

- **建议：scanner 重放等待输入。** 保留共同锚点，增加不可变的逐支等待输入／证据。scanner 对 ON_TIME 使用空等待，对晚支先执行一 tick 等待，再生成候选入口并重放 `C`。如果继续保存调用者声明的 `entry_states`，scanner 必须比对它与重放结果完全一致，避免两个入口来源。
- **调用者先构造候选入口。** 把晚支入口改成真实 `tick=t+1` 状态；同时交付共同源锚点、等待输入和重放证据。scanner 校验这条来源链后再从入口重放。仅删除“同 tick”约束仍不足以证明晚到。

scanner 已有计数物理适配器、UNKNOWN 分类和依赖收集，第一种方案改动最集中。合同检查只处理结构与 tick 关系；涉及世界的入口重放留在 scanner。

最小 P0 修复可以限定等待期间必须仍在同一安全支撑上，沿用正式启动证明的限制。超出此条件时淘汰候选并报告该覆盖边界。如果要允许等待期间已离地或越过不可停止区，就必须把等待边界也纳入风险区间，保留已经发生的承诺、累计伤害和身体责任；不能直接从较低的晚支入口开始，把此前过程丢掉。

## 6. 账本、边界和证明怎样绑定

候选边界序号 `k` 继续表示已经执行了 `k` 条共享候选命令。时间单独计算：准时边界为 `t+k`，晚支边界为 `t+1+k`。两支的承诺边界序号可以相同，但其绝对 tick 不同，不能把相同序号写成相同实际承诺时间。

逐支事实至少绑定：共同锚点身份、账本快照／修订身份、候选首条生效 tick、等待输入、每个相关控制的提交顺序与可应用 tick。客户端已有回执时，保留其 session、control sequence、actual movement tick 和应用值。模拟中的未来分支只能标成已知窗口下的条件预测，不能写成尚未收到的实际回执。

在每个候选边界，停车尾迹仍先消费该分支当前已经提交且不可撤回的输入，再追加中性停车输入。账本必须将输入值与其可应用 tick 绑定，不能只有“它等于候选切片”。如果等待 tick 实际继续持有旧 WALK，必须重放旧 WALK；把它换成 NEUTRAL 会形成另一条错误时间线。

对 Task 3／4 的具体影响：

- **Task 3 合同。** 改成逐支候选入口与生效 tick 对齐；保留物理规则、session、共享候选和两支预算限制。场景登记可保留，但“注册了两支”不等于已实现真实晚到。
- **Task 4 证明。** 保存共同源状态、逐支等待输入／状态、候选入口、生效时间和账本事实；收集等待过程的世界依赖。`BranchScan.states` 可以继续从候选入口开始，但等待历史必须另存，且不能绕过等待期间的安全与伤害检查。
- **预算。** 等待的真实 `step()` 和必要验证消耗同一 `max_physics_steps`。节点保持有界。定义 `max_trajectory_ticks` 计候选长度还是从共同锚点到退出的总长度，并在报告中同时列出；首条晚一 tick 的候选总用时不能被隐藏。
- **哈希。** 当前 `trajectory_digest()` 会遍历新增 dataclass 字段，可继续使用。加入等待过程、时间安排和事实身份后哈希自然变化。旧证明不自动升级；新证明必须重新扫描。保留旧组件结果及其缺少真实时序验证的边界。
- **validate。** 保留已修正的“只校验本次提交边界”规则，不重新要求未来无关回执。校验仍检查每个声明分支；当前边界额外核对其绝对 tick、等待来源和账本安排。身份或已知安排改变返回 `STALE`，当前必需证据缺失返回 `NEEDS_INFORMATION`，不丢弃不利分支。
- **P2 消费期限。** 真正提交时还必须检查当前观察和命令是否处于已证明窗口；相同 `input_ledger_id` 或未变哈希不能让过期命令重新获得资格。这属于后续驱动接入的必需检查，当前 Task 4 没有实际控制消费者。

## 7. 可执行修复与测试清单

修复限定在原型合同、scanner、两份直接测试和对应 TP 文档，不触碰正式 solver、Session 或 Runtime。

1. 先新增 RED：现有 `(tick10,tick10)/(11,12)` 不得被接受为准时／真实晚一 tick 证明；正确晚支候选入口 `tick11` 必须能表达。
2. 增加已知等待输入和共同来源。由计数 `step()` 重放一 tick，验证完整入口及 `entry.tick+1=effect_tick`；两支逐步断言 `states[k+1].tick == effect_tick+k`。
3. 用非零速度复现本文数值，验证晚支等待后位置、速度、冷却、碰撞和支撑来自真实重放。静止夹具也必须断言晚支 tick 增加一。
4. 验证旧 WALK 持续一 tick 与 NEUTRAL 等待得到不同入口；缺等待证据必须返回信息不足，不能静默取中性。
5. 构造明确区分两支结果的边缘／冷却夹具：准时安全而真实晚支不满足安全或落地条件时不得发 proof。先确认该夹具确实通过 prelude 产生差异，再冻结它；不能再用随意 `.02` 偏移。
6. 验证等待期间 UNKNOWN 单独返回 `UNKNOWN_WORLD`，等待期间绑定的支撑改变使旧证明 `STALE/WORLD_DEPENDENCY`；远处未绑定格仍可复用。
7. 验证相同候选只能提交一份；逐支候选、逐支改 yaw／改顺序被拒绝。两支预算不足仍是 `TIMING_BRANCH_BUDGET`，不退化为只验证准时。
8. 验证等待计入物理 step 预算，精确上限不越界；重复重放计数和哈希一致；改变等待输入、effect tick 或控制身份使旧证明失效。
9. 对齐逐支候选边界的绝对 tick，验证尾迹先消费在途输入；保留两次风险区间、落地后不回写旧承诺、累计伤害及稳定到零的既有检查。
10. 验证 `validate_commitment()` 当前边界事实变化／缺失的分类、已知 stale 优先级，以及未来无关缺回执不阻塞当前提交。时间对齐修复不能撤回 Task 4 已通过的当前边界限定修正。
11. 先运行两份 TP 直接组件检查，再启动修正后 Task 5 参考搜索。同步修改架构、阶段和验收中对“晚一 tick 已验证”的表述，旧审查记录只追加更正，不改写旧事实。

修复完成的最低证据是：从同一真实锚点得到两条可重放时间线，第一条候选命令分别应用于 `t+1` 与 `t+2`，等待输入有明确来源，两支使用相同候选且任一支失败都不会获准执行。新的组件通过仍不代替 P0 参考搜索、P2 闭环或 Fabric 证据。

## 8. 对 `81fd3e8a` 的限定复审

复审来源：`81fd3e8ac763d2fa027aad30525bde5079878d95`。本节只追加检查结果，原审查事实不改写。生产代码和测试均未修改。

**原来的假时序双分支已修复。仍有一项 P1 和一项 P2 未关闭，当前不建议把这轮修正整体签署为通过。** P1 是等待期间已经失去停车能力却仍获得范围外证明；P2 是缺等待证据掩盖已知的当前时间变化。本次没有发现误把这两项反例写成请求级 `FOUND`，也没有正式控制或 Fabric 消费证据。

### 8.1 原报告第 5—7 节的逐项结果

| 要求 | 实际证据 | 结论 |
|---|---|---|
| 共同观察锚点 | `anchor_state` 固定为 ON_TIME 入口；`_prove_entry()` 所有分支从该状态出发 | 通过 |
| 等待来源、tick、提交顺序 | 不可变 `KnownInputApplication` 绑定 session、control sequence、effect tick、输入值、证据身份；等待控制序号小于共同候选首序号；等待 tick 为 `t+1` | 通过当前事实接口；真实账本生产者仍属 P2 接入 |
| 条件预测与实际回执分开 | `ApplicationEvidence` 分开 PREDICTED／APPLIED；实际回执 tick 必须等于 effect tick；条件预测不能携带 actual tick | 通过 |
| 完整晚支入口来自真实 step | scanner 按等待事实调用计数 `step()`，比较完整 `PhysicsState`，位置／速度／冷却造假均被拒绝 | 通过 |
| 首条候选分别在 `t+1`／`t+2` | 跨隙重放真实输出 `(11,12)`；静止等待也推进 tick | 通过 |
| 同一候选及相同提交顺序 | 两支共用单个 `inputs`；候选第 k 条控制序号都为共同首序号加 k，输入值和 yaw 没有逐支优化 | 通过 |
| 等待 UNKNOWN 与缺证据 | 等待输入缺失返回 MISSING_INPUT_APPLICATION；仅等待方向触及 UNKNOWN 的夹具返回 UNKNOWN_WORLD | 通过 |
| 等待高度／碰撞／支撑 | 检查等待前后同高、着地、无横碰及已知支撑比例至少 0.15；实际脚下离开支撑的反例拒绝 | 端点条件通过，等待是否越过不可停止边界未通过，见 P1 |
| 等待伤害与停车尾迹的累计伤害 | 候选和每条尾迹都将 `prelude_states[:-1]` 加入累计伤害；当前限定等待不离地，不新增合法等待落地伤害族 | 通过限定范围；既有一／两次落地额度检查保留 |
| 固定预算与总时长 | 等待 step 与候选／尾迹共用物理预算；精确上限通过，少一步返回 PHYSICS_STEP_BUDGET；40 tick 候选的总时长为40／41 | 通过 |
| 等待依赖进入证明 | 等待计算器和支撑查询依赖合入 dependency_facts，变化后旧证明过期 | 通过 |
| proof 哈希 | 新等待状态、事实、effect tick、绝对边界和计数都经现有 dataclass 递归哈希；同输入确定，旧 Walk／中性等待哈希不同 | 通过 |
| validate 当前边界 | 两支都核对当前的输入、绝对 tick 和应用事实；未来／过去无关缺证据不阻塞当前，验证不重放物理 | 通过正常组合；缺等待证据与已知当前变化的优先级未通过，见 P2 |
| 任一支失败不发 proof | 等待离开支撑、入口造假、UNKNOWN 和预算耗尽均无 proof | 通过已有明确拒绝路径；尚有未被判为失败的范围外等待，见 P1 |
| 文档与正式边界 | 架构、阶段、验收追加旧证据更正；候选长度和总用时分别记录，P0 未签署，时间盒未重置 | 通过记录原则；“等待越过风险区间未交付”的边界未被代码完整执行 |

### 8.2 [P1] 等待仍有支撑，但已经越过最后可停车状态

位置：`commitment.py:216–233` 的等待资格检查，以及 `308–335` 的分支／风险生成。检查端点有支撑，不能推出从该端点还能停车。

下面只读反例复用项目真实物理和已知跨隙世界：

```python
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.commitment import scan_commitment
from tests.motion_nav.test_trajectory_proto_commitment import (
    scan_request, ledger, world, JUMP, WALK, NEUTRAL, physics_state,
)
inputs = (JUMP,) + (WALK,) * 10 + (NEUTRAL,) * 20
state = replace(physics_state(), position=(.5, 1., 1.),
                velocity_blocks_per_tick=(0., -.0784, .1))
local = world(gap=True)
req = scan_request(inputs, state=state, two=True, prelude=WALK, local=local)
result = scan_commitment(req, inputs, local, ledger(req, inputs))
```

实际输出：

```text
status: VERIFIED_CANDIDATE; proof: True
ON_TIME entry z=1.0, tick=10, tail0=SAFE_STOP, risks=()
LATE_ONE_TICK entry z=1.1980000090740741, tick=11, tail0=UNSAFE
late risk:
  last_abandon_boundary=None
  first_committed_boundary=0
  first_committed_tick=11
  commitment_effect_tick=12
  recovered_tick=23
prelude_effect_tick=11
```

等待前可以中性停车。旧 Walk 等待后，身体仍着地且支撑约为 0.17，满足新的 0.15 下限；但它的惯性停车尾迹已经落下坑边。进入不可停止状态发生在等待输入应用于 tick11 时。证明只从候选入口记录风险，丢掉了等待前的可放弃边界，并把开始承诺的输入应用写到候选 tick12。

这条完整跳跃候选最终可以安全落地，所以不能把反例写成“实际伤害已经发生”。缺陷在于原型授予了文档明确未交付的等待风险过程，并报告了错误的承诺输入时间。后续若以 tick12 作为必须完成证明的截止点，就会晚于真实越界。

最小修正：在本轮限定“等待不能越过不可停止区”的模型里，晚支等待结束的候选 boundary0 必须存在安全停车尾迹。若该尾迹为 UNSAFE，返回 `CANDIDATE_REJECTED/PRELUDE_UNSAFE`，不给 proof。UNKNOWN 和尾迹预算耗尽继续保持原类型。现有 scanner 已经计算了该尾迹，无需增加世界模型或正式导航状态。如果要允许这类等待，则需单独把等待边界纳入承诺扫描，正确保留等待前的最后可放弃状态与 tick11 等待输入；不能只改一个报告字段。

最低回归：上面的准时分支继续通过；同一候选加真实旧 Walk 等待的双支必须拒绝。保留“候选入口已处于风险中”的单支既有用途，不把所有正常候选的 tail0 统一要求为安全。

### 8.3 [P2] 缺等待证据抢先返回，掩盖已知当前时间变化

位置：`validate_commitment()` 的 `commitment.py:384–386`。该返回发生在当前边界的已知差异检查之前。

最短反例：

```python
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.commitment import scan_commitment, validate_commitment
from tests.motion_nav.test_trajectory_proto_commitment import scan_request, GAP_INPUTS, ledger, world, JUMP
req = scan_request(GAP_INPUTS, two=True)
local = world(gap=True)
proof = scan_commitment(req, GAP_INPUTS, local,
                        ledger(req, GAP_INPUTS, inflight=(JUMP,))).proof
missing = replace(req, branch_preludes=((), None))
facts = [list(rows) for rows in proof.boundary_inputs]
facts[1][1] = replace(facts[1][1], absolute_tick=13)  # 原 proof 为12
changed = tuple(tuple(rows) for rows in facts)
result = validate_commitment(proof, missing, local, changed, boundary=1)
```

| 组合 | 实际结果 | 应有结果 |
|---|---|---|
| 等待证据完整，当前晚支 boundary1 从 tick12 改为13 | STALE／INPUT_LEDGER，无 proof | 相同 |
| 等待证据缺失，当前晚支 boundary1 同样明确改为13 | NEEDS_INFORMATION／MISSING_INPUT_APPLICATION，无 proof | STALE／INPUT_LEDGER，无 proof |
| 只缺等待证据，当前边界保持原值 | NEEDS_INFORMATION／MISSING_INPUT_APPLICATION，无 proof | 相同 |

它没有误授予执行资格，但破坏了本轮文档的“明确过期的时间安排优先 STALE”，也遗漏了原报告第7节第10项的组合检查。

最小修正：保留请求身份、哈希和世界依赖检查在前；取出当前边界并先核对所有已知差异，之后再统一处理等待或当前证据缺失。不要恢复全表证据校验。增加“缺 prelude＋当前绝对 tick／控制序号改变”的两个反例，分别验证名义和晚支；只有缺失时仍返回 NEEDS_INFORMATION。

### 8.4 本次直接检查与只读变异

与第3节相同的项目 Windows 命令，当前结果为 `Ran 52 tests in 35.058s / OK`。完整运动导航和 Fabric 未重跑，因为本次没有代码修改且审查对象是隔离组件；52项不覆盖上面两项新增反例。

另外在 Python 进程内替换单个函数，运行对应已有行为检查。没有写入或修改源码：

| 内存变异 | 对应检查 | 结果 |
|---|---|---|
| 跳过完整等待入口一致性 | 位置／速度／冷却入口造假 | 1项行为失败，变异被发现 |
| 强制把等待输入替换为中性 | 旧 Walk 与中性等待比较 | 1项行为失败，变异被发现 |
| 跳过真实身体投影支撑查询 | 等待滑出脚下支撑 | 1项行为失败，变异被发现 |
| 等待改用未计数 raw step | 等待预算与40／41总用时 | 1项计数失败，变异被发现 |
| 提交只验证 ON_TIME 当前事实 | 晚支当前缺证据 | 1项行为失败，变异被发现 |
| 提交重新要求所有边界事实 | 未来无关缺证据 | 1项报错，变异被发现；错误为新增全表要求遇到缺事实，并非环境问题 |

独立篡改两支当前事实的绝对 tick、control sequence 或 effect tick 后重新扫描，六种情况均返回 STALE／INPUT_LEDGER且无 proof。单独换 evidence_id 后重新扫描可以形成新的绑定证明，这符合重新扫描的语义；复用旧 proof 时 evidence_id 变化必须过期。准时和晚支真实首条 tick、完整入口、同一候选、固定计数和当前边界限定均得到独立支持。

复审结论仅关闭原来的假双分支问题。上面的 P1／P2 仍须按最小反例处理并复核；不重置两周时间盒，也不把本轮组件更正写成参考搜索或 P0 签署。

## 9. 对 `9f6bd4f3` 的最终复审

来源：`9f6bd4f367c6b653d7104ebbfb3886b59566f17e`。仅核对第8节留下的 P1／P2 及直接回归边界，不扩大到新的产品能力。代码和测试均未修改。

**两项问题已关闭。本次限定范围没有未关闭 P0／P1／P2，可以结束时序修正审查。** 第8节的失败记录保留为历史；本结论不代表参考搜索、P0 整体或 Fabric 已通过。

### 9.1 原最短反例与边界

| 场景 | `9f6bd4f3` 结果 | 结论 |
|---|---|---|
| 第8.2节 `z=1.0/vz=.1`、候选先 Jump、晚支先旧 Walk 等待；等待后 tail0 不安全 | CANDIDATE_REJECTED／PRELUDE_UNSAFE，无 proof | 原 P1 关闭 |
| 同一场景只保留准时分支 | VERIFIED_CANDIDATE，tail0 安全 | 原准时成功保留 |
| 将原晚支 `z=1.1980000090740741` 状态作为无 prelude 的单支入口 | VERIFIED_CANDIDATE，tail0 为 UNSAFE | 未误伤已处于风险中的既有入口用途 |
| 缺等待证据，同时名义或晚支当前 boundary1 的绝对 tick 改变 | STALE／INPUT_LEDGER，无 proof | 原 P2 关闭 |
| 缺等待证据，同时两支任一当前输入值、control sequence 或 effect tick 改变 | STALE／INPUT_LEDGER，无 proof | 新增组合检查覆盖两支、四类事实变化 |
| 只缺等待证据，当前事实不变 | NEEDS_INFORMATION／MISSING_INPUT_APPLICATION，无 proof | 缺信息含义保留 |
| 当前 boundary1 完整，未来 boundary20 或末 boundary33 缺证据 | VERIFIED_CANDIDATE，物理 step 为0 | 未来无关证据不阻塞当前提交 |

scanner 的新增拒绝只作用于“非空等待输入＋候选 boundary0 停车尾迹不安全”。它复用已经算出的尾迹，未将所有初始不可停车状态统一淘汰。等待尾迹计算中的 UNKNOWN 或固定预算失败仍沿既有异常路径返回类型结果，没有被这一拒绝分支改写。

validate 先检查当前边界的已知差异，再统一处理等待和当前证据缺失。请求身份、哈希与世界依赖检查仍在前，当前边界限定没有扩大为全表回执要求。

### 9.2 定向检查和只读回退变异

在项目 Windows 环境通过 conda 调用 Python，以 `python -m unittest ... -v` 运行以下五项：

- `test_wait_cannot_cross_last_safe_stop_even_with_known_support`；
- `test_missing_prelude_does_not_mask_known_current_schedule_changes`；
- `test_submission_uses_current_boundary_only_for_each_branch`；
- `test_commitment_can_precede_physical_takeoff`；
- `test_two_distinct_risks_keep_their_own_boundaries`。

模块均为 `tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests`。结果：`Ran 5 tests in 0.495s / OK`，零失败、错误和跳过。另独立运行原最短反例，确认拒绝／过期结果和无 proof，并验证未来 boundary20／33 缺失时零物理 step 继续通过。

本次只在 Python 进程内做四项回退变异：

| 内存变异 | 实际结果 |
|---|---|
| 删除等待结束后 tail0 安全门槛 | 原等待反例检查失败，变异被发现 |
| 错把门槛施加到所有初始不可停车入口 | 无 prelude 单支对照失败，变异被发现 |
| 恢复缺等待证据的提前返回 | 两支四类当前事实变化共8个子检查失败，变异被发现 |
| 恢复提交时的全表证据要求 | 未来无关缺证据检查报错，变异被发现 |

四项均被发现；未写入源码。检查对象严格限于两项修正及相邻回归，没有重跑正式完整矩阵或 Fabric。

### 9.3 D089 的结束条件

本任务已经完成两轮限定修正：`81fd3e8a` 修正真实晚支模型，`9f6bd4f3` 关闭相邻的等待风险和结果分类回归。第二轮后，本次冻结问题均关闭，因此无需止损，也不建议继续第三轮修正。

按 D089，如果后续同一冻结任务重新发现交付阻塞，应结束当前任务并保留失败；不能自动追加第三轮或更换方法。新范围需要重新获得明确授权。本次关闭的是时序组件审查，P0 的参考搜索、冻结场景和性能门槛仍按原阶段验收，时间盒不重置。
