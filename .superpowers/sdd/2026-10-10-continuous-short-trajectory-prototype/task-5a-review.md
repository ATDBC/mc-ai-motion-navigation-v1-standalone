# Task 5A 独立搜索核心复核

审查对象：`343d10de97f22854e4eb44fa6a55d585cdb7a82d`。平台：项目 Windows `.venv/python.exe`。审查过程中未修改生产代码、原型代码或测试；下文反例和变异只在独立 Python 进程中运行。

**发现一项 P1 和一项 P2，不能签署 Task 5A 整体通过。** 三个冻结代表仍全部 FOUND，原报告的计数与哈希得到独立复现。新的 P1 是风险恢复支撑修正旁边的遗漏：当前支撑一直未达到恢复下限时，未关闭的风险被直接丢弃，搜索随后仍能返回 FOUND。依照本任务报告的 D089 边界，应先停止本轮交付，保留反例并交由父任务决定后续范围，不能自动追加同任务第三轮修正。

已读取 Task 5A brief/report、D095、TP 架构/阶段/验收、Task 3/4 合同与承诺实现、时序专项审查及限定修正报告，以及正式 `step()`、`query_support()`、`GoalState.accepts()`。本审查使用 verification-before-completion 技能，结论只覆盖下面的独立证据。

## 1. [P1] 未关闭风险从证明里消失，搜索仍返回 FOUND

位置：`experiments/motion_navigation/trajectory_proto/commitment.py:274`、`:281`；搜索放行位置为 `experiments/motion_navigation/trajectory_proto/reference_search.py:109`。

`_risks()` 在恢复边界的真实支撑不足 0.15 时继续寻找后续恢复边界。这一步是对的。但它在循环结束时没有处理仍然非空的 `first`，而是直接返回已经关闭的区间。于是，一次明确出现过 UNSAFE 停车尾迹的承诺可以从 proof 中完全消失。末态目标检查只要求 `QueryStatus.FEASIBLE`，没有与恢复下限保持一致，所以这个 proof 还能成为请求级 FOUND。

只读反例全部使用原夹具中的完整石头方块与已知 AIR，没有未知格、特殊材料、自造运动公式或碰撞箱。将 Gap1 的落地地板改为仅在 `x<=0` 存在，身体沿 `x=1.25` 通过，最后停在落点边缘。两支的真实支撑比例均为 1/12；它们满足目前 `_goal_check()` 的任意正支撑条件，却不满足 `_risks()` 的恢复条件。

```python
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture
from experiments.motion_navigation.trajectory_proto.reference_search import reference_search
from mc2p.motion_nav.world_model import Aabb, CellFact, CellKnowledge, WorldView
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET

fixture = representative_fixture("jump_gap_1")
original = reference_search(fixture.request, fixture.world)
facts = {}
for x in range(-3, 4):
    for y in range(-5, 7):
        for z in range(-3, 13):
            fact = fixture.world.cell((x, y, z))
            if y == 0 and z >= 2 and x >= 1:
                fact = CellFact(CellKnowledge.AIR, fact.stamp)
            facts[(x, y, z)] = fact
world = PhysicsWorldView(
    WorldView.detached(fixture.world.session, 3, 0, facts), JAVA_1_21_RULESET)
anchor = replace(fixture.request.anchor_state, position=(1.25, 1., .5))
late = step(anchor, fixture.request.branch_preludes[1][0].tick_input,
            world, JAVA_1_21_RULESET).next_state
goal = replace(fixture.request.goal, region=Aabb(1.1, 1., 2.3, 1.4, 1.05, 3.3))
request = replace(
    fixture.request, entry_states=(anchor, late), goal=goal,
    input_prefix=original.inputs,
    budget=replace(fixture.request.budget, max_trajectory_ticks=len(original.inputs)))
result = reference_search(request, world)
```

实际结果：

