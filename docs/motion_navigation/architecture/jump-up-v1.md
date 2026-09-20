# 一格上升动作 V1

日期：2026-09-19
状态：B05 已实现并通过 Fabric 验收

## 作用

本设计只负责一种动作：从普通地面低速跳上水平相邻、顶面高一格的平台。它验证导航系统能否把一种跨高度能力接入现有世界知识、后台规划、路线接纳、动作执行和唯一输入出口。

## 模块边界

| 模块 | B05 新职责 | 不拥有的状态 |
|---|---|---|
| 世界知识与几何 | 保存多层已知事实；检查起点、轨迹净空和落地支撑 | 当前跳跃阶段 |
| JumpUp 能力 | 声明入口状态、目标状态、实测轨迹包络、依赖和代价 | 当前目标、当前路线 |
| 后台规划 | 把已确认 JumpUp 当作有向边参与搜索 | 动作许可、真实输入 |
| 路线接纳 | 核对候选身份、依赖、身体接入和动作段顺序 | 空中阶段推进 |
| 动作段执行器 | 按顺序运行 Walk 或 JumpUp，并根据真实观测交接 | 世界事实、后台搜索 |
| MotorGateway | 仲裁并发送完整移动输入，处理租约和回执 | 判断跳跃是否成功 |

## 已实现的最小契约

下列名称用于约束职责。实现时可以按现有仓库风格调整文件内组织，但不能丢失这些信息。

### JumpUpProfile

配置文件 `config/motion-navigation/jump-up-v1.json` 保存校准后冻结的能力范围：

- 适用游戏、Fabric 和物理配置版本；
- 支持材质与角色姿态；
- 允许的入口水平速度和相对起跳位置；
- 输入序列及每段最长 tick；
- 逐 tick 参考位置、速度和误差包络；
- 离地、最高点、落地和停稳的时间界限；
- 目标落地区域与后续 Walk 允许的出口状态；
- 动作预计时间。

当前 Profile 只支持 Minecraft 1.21、普通草方块、站立姿态和相邻一格上升。入口水平速度不超过 `0.1 格/秒`。相对起点中心允许向后 `0.18` 格、向前 `0.01` 格、横向 `0.16` 格，同时仍受半径 `0.18` 格约束。范围采用非对称限制，因为实测证明角色稍微靠前并带有速度时，会先撞到高台侧面。没有匹配配置时返回 `UNSUPPORTED`。

### JumpUpEdge

后台图边至少包含：

```text
start_node
end_node
profile_id
direction
cost_seconds
dependencies
```

它是有向边。首版只允许水平相邻且目标脚底高度高一格。边成立表示静态知识和能力范围允许尝试，不表示当前身体已经获得执行许可。

### RouteAction

接纳路线由有序动作段组成：

```text
WalkSegment(fixed_route, entry, exit, dependencies)
JumpUpSegment(edge, entry_region, landing_region, dependencies)
```

相邻 Walk 边可以继续合并。合并必须在 JumpUp 处停止。`route_id` 使用世界、目标、节点和动作类型计算；身体进度不参与身份。

### JumpUpDecision

每帧输出至少包含：

```text
state
movement
input_lease_ticks
missing_cells
reason_code
control_time_ns
```

`reason_code` 只用于诊断，不能反向决定动作权限。权限来自状态、几何结果、输入回执和最新身体观测。

## 几何和安全检查

1. 起跳身体必须站立、接地，并位于 Profile 的入口集合。
2. 起跳支撑和目标支撑必须已知、可用且材质匹配。
3. 用校准轨迹的相邻采样做连续身体扫掠，不能只检查最高点和落点。
4. 扫掠包络按校准误差扩大。扩大部分用于碰撞安全，不能伪造更大的脚下接触面积。
5. 空中阶段不要求持续支撑；落地阶段必须检查整个落地区域和后续可恢复范围。
6. 任何关键空间未知都返回 `NEEDS_INFORMATION`。已知碰撞返回 `BLOCKED`。形状、材质或物理配置未支持返回 `UNSUPPORTED`。
7. 路线收益不能抵消上述结果。

