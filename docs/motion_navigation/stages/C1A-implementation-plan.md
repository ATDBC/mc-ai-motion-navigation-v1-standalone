# C1-A 固定可见目标单次近战实施计划

> **实施要求：** 逐项执行本计划。直接实施时使用 `superpowers:executing-plans`；若用户明确要求子 agent，再使用 `superpowers:subagent-driven-development`。复选框（`- [ ]`）用于记录进度。

**Goal:** 在已知平地中复用现有导航、视角仲裁和唯一输入出口，接近一个固定可见敌人，提交一次绑定实体身份的普通近战攻击，并用后续正式观察确认命中。

**Architecture:** C1-A 使用一个纯规则策略生成局势评估、完整候选集合和唯一选择，由有状态驱动器依次调用现有 `PointGoalDriver`、视角意图和 `AttackEntityV1`。客户端在普通攻击调用前重新核验准星实体、距离、界面和冷却；回执只证明调用已执行，命中由同一实体受击动画从 0 变为正数确认。现有 Runtime trace 保存完整观察和仲裁结果，新增有界异步包装及离线重放分析，不建立第二套运行时。

**Tech Stack:** Python 3.11、`unittest`、Java 21、Minecraft 1.21、Fabric Loader 0.15.11、Fabric API 0.100.6+1.21、现有 V3 正式观察与 Action V1 协议。

**Spec:** `docs/motion_navigation/architecture/C1-combat-vertical-slice-v1.md`

## Global Constraints

- 版本继续锁定 Minecraft 1.21、OpenJDK 21、Python 3.11 和现有 Fabric 版本，不升级依赖。
- C1-A 只使用固定、持续可见、关闭 AI 的单个敌人；不实现移动敌人、交战记忆、击杀、连续攻击、盾牌、食物、装备切换、跳劈或世界修改。
- 正式 actor 只读取结构化观察；测试场景命令不能进入 actor 输入。
- `MotorGateway` 仍是唯一输入出口；攻击不能通过第二条客户端控制通道发送。
- 攻击请求必须绑定 `track_id`；准星指向其他实体时拒绝，不能回退为攻击当前准星实体。
- 执行回执与命中证据分开；攻击调用成功不能直接完成任务。
- 目标修订由任务层拥有，客户端不复制任务目标状态。
- 日志记录不参与决策；日志消费者阻塞不能阻塞控制。
- 每个任务先写失败测试，再做最小实现，相关检查通过后单独提交。
- 用户提供的 `docs/reference/MC 伙伴运动层：战斗切片.md` 只作参考，不成为现行接口来源。

## Review Focus

1. **瞄准后、攻击提交前目标修订变化：** 旧攻击意图必须失效，不能攻击仍在准星下的旧实体。Task 5 的目标修订测试固定该行为。
2. **攻击前目标已经处于受击动画：** 持续的正数不能确认新命中；必须观察到攻击前基线为 0，攻击后再变为正数。Task 4 和 Task 5 均覆盖。
3. **准星命中错误实体：** Java 客户端必须返回 `wrong_entity_target`，不得调用 `doAttack()`。Task 2 的 Java 守卫和执行器测试覆盖。
4. **日志队列满或消费者阻塞：** 控制侧 `write()` 立即返回并累计缺口；该运行不能作为完整成功证据。Task 3 的阻塞消费者测试覆盖。
5. **攻击提交后收到取消：** 不重发攻击，继续记录实际受击结果，任务终态为 `cancelled_after_submit`。Task 5 的驱动器测试覆盖。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `mc2p/contracts/observation_v2.py` | 在现有可见实体值中保存受击动画剩余 tick |
| `mc2p/contracts/action_v1.py` | 定义一次性的 `AttackEntityV1` |
| `mc2p/skills/targeting.py` | 对新鲜 V3 观察做纯实体准星确认 |
| `mc2p/skills/fixed_melee.py` | 保存 C1-A 目标、阶段、局势评估、候选和纯规则选择 |
| `mc2p/skills/fixed_melee_driver.py` | 串联 PointGoal、瞄准、攻击、确认、取消和任务记录 |
| `mc2p/runtime/async_trace.py` | 给现有 TraceSink 增加有界非阻塞写入和缺口统计 |
| `scripts/c1_melee_evidence.py` | 从封存观察流离线重放上层决策并定位最早契约断点 |
| `scripts/c1_fixed_melee_runtime.py` | 运行固定 C1-A 场景和整理逐局证据 |
| `scripts/probe_fabric_deployment_observation.py` | 接入 `--c1-fixed-melee-probe` 正式 Fabric 入口 |

实现不创建通用武器注册表、威胁代价场、实体预测器或新的后台进程。

