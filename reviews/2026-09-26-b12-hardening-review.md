# B12 审查整改复审（第十轮）

日期：2026-09-26
审查对象：`origin/main` 提交 [`211af42`][base]（D032：持续瞄准移动、伤害来源、跨类别失败上限）
性质：外部审查意见，不属于 `docs/motion_navigation/` 四类正式文档，不改变任何阶段结论或验收门槛
前几轮：[整体审查](2026-09-24-architecture-review.md)、[C1-R 复审](2026-09-25-c1r-rereview.md)、[第三轮](2026-09-25-c1r-rereview-2.md)、[第四轮](2026-09-25-c1r6-review.md)、[第五轮（B11）](2026-09-25-b11-review.md)、[第六轮（D026）](2026-09-25-d026-review.md)、[第七轮](2026-09-25-final-review.md)、[第八轮（D027）](2026-09-25-d027-review.md)、[第九轮（B12）](2026-09-26-b12-review.md)

本文代码链接都指向 `211af42`。本分支仍基于旧快照，请不要按本分支的相对路径查看代码。

## 结论

1. **上一轮提出的问题都已处理，而且做法对路。**
   - **跨类别失败上限：** 新增“自上次命中以来的失败总数”，默认 6 次。用上一轮的脚本交替输入两类失败，第 6 次返回 `NO_PROGRESS_RETRY_EXHAUSTED`。
   - **持续瞄准下的移动：** 战斗先给出本帧视角，导航按“转头后的朝向”重新计算普通步行，移动意图绑定这份视角（[`navigation_session.py:839`][conditioned]）；只有这份视角胜出，移动才被采用（[`arbiter_v1.py:192`][arbiter]）。这正是上一轮建议的顺序，而且没有另写一套战斗移动器。
   - **伤害来源：** 从 1.21 的 `EntityDamageS2CPacket` 取得伤害类型、造成伤害的实体和直接来源实体，形成更高的 `SOURCE_CONFIRMED` 证据等级。事件明确指向其他来源时，不再用受伤动画把它升级为机器人命中。
   - **真实墙体：** “未交战目标在墙后”已改用真实石墙在 Fabric 中验证。
2. **实机证据补上了，而且顺序正确。** 新的 B12-B 公开批次中，有 12 帧非零转头，其中 10 帧同时执行了移动（9 帧 15°，1 帧 11.6°）。没有一次因“绑定视角落选”或“5° 上限”被压下。在客户端事件里，视角先生效，移动输入被消费时，姿态已经是转头后的朝向，说明“按最终朝向算移动”在游戏里是成立的。
3. **一个中等问题：B03 回归没有测到本轮改动。**
   - 整改计划写的是用 B03 “检查一 tick 普通步行授权没有造成断续”。
   - 但 B03 探针直接驱动 `FixedRouteController`，按控制器自己的 2 tick 租约提交，不经过 `NavigationSession`。
   - 所以它既不带一帧授权，也不带视角绑定。C1-B 走的是正式导航路径，它的通过才是有效的覆盖（第二节）。
4. **一个小问题：** 伤害事件缓冲会一直留着“目标还没有追踪编号”的事件，导致 `damage_events_dropped` 在长时间运行中持续增长，失去“丢了有用证据”的含义。归因本身不受影响（第三节）。
5. **证据仍然偏薄的地方：**
   - 活动目标只有 1 次试验、2 个组合请求。转头由夹具把目标瞬移到侧面触发，同一段转头中还有 2 帧导航给出了中性移动，原因没有记录。
   - 伤害来源的实机样本各只有 1 个（玩家近战 1 次、着火 1 次）。其他实体造成的伤害只有组件测试，文档对此写得如实。

## 审查范围

- 阅读了以下文档：
  - D032、B12 审查整改实施计划；
  - B12 攻击证据、B12-B 的架构与验收修订；
  - B12 阶段文档。
- 阅读了以下代码的全部改动：
  - `action_v1.py`、`arbiter_v1.py`；
  - `navigation_session.py`、`navigation_session_driver.py`、`action_route_executor.py`；
  - `melee_strike_driver.py`、`moving_melee_driver.py`、`attack_evidence.py`；
  - `observation_v3.py`、客户端载荷解码；
  - Fabric 的 `ClientDamagePacketMixin`、`ClientDamageEventBuffer`、`ClientObservationCollector`；
  - B03 探针的提交路径。
