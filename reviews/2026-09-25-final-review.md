# D026 二次整改复审（第七轮）

日期：2026-09-25
审查对象：`origin/main` 提交 [`1ea37d7`][base]（D026 二次复审整改、三个新的实机批次）
性质：外部审查意见，不属于 `docs/motion_navigation/` 四类正式文档，不改变任何阶段结论或验收门槛
前几轮：[整体审查](2026-09-24-architecture-review.md)、[C1-R 复审](2026-09-25-c1r-rereview.md)、[第三轮](2026-09-25-c1r-rereview-2.md)、[第四轮](2026-09-25-c1r6-review.md)、[第五轮（B11）](2026-09-25-b11-review.md)、[第六轮（D026）](2026-09-25-d026-review.md)

本文代码链接都指向 `1ea37d7`。本分支仍基于旧快照，请不要按本分支的相对路径查看代码。

## 结论

1. **上一轮提出的问题全部修好，而且这次有实机证据。**
   - 上一轮的复现脚本在新代码上都得到了预期结果，包括：
     - 边缘改目标；
     - B10 探针的输入账本；
     - 准备期限的计时范围；
     - 跨两 tick 的匹配残差；
     - 先跑测试再校验时的缓存误报。
   - 三个新批次我都从原始行重新统计了，与文档一致（第四节）。
   - B11 新增了“边缘改目标”和“点击后改目标”两类实机场景，各跑两次，全部通过。
   - C1-C 新批次中，第四轮发现的那次受击（同一试次、同样的序号 445／446）在游戏里已经合并成一次击退。这是 D024 第一次有实机证据。
   - C1-C 测试框架改为先等服务端确认攻击，再进入下一控制帧，关闭了 `damage_stage_mismatch`。
   - 我同意 B11 的跨阶段门槛已经关闭。
2. **新发现一个中等问题：正式导航路径没有把动作证明的生效窗口交给 Runtime**（第二节）。
   - 本轮新增的架构规则要求把最早和最晚生效 tick 交给 Runtime，但目前只有 B10 探针这样做。战斗和 B11 实际使用的 `RuntimeNavigationDriver` 仍按单 tick 登记。
   - 跨隙首条命令的证明允许晚一个 tick 生效。这种情况在正式路径上会被判为输入丢失：机器人仍会安全落地，但动作会中断并重新规划。
   - B10 实机这次的 200 条已验证命令全部在最早 tick 生效，所以两条路径的差别没有被触发。
3. **三个小问题**（第三节）：
   - 新的起点选择在“整格方块边缘、旁边是半砖”时，会选到低半格的半砖，与架构文档“选择与脚底高度最接近的支撑面”不一致；
   - D024 的归因窗口实际放宽到了最多 8 tick。这个方向来自我上一轮的建议，但 D024 仍写着“只等一个 tick”，并且保留着反对放宽的理由。C1-R6 验收中对我复现结果的描述也过时了；
   - D024 引用的 C1-C 历史归档（运动 tick 448／449）已经从公开包中移除，但 D024 仍写“历史公开归档保持原字节”。

## 审查范围

- 阅读了以下文档的全部改动：
  - D023、D024、D026；
  - B10-C、B11、C1-C、C1-R 的验收文档；
  - B10、C1-C 和支撑面的架构文档；
  - B11 的阶段文档；
  - README。
- 阅读了以下代码的全部改动：
  - `external_motion.py`、`navigation_session.py`、`physics_adapter.py`、`world_model.py`；
  - `player_runtime_v1.py`、`block_placement_driver.py`；
  - B10、B11、C1-C 的实机脚本，以及导出与证据工具。
- 在 Linux 容器（Python 3.11.15、OpenJDK 21）中按 README 运行了全部声明的检查，并重跑了前两轮的复现脚本。
- 新写了三个复现脚本（第七节）。
- 解开了三个新批次，从原始行重新统计。
- 没有运行 Fabric 实机。

## 一、上一轮问题复核