### Task 1: 在正式实体观察中加入受击动画证据

**Files:**
- Modify: `mc2p/contracts/observation_v2.py`
- Modify: `mc2p/backends/client_observation_payload.py`
- Modify: `mc2p/backends/runtime_overlays/mc121_observation/ClientObservationCollector.java`
- Modify: `tests/observation_v2_fixtures.py`
- Modify: `tests/test_observation_v2_contract.py`
- Modify: `tests/test_client_observation_payload.py`
- Modify: `tests/test_v3_navigation_trace.py`

**Interfaces:**
- Consumes: 客户端当前可见的 `LivingEntity.hurtTime`。
- Produces: `VisibleEntityV2.hurt_animation_ticks: int | None`。当前 Java 生产者始终发送 `0..20` 的整数；`None` 只表示读取到新增字段以前封存的历史证据，不能用于 C1 命中确认。

- [ ] **Step 1: 写契约和严格解码失败测试**

在 `tests/test_observation_v2_contract.py` 增加：

```python
def test_visible_entity_hurt_animation_is_bounded(self):
    entity = VisibleEntityV2(
        "entity-1", "minecraft:zombie", None,
        Vec3V0(0, 0, 2), Vec3V0(0, 0, 0), 0.0, 0.0,
        Vec3V0(.6, 1.95, .6), "standing", True, (),
        hurt_animation_ticks=10,
    )
    self.assertEqual(entity.hurt_animation_ticks, 10)
    self.assertIsNone(replace(entity, hurt_animation_ticks=None).hurt_animation_ticks)
    for invalid in (-1, 21, True, 1.5):
        with self.subTest(invalid=invalid), self.assertRaises(ContractViolation):
            replace(entity, hurt_animation_ticks=invalid)
```

在 `tests/test_client_observation_payload.py` 增加严格字段测试：合法新 payload 的首个实体带 `hurt_animation_ticks=7` 时正确恢复；字段缺失的旧 payload 恢复为 `None`；显式传入 `null`、布尔值或 21 时拒绝。这样只兼容已经封存的旧格式，不允许当前生产者用 `null` 冒充有效观察。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_observation_v2_contract tests.test_client_observation_payload -v
```

Expected: FAIL，错误指向缺少 `hurt_animation_ticks` 字段或 `VisibleEntityV2` 不接受该参数。

- [ ] **Step 3: 实现最小契约和 Java 生产者**

在 `VisibleEntityV2` 末尾增加：

```python
hurt_animation_ticks: int | None = None
```

在 `__post_init__` 中允许 `None`，否则严格检查 `type(value) is int and 0 <= value <= 20`。`_visible_entity()` 只接受旧键集合或“旧键集合加 `hurt_animation_ticks`”两种精确形式；新字段存在时必须是整数。更新当前测试 payload 夹具，使当前生产者格式总是显式包含该字段。

在 `ClientObservationCollector.visibleEntity(...)` 中写入：

```java
value.addProperty("hurt_animation_ticks",
        entity instanceof LivingEntity living ? living.hurtTime : 0);
```

- [ ] **Step 4: 验证 trace 往返仍保持完全一致**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_observation_v2_contract tests.test_client_observation_payload tests.test_observation_v3_contract tests.test_v3_navigation_trace tests.test_visible_equipment_projection -v
```

Expected: PASS。`trace_projection(restore_snapshot(raw)) == raw` 仍成立。

- [ ] **Step 5: 提交观察契约**

```powershell
git add mc2p/contracts/observation_v2.py mc2p/backends/client_observation_payload.py mc2p/backends/runtime_overlays/mc121_observation/ClientObservationCollector.java tests/observation_v2_fixtures.py tests/test_observation_v2_contract.py tests/test_client_observation_payload.py tests/test_v3_navigation_trace.py
git commit -m "feat(combat): observe entity hurt animation"
```

### Task 2: 增加绑定实体身份的一次性近战操作

**Files:**
- Modify: `mc2p/contracts/action_v1.py`
- Modify: `mc2p/runtime/arbiter_v1.py`
- Modify: `mc2p/backends/runtime_overlays/mc121_actions/ClientActionRequest.java`
- Modify: `mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java`
- Modify: `mc2p/backends/runtime_overlays/mc121_observation/ClientEntityIndex.java`
- Modify: `mc2p/backends/runtime_overlays/mc121_observation/ClientObservationCollector.java`
- Create: `mc2p/backends/runtime_overlays/mc121_actions/ClientEntityGuard.java`
- Create: `tests/java/ClientEntityGuardTest.java`
- Modify: `tests/test_action_arbiter_v1.py`
- Modify: `tests/test_client_behavior_executor.py`
- Modify: `tests/java/ClientActionDecodeTest.java`
- Modify: `tests/test_fabric_behavior.py`