- 在 Linux 容器（Python 3.11.15、OpenJDK 21）中按 README 运行了全部检查，并重跑了前三轮的复现脚本。
- 解开新的 B12-A、B12-B 公开批次，逐帧核对了仲裁结果、客户端视角与输入事件顺序、伤害来源诊断。
- 没有运行 Fabric 实机。

## 一、上一轮问题复核

| 上一轮问题 | 修复 | 我的复核 |
|---|---|---|
| 两类失败交替出现时额度不会耗尽 | `failures_since_confirmed_hit`，上限 6（[`attack_evidence.py:209`][retry-limit]、[`:286`][retry-check]） | 上一轮脚本：第 6 次失败起返回 `NO_PROGRESS_RETRY_EXHAUSTED`；确认命中后清零 |
| 5° 上限在近身横移时压下移动 | 战斗视角先确定，导航按转头后的朝向计算普通步行（[`action_route_executor.py:499`][executor]），移动绑定具体视角意图 | 新批次：12 帧转头中有 10 帧同时移动，没有一次被压下；5° 规则只在没有绑定视角时作为保守路径 |
| 核心路径没有实机样本 | 新增活动目标专项；大幅转头边界改为“转头时继续按新朝向移动” | 见第四节 |
| 伤害来源 | `EntityDamageS2CPacket` → 有界事件缓冲 → Observation V3 → `SOURCE_CONFIRMED`（[`melee_strike_driver.py:355`][source-match]） | 新 B12-A 批次：玩家近战得到 `minecraft:player_attack` 和 `source_confirmed_hit`；着火得到 `minecraft:on_fire`，没有来源实体 |
| “未交战目标在墙后”没用真实遮挡 | 改为真实石墙，两次都在 Fabric 中验证 | 新批次中 2 次，结果均为未移动、未攻击 |
| 缺少决定记录、B12-A 批次未公开 | D032；公开 B12-A 精简批次 | 已公开 `e2dc4fff` |

**伤害来源的匹配逻辑，我逐项核对过，是对的：**
- 只接受攻击之后编号更大的事件；
- 世界 tick 不早于攻击时刻；
- 目标就是本次攻击的追踪编号；
- 来源或直接来源是机器人自己时，判为来源确认；
- 明确是其他来源时，受伤动画不能再把它升级为命中；
- 两者同时出现（例如着火中被机器人打中）时，以来源确认为准。

## 二、B03 回归没有覆盖“一帧普通步行授权”

**背景：** 本轮把所有普通 `WalkSegment` 的非空移动改为一帧授权，而且依赖当前观察朝向或绑定视角（[`navigation_session.py:1400`][lease]）。影响的是所有经过 `NavigationSession` 的步行，不只是战斗。

**计划的写法：** 整改计划第 5 步第 3 条（[`B12-review-hardening-plan.md:109`][plan-b03]）写的是用 B03 “检查一 tick 普通步行授权没有造成断续”。验收记录也把 B03 列为这项改动的回归（[B12-B 验收第 148 行][acc-b03]）。

**实际情况：**
- B03 探针 `fixed_route_runtime_core.py` 直接创建 `FixedRouteController`，提交时使用 `valid_for_ticks=decision.input_lease_ticks`（[第 258 行][b03-step]，默认 2 tick）。
- 它不经过 `NavigationSession`，也就不带一帧授权或视角绑定。
- 所以这次 B03 通过说明的是“地面控制器本身没有退化”，不能说明“一帧授权没有造成断续”。

**有效覆盖在哪里：**
- C1-B（`646f449c`）通过 `MovingMeleeDriver` → `RuntimeNavigationDriver` → `NavigationSession` 追击移动目标，确实走了这条路径。
- 上一轮我也在 B12-B 批次中统计过：移动 tick 之后从未紧跟 `lease_exhausted`。