| 上一轮问题 | 修复 | 我的复核 |
|---|---|---|
| 边缘改目标导致任务失败 | 起点改为按碰撞箱覆盖的支撑面查找（[`_surface_for_body`][surface]） | 上一轮脚本：两个时机都成功，各确认 1 块；实机 `c17b889e` 中两类新场景各 2 次通过 |
| B10 探针的本地账本 | 改用 `runtime.input_ledger`；已验证命令的窗口通过 `input_execution_window` 交给 Runtime | 探针不再调用 `observe_sample`；中性帧由 Runtime 统一登记 |
| 准备期限覆盖确认等待 | 只在 `READY` 且未等待仲裁时计时；发出后清零 | 上一轮脚本：期限 3 和 120 都在第 4 次观察确认成功 |
| 仲裁等待不是连续计数 | 不提出操作的帧会清零 | 读代码确认 |
| 跨两 tick 的匹配残差被报成“未验证” | 起点等于伤害 tick 的完整残差可以归因 | 上一轮脚本：448→450 匹配 → `damage_without_motion_residual`，没有事件 |
| 先跑测试再 `verify` 失败 | 校验时忽略 `__pycache__` 和 `.pyc` | 跑完全部测试后再校验：`STANDALONE_EXPORT_OK files=633` |

D026 还采纳了第五轮的一条建议：动作证明要预先声明计划中的世界变化，也就是在哪个 tick、改变哪一格、预期是什么结果。

## 二、正式导航路径没有传生效窗口

**本轮的机制：**
- Runtime 新增了参数 `control_frame(..., input_execution_window=...)`；
- [`_submit_input_record`][submit-record] 在收到窗口时，按证明给出的最早和最晚 tick 登记；
- 没有收到窗口时，登记 `requested == latest == 当前运动 tick + 1`，只允许一个 tick。

**只有 B10 探针传了窗口**（[`b10_gap_solver_runtime.py:732`][b10-window]）：
- 全仓库只有这一处业务代码使用这个参数，另外只有一条 Runtime 单元测试；
- [`RuntimeNavigationDriver.tick`][nav-tick] 和它服务的父驱动（移动近战、B11 世界改变导航）调用 `control_frame` 时都不传窗口；
- 但 [B10 架构文档][b10-arch] 本轮新写的规则是：“经过 Runtime 提交的已验证命令，要把求解器给出的最早和最晚生效 tick 一并交给 Runtime。”

**复现**：[`formal_path_drops_verified_start_window.py`](2026-09-25-final-repro/formal_path_drops_verified_start_window.py)。证明允许首条命令在 tick 11 或 12 生效，这里让它在 tick 12 生效：

| 登记方式 | 账本判定 | 执行器下一步 |
|---|---|---|
| 探针方式（传窗口 11..12） | `applied` | `submit_verified_command`，继续第 1 条命令 |
| 正式驱动方式（Runtime 默认 11..11） | `applied_outside_window` | `input_applied_outside_window`，放弃已验证动作 |

我也直接检查了 `PlayerRuntimeV1`：不传窗口时，登记记录的 `requested_first_tick` 和 `latest_allowed_first_tick` 相同。

**影响：**
- 首条输入晚一 tick 生效，在实机中确实出现过：C1-R R4 的失败记录 `20260924T184028642222Z-82a2c0f0` 就是这种情况。R4 因此让两个相邻 tick 的分支都经过完整验证。
- 正式路径上它不会造成危险，因为执行器会保持落地责任并重新规划。但跨隙会被打断，而且 B10 的实机通过率不能代表战斗和 B11 路径。
- 这个问题在旧版本里就存在：旧探针用本地账本传窗口，正式路径一直按单 tick 登记。本轮探针改用 Runtime 账本以后，两条路径的差别才变成“同一个 Runtime 的两种登记方式”。

**建议：**
- 把窗口放在导航提案里，跟随移动意图一起提交；
- Runtime 只在该意图赢得移动仲裁时才采用这个窗口；
- 这样父驱动（战斗、B11）不需要知道动作证明的细节；
- 以后 B12-B 在同一帧里组合多个来源时，也需要这种“附着在获胜意图上的元数据”，可以借这次一并确定接口；
- 补一条正式驱动的集成测试：首条命令在最晚允许 tick 生效。

## 三、小问题

### 1. 起点选择在半砖旁会选错高度

**复现**：[`start_surface_prefers_lower_slab.py`](2026-09-25-final-repro/start_surface_prefers_lower_slab.py)。身体站在整格方块顶面（y=64），中心越过边缘 0.15–0.20 格，与 B11 的站位相同：

| 方块旁边 | 选中的起点 |
|---|---|
| 空气 | 第 0 列，高度带 64（正确） |
| 下半砖（顶面 63.5） | 第 1 列，高度带 63（半砖） |