**Interfaces:**
- Consumes: 当前正式观察中的 `track_id`、客户端最新准星实体和普通近战条件。
- Produces: `AttackEntityV1(entity_ref: str)`；成功调度返回 `pending_confirmation/entity_attack_dispatched`，所有拒绝返回明确 reason。

- [ ] **Step 1: 写 Python 动作与仲裁失败测试**

新增测试证明：

```python
operation = AttackEntityV1("entity-session-7")
intent = ActionIntentV1(
    "attack-1", "combat", "episode-1", 4, ActionPriorityV0.TASK,
    100, 200, operation=operation,
)
arbiter.submit(intent)
decision = arbiter.resolve(150, "episode-1", 4, 9, 190)
self.assertEqual(decision.action.operation, operation)
self.assertEqual(dict(decision.selected_intents)["operation"], "attack-1")
self.assertIsNone(arbiter.resolve(151, "episode-1", 5, 10, 190).action.operation)
```

同时验证空 `entity_ref`、超过 128 字符、布尔值和未知操作被拒绝；`forbidden_actions=("attack_entity",)` 会消费并抑制该操作，不会延迟重放。

- [ ] **Step 2: 运行 Python 测试并确认失败**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_action_arbiter_v1 tests.test_fabric_behavior -v
```

Expected: FAIL，`AttackEntityV1` 尚不存在或仲裁禁止词表不接受 `attack_entity`。

- [ ] **Step 3: 定义正式动作和线协议**

在 `action_v1.py` 增加：

```python
@dataclass(frozen=True, slots=True)
class AttackEntityV1:
    entity_ref: str
    kind: str = field(default="attack_entity", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.entity_ref, "attack entity reference")
        if len(self.entity_ref) > 128:
            raise ContractViolation("attack entity reference exceeds 128 characters")
```

把它加入 `BehaviorOperationV1` 和 `_OPERATION_TYPES`，并在 `_FORBIDDABLE` 加入 `attack_entity`。Java `ClientActionRequest.validateOperation()` 只接受精确键 `kind/entity_ref`，并沿用同一标识符格式。

- [ ] **Step 4: 写并运行 Java 实体守卫失败测试**

`ClientEntityGuardTest` 固定以下结果：

```java
expect(null, ClientEntityGuard.validate("entity-1", "entity-1", 2.8, 3.0));
expect("target_miss", ClientEntityGuard.validate("entity-1", null, 0.0, 3.0));
expect("wrong_entity_target", ClientEntityGuard.validate("entity-1", "entity-2", 2.0, 3.0));
expect("target_out_of_reach", ClientEntityGuard.validate("entity-1", "entity-1", 3.01, 3.0));
```

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_client_behavior_executor -v
```

Expected: FAIL，守卫类或 `attack_entity` 分派尚不存在。

- [ ] **Step 5: 接入正常客户端攻击**

给 `ClientEntityIndex` 增加只读 `trackId(Entity entity)`；只返回当前世界中仍保留的本地 track，不创建新 track。`ClientObservationCollector` 暴露受限静态查询给动作执行器。

`ClientBehaviorExecutor.attackEntity(...)` 按顺序检查：无 GUI、未骑乘、未使用物品、未挖掘、客户端点击冷却为 0、`player.getAttackCooldownProgress(0.0f) >= 1.0f`、刷新后的 `EntityHitResult`、track 身份、实际命中距离和当前租约。距离上限读取 `player.getEntityInteractionRange()`，不能把测试里的 `3.0` 写成运行常量。`getAttackCooldownProgress()` 在原版中会钳制到 `1.0`；组件测试和 Fabric 实测都要证明满冷却时能通过，若映射版本返回不到 `1.0`，先记录证据再冻结同一处容差，不能让 Python 和 Java 各用一个阈值。全部通过后只调用一次：

```java
boolean dispatched = ((ClientBehaviorAccess) client).mc2p$attack();
if (!dispatched) { reject("attack_not_dispatched"); return; }
status = "pending_confirmation";
reason = "entity_attack_dispatched";
```

该方法不改变目标、不重试、不把返回值解释成命中。