**建议二选一：**
- 把计划和验收中对 B03 的描述改为“地面控制器回归”；
- 或者补一组经过 `NavigationSession` 的长路线实机回归（例如 B04 已知图或 B07 支撑面路线），用它来证明“一帧授权不造成断续”。

## 三、伤害事件缓冲会留着无法送达的事件

**问题：**
- [`ClientDamageEventBuffer.snapshot`][buffer-skip] 遇到“目标没有追踪编号”的事件时直接跳过，但没有给它标记送达代次。
- 下一次采样只清除已经送达的事件（[第 47 行][buffer-ack]），所以这类事件会一直留在缓冲里，直到 64 个槽位溢出。
- 追踪编号只分配给进入正式感知范围的实体。视野外受伤的实体都会产生这类事件，例如白天在身后燃烧的僵尸、踩到仙人掌的怪物。

**复现**：[`UnregisteredDamageTargetsFillBuffer.java`](2026-09-26-b12-hardening-repro/UnregisteredDamageTargetsFillBuffer.java)，不依赖 Minecraft，直接编译运行。每轮记录一次“看不见的僵尸着火”和一次“机器人命中可见目标”：

| 轮数 | 丢弃计数 |
|---:|---:|
| 11 | 0 |
| 64 | 2 |
| 70 | 8 |

70 次机器人命中全部送达。

**影响：**
- 归因不受影响：滞留的事件世界 tick 较旧，会被攻击证据的时序条件过滤掉，而且攻击证据目前也不读取丢弃计数。
- 但常驻运行时，`damage_events_dropped` 会随时间一直增长，以后没法用它判断“有没有丢掉有用的证据”。

**建议：** 采样时无法解析目标的事件，要么在本代次一并确认丢弃（另记一个“未登记目标”计数），要么作为“目标未登记”的条目送出。

## 四、实机证据核对

我从原始记录重新统计：

| 批次 | 我的统计 | 文档 |
|---|---|---|
| B12-B `20260926T063032338012Z-780e0386` | 33 个场景：正例 24（前 8、后 8、左 4、右 4）、Fabric 边界 8（大幅转头、未交战目标在墙后、遮挡后导航、攻击距离内补看各 2）、活动目标 1。非零转头 12 帧，其中 10 帧同时移动；压下原因只有 `lower_priority_movement` 和 `expired` | 一致 |
| 活动目标请求 831–834 | 视角在客户端 tick N 生效，移动输入在 N+1 被消费，此时姿态朝向已是新值（-30.0°、-56.6°）；832、834 带移动，831、833 由导航给出中性移动 | 文档记为“两个组合请求” |
| B12-A 伤害来源 `20260926T055043431485Z-e2dc4fff` | 8 个游戏事实反例全部通过；攻击尝试中有 1 次 `source_confirmed_hit`（`minecraft:player_attack`）；着火诊断为 `minecraft:on_fire`，没有来源实体 | 一致 |

**活动目标这一项需要注意：**
- 在 4 帧连续转头中，只有 2 帧带移动。另外 2 帧，导航给出的就是中性移动，而不是被仲裁压下。
- 这两帧与目标被夹具瞬移后的战斗目标修订同时出现，但 trace 里没有记录导航当时的决策原因，所以无法从公开证据判断原因。
- 目前的证据只说明“组合能发生，而且顺序正确”，还不能说明“持续追击移动目标时，有多少帧能边转头边移动”。

**建议：**
- 在导航意图记录中写入路线决策原因；
- 补一个不靠瞬移、持续数秒追击活动僵尸的场景，统计“转头帧中带移动的比例”。

## 五、检查结果

| 检查 | 结果 |
|---|---|
| `export_motion_navigation_standalone.py verify --root .`（测试前后各一次） | 两次都是 `STANDALONE_EXPORT_OK files=661` |
| `check-java` | 找到 JDK 21 |
| `tests/motion_nav` | 456／456 |
| C1 证据与重放 | 43／43 |
| 公共控制、仲裁、失败处置、交战记忆与战斗驱动 | 171／171 |
| B10／B11／B12 探针、注入验收与部署入口 | 58 项，57 项通过，1 项因只适用于 Windows 而跳过 |
| Java 门禁（含伤害事件缓冲） | 3／3 |
| `public_runtime_evidence.py verify` | `PUBLIC_RUNTIME_EVIDENCE_OK archives=9 bytes=15774823` |