## 阶段推进

| 当前阶段 | 进入条件 | 离开条件 |
|---|---|---|
| PREPARE | 路线进入 JumpUp，身体仍在起跳前 | 身体满足入口状态并完成最终几何核对 |
| REQUEST_TAKEOFF | 已获准提交起跳输入 | 输入已确认且实际身体离地；或超过离地窗口失败 |
| AIRBORNE | 实际 `is_on_ground=false` 且高度开始变化 | 重新接地，进入落地核验 |
| VERIFY_LANDING | 观察到接地 | 层高、水平区域、支撑和出口状态全部成立 |
| COMPLETE | 落地核验通过 | 路线执行器交给下一动作段 |

不能通过累计控制 tick 猜测已经离地或已经落地。控制 tick 只用于超时和选择当前输入。

## 取消和输入失联

- PREPARE 中取消：不再起跳，复用地面制动，停稳后完成取消。
- REQUEST_TAKEOFF 中取消：如果还未离地，停止续租跳跃输入并按地面规则结束；如果观测已经离地，转为空中取消。
- AIRBORNE 中取消：记录取消中，执行 Profile 声明的落地控制。落地前不能报告取消完成。
- VERIFY_LANDING 中取消：完成落地核验和必要停稳，再报告取消。
- 输入回执被拒绝：不把动作推进到下一阶段。
- 空中输入失联：MotorGateway 仍按租约释放输入；执行器继续观察实际轨迹，安全落地后报告 `INPUT_LOST`。
- 控制器在启动时保存世界会话。后续帧来自新会话时，旧动作立即失败并输出中立输入。
- PREPARE 到 CANCELLING 属于一个活动动作。第二次 `start()` 不能覆盖它；COMPLETE、CANCELLED、INPUT_LOST、FAILED、UNSUPPORTED 和 BLOCKED 等终态不会被后续身体帧自行改写。
- 活动保护同时存在于 JumpUp 控制器和上层动作路线执行器。新路线不能通过新建控制器来绕过空中动作的保护。

## 与 B04 的兼容关系

- 未启用 `JumpUpProfile` 时，只生成原有同高 Walk 节点和边。
- B04 的四邻接 Walk 代价、后台隔离和路线身份不变量继续成立。
- B03 控制器不接受跨高度折线，也不增加空中特判。
- 路线接纳和执行器负责把动作段分开，再调用各自控制器。
- B05 不改变正式感知权限，也不读取测试夹具的隐藏坐标。
- `KnownMapBounds.extra_top_clearance_cells` 由启用的能力显式填写。普通 Walk 使用 `0`；当前 JumpUp 使用 `1`。因此 B04 不会为了未启用的跳跃复制或查询额外顶部空间。

## 实现位置

- `mc2p/motion_nav/jump_up.py`：Profile、几何查询、动作边和观测驱动的阶段执行器；
- `mc2p/motion_nav/action_route.py`：有类型的 Walk／JumpUp 动作段；
- `mc2p/motion_nav/action_route_executor.py`：动作段顺序与控制器交接；
- `mc2p/motion_nav/known_map_planner.py`：多层节点、可选 JumpUp 边和显式顶部净空；
- `mc2p/motion_nav/planner_worker.py`：把 JumpUp Profile 随快照请求交给后台进程；
- `mc2p/motion_nav/route_admission.py`：把候选边转换成动作路线，并把动作类型写入路线身份。

## 已知边界

V1 不支持向下动作、跨空隙、连续跳跃、跑跳、非完整支撑面或动态障碍。它也不承诺连续空间最短路径。后续能力沿同一动作段接口增加，不能修改 `JumpUp` 的既有证据来冒充新的能力。