- **原因：** [`_surface_for_body`][surface] 在所有与碰撞箱重叠的支撑面里按三维距离取最近。半砖虽然低半格，但水平上更近，所以胜出。
- **与文档的出入：** [支撑面架构][support-doc]写的是“选择与当前身体位置和脚底高度最接近的一个”。
- **影响：** B11 只用整格方块，不会遇到。但 B07 已经支持半砖和楼梯，击退或急停后停在这类边缘时，路线会从错误的高度开始。
- **建议：** 着地时先筛出顶面高度与脚底一致的支撑面，再比较水平距离。

### 2. D024 的窗口已经放宽，文档没有同步

- **代码：** 现在只要一段完整残差从伤害 tick 开始，就可以归因（[`external_motion.py:474`][attribute]），不再限制终点。
- **这符合我上一轮的建议**：残差完整，就说明中间每个 tick 的世界和输入都齐全。
- **但它带来一个取舍：** 残差缺世界格期间，锚点一直停在伤害 tick。等世界格补齐后，第一段完整残差可以跨到 8 tick 的回放上限。复现见 [`damage_window_spans_missing_world_chain.py`](2026-09-25-final-repro/damage_window_spans_missing_world_chain.py)：伤害后连续 6 次缺世界格，448→455 的偏差最终被合并为 `DAMAGE_KNOCKBACK`。
- **与 D024 的冲突：** 这正是 D024“为什么只等一个 tick”一节担心的情况：“一次着火扣血后，稍晚发生的活塞推动可能被误认为伤害击退。”
- **目前的影响：** 恢复流程对击退和其他外力的处理相同，事件来源只影响原因标签（[`moving_melee_driver.py:758-762`][label]）。所以这主要是文档与语义的问题，要到 B12-A 区分“目标受伤”和“伤害来源”时才会影响行为。

**建议二选一，并写进 D024：**
- 保留当前做法：更新 D024 的第 2–4 条和“为什么只等一个 tick”一节，写明窗口最多 8 tick，以及接受的误归因风险；
- 或者只允许多 tick 的**匹配**残差确认“没有击退”；多 tick 的**偏差**残差，只要中间出现过缺世界或缺输入，就判为 `DAMAGE_WITH_UNVERIFIED_MOTION`。

**另外两处文档也过时了：**
- C1-R6 验收第 59 行仍写“评审方提供的复现现在依次得到 … `damage_motion_unverified_gap`”。在当前代码上，这个复现（第五轮的 `damage_grace_then_missing_world.py`）得到的是 `damage_knockback_confirmed`；
- D026 的“证据”一节也有同样的描述。

### 3. D024 引用的历史归档已从公开包中移除

- 本轮用新的通过批次替换了 B10-C、C1-C、B11 三个旧批次。其中旧的 C1-C 通过批次 `97bc0db8`，正是 D024 回归测试所依据的样本（运动 tick 448／449）。
- D024 第 31 行仍写“历史公开归档保持原字节和原判定”。C1-R 的阶段和验收文档中，C1-C 的结果也仍引用 `97bc0db8`。
- 新的 C1-C 批次里有同一试次的同一时序（序号 445／446，运动 tick 451／452），而且已经被正确合并，可以作为新的实机证据。但要从公开仓库核对“修正前确实被拆成两条”，只能回到 `7827f25` 之前的提交。
- **建议二选一：**
  - 保留 `97bc0db8`（1.39 MB；公开包目前 15.1 MB，上限 30 MiB）；
  - 或者在 D024 中注明它只存在于旧提交，并改用新批次的 451／452 作为实机依据。

## 四、实机证据核对

三个新批次都是我从原始行重新统计的，没有使用归档里的汇总字段：

| 批次 | 我的统计 | 文档 |
|---|---|---|
| B10-C `20260925T123521898324Z-5d0a66dc` | 求解验证 140 次全部落地、0 次水平碰撞、每次只有一个跳跃脉冲；142 次求解 P95／P99／最大 24.1324／24.9028／27.8626 ms；协调 10 次全部落地；210 个协调控制帧，其中 200 帧提交已验证命令、全部在最早 tick 生效，0 帧迟到，0 个中性落地帧 | 一致 |
| C1-C `20260925T124344500270Z-ac8d6347` | 30 个试次全部通过（正例 20、反例 10）；33 次 `damage_knockback_confirmed`，其中 1 次先经过 `damage_motion_grace`（`post-recovery-rehit-north-05`，序号 445→446）；没有 `unverified`，也没有无来源外力；恢复阶段 started 33、completed 26、cancelled 7 | 一致 |
| B11 `20260925T120057978030Z-c17b889e` | 正例 60（单格 40、两格 10、三格 10），确认放置 90 次；边界和反例 24 个全部通过；“边缘改目标”和“点击后改目标”各 2 次，都是 1 次操作、最终成功 | 一致 |