- [ ] **Step 6: 运行协议、仲裁和 Java 检查**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_action_arbiter_v1 tests.test_client_behavior_executor tests.test_java_observation_request_v3_protocol tests.test_fabric_behavior -v
```

Expected: PASS；Java decode 接受合法 `attack_entity`，拒绝额外键、错误实体和超距目标。

- [ ] **Step 7: 提交攻击协议**

```powershell
git add mc2p/contracts/action_v1.py mc2p/runtime/arbiter_v1.py mc2p/backends/runtime_overlays/mc121_actions/ClientActionRequest.java mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java mc2p/backends/runtime_overlays/mc121_actions/ClientEntityGuard.java mc2p/backends/runtime_overlays/mc121_observation/ClientEntityIndex.java mc2p/backends/runtime_overlays/mc121_observation/ClientObservationCollector.java tests/java/ClientEntityGuardTest.java tests/java/ClientActionDecodeTest.java tests/test_action_arbiter_v1.py tests/test_client_behavior_executor.py tests/test_fabric_behavior.py
git commit -m "feat(combat): add targeted melee operation"
```

### Task 3: 让完整观察记录异步且有界

**Files:**
- Create: `mc2p/runtime/async_trace.py`
- Create: `tests/test_async_trace.py`
- Modify: `mc2p/runtime/__init__.py`

**Interfaces:**
- Consumes: 任意现有 `TraceSinkV0`。
- Produces: `BoundedAsyncTraceWriter(sink, capacity=256)`，实现 `write()`、`close()` 和 `stats: AsyncTraceStats`。

- [ ] **Step 1: 写阻塞消费者与队列满测试**

测试用 `threading.Event` 阻塞底层 sink 5 秒，连续写入超过容量的记录，并断言：

```python
started = time.perf_counter_ns()
for index in range(32):
    writer.write("observation", {"index": index})
elapsed = time.perf_counter_ns() - started
self.assertLess(elapsed, 50_000_000)
self.assertGreater(writer.stats.dropped_records, 0)
```

解除阻塞并 `close()` 后，底层最后一条记录必须是 `trace_delivery_summary`，包含写入数、按 `record_type` 统计的丢弃数、容量、队列峰值、调用线程投影耗时分布和 worker failure。另测底层抛异常后，下一次 `write()` 或 `close()` 明确抛出，不静默丢失。该包装只保证控制线程不会等待磁盘消费者；`trace_projection` 仍在调用线程形成不可变快照，其耗时必须进入正式 P95／P99，不能从控制预算中排除。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_async_trace -v
```

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现单 worker、有界队列和封存汇总**

接口固定为：

```python
@dataclass(frozen=True, slots=True)
class AsyncTraceStats:
    accepted_records: int
    written_records: int
    dropped_records: int
    capacity: int
    peak_depth: int
    worker_failed: bool

class BoundedAsyncTraceWriter:
    def __init__(self, sink: TraceSinkV0, *, capacity: int = 256) -> None: ...
    @property
    def stats(self) -> AsyncTraceStats: ...
    def write(self, record_type: str, payload: object) -> None: ...
    def close(self) -> None: ...
```

`write()` 在调用线程执行 `trace_projection` 后使用 `put_nowait`；满队列只累计缺口，不把成功证据静默改写成完整。worker 串行调用原 sink。`close()` 只在控制会话结束后等待队列排空，直接写入汇总，再关闭原 sink。禁止 daemon thread，禁止无界队列。底层 sink 永久不返回属于证据系统失败；本阶段用 5 秒阻塞注入证明控制可继续，不声称 Python 能强行终止永久卡死的第三方写入函数。

- [ ] **Step 4: 验证现有分段证据仍可读取**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_async_trace tests.test_segmented_trace tests.test_playground_long_stream -v
```

Expected: PASS；无丢弃运行仍能通过 `iter_segmented_jsonl()` 完整校验。

- [ ] **Step 5: 提交异步记录边界**

```powershell
git add mc2p/runtime/async_trace.py mc2p/runtime/__init__.py tests/test_async_trace.py
git commit -m "feat(runtime): add bounded asynchronous trace sink"
```

### Task 4: 建立纯 C1-A 局势评估、候选和选择

**Files:**
- Modify: `mc2p/skills/targeting.py`
- Create: `mc2p/skills/fixed_melee.py`
- Modify: `mc2p/skills/__init__.py`
- Modify: `tests/test_targeting.py`
- Create: `tests/test_fixed_melee.py`

**Interfaces:**
- Consumes: `ObservationSnapshotV3`、目标身份、当前 C1-A 阶段、决定时间和攻击确认基线。
- Produces: `CombatTargetV1`、`FixedMeleePhase`、`MeleeAssessmentV1`、完整 `MeleeCandidateV1` 集合和 `FixedMeleeDecisionV1`。

- [ ] **Step 1: 写实体准星确认测试**

在 `tests/test_targeting.py` 增加：同一 `track_id`、`interaction_v1`、合法时钟和新鲜观察时返回 true；错误实体、navigation profile、缺失 targeting、特权字段、旧时钟和过期观察返回 false。

目标接口：

```python
def confirmed_entity_target(
    observation: ObservationSnapshotV3,
    *, entity_ref: str, now_ns: int,
    controller_clock_id: str,
    freshness_ns: int = 500_000_000,
) -> bool: ...
```

- [ ] **Step 2: 写策略表驱动失败测试**

使用冻结观察构造以下表：

| 阶段／观察 | 唯一选中候选 |
|---|---|
| 距离大于 2.8 格 | `approach` |
| 已到站但准星未命中 | `aim` |
| 已对准但目标受击动画仍为正数 | `wait_hurt_clear` |
| 准星命中但攻击冷却小于 1 | `wait_cooldown` |
| 准星命中且冷却为 1 | `attack` |
| 已提交攻击、受击仍为 0 | `wait_hit` |
| 攻击前为 0、提交后的新观察大于 0 | `complete` |
| 攻击前目标受击动画大于 0 | 不允许提交攻击，保持 `wait_hurt_clear`，直到新观察归零 |
| 确认期限到达 | `fail_confirmation_timeout` |

每次决定必须包含所有固定候选及各自的 `FEASIBLE/BLOCKED/NOT_APPLICABLE` 和原因，不能只记录胜者。

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_targeting tests.test_fixed_melee -v
```

