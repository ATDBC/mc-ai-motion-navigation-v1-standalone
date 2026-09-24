# B05 验收结果：最小跨高度动作

日期：2026-09-19
结论：通过

## 实际结果

| 项目 | 结果 |
|---|---|
| 校准 | 单动作轨迹通过；离地序列 4，落地序列 12，最高上升 `1.252203` 格 |
| 组合路线 | 后台生成并执行 Walk—JumpUp—Walk；终点三维误差 `0.141625` 格，终点水平速度 `0.062430 格/秒` |
| 有效动作 | 100/100 到达正确层高；四个正方向各 25 次 |
| 入口扰动 | 覆盖静止偏移、横向偏移和最高实测 `0.095109 格/秒` 的移动入口 |
| 落地 | 最大水平误差 `0.168552` 格；错误完成、碰撞和掉落均为 0 |
| 取消 | PREPARE、REQUEST_TAKEOFF、AIRBORNE、VERIFY_LANDING 均到达 `CANCELLED`；REQUEST_TAKEOFF 先实际发送起跳输入，下一观测速度约 `1.07 格/秒`，落地并减速至约 `0.093 格/秒` 后才结束 |
| 拒绝 | 超速入口、低顶、未支持材质、未知几何、未观察到离地分别返回 `UNSUPPORTED`、`BLOCKED`、`UNSUPPORTED`、`NEEDS_INFORMATION`、`FAILED` |
| 控制耗时 | 1542 个样本；P95 `0.3452 ms`，P99 `0.3623 ms`，最大 `0.4412 ms` |
| 组件回归 | `tests/motion_nav` 共 129 项通过 |
| 实机回归 | B03 固定路线与 B04 已知图探针均通过 |

正式证据：

- 校准：`artifacts/fabric-deployment/20260919T002850521136Z-f44ee64f`；
- 百次、取消和拒绝：`artifacts/fabric-deployment/20260919T051031830636Z-f8da78b3`；
- Walk—JumpUp—Walk：`artifacts/fabric-deployment/20260919T013756389007Z-9959d166`；
- B03 回归：`artifacts/fabric-deployment/20260919T011433784299Z-67619d31`；
- B04 回归：`artifacts/fabric-deployment/20260919T012100565979Z-719a291e`。

## 判定口径

| 项目 | 通过条件 |
|---|---|
| 能力范围 | 只声明经过实测冻结的低速、一格上升、普通草方块 JumpUp |
| 合法输入 | 正式机器人只使用结构化观察、当前身体和世界知识；测试真值不回流 |
| 动作启动 | 入口身体、起跳支撑、完整净空、目标支撑和落地区域全部满足 |
| 动作推进 | 输入回执不能替代离地；控制 tick 不能替代落地观测 |
| 完成 | 正确层高接地、位于目标区域、支撑成立，并进入下一 Walk 的入口状态 |
| 拒绝 | 低顶、未知关键空间、未知落地、速度越界、错误姿态和未支持材质均不启动 |
| 取消 | 起跳前按地面规则结束；离地后实际落地并停稳才结束 |
| 路线 | 后台产生并执行 Walk—JumpUp—Walk；同 XZ 不同高度不误判到达 |
| 安全 | 可避免碰撞、掉落、错误层完成和把未知当空气均为 0 |
| 控制耗时 | P95 ≤8 ms、P99 ≤15 ms、最大值 <30 ms |
| 回归 | 全部运动导航测试通过；B03、B04 代表性 Fabric 场景不退化 |

## 验收覆盖

### T1：契约和纯函数

- `JumpUpProfile` 拒绝缺少单位、非有限数值、无版本身份和不完整轨迹；
- 多层快照只复制声明范围，变化期间返回 `STALE`；
- 相同 XZ 的上下层是不同节点；
- JumpUp 只连接水平相邻、向上一格的节点；
- 轨迹中间碰撞会否决动作，即使起点和终点都可站；
- 空中阶段不错误要求脚下连续支撑；
- 未知、阻塞和未支持产生不同结果；
- 路线身份包含动作类型，进度变化不改变身份。

### T2：参数化和对照

- 带同高 Walk 和一格 JumpUp 的随机有限图与独立 Dijkstra 比较；
- 至少 100 组闭环覆盖起点偏移、入口速度、四个正方向和落地偏差；
- 对轨迹相邻采样做连续扫掠暴力参考；
- JumpUp 能力关闭时，B04 图和路径结果保持一致。

### T3／T4：状态回放与闭环

- 输入已确认但没有离地；
- 离地观测迟到但仍在允许窗口；
- 空中收到取消、新目标或输入失联；
- 世界会话变化后旧动作立即失败并输出中立输入；活动中的 JumpUp 不能被第二次启动覆盖；
- REQUEST_TAKEOFF 已发出起跳输入、下一观测仍可能接地或已离地时收到取消；
- 接地但落在原层、错误层或目标区外；
- 落地成功但速度尚未进入下一 Walk 的入口范围；
- JumpUp 前相关格变化与无关远处变化。

### T5：Fabric 实机

1. 校准运行：固定版本、普通草方块、自动跳关闭、非飞行状态，保存逐 tick 原始轨迹和回执。
2. 单动作运行：声明范围内 100 次有效 JumpUp，覆盖校准后冻结的有限偏移和速度扰动。
3. 组合运行：至少一条 Walk—JumpUp—Walk 路线重复执行，验证后台规划、路线接纳和动作交接。
4. 拒绝运行：低顶、未知落地、入口速度越界、输入拒绝和未实际离地。
5. 取消运行：PREPARE、REQUEST_TAKEOFF、AIRBORNE、VERIFY_LANDING 四个阶段分别注入取消。
6. 回归运行：B03 的空地／转角代表场景和 B04 的空地／高墙／悬浮墙／坑场景。

## 必须保存的结果

- 完整源码身份和依赖版本；
- 世界种子、夹具坐标、起点和目标层；
- 校准输入序列和 Profile 数值；
- 每 tick 的位置、速度、接地、碰撞、姿态、输入请求、实际应用回执和动作阶段；
- 规划请求、候选节点／动作段、依赖、路线身份和接纳结果；
- 每次取消注入时刻和最终状态；
- 控制、几何、后台搜索和总耗时；
- 所有失败，不删除或改写。

## 实际命令

组件测试沿用项目入口：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
```

```powershell
# 单动作校准
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python scripts/probe_fabric_deployment_observation.py --b05-jump-calibration-probe `
  --seed 21001 --server-port 25640 --ipc-port 8183 --timeout-seconds 300

# Walk—JumpUp—Walk
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python scripts/probe_fabric_deployment_observation.py --b05-jump-route-probe `
  --seed 21001 --server-port 25649 --ipc-port 8192 --timeout-seconds 300

# 100 次、四阶段取消和拒绝分支
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python scripts/probe_fabric_deployment_observation.py --b05-jump-acceptance-probe `
  --seed 21001 --server-port 25650 --ipc-port 8193 --timeout-seconds 600
```

## 当前证据边界

本次结果只证明当前 Profile 声明的普通草方块、站立、低速、相邻一格上升。未支持半砖、楼梯、冰面、动态障碍、跨坑、下降、冲刺和连续跳跃。百次试验使用一个固定物理版本和本地 Fabric 服务端；后续版本或材质变化必须重新校准，不能沿用本结果推断。