**前几轮复现脚本在新代码上的结果：**
- 第八轮“空中多一个 tick”：执行器保持落地，Runtime 没有失败；
- 第七轮“半砖旁的起点”：选中整格方块；
- 第七轮“伤害窗口”：跨多 tick 的偏差判为 `unverified_span`；
- 第九轮“交替失败”：第 6 次失败返回 `NO_PROGRESS_RETRY_EXHAUSTED`。

第九轮的 `strafe_aim_yaw_budget.py` 模拟的是未绑定视角时的 5° 规则。现在战斗移动走的是绑定视角的路径，这个估算只在没有战斗视角时才适用。

## 六、对照总体设想

**本轮之后，战斗执行层具备了“最优策略战斗”需要的三项基础事实：**

1. **打没打中、是谁打的。**
   - `SOURCE_CONFIRMED` 让命中归因不再靠时间窗口猜测。
   - 缓冲里也有“目标是自己”的事件和来源实体，下一步可以直接回答“谁在打我”，进而确定该还击哪个目标，交战许可也不必只靠视觉历史。
2. **边瞄准边移动。** 战斗先定朝向、导航按新朝向走，已经在游戏里成立，而且不需要第二套身体控制器。
3. **失败有类型、有上限。** 上层能在“攻击不起作用”时及时换策略，而不是等任务超时。

**下一步：** D032 明确把战术目标交给 Jev 另行设计。现在执行层已经能报告足够的事实，适合开始定义“Jev 给出战术目标（后撤点、侧移点、掩体位置）→ 导航负责走过去 → 战斗负责视角和攻击”的接口。在此之前，建议先补上第四节的持续追击场景，确认绑定视角的移动在活动目标下的实际比例。

## 七、建议（按优先级）

1. **B03 的描述：** 改为“地面控制器回归”，或者补一组经过 `NavigationSession` 的长路线实机回归。
2. **活动目标证据：** 在导航意图记录中写入决策原因；补一个持续追击活动僵尸的场景，统计转头帧中带移动的比例。
3. **伤害事件缓冲：** 采样时丢弃或单独报告无法解析目标的事件，保持 `damage_events_dropped` 的含义。
4. **伤害来源样本：** 多敌人或队友场景出现时，补“其他实体造成伤害”的实机样本（文档已计划）。

## 八、复现脚本

目录：[`2026-09-26-b12-hardening-repro/`](2026-09-26-b12-hardening-repro/)。

| 脚本 | 运行方式 | 结果 |
|---|---|---|
| `UnregisteredDamageTargetsFillBuffer.java` | 在 `211af42` 检出目录的仓库根目录，用 `javac` 编译 `ClientDamageEventBuffer.java` 和本文件后运行（命令见文件头） | 丢弃计数：11 轮 0，64 轮 2，70 轮 8；70 次机器人命中全部送达 |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e
[conditioned]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/motion_nav/navigation_session.py#L834-L853
[arbiter]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/runtime/arbiter_v1.py#L192-L202
[executor]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/motion_nav/action_route_executor.py#L497-L509
[retry-limit]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/skills/attack_evidence.py#L209
[retry-check]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/skills/attack_evidence.py#L285-L287
[source-match]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/skills/melee_strike_driver.py#L355-L376
[lease]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/motion_nav/navigation_session.py#L1400-L1424
[plan-b03]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/docs/motion_navigation/stages/B12-review-hardening-plan.md#L109
[acc-b03]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/docs/motion_navigation/acceptance/B12B-partial-observation-combat-motion.md#L148
[b03-step]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/scripts/fixed_route_runtime_core.py#L258-L259
[buffer-skip]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/backends/runtime_overlays/mc121_observation/ClientDamageEventBuffer.java#L53-L58
[buffer-ack]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/211af4239351d8cf5e9d8b5cbb9fc67e82a1f12e/mc2p/backends/runtime_overlays/mc121_observation/ClientDamageEventBuffer.java#L46-L49