Expected: FAIL，纯策略类型和实体确认函数尚不存在。

- [ ] **Step 4: 实现固定类型和纯函数**

核心接口固定为：

```python
class FixedMeleePhase(StrEnum):
    APPROACHING = "approaching"
    ALIGNING = "aligning"
    WAITING_HURT_CLEAR = "waiting_hurt_clear"
    WAITING_COOLDOWN = "waiting_cooldown"
    READY_TO_ATTACK = "ready_to_attack"
    CONFIRMING_HIT = "confirming_hit"
    COMPLETE = "complete"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class CombatTargetV1:
    task_id: str
    goal_id: str
    revision: int
    episode_id: str
    track_id: str

def decide_fixed_melee(
    observation: ObservationSnapshotV3,
    *, target: CombatTargetV1,
    phase: FixedMeleePhase,
    generation: int,
    now_ns: int,
    controller_clock_id: str,
    attack_observation_sequence_id: int | None = None,
    pre_attack_hurt_animation_ticks: int | None = None,
    confirmation_deadline_ns: int | None = None,
) -> FixedMeleeDecisionV1: ...
```

策略使用 `confirmed_entity_target()` 和最新可见实体，不保存自己的观察副本。当前实体的 `hurt_animation_ticks` 为 `None` 时返回 `missing_hurt_animation_observation`，不能攻击或确认命中。`stable_attack_position()` 根据当前脚点到目标脚点的水平向量，在目标外侧 2.5 格返回世界坐标；零长度向量返回明确失败。

- [ ] **Step 5: 运行纯策略与旧 targeting 回归**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_targeting tests.test_fixed_melee tests.test_interaction_aim tests.test_point_goal -v
```

Expected: PASS。

- [ ] **Step 6: 提交纯策略**

```powershell
git add mc2p/skills/targeting.py mc2p/skills/fixed_melee.py mc2p/skills/__init__.py tests/test_targeting.py tests/test_fixed_melee.py
git commit -m "feat(combat): add fixed melee decision policy"
```

### Task 5: 串联接近、瞄准、攻击、确认和取消

**Files:**
- Create: `mc2p/skills/fixed_melee_driver.py`
- Modify: `mc2p/runtime/player_runtime_v1.py`
- Create: `tests/test_fixed_melee_driver.py`
- Modify: `tests/test_player_runtime_v1.py`

**Interfaces:**
- Consumes: Task 1–4 的观察、操作、纯策略和现有 `PointGoalDriver`。
- Produces: `FixedMeleeDriver.start(target, now_ns)`、`replace_target(target, now_ns)`、`tick(profile, owner_deadline_ns)`、`cancel(profile, reason)` 和不可变 `FixedMeleeReportV1`。

- [ ] **Step 1: 写成功链的失败测试**

构造一个后端序列：目标在 5 格外 → PointGoal 到达 2.5 格 → 多帧瞄准 → 冷却完成 → `attack_entity` 获得 `pending_confirmation` → 下一观察同一目标 `hurt_animation_ticks=10`。

断言：

```python
self.assertEqual(driver.report.state, "complete")
self.assertEqual(driver.report.reason, "hit_confirmed")
self.assertEqual(sum(
    action.operation is not None and action.operation.kind == "attack_entity"
    for action in backend.actions
), 1)
self.assertEqual(driver.report.target_revision, 1)
```

同时断言 approach 阶段使用真实 `PointGoalDriver`，完成并释放其 ordered source 后才注册战斗 source。

- [ ] **Step 2: 写五类 Review Focus 与生命周期失败测试**

至少包括：

- 目标修订在瞄准后改变，旧攻击为 0 次；
- 准星错误实体，仲裁虽选中 operation，客户端拒绝后任务报告 `client_rejected/wrong_entity_target`；
- 攻击前受击动画已经大于 0，先等待归零，不提交攻击；
- 攻击提交后取消，不重发；驱动器继续只读观察到确认窗口结束，并最终报告 `cancelled_after_submit`，同时单独保存是否实际观察到命中；
- 接近、瞄准、冷却等待、仲裁抑制、回执缺失和确认超时分别产生不同 reason；
- Runtime 世界会话变化后失败关闭；
- 同一 observation sequence 不重复决定或重放一次性攻击。

- [ ] **Step 3: 运行驱动器测试并确认失败**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_fixed_melee_driver tests.test_player_runtime_v1 -v
```

