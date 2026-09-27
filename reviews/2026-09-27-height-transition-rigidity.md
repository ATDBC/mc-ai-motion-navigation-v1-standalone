# 高度衔接是否过于僵硬：现状、实测与改法

日期：2026-09-27
对象：`origin/main` 提交 [`4b9e73e`][base] 的地面支撑、规划、动作执行和交接链路
性质：外部审查意见，回应“地面支撑算法链路是否太僵硬，影响不同高度移动的衔接与丝滑”。不属于 `docs/motion_navigation/` 四类正式文档。

## 结论

1. **是的，现在的链路在高度变化处明显僵硬。每一次高度变化都要停下来、单独执行、再停下来。**
   - 在完全已知、没有延迟的闭环模拟里：
     - 同样的路线比“按住前进键、让原版自动上台阶和下落”慢 1.3 到 2.4 倍；
     - 中途完全停下 2 到 8 次。
   - “下一整格”在没有延迟时就已失败；输入晚 1 tick 后，其余 4 条里又有 3 条失败。
2. **僵硬来自四层，彼此叠加：**
   - **支撑图：** 只有高度完全相同才连 Walk；任何高差，哪怕是 1/16 格的土径，都变成固定 0.8 秒、必须停稳的 Step。
   - **控制器：** Walk 要求全程同一高度、每帧着地；Step、上跳、下落都只接受基本静止的入口（速度不超过 0.1 格/秒）。
   - **交接：** 前一段只按“半径多少以内停稳”收尾，不知道下一段需要的方向性入口。模拟里，平地走到下落点时已经因此失败。
   - **代价：** 动作代价是常数，规划会绕开一块半砖，而不是直接走过去。
3. **这不是某个参数没调好，而是交付顺序留下的结构：** B05—B09 先把每种高度动作做成“静止入口、单独校准”，B10 只把跨隙改成了可以带速度进入。这样安全、容易验收，但到了追求连续移动的阶段，就成了主要瓶颈。
4. **改法不需要新框架，项目里已经有合适的工具：** 1.21 运动计算器，以及 B10 的“先用计算器验证、再连续执行”。建议按四步推进：
   - 先修交接契约；
   - 再把台阶高度以内的高差并入 Walk，由计算器验证整段；
   - 然后让规划代价来自计算器；
   - 最后把上跳和下落改成可以带速度进入。
   - 第二步收益最大；第一步修的是一个已经会导致失败的契约错配。

## 一、实测

方法见 [`height_transition_cost.py`](2026-09-27-height-transition-repro/height_transition_cost.py)：
- 场景是 7 格宽的直道，世界完全已知，所以绕路没有好处；
- “现行链路”用仓库的 `build_surface_graph`、`astar_surface_plan`、`RouteAdmitter`、`ActionRouteExecutor`，与运动计算器 `physics_1_21.step` 组成闭环；
- “按住前进”用同一个计算器，只按住前进键，遇到整格高差才跳，在终点前松键刹车。它只是一个下限参照，不是建议的控制器。

| 场景 | 现行链路：动作序列 | 用时（tick） | 中途停稳 | 输入晚 1 tick | 按住前进 |
|---|---|---|---|---|---|
| 连续上两个半砖（B07 形状，5 格） | Walk, Step, Step, Walk | 47 | 3 次 | 完成，65 | 28，不停 |
| 连续下两个半砖 | Walk, Step, Step, Walk | 61 | 5 次 | **失败**：`step_timeout` | 31，不停 |
| 草方块和土径交替（每格高差 1/16，8 格） | 8 个 Step | 100 | 8 次 | **失败**：`step_timeout` | 42，不停 |
| 上一整格 | Walk, JumpUp, Walk | 45 | 2 次 | **失败**：`fixed_route_stalled` | 35 |
| 下一整格 | Walk, ControlledDrop, Walk | **失败**：`entry_position_out_of_range` | — | 失败 | 29，不停 |

绕路测试 [`slab_bump_detour.py`](2026-09-27-height-transition-repro/slab_bump_detour.py)：平地上从 z=0 走到 z=6，正中间放一块下半砖。
- 规划器选择绕到旁边一列再绕回来，多拐两个直角，规划代价 1.83 秒；
- 直接走过去，按它的常数代价要约 2.97 秒；在模拟里，“按住前进”直接走过这块半砖只需 32 tick（1.6 秒），全程不停。

## 二、僵硬在哪里

### 1. 支撑图：同高才是 Walk，其余都是专门动作

- [`known_map_planner.py:1459`][graph-walk]：只有 `abs(delta_y) <= 1e-6` 的相邻两格才建 Walk 边，代价按最高速度计算。
- [`:1477`][graph-step]：只要有高差，并且在 1 格以内，就建 Step 边，代价固定为 `cost_seconds = 0.8`。
- 邻居只有 4 个方向（[`:34`][dirs]），路线是经过格子中心的直角折线。
- **后果：** 原版里不需要任何操作的高差（下半砖、楼梯、土径、耕地、地毯、雪层；原版台阶高度是 0.6），在这里都被拆成一个个独立动作。土径和草方块交替的小路，每一格都要停一次。

### 2. 控制器：Walk 只能同高，特殊动作只能静止进出

- Walk 在当前高度偏离路线高度超过 0.10 时，判为 `fixed_route_level_mismatch`（[`fixed_route.py:456`][level]）；只要有一帧不着地，就判为 `ordinary_ground_state_lost`（[`:394`][ground]）。所以原版自动上台阶、短暂下落，都不能留在 Walk 里完成。
- Step 的入口和出口速度都不超过 0.1 格/秒（[`step-b07-v1.json:13`][step-limits]），也就是必须停稳。这份配置自己也写明：“只验证了静止入口，带速连续衔接不在 B07 范围内”。
- 上跳和受控下降的入口速度同样不超过 0.1 格/秒。B10 只给跨隙建立了带速入口的求解和验证。

