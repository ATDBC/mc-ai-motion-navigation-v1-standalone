# Task 4 真实晚一 tick 限定修正报告

本轮依据 `timing-branch-audit.md` 第 5—7 节，在 Task 5 前修正 P1 时序缺陷。来源为审查提交 `87ed98d03fe08aa1412ab80fa7fbc3f2feeb8425`，聚焦提交为 `81fd3e8ac763d2fa027aad30525bde5079878d95`，时间 `2026-10-10T12:50:54+08:00`。只修改原型 contracts／commitment、两份直接测试和 TP 架构／阶段／验收，未修改生产 `mc2p`、正式 solver、Session 或 Runtime。旧 Task 3／4 报告与审查结果保留，TP 文档追加证据更正。本报告保存在本地代理目录，不纳入源码提交。

## 结论与接口

请求第一支入口就是共同锚点 `S_t`，由只读 `anchor_state` 属性返回。`branch_preludes` 保存每支明确的等待应用事实：ON_TIME 必须为空，LATE_ONE_TICK 必须为恰一 tick 的已知输入，或用 `None` 明确表示缺少应用证据。扫描器不补默认中性。

合同要求候选入口的 tick 分别为 t／t+1，并逐支满足 `entry.tick+1 == allowed_effect_tick`。扫描器拥有等待重放，使用同一个 `CountedPhysics` 从共同锚点实际执行 prelude，把完整状态与请求声明的入口比较。位置、速度、姿态、冷却、接触、资源和其他状态字段不能靠手工偏移冒充重放。

两支从各自真实入口执行同一个不可变候选。第一条候选输入分别实际进入 t+1／t+2；逐步断言候选状态 tick 等于 `effect_tick+k`。等待输入不放进共享候选，不能为各支更换候选值、yaw、长度或顺序。

`KnownInputApplication` 绑定 session、control sequence、输入值、生效 tick、证据身份和证据类型。`conditional_prediction` 只表示已知安排下的条件预测，不冒充尚未收到的回执；`observed_application` 必须保存与 effect tick 相同的 actual movement tick。请求保存共同候选的首个 control sequence，第 k 条命令在两支中都使用该序号加 k。

`BoundaryInputs` 增加绝对边界 tick 和逐项应用事实。边界 k 的绝对 tick 为 `entry.tick+k`，不可撤回输入的生效 tick 从其下一 tick 起算。事实缺少值、时间或应用来源时返回 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`；已知时间安排不一致不能继续获得旧证明资格。

`BranchScan` 另存等待状态、等待依赖、首条候选生效 tick 和从共同锚点到候选退出的 `total_ticks`。`StopTail` 保存绝对边界 tick；`RiskInterval` 保留相对边界、绝对边界 tick，以及对应承诺命令的生效 tick。这些新增字段及请求中的等待安排都进入原有确定性哈希。旧证明不能自动升级。

## 严格 RED 与 GREEN

先在旧实现上补三项行为测试，运行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts.TrajectoryProtoContractsTests.test_same_tick_entries_cannot_claim_real_late_branch tests.motion_nav.test_trajectory_proto_contracts.TrajectoryProtoContractsTests.test_late_candidate_entry_tick_is_one_after_common_anchor tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_real_neutral_delay_changes_nonzero_speed_and_first_effect_tick -v
```

实际输出：

```text
test_same_tick_entries_cannot_claim_real_late_branch ... FAIL
AssertionError: ContractViolation not raised
test_late_candidate_entry_tick_is_one_after_common_anchor ... ERROR
ContractViolation: entry hypotheses must share anchor tick and physics rules
test_real_neutral_delay_changes_nonzero_speed_and_first_effect_tick ... FAIL
actual tick=10, position.z=.5; expected tick=11, position.z=.6
Ran 3 tests in 0.059s
FAILED (failures=2, errors=1)
```

然后先扩充新应用事实和等待接口的测试，再运行两份直接模块：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment -q
```

实际第二轮 API RED 为 `ImportError: cannot import name 'ApplicationEvidence'`，`Ran 2 tests in 0.000s / FAILED (errors=2)`。这来自新增 API 缺失。只实现合同结构后，四项合同定向检查通过；扫描模块因 `BoundaryInputs` 尚无绝对时间与应用安排字段，实际为 `31 errors`。该中间状态没有被当成行为验证完成。

加入 BoundaryInputs 新字段后、尚未修改等待扫描行为时，执行四项关键行为检查：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_missing_prelude_never_defaults_to_neutral tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_declared_late_state_must_match_entire_real_prelude_state tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_any_real_wait_branch_failure_prevents_proof tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_unknown_is_detected_in_prelude_step_with_its_actual_input -v
```

