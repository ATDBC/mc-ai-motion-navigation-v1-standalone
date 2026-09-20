# B08 验收：地面速度、姿态与低通道

日期：2026-09-19
状态：通过

## 验收范围

本验收只判断 Walk、Sprint、Crouch 和“已经合法进入后的 Crawl”。Crawl 的入口能力不在 B08 结论中。

## 自动检查

必须覆盖正式模式观测、有限切换、低净空、不同模式制动、取消、输入失联、相机转动下的世界方向保持，以及 Sprint 资源反例。

## Fabric 场景

| 场景 | 前置条件 | 必须证明 |
|---|---|---|
| B08-S01 | 开阔普通地面、资源充足 | Walk→Sprint→Walk，实际状态均由观测确认 |
| B08-S02 | 开阔普通地面 | Walk→Crouch→Walk，潜行与姿态均由观测确认 |
| B08-S03 | 已合法进入一格高通道 | Crawl 保持并移动，不碰撞、不尝试站起 |
| B08-S04 | Crawl 接近足够净空出口 | 净空成立后退出 Crawl，并继续普通移动 |
| B08-S05 | Sprint 途中资源条件失效 | 不继续依赖 Sprint，给出受控降级或明确失败 |
| B08-S06 | 各模式移动中取消 | 在对应制动范围内安全停止 |
| B08-S07 | 各模式移动中断开输入确认 | 进入 `INPUT_LOST`，不继续生成新动作 |
| B08-S08 | 相机转向而路线不变 | 世界方向误差仍满足固定路线门槛 |

## 指标门槛

- 所有声明支持的场景成功率为 100%。Sprint、Crouch、Crawl 各至少运行 10 条独立实体路线；纯契约失败分支使用参数化反例，不为相同输入机械重复计数。
- 无碰撞、无掉落、无越过未知净空、无绕过动作仲裁。
- 模式请求到实际状态确认不超过配置上限；超时试验必须稳定终止。
- 终点误差和终点速度不宽于 B03 对应固定路线门槛。
- 控制决策 P99 不超过 50 ms；单帧最大值不超过 100 ms。
- Crawl 入口只记录为测试前置条件，不能计入能力成功数。

## 结果

### 自动检查

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_observation_v2_contract tests.test_client_observation_payload -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/build_fabric_deployment_probe.py
```

结果分别为 187 项通过、19 项通过和 `FABRIC_DEPLOYMENT_BUILD_OK`。

### Fabric 实测

命令：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/probe_fabric_deployment_observation.py --b08-ground-modes-probe --timeout-seconds 600
```

通过产物：`artifacts/fabric-deployment/20260919T150933756779Z-6462e1ed`。

| 模式 | 路线次数 | 最坏终点误差 | 最坏终点速度 | 最多控制帧 |
|---|---:|---:|---:|---:|
| Sprint | 10 | 0.1816 格 | 0.0791 格／秒 | 14 |
| Crouch | 10 | 0.0182 格 | 0.0629 格／秒 | 29 |
| Crawl | 10 | 0.0182 格 | 0.0629 格／秒 | 29 |

Sprint、Crouch 和 Crawl 的首次路线都在中途转动相机 90°，仍沿原世界路线完成。三种模式各完成一次真实加速后的取消：停止位移分别为 0.1492、0.0297、0.0297 格，终态速度均不超过 0.053 格／秒。三种模式的输入失联检查都返回 `INPUT_LOST` 和中性输入。

Crawl 夹具只建立一格高通道和预置位置。游戏正式观测报告 `pose=swimming`、`isSwimming=false`、未浸没。移除顶板后 2 帧恢复站立，随后 Walk 在 11 帧内完成一格路线，终点误差 0.1429 格。这个夹具不计作机器人具备 Crawl 入口能力。

控制决策 P95 为 5.573 ms，P99 为 5.877 ms，最大值为 6.528 ms。所有 Fabric 检查通过，无碰撞、掉落或绕过动作仲裁。

### 资源和失败边界

- 正式观测提供饥饿值、实际冲刺、实际潜行和姿态；请求输入不会被当作状态确认。
- Sprint 资源在执行前或执行中不足时，控制器返回 `ground_mode_resource_unavailable`。
- 搜索反例证明：消耗资源的 Sprint 快前缀无法继续时，较慢的 Walk 前缀仍会保留并完成目标。
- 模式确认超时、非法 Crawl 入口、低姿态退出净空不足、取消和输入失联分别有独立结果。

### 边界

B08 没有实现从站立主动进入 Crawl，也没有实现水中入口、开合活板门、跨坑、下降、攀爬或游泳。这些能力不能从本次通过结论中推导。