Expected: FAIL，驱动器和 combat trace 事件尚不存在。

- [ ] **Step 4: 扩展内部任务记录白名单**

`PlayerRuntimeV1.record_task_event()` 增加四个精确 schema：

```python
"combat_assessment": "mc2p.combat-assessment.v1"
"combat_candidates": "mc2p.combat-candidates.v1"
"combat_selection": "mc2p.combat-selection.v1"
"combat_skill": "mc2p.combat-skill.v1"
```

每条记录必须带当前 `episode_id`。Runtime 原有 `dispatch` 和 `step` 继续分别保存仲裁结果、完整后观察和客户端回执。

- [ ] **Step 5: 实现顺序驱动器**

接口固定为：

```python
class FixedMeleeDriver:
    def __init__(
        self, runtime: PlayerRuntimeV1,
        navigation_state: NavigationState,
        point_policy: PointGoalPolicy,
        *, clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None: ...

    def start(self, target: CombatTargetV1, now_ns: int) -> None: ...
    def replace_target(self, target: CombatTargetV1, now_ns: int) -> None: ...
    def tick(self, profile: BehaviorProfileV0,
             owner_deadline_ns: int) -> RuntimeStepResultV1 | None: ...
    def cancel(self, profile: BehaviorProfileV0,
               reason: str) -> RuntimeStepResultV1 | None: ...
```

`start()` 从当时的正式观察冻结 `episode_id` 和 `controller_clock_id`。后续观察任一身份变化都关闭当前任务，不能把新会话的数据接到旧攻击上。

关键顺序：

1. 从当前正式可见目标计算 2.5 格站位，建立 `PointGoal`；
2. `PointGoalDriver` 报告 success 后调用其 `stop()`，确认中立释放和 source 注销；
3. 注册 combat ordered source；
4. 每个 interaction 帧调用 `decide_fixed_melee()`，依次记录 assessment、candidates、selection；
5. `aim` 复用 `ObservationGate` 和实体中心角度，提交一次 look；
6. `wait_hurt_clear/wait_cooldown/wait_hit` 发送中立帧取得新观察；
7. `attack` 记录攻击前 hurt 基线，提交一次 `AttackEntityV1`；
8. 检查 `ArbitrationDecisionV1.selected_intents` 后记录 `combat_skill`；
9. 只有同一目标的新观察产生 0→正数变化才完成；
10. 攻击提交前调用 `replace_target()` 时，先使旧目标的导航、瞄准和攻击状态失效，再按新修订重新开始。攻击提交后发生目标修订时，当前驱动器进入只观察的 `target_revised_after_submit` 收尾状态；新目标必须等当前动作责任封存后由新的驱动器接管，不能并行复用旧结果；
11. 攻击提交后的 `cancel()` 进入观察收尾状态。该状态不再注册攻击意图，只取得后续正式观察，并保持任务终态为 `cancelled_after_submit`。

操作与非中立 look 不在同一 tick 提交，沿用现有仲裁冲突规则。

- [ ] **Step 6: 验证驱动器、导航和仲裁回归**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_fixed_melee_driver tests.test_point_goal_driver tests.test_action_arbiter_v1 tests.test_player_runtime_v1 -v
```

Expected: PASS。

- [ ] **Step 7: 提交 C1-A 在线闭环**

```powershell
git add mc2p/skills/fixed_melee_driver.py mc2p/runtime/player_runtime_v1.py tests/test_fixed_melee_driver.py tests/test_player_runtime_v1.py
git commit -m "feat(combat): connect fixed melee execution"
```

### Task 6: 离线重放上层决策并定位最早失败层

**Files:**
- Create: `scripts/c1_melee_evidence.py`
- Create: `tests/test_c1_melee_evidence.py`

**Interfaces:**
- Consumes: 正常封存的 segmented trace 和 C1 运行清单。
- Produces: `replay_c1_melee(evidence: Path) -> dict`，包含通过状态、重放帧数、最早失败层、身份和原因。

- [ ] **Step 1: 写完整重放和逐层篡改测试**

用 Task 5 的真实驱动器夹具生成一条封存 trace。原始 trace 必须通过。分别篡改：

1. assessment 的目标距离；
2. candidates 中删除 `attack`；
3. selection 改为不可行候选；
4. combat skill 的 operation 改成其他实体；
5. dispatch 的 selected intent 或最终 operation；
6. step receipt 改为拒绝或删去受击变化。

断言最早失败层依次为：

```python
("situation_assessment", "candidate_coverage", "selection",
 "skill_execution", "arbitration", "motion")