实际 `Ran 4 tests in 0.190s / FAILED (failures=4)`：

- 等待证据缺失仍错误返回 VERIFIED_CANDIDATE。
- 伪造完整晚支入口仍错误返回 VERIFIED_CANDIDATE。
- 真实旧 Walk 等待后滑出支撑的分支仍错误获得 proof。
- UNKNOWN 最后一次调用是 tick11 的候选 Walk，而非 tick10 的真实等待 Strafe。

完成计数等待重放、入口比较、支撑查询、依赖／预算／伤害绑定后：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment -v
```

得到 `Ran 31 tests in 1.449s / OK`。

另补一项“绝对时间已知变更，同时缺应用值”的 RED：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_known_clock_change_is_stale_even_with_missing_application_values -v
```

实际为 `Ran 1 test in 0.071s / FAILED (failures=1)`，错误状态为 NEEDS_INFORMATION，预期为 STALE。最小修正是逐项比较当前边界中已经知道的值、tick 和应用安排：任何已知矛盾先返回 `STALE/INPUT_LEDGER`，没有矛盾的缺事实才等待信息。最终扫描模块为 `Ran 32 tests in 1.503s / OK`。

## 数值与明确区分分支的场景

非零速度共同锚点为 tick10、位置 `(0.5,1.0,0.5)`、速度 `(0,-0.0784,0.1)`。现有计算器的中性等待真实得到：

| 项目 | movement tick | z | vz |
|---|---:|---:|---:|
| 中性等待后的晚支入口 | 11 | 0.6 | 0.0546 |
| 晚支首条 Walk 后 | 12 | 0.7526000090740741 | 0.08331960495444446 |
| 等待期间持续旧 Walk 的入口 | 11 | 0.6980000090740741 | 0.10810800495444446 |

完整入口直接与 `step()` 输出相等，数值另有独立断言。静止场景仍将晚支入口推进到 tick11，首条候选在 tick12 生效。两个入口的真实状态和两个预测时间线均保留，不能挑更有利的分支。

区分安全结果的边缘场景使用共同锚点 z=1.19、vz=.1，一格跨隙，候选为 Jump＋10 tick Walk＋20 tick 中性。准时单支确实得到 VERIFIED_CANDIDATE。真实等待期间持续旧 Walk 则推进到 z=1.388000009074074，身体投影完全越过起点支撑；虽计算器本 tick 的 prior vertical contact 仍使 on_ground 为真，公开支撑查询发现实际支撑已经没有。双支候选返回 `CANDIDATE_REJECTED/PRELUDE_UNSAFE` 且无 proof。

等待期间起跳、发生横碰或改变高度也单独拒绝。UNKNOWN 场景让 nominal 全候选通过，但等待 Strafe 读取到缺失格；观察实际调用的 state tick 和输入值，确认在 tick10 的 prelude step 返回 `UNKNOWN_WORLD`，不是因随后候选碰巧失败而假覆盖。

## 支撑、伤害与预算范围

本轮只允许等待前后同高、真实着地、无横碰，且真实支撑比例不低于 0.15。0.15 来源是 `action_route_executor._safe_ground_state` 的既有正式执行下限，原型仅记录来源，不导入生产私有函数。实际面积由现有公开 `query_support()` 计算，不复制运动或支撑公式。查询读取的事实、等待 step 的全部依赖都绑定到证明。

“同一支撑”在本轮表示同高已知安全支撑面，不要求脚下仍是同一个方块 ID。它不允许等待期间离地、改变高度或越过实际支撑边缘；更一般的空中等待与等待风险区间未实现。超出范围是单候选拒绝，不能归入请求级 BLOCKED。

等待状态加入正常轨迹伤害计算，停车尾迹仍用“等待历史＋已走候选前缀＋尾迹”累计，不重置额度。两次真实落地的旧测试全部保留；本轮没有修改 `_damage()` 的计算规则。

`max_trajectory_ticks` 继续限制共同候选长度，最高 40。晚支另有一 tick 已知等待，锚点到退出最长 41，`BranchScan.total_ticks` 单独记录。等待的真实 `step()` 计入与候选和全部停车尾迹相同的 `max_physics_steps`。节点仍按每支展开及每个边界停车计数；没有新增搜索器、队列、缓存或 worker。

40 tick 中性双支组件明确记录总长 40／41。所有物理调用的计数等于“候选步数＋等待步数＋所有实际尾迹步数”；精确 step 上限允许完成，上限减一则 typed PHYSICS_STEP_BUDGET 且不越界。重复输入的结果、计数和哈希一致，分支预算不足仍是 TIMING_BRANCH_BUDGET，不删除晚支。