**证据边界：**
- B10 这次没有出现迟到或中性落地帧，所以“中性帧由 Runtime 记录”和“两 tick 起跳窗口”只有组件证据；
- B11 的两个新场景只覆盖整格方块和直桥。

## 五、公开仓库复现结果

| 检查 | 结果 |
|---|---|
| `export_motion_navigation_standalone.py verify --root .`（测试前后各一次） | 两次都是 `STANDALONE_EXPORT_OK files=633` |
| `check-java` | 找到 JDK 21 |
| `tests/motion_nav` | 448／448 |
| C1 证据与重放 | 43／43 |
| 公共控制、仲裁、失败处置、交战记忆与战斗驱动 | 147／147 |
| B10／B11 探针与部署入口 | 32 项，31 项通过，1 项因只适用于 Windows 而跳过 |
| Java 门禁 | 2／2 |
| `public_runtime_evidence.py verify` | `PUBLIC_RUNTIME_EVIDENCE_OK archives=7 bytes=15071127` |

## 六、对照总体设想

- **B11 已经可以作为“创造地形”的底座：**
  - 单块事务；
  - 任务级额度；
  - 放置期间改目标；
  - 有上限的准备和仲裁等待；
  - 以及三类跨阶段回归。
- **D026 已经把“动作途中计划改变世界”写进证明边界**，为以后跳起垫块、战斗中筑墙留出了正确的接口。
- **第二节的问题与 B12-B 直接相关：**
  - 同一控制帧里，移动、视角和操作可能来自不同拥有者；
  - 每个拥有者附带的约束（生效窗口、计划中的世界变化）都应该绑定在它自己的意图上，由 Runtime 在仲裁后统一登记；
  - 现在“窗口只能由调用方直接传给 Runtime”的做法，只适用于单一来源的探针。

## 七、建议（按优先级）

1. **生效窗口：** 由导航提案携带，Runtime 在意图获胜后登记；补正式驱动的“最晚 tick 生效”集成测试。
2. **起点选择：** 着地时按脚底高度筛选支撑面，补半砖和楼梯边缘的组件测试。
3. **D024 文档：** 明确多 tick 窗口的取舍；更新 C1-R6 验收第 59 行和 D026 证据段的过时描述。
4. **公开证据：** 保留 `97bc0db8`，或者在 D024 中改用新批次的实机依据。

## 八、复现脚本

目录：[`2026-09-25-final-repro/`](2026-09-25-final-repro/)。在 `1ea37d7` 检出目录的仓库根目录运行。

| 脚本 | 命令前缀 | 结果 |
|---|---|---|
| `formal_path_drops_verified_start_window.py` | `PYTHONPATH=.:reviews/2026-09-25-final-repro` | 传窗口：继续执行；按 Runtime 默认登记：`input_applied_outside_window` |
| `start_surface_prefers_lower_slab.py` | `PYTHONPATH=.` | 旁边是空气时选中方块；旁边是下半砖时选中半砖 |
| `damage_window_spans_missing_world_chain.py` | `PYTHONPATH=.` | 448→455 的偏差被合并为 `DAMAGE_KNOCKBACK` |

`gap_session.py` 是从第三轮复制来的辅助文件，只改了说明文字。

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/1ea37d759704f3666a59a6bef191377f5a3a71ce
[surface]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/mc2p/motion_nav/navigation_session.py#L1403-L1431
[submit-record]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/mc2p/runtime/player_runtime_v1.py#L524-L548
[b10-window]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/scripts/b10_gap_solver_runtime.py#L726-L737
[nav-tick]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/mc2p/skills/navigation_session_driver.py#L140-L163
[b10-arch]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/docs/motion_navigation/architecture/B10-motion-solving-continuous-execution-v1.md#L98
[support-doc]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/docs/motion_navigation/architecture/support-surfaces-v1.md#L40
[attribute]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/mc2p/motion_nav/external_motion.py#L466-L477
[label]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1ea37d759704f3666a59a6bef191377f5a3a71ce/mc2p/skills/moving_melee_driver.py#L758-L762