### 3. 交接：靠“停在某个点附近”，而不是“进入下一段的入口范围”

- 前一段 Walk 只得到一个半径容差和停稳速度（[`action_route_executor.py:149`][handoff]）。
- 但下一段的入口是有方向的：受控下降要求“向后偏差不超过 0.01 格、向前不超过 0.08 格”（[`air-motions-b09-v1.json:104`][drop-entry]），而 Walk 只知道“半径 0.12 以内”。
- 模拟里，Walk 停在离边缘侧中心差 0.064 格的位置，下落以 `entry_position_out_of_range` 拒绝。实机之前能通过，取决于 Walk 恰好停在稍微冲过的一侧。
- 另外，每段都刹车到一个点，所以对输入延迟很敏感：冲过头时会爬上下一层，或者从边缘退下来。第十五轮 B07 的分析里已经看到这种现象。

### 4. 代价：常数代价与真实用时脱节

- Step 0.8 秒、下落 0.7 秒、上跳和跨隙也是固定代价，但走过一块半砖实际只多花零点几秒。
- A* 因此会为了避开半砖绕路。绕路又带来更多拐角和减速，最终更不“丝滑”。

## 三、为什么会这样

这是有意为之的交付顺序：每种高度动作先在静止入口下单独校准、单独验收，失败时容易定位，也不会把未经验证的衔接带进正式链路。B07 的配置、B09 的入口窗口、B10 只把跨隙改成带速进入，都是这个思路的延续。

这个顺序在能力建设期是合理的。但现在要的是“像 2P 玩家一样连续移动”，瓶颈已经从“能不能做到”变成“能不能不停地做到”。继续增加专门动作和接续矩阵的行数，只会让交接点更多。

## 四、建议的改法（按顺序，每步都可以单独验收）

### 第一步：交接改为“进入下一段的入口范围”（小改动，先修错配）

- 下一段动作公开自己的入口范围：有方向的位置窗口、速度区间和朝向。上跳、下落、Step 已有这些参数，只是没有传给前一段。
- 前一段 Walk 以这个范围为目标，不再只用一个半径容差加停稳速度。
- **验收：** 本文“下一整格”的场景，在 0 tick 和 1 tick 延迟下都能完成；原有的接续矩阵不退化。

### 第二步：台阶高度以内的高差并入 Walk，由计算器验证整段（收益最大）

- **规划：** 相邻两格高差在台阶范围内时，照样建 Walk 边，路线点带上每格的支撑高度。Step 只在计算器验证失败时作为后备，例如特殊外形或贴墙的情况。
- **接纳：** 用现有的 `physics_rollout`，按巡航速度把整段 Walk 模拟一遍，确认身体一直有支撑、没有碰撞、预期的短暂离地都落在已知支撑上。这正是 B10 对跨隙做的事，扩展到地面段即可。
- **控制器：**
  - 高度检查改为对照“当前进度处的预期支撑高度”；
  - 计算器预测到的短暂离地，按落地确认处理，不立即判为失败。
  - 这一条同时覆盖 B07 连续路线 `ordinary_ground_state_lost` 所在的那类问题。
- **验收：**
  - 半砖、楼梯、土径、地毯、雪层路线中途不停；
  - 用时不超过“按住前进”的 1.3 倍；
  - 1 tick 延迟下全部完成；
  - 原有的安全、碰撞和反例检查不退化。

### 第三步：规划代价来自计算器

- 各类边的代价改为用计算器按巡航速度算出的用时，拐角也计入减速时间，不再使用 0.8 秒这类常数。
- **验收：** 本文的“单块半砖”场景直接走过；对比规划代价与实际用时的误差。

### 第四步：上跳和受控下降改为带速进入

- 沿用 B10 跨隙的方式：求解器用计算器找出从运动状态进入的输入序列，经验证后由 `VerifiedMotionExecutor` 执行；静止入口保留为后备。
- **验收：** 行走中上一格、走下一格时，中途不停；动作许可窗口不放宽。

### 不建议的做法

- **不经计算器验证，就假定“原版会自动上台阶”：** 碰撞箱、天花板高度和边缘处的细节都需要验证。
- **放宽现有的静止入口参数来“提速”：** 这些参数是按静止入口校准的，放宽后证据就不成立了。
- **引入通用轨迹优化框架：** 计算器和 B10 的验证链路已经够用，按 AGENTS.md，不提前引入新框架。
- **8 方向或任意角度路径平滑：** 能让转弯更顺，但不是高度衔接的问题，应单独做。

## 五、复现

目录：[`2026-09-27-height-transition-repro/`](2026-09-27-height-transition-repro/)。在 `4b9e73e` 检出目录的仓库根目录运行。

| 脚本 | 运行方式 | 结果 |
|---|---|---|
| `height_transition_cost.py` | `PYTHONPATH=. python -B <脚本路径>` | 第一节的表格 |
| `slab_bump_detour.py` | `PYTHONPATH=. python -B <脚本路径>`（与上一个脚本放在同一目录） | 规划器绕开单块半砖，代价 1.83 秒 |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/4b9e73e9191f52d4ca4aa8468b32c671db223b87
[graph-walk]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/known_map_planner.py#L1459-L1475
[graph-step]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/known_map_planner.py#L1476-L1490
[dirs]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/known_map_planner.py#L34
[level]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/fixed_route.py#L456-L459
[ground]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/fixed_route.py#L394-L396
[step-limits]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/config/motion-navigation/step-b07-v1.json#L10-L18
[handoff]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/action_route_executor.py#L149-L216
[drop-entry]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/config/motion-navigation/air-motions-b09-v1.json#L100-L106