```

存在 `trace_delivery_summary.dropped_records > 0` 时返回 `incomplete_evidence`，不运行成功重放。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_c1_melee_evidence -v
```

Expected: FAIL，重放器尚不存在。

- [ ] **Step 3: 实现流式重放和因果连接检查**

`replay_c1_melee()` 使用 `iter_segmented_jsonl()`，通过 `scripts.navigation_motion_evidence.restore_snapshot()` 恢复 reset/step 中的正式观察。它按 `decision_generation` 连接四个 combat 事件，再连接 ordered intent、dispatch、step、receipt 和后观察。

每帧重新调用 `decide_fixed_melee()`，比较 `trace_projection()` 后的 assessment、全部 candidates 和 selection。随后检查选中候选生成的意图、仲裁最终命令和实际回执。首次不一致立即固定层级和身份，后续差异只作为 `consequences` 保存。

- [ ] **Step 4: 验证严格重放和既有 snapshot 恢复**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_c1_melee_evidence tests.test_v3_navigation_trace tests.test_segmented_trace -v
```

Expected: PASS。

- [ ] **Step 5: 提交离线证据分析**

```powershell
git add scripts/c1_melee_evidence.py tests/test_c1_melee_evidence.py
git commit -m "feat(combat): replay C1 decision evidence"
```

### Task 7: 增加固定种子的 Fabric C1-A 场景

**Files:**
- Create: `scripts/c1_fixed_melee_runtime.py`
- Modify: `scripts/probe_fabric_deployment_observation.py`
- Modify: `tests/test_fabric_deployment_probe.py`
- Create: `tests/test_c1_fixed_melee_runtime.py`

**Interfaces:**
- Consumes: `PlayerRuntimeV1`、固定服务器命令回调、Task 5 驱动器和 Task 6 重放器。
- Produces: `--c1-fixed-melee-probe`；40 个正式成功样本、七个有界反例和封存证据报告。

- [ ] **Step 1: 写探针接线和场景清单失败测试**

测试必须证明：

- 新 CLI 与其他 probe 互斥；
- `frozen_deployment_sources()` 包含 C1 新模块、动作／观察协议、Java executor、实体索引、异步记录器和重放器；
- 场景清单只有四个正方向，每方向 10 次；
- 世界种子、场景种子、代码哈希、初始位置、目标位置、朝向、装备和确认期限都写入 manifest；
- 运行列表在执行前冻结，失败后不能删样本。

- [ ] **Step 2: 运行接线测试并确认失败**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_fabric_deployment_probe tests.test_c1_fixed_melee_runtime -v
```

Expected: FAIL，CLI 和运行模块尚不存在。

- [ ] **Step 3: 实现固定场景准备**

probe 的服务器命令回调仅允许以下固定模板：

```text
difficulty normal
time set midnight
gamerule doDaylightCycle false
gamerule doWeatherCycle false
gamerule doMobSpawning false
weather clear
kill @e[type=!minecraft:player]
kill @e[type=minecraft:zombie,tag=mc2p_c1]
item replace entity MC2PProbe weapon.mainhand with minecraft:stone_sword
tp MC2PProbe <x> <y> <z> <yaw> 0
summon minecraft:zombie <x> <y> <z> {NoAI:1b,PersistenceRequired:1b,Silent:1b,Tags:["mc2p_c1"]}
```

每个正式批次先冻结午夜、天气和自然生物生成，再清除非玩家实体并生成本轮唯一僵尸。这样太阳灼烧、天气变化、自然生成生物和上一轮残留实体都不能制造受击动画。`kill` 和世界规则只属于测试夹具准备，不进入 actor 权限。

场地用现有 fixture writer 建立已知草方块平地和外围墙。actor 只从正式观察中选择唯一可见 zombie 的 `track_id`，不读取命令中的实体坐标或标签。场景测试必须断言正式攻击提交以前，目标的 `hurt_animation_ticks` 始终为 0，且目标没有着火；否则该样本按夹具污染失败保留，不能重新分类或重跑替换。