```text
FOUND / verified_trajectory
counts = ScanCounts(nodes=73, physics_steps=509, tail_ticks=436)
两支末态 = (1.25, 1.0, 2.900102689134391)
两支 goal accepted = True
两支 support_fraction = 0.0833333333333334
两支 UNSAFE 停车边界 = [3, 4]
两支 final tail = SAFE_STOP
两支 risk_intervals = ()
```

这不是“已经造成伤害”的反例。错误是发出的完整证明遗漏了一次已经发生的承诺，不能再据此确定承诺输入、放弃截止点或承诺后的身体责任。

限定修法：`_risks()` 结束时仍有未关闭的 `first`，必须拒绝候选并不给 proof，例如沿现有 `CANDIDATE_REJECTED/FINAL_STOP_UNSAFE` 返回。当前阶段不支持用未结束区间授予完整证明。末态支撑检查也应使用与原型安全退出一致的公开支撑下限，避免以任意正面积作为已经安全恢复。不要降低恢复下限来消除反例。增加上述两个真实分支的回归，要求失败时仍有类型结果、无 proof，并保留原 Gap1 在 boundary17 恢复的成功。

## 2. [P2] 未完成的资源预测被用来证明资源目标

位置：`experiments/motion_navigation/trajectory_proto/reference_search.py:112`；资源结果在 `experiments/motion_navigation/trajectory_proto/physics.py:49` 返回后，搜索与 scanner 仅保留 `next_state`。正式计算器边界见 `mc2p/motion_nav/physics_1_21.py:548`。

`_goal_check()` 把 `state.food_points` 直接交给 `GoalState.accepts()`。但是现有物理计算器没有完整饥饿管理时钟，每个成功 step 明确返回 `ResourceStatus.CONDITIONAL` 和 `server_hunger_clock_not_in_physics_state`。计算器保留入口的 food/saturation 字段，并不证明它们在退出时仍然相同。搜索忽略这个条件，资源下限可能被未证明的旧值满足。

最小复现：取 `jump_up_straight`，把 anchor 的 `saturation_points` 改为 0，再用真实 Neutral step 构造 late，目标增加 `minimum_resources=ResourceState((("food_points",20.),))`。实际仍返回 FOUND，两支 accepted 为 True、food 为20、saturation为0。独立重放 FOUND 输入后，每一步资源状态均为 conditional，跳跃累计 exhaustion 为0.05，缺失原因均是饥饿时钟不在物理状态中。因此当前证据无法排除服务端在这段期间减少食物值，也无法证明这个新增目标条件。

这项问题不改写三个冻结代表的结果：它们的 `minimum_resources` 为空。它影响当前公开请求合同已接受的非空资源目标。

限定修法：首版可以明确拒绝不支持的非空资源下限，或返回有明确资源缺失原因的 `NEEDS_INFORMATION`，不把缺失资源状态归为 BLOCKED、STALE 或搜索耗尽。若要支持这类目标，应另行提供保守资源下界或完整资源事实，并将它们进入 proof；本轮不需要扩展生产物理模型。增加“空资源目标仍通过、未知资源下界不得 FOUND”的行为检查。

## 3. 去重的实际覆盖边界

`reference_search.py:175`、`:207` 只用 nominal 的完整 `PhysicsState` 去重。没有位置/速度量化，启发函数只决定堆顺序。不过，相同 nominal 状态不意味着相同 late 状态，不能把这种去重解释为双支的安全支配证明。

真实物理小反例：平地 anchor 的 `jumping_cooldown_ticks=2`，late 为真实 Neutral 等待，字母表为 Jump/Walk/Neutral。前缀 `(Jump, Neutral)` 与 `(Walk, Neutral)` 在两 tick 后的完整 nominal 状态严格相等。late 状态却不同：前者高度为 `1.7532000064849853`，后者为1。再为两者接相同的4 tick Walk和7 tick Neutral，并把目标收窄到 `z=1.5728769888062877 ± 1e-8`，Walk 开头的固定候选 FOUND；Jump 开头的固定候选失败，late 末态仍有 `vz=0.029662545239930633` 且 z 为 `1.514338115235853`。

