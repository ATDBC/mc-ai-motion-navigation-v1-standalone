# B02 验收记录：世界知识与几何核心

日期：2026-09-18
结论：数据、几何和 profile 4 基础观察通过；后续动作时序仍在整改

2026-09-26 核查确认，原记录中的 Fabric 观察来自已经弃用的 profile 3 稀疏射线。世界会话、三态知识、按需空气、扫掠、支撑和普通地面预测的结论继续有效。profile 4 基础观察已在 `artifacts/fabric-deployment/20260926T103714568442Z-0d4f1f72` 重新通过；B03 也在 `artifacts/fabric-deployment/20260926T104043459251Z-e263f3ec` 通过。B10 暴露的端到端时序问题不计入 B02 通过结论，但必须在恢复后续阶段验收前解决。影响范围见 [D035](../decisions/0035-formal-surface-depth-only.md)。

## 已验证行为

| 检查 | 结果 |
|---|---|
| 世界会话隔离 | 旧会话事实被拒绝；变化墓碑阻止延迟旧事实恢复 |
| 时间单位 | 只允许同一世界会话、同一控制时钟相减；结果统一为秒 |
| 三态知识 | 已知方块、已知空气和未知分别保存；未知不等于空气 |
| 身体直接感知 | 身体碰撞盒占据的空气格和脚下实际接触支撑直接成为合法事实；更深地下格不会泄露 |
| `pre-floating-7ab0b35f` 适配 | 可见方块的完整块、局部碰撞盒、空碰撞和不支持形状得到保留 |
| 空气确认 | 一批 400 格只调用后端一次；只缓存返回的空气正例 |
| 重复空气请求 | 400 格连续请求 1,001 次，后端仍只调用一次 |
| 世界变化 | 对应空气正例失效；未到重试时间的未知不会逐 tick 重复查询 |
| 连续扫掠 | 能发现起终点之间的障碍；已知空气才可排除未知 |
| 支撑 | 返回实际支撑面积和比例；允许报告部分悬空，不伪装成完整支撑 |
| 普通地面预测 | 长度、秒、格／秒、格／秒²和弧度契约通过；松键后保留惯性并衰减 |
| 相机与世界方向 | 多个 yaw 下双向换算闭合；pitch 不改变水平输入语义 |
| 统一运行入口 | Fabric 与 CraftGround 的 Observation V3 在自动测试中产生相同 `NavigationFrame` 语义 |
| Fabric 空气查询 | 两个独立 JVM 会话各请求 128 个空气格和 1 个固体；空气全部返回，固体没有通过空气来源返回 |
| 固定场景 | S00 空地、S02 墙角、S04 悬浮墙、S05 坑通过支撑和连续扫掠检查 |
| 扫掠交叉验证 | 一万组随机完整方块案例与 1,001 点独立细采样参考一致 |

## 本机微基准

同一进程、纯 Python、400 个空气位置：首次批量确认约 1.152 ms；缓存命中中位约 0.499 ms、P95 约 0.513 ms。长度 4 格、涉及 10 个体素的连续扫掠中位约 0.0117 ms、P95 约 0.0119 ms。

这些结果只衡量核心数据结构和纯函数，不包含游戏通信和控制闭环。

## Fabric 实机空气查询

正式运行：`artifacts/fabric-deployment/20260918T145741886164Z-5f828079/result.json`。

两个独立客户端 JVM 都完成同一项查询：一次请求包含 128 个已知空气位置和脚下 1 个固体位置。两次分别确认 128 个空气正例，固体没有获得 `air_query` 来源。客户端整帧采集分别耗时 2.571 ms 和 2.717 ms，均低于 50 ms 游戏 tick。这里记录的是整帧采集时间，不能把它与另一帧直接相减并宣称为空气查询的单独开销。

协议上限 512 格由 Java 与 Python 契约测试覆盖。实机只测了当前默认预算 128 格，因此 B03 若要提高默认预算，必须重新测量。

B03 最终产物 `artifacts/fabric-deployment/20260918T155435826854Z-5adc1011/result.json` 另外验证了身体直接感知。最终帧能同时导出身体所在的空气和脚下支撑；跨格站立时只增加身体实际覆盖和接触到的格子。

## 普通地面校准

校准结果：`artifacts/fabric-deployment/20260918T145741886164Z-5f828079/b02-ground-calibration.json`。接受的参数另存于 `config/motion-navigation/ordinary-ground-v1.json`。

第一组独立 JVM 轨迹提供 55 个普通步行样本，第二组 55 个样本只用于验证；两组都包含 11 个纯前进、8 个纯侧移、8 个斜向和 28 个松键样本，并排除开头同时转头的 1 帧。单步验证位置误差 P95 为 0.00283 格、最大 0.00410 格；速度误差 P95 为 0.0309 格／秒、最大 0.0447 格／秒。第二组轨迹还能形成 50 个连续 6 步窗口，位置误差 P99 和最大值都是 0.0244 格，低于 0.15 格门槛。

这个模型只覆盖普通地面步行。疾跑、潜行、跳跃、碰撞、液体和空中运动必须使用新的能力模型和独立验收。

## 检查命令

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
```

Fabric 实机命令：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python scripts/probe_fabric_deployment_observation.py --b02-air-probe `
  --server-port 25621 --ipc-port 8161 --timeout-seconds 240

D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python scripts/calibrate_b02_ground_motion.py `
  --calibration-trace artifacts/fabric-deployment/20260918T145741886164Z-5f828079/client-0/trace.jsonl `
  --validation-trace artifacts/fabric-deployment/20260918T145741886164Z-5f828079/client-1/trace.jsonl `
  --output artifacts/fabric-deployment/20260918T145741886164Z-5f828079/b02-ground-calibration.json
```

## 证据边界

- CraftGround 只通过契约、沙箱配方、传输和共同适配器自动测试，没有 B02 实机结论；D005 已把它移出本阶段门槛。
- 四个固定场景验收的是 B02 几何结论，不是自动行走闭环。
- 当前观察没有维度标识；跨维度前必须新开 episode，B02 没有验收同一任务内的连续跨维度。
- 固定路线跟踪、墙边／坑边缓冲、停止、取消和输入失联不属于本记录，后续结果见 B03 验收。