- [ ] **Step 4: 实现 40 次成功样本和七个反例**

四个方向的起始点距目标 5 格，朝向目标。每次重新召唤实体并创建新的 C1 驱动器。成功样本必须满足：站位、严格回执、同一 track 的新受击动画、攻击次数恰为 1、无世界修改、无物品切换、trace 无缺口。

正式运行必须把现有 `SegmentedTraceWriter` 包在 `BoundedAsyncTraceWriter` 中再交给 `PlayerRuntimeV1`。场景脚本不得另写一份同步的完整观察日志来绕开该验收。

反例依次注入：接近取消、瞄准取消、提交后取消、目标修订、错误准星、已知墙阻挡和命中确认超时。反例在运行清单中预先登记，不进入 40 次分母。

- [ ] **Step 5: 在真实运行结束后离线重放每个样本**

先关闭控制会话，再调用 `replay_c1_melee()`。任何正式成功样本重放不一致、证据未封存或记录缺口都会让整轮失败。输出至少包含：

```text
c1-fixed-melee-trials.jsonl
c1-fixed-melee-summary.json
c1-fixed-melee-replay.json
c1-fixture-commands.jsonl
runtime-trace/trace/
```

- [ ] **Step 6: 运行非游戏组件测试**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_c1_fixed_melee_runtime tests.test_fabric_deployment_probe tests.test_c1_melee_evidence -v
```

Expected: PASS。

- [ ] **Step 7: 提交 Fabric 场景入口**

```powershell
git add scripts/c1_fixed_melee_runtime.py scripts/probe_fabric_deployment_observation.py tests/test_c1_fixed_melee_runtime.py tests/test_fabric_deployment_probe.py
git commit -m "test(combat): add fixed melee Fabric probe"
```

### Task 8: 完成回归、实机验收和文档收口

**Files:**
- Modify after successful run: `docs/motion_navigation/acceptance/C1A-fixed-visible-melee.md`
- Modify after successful run: `docs/motion_navigation/stages/C1-fixed-visible-melee.md`
- Modify after successful run: `AGENTS.md`

**Interfaces:**
- Consumes: Task 1–7 全部代码和正式 Fabric 产物。
- Produces: 可审查的 C1-A 完成结论或保留失败结论；不提前开启 C1-B。

- [ ] **Step 1: 运行 C1-A 专项组件测试**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_observation_v2_contract tests.test_client_observation_payload tests.test_action_arbiter_v1 tests.test_client_behavior_executor tests.test_async_trace tests.test_targeting tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_c1_melee_evidence tests.test_c1_fixed_melee_runtime tests.test_fabric_deployment_probe -v
```

Expected: PASS。

- [ ] **Step 2: 运行运动／导航全量回归**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
```

Expected: PASS，B03–B10 已发布范围没有回归。

- [ ] **Step 3: 运行相关 Runtime、观察与记录回归**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_player_runtime_v1 tests.test_point_goal_driver tests.test_observation_v3_consumer_gate tests.test_segmented_trace tests.test_v3_navigation_trace -v
```

Expected: PASS。

- [ ] **Step 4: 运行正式 Fabric C1-A**

Run:

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/probe_fabric_deployment_observation.py --c1-fixed-melee-probe --seed 21001 --server-port 25680 --ipc-port 8223 --time-diagnostics --timeout-seconds 900
```

Expected: 40／40 有效可达样本确认命中，七个反例全部正确分类，错误实体受击、主动世界修改、未授权物品操作和关键日志缺口均为 0；控制帧和输入应用计时满足冻结门槛。

- [ ] **Step 5: 根据真实结果更新阶段和验收文档**

只记录实际产物路径、命令、样本数、通过数、失败分类、P95/P99、日志队列峰值和重放结果。若任一门槛失败，状态保持“未完成”，保留失败产物，不降低门槛。

- [ ] **Step 6: 检查差异并提交 C1-A 结果**

Run:

```powershell
git diff --check
git status --short
```

只暂存本计划产生的代码、测试和三份更新文档，不暂存用户提供的未跟踪参考文档。

```powershell
git add AGENTS.md docs/motion_navigation/acceptance/C1A-fixed-visible-melee.md docs/motion_navigation/stages/C1-fixed-visible-melee.md
git commit -m "docs(combat): record C1-A acceptance"
```

## 计划完成后的边界

C1-A 完成后仍不实现 `ENGAGEMENT`。开始 C1-B 前，先根据 C1-A 的真实记录冻结以下内容：正常僵尸的位置权威更新来源、仇恨关系来源、目标死亡确认、连续攻击冷却、种子集合和移动目标的成功／失败门槛。C1-A 代码不得用固定坐标、服务端标签或测试命令填补这些后续契约。