## 提交校验没有撤回上轮修正

提交先检查明确过期的请求、世界、锚点、目标、账本、等待来源、控制序号和证明依赖，再只核对各支当前边界。绝对边界 tick、应用 effect tick、control sequence 或等待证据身份改变，旧证明返回 typed STALE；当前必要等待／应用证据缺失返回 NEEDS_INFORMATION。不重新重放或选择另一条证明。

过去／未来的无关回执缺失仍不阻塞当前提交。各支当前不可撤回输入依然先进入停车尾迹。同一输入前缀、两个独立风险区间、落地后不回写旧承诺、累计落地伤害、稳定速度后滑行到零、已知 stale 优先级等旧行为检查均保留。

## 变异检查

使用独立进程内存变异，没有改写源码：

```powershell
@'
import unittest
from unittest.mock import patch
import experiments.motion_navigation.trajectory_proto.commitment as implementation
from tests.motion_nav.test_trajectory_proto_commitment import TrajectoryProtoCommitmentTests
names = (
 'test_declared_late_state_must_match_entire_real_prelude_state',
 'test_any_real_wait_branch_failure_prevents_proof',
 'test_unknown_is_detected_in_prelude_step_with_its_actual_input',
 'test_prelude_steps_dependencies_and_total_duration_are_bound',
)
def skipped(counter, request, branch_index, world):
    states = (request.anchor_state,) if branch_index == 0 else (request.anchor_state, request.entry_states[branch_index])
    return states, ()
with patch.object(implementation, '_prove_entry', side_effect=skipped):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(TrajectoryProtoCommitmentTests(name) for name in names))
    assert len(result.failures) == 4 and not result.errors
with patch.object(implementation, '_safe_support', return_value=True):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
       TrajectoryProtoCommitmentTests('test_any_real_wait_branch_failure_prevents_proof')]))
    assert len(result.failures) == 1 and not result.errors
'@ | .\.venv\python.exe -
```

跳过等待重放：`Ran 4 tests in 0.165s / FAILED (failures=4)`，包括物理计数 198 与必须包含等待的 199 不相等。忽略实际支撑：`Ran 1 test in 0.067s / FAILED (failures=1)`。命令退出 0，是末尾断言确认了预期失败数量，不代表变异实现通过。

## 最终 Windows 验证

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment -q
```

`Ran 52 tests in 37.765s / OK`（合同 20、扫描 32）。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

`Ran 62 tests in 16.080s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

`Ran 12 tests in 27.796s / OK`。公开排除已覆盖两个直接模块，不修改导出配置或脚本。`git diff --check` 与 `git diff --cached --check` 均返回 0。提交只包含 7 个限定文件，提交后的 `git status --short` 为空。检查后只有文档、报告写入和独立内存变异，没有再改实现或测试。

## 文档与剩余范围

TP 架构新增明确时间线与事实定义；阶段／验收追加旧双支证据边界的更正，不改写旧通过数字或审查结论。首笔代码提交的两周时间盒不变。

未实现或运行 Task 5 搜索器、P0 全部冻结正例、正式性能量尺、P2 当前观察／真实命令期限消费、实际客户端账本接入、闭环、Fabric 或 worker 生命周期。本次“真实晚一 tick”指现有计算器确实重放了一 tick 等待，不能宣称已收到实际客户端迟到回执。新的组件通过仍不签署 P0。

## 末次限定修正：等待后的停车资格与分类优先级

以下为复审 `e5d44a748aae090bf55dd908ccdd4f84ef481fa0` 第 8 节之后的末次限定修正，聚焦提交为 `9f6bd4f367c6b653d7104ebbfb3886b59566f17e`，时间 `2026-10-10T13:01:35+08:00`。上文 52 项是首轮真实时序修正的原结果，不覆盖复审新反例，保留不回写。本轮只改 scanner、扫描测试和 TP 架构／阶段／验收；生产代码及旧审查原始结论未改，未扩展等待风险模型。