这证明完整 nominal 相等不足以保证两个共享候选可以互相替代。当前默认预算上的自由搜索，两种版本均先耗尽节点；本审查没有把该次耗尽归因于去重，也没有声称已复现默认三族丢解。因此此项作为明确的覆盖边界，不列为新的交付阻塞。现有“不声明全局完整性/最优性”记录正确。coverage 后续应明确“只按 nominal 状态合并，可能丢失不同 late 轨迹或历史资格”，不要只写“完整状态相等”而使读者误以为所有时序分支都参与键。扩展入口冷却、非零速度或其他时序事实前，需要决定保留分支状态/相关历史，还是继续如实接受这种不完整性。

## 4. 已验证的实现与边界

| 检查点 | 独立结论 |
|---|---|
| 通用逐 tick 搜索 | 核心没有动作名、场景ID或 Walk→Jump 组合分支；直接调用计数后的正式 step。场景名称仅在夹具建图层使用。 |
| nominal 搜索、共享候选 | nominal 到达且停止后才扫描；两支从共同 anchor 真实重放 prelude 与同一 inputs，然后分别检查同一目标。晚支目标失败不会仅保留准时支。 |
| 总预算 | 一个 CountedPhysics 覆盖 frontier、前缀、等待、双支候选、尾迹、恢复支撑和目标查询。scanner 接收相同对象；预算不匹配会拒绝。真实调用数199的耗尽反例通过。 |
| 节点/队列界限 | nodes在展开和扫描/查询之前收费，step在调用前收费，失败尝试也收费。frontier和seen生成量同时被总step上限约束，候选长最高40；没有每候选重置预算。 |
| GoalState | 位置、水平停止、pose、mode、yaw均从末态传入；支撑来自公开查询，依赖进入proof。支撑退出一致性与资源完整性存在上面两项缺口。 |
| typed分类 | UNKNOWN保留NEEDS_INFORMATION；候选碰撞、unsupported、invalid仅淘汰，不授请求BLOCKED。预算分别返回NODE/PHYSICS_STEP/TRAJECTORY_TICK/TIMING_BRANCH_BUDGET；当前没有请求级必要条件证明。 |
| 恢复支撑修正 | 原Gap1正确在boundary17恢复，查询收费且依赖绑定；UNKNOWN公共返回注入保留NEEDS_INFORMATION。该证据仍不能覆盖未关闭区间，见P1。 |
| 固定顺序 | 按supported_inputs tuple展开，heap平局以递增serial处理；set只用于成员测试或经排序后的依赖。源码不读墙钟。 |
| 确定性 | 独立进程以PYTHONHASHSEED=1、17、999运行三族，结果/proof哈希及全部计数完全一致，并与报告一致。 |
| 测试与变异 | 四项报告变异均独立复现为1项行为失败、0错误；新P1/P2不被原17项覆盖。原通过数字保留。 |

## 5. 本次 Windows 验证

以下均退出0、零失败/错误/跳过：

| 命令范围 | 实际结果 |
|---|---|
| `tests.motion_nav.test_trajectory_proto_reference_search -v` | 17项，1.342s，OK |
| 合同/承诺/搜索三个模块 `-q` | 71项，39.770s，OK |
| b09r_physics_step、b09r_physics_rollout、b10_gap_solver、motion_continuation_quality `-q` | 62项，16.182s，OK |
| standalone_export `-q` | 12项，27.625s，OK |

只读变异独立结果：跳过晚支目标、给scanner重置预算、跳过真实prelude、恢复区间不检查公开支撑，分别产生1项行为失败、0错误，错误FOUND或错误恢复boundary5被原测试发现。每次使用 `unittest.mock.patch`，源码字节未改。外围命令的退出0是因为它确认了预期失败数，不是变异通过。

三个代表的计数保持原报告：49/227/170、58/368/282、251/1061/436，顺序为总nodes/总physics step/tail ticks。未运行完整P0清单、闭环、正式性能分位数、worker或Fabric；不把本次复核写为P0签署。