先新增两个行为测试，执行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_wait_cannot_cross_last_safe_stop_even_with_known_support tests.motion_nav.test_trajectory_proto_commitment.TrajectoryProtoCommitmentTests.test_missing_prelude_does_not_mask_known_current_schedule_changes -v
```

实际 RED：`Ran 2 tests in 0.321s / FAILED (failures=9)`。

第一项用 tick10、z=1.0、vz=0.1 的共同锚点，候选为 Jump＋10 tick Walk＋20 tick 中性。准时单支真实通过且 tail0 为 SAFE_STOP；旧 Walk 等待得到 tick11、z=1.1980000090740741，仍着地、同高、无横碰，但该入口的停车尾迹已为 UNSAFE。旧双支错误返回 VERIFIED_CANDIDATE。这是停车资格不足，不是已经发生实际伤害。

新守卫只针对非空 prelude：已计算的候选 boundary0 尾迹必须 SAFE_STOP，UNSAFE 则 `CANDIDATE_REJECTED/PRELUDE_UNSAFE` 且不发 proof。UNKNOWN 和尾迹预算耗尽由原 `_tail()` 继续保留原类型。单支已有承诺入口仍允许完成安全候选：测试把真实晚支入口作为新单支锚点，确认其 tail0 虽 UNSAFE，既有单支用途仍通过。没有把所有候选 tail0 都改为必须安全，也没有补一个新的等待风险状态机。

第二项在 prelude 缺失的同时，分别改变准时与晚支当前 boundary1 的绝对 tick、输入值、control sequence 和 effect tick，8 个组合原本都错误返回 NEEDS_INFORMATION。特别保留审查要求的晚支绝对 tick12→13。修正把缺 prelude 的提前返回移动到所有当前已知字段比较之后；任一已知矛盾先 `STALE/INPUT_LEDGER`，只有全部已知字段一致且缺必要事实时才等待信息。原请求身份、世界依赖、哈希的优先级及未来无关事实不阻塞当前的规则都保留。

最小实现后执行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment -q
```

实际 `Ran 34 tests in 1.806s / OK`，原 32 项全部保留。

### 删除守卫与旧提前返回的变异

用 AST 在独立进程内删除这次新增的精确守卫，另恢复旧的缺 prelude 提前返回位置；未写入源码：

```powershell
@'
import ast
import inspect
import unittest
from unittest.mock import patch
import experiments.motion_navigation.trajectory_proto.commitment as implementation
import tests.motion_nav.test_trajectory_proto_commitment as checks

class DeleteGuard(ast.NodeTransformer):
    count = 0
    def visit_If(self, node):
        if ast.unparse(node.test).startswith('request.branch_preludes[branch_index] and row.boundary == 0'):
            self.count += 1
            return None
        return self.generic_visit(node)
tree = ast.parse(inspect.getsource(implementation.scan_commitment))
delete = DeleteGuard()
tree = ast.fix_missing_locations(delete.visit(tree))
assert delete.count == 1
scope = dict(vars(implementation))
exec(compile(tree, '<delete-prelude-tail-guard>', 'exec'), scope)
with patch.object(checks, 'scan_commitment', scope['scan_commitment']):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        checks.TrajectoryProtoCommitmentTests('test_wait_cannot_cross_last_safe_stop_even_with_known_support')]))
    assert len(result.failures) == 1 and not result.errors

tree = ast.parse(inspect.getsource(implementation.validate_commitment))
function = tree.body[0]
point = next(index for index, node in enumerate(function.body)
             if isinstance(node, ast.If) and 'type(boundary_inputs)' in ast.unparse(node.test))
early = ast.parse('''if any(prelude is None for prelude in request.branch_preludes):
    return ScanResult(ScanStatus.NEEDS_INFORMATION, SearchReason.MISSING_INPUT_APPLICATION, ScanCounts())''').body[0]
function.body.insert(point, early)
scope = dict(vars(implementation))
exec(compile(ast.fix_missing_locations(tree), '<old-missing-prelude-return>', 'exec'), scope)
with patch.object(checks, 'validate_commitment', scope['validate_commitment']):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        checks.TrajectoryProtoCommitmentTests('test_missing_prelude_does_not_mask_known_current_schedule_changes')]))
    assert len(result.failures) == 8 and not result.errors
'@ | .\.venv\python.exe -
```

删除 tail0 守卫：`Ran 1 test in 0.092s / FAILED (failures=1)`。恢复旧提前返回：`Ran 1 test in 0.227s / FAILED (failures=8)`。末尾断言确认预期失败，所以整个变异量尺退出 0；不是把错误实现当成通过。

### 末次 Windows 检查

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment -q
```

`Ran 54 tests in 38.531s / OK`（20 合同＋34 扫描）。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

`Ran 62 tests in 16.174s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

`Ran 12 tests in 28.088s / OK`。`git diff --check` 和 `git diff --cached --check` 均返回 0，提交后 `git status --short` 为空，提交只含上述 5 个限定文件。检查后只追加文档／报告、执行独立进程变异，未再修改实现或测试。TP 证据更正已追加等待后 tail0 门槛与缺 prelude 的分类组合；旧通过数字保留，P0 未签署，时间盒不重置。
