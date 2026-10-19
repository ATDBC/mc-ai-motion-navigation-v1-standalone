# D120 带动量入口校准计划

日期：2026-10-19。状态：**COMPLETED；S0—S4 校准与最终发布门禁均通过。** 对应 [D120](../decisions/0120-resume-momentum-entry-calibration.md) 和[验收表](../acceptance/d120-momentum-entry-calibration.md)。

## 1. 本次交付

回答一个问题：第一跳落地后，对入口加入预登记误差，第二跳和潜行后备是否都能安全执行。先交付有限样本覆盖与旧 raw 门禁，通过后才运行朝南三场、四方向 36 场。不交付生产跑酷接入。

## 2. 文件范围

| 文件 | 修改内容 |
|---|---|
| `scripts/d120_momentum_entry_calibration.py` | 冻结有限样本、状态扰动、后缀推演、旧 raw 只读门禁 |
| `scripts/d120_momentum_calibration.py` | D120 专用清单、Fabric 启动和独立结果封存 |
| `tests/motion_nav/test_d120_momentum_entry_calibration.py` | RED、负例、证据身份、采样覆盖与包装器门禁 |
| `scripts/probe_fabric_deployment_observation.py` | 必要时只增加 D120 清单接线，复用既有 Runtime 循环 |
| `config/motion-navigation/standalone-export-v1.json` | 登记新文件；不改历史指纹 |
| D120 三份文档、根入口与打包入口 | 记录范围、真实结果、停止点 |

旧 D118 输入、fixture、冻结哈希和原始证据不改。`mc2p/` 与 Java 不改。

## 3. S0：先固定要检验的状态和 RED

从 `artifacts/fabric-deployment/20261018T145935753542Z-be4892d2` 读取真实物理事件。每个 trial 独立使用自己的 fixture、origin 与 feet_y，不把 raw 直接塞进 y=64 的探索世界。

测试先失败，再实现：

1. 找到第一跳 landed 的 post-state，检查 L 的身份和着地；改用 pre-state 必须拒绝。
2. 样本缺失、重复、错 hash、未采维度扩大为区间必须拒绝或保持 unverified。
3. 扰动必须实际传进计算器。只改报告、不改推演状态的变异必须失败。
4. L+2 的固定 hold 不得报告成 L+1 或真实传输晚到。
5. fallback 中途离开平台再落回仍然失败；不能只检查最终着地。
6. 删除落点支撑或把必需格改 UNKNOWN，不能仍报告安全。
7. neutral/back 的旧 nominal 对照必须失败；四方向旋转后成功判定一致。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_d120_momentum_entry_calibration -v
```

每项记录失败来自缺失能力或被植入的错误，不接受只因导入失败而声称全部 RED 已建立。

## 4. S1：冻结有限联合覆盖并实现离线推演

名义入口由计算器执行冻结第一跳后得到，不能从 Fabric 成功结果反推。清单固定以下联合格点：

- 本地横向位置：`-0.002／0／+0.002` 格；
- 本地纵向位置：`-0.002／0／+0.002` 格；
- 水平横向速度：`-0.0002／0／+0.0002` 格/tick；
- 水平纵向速度：`-0.0002／0／+0.0002` 格/tick；
- yaw：`-0.1／0／+0.1` 度。

五个轴取完整笛卡尔积，共 243 个入口。顺序依次为横向位置、纵向位置、横向速度、纵向速度、yaw，每轴按负、零、正枚举。y、竖直速度、着地、姿态、身体尺寸和疾跑／潜行状态绑定名义 landed post-state，不制造与支撑事实矛盾的状态。位置和速度半径分别是既有校准预算的两倍。

每个入口分别运行三个后缀：L+1 jump、固定 RUN 后 L+2 jump、L+1 sneak。每个跳跃后缀最多推进 42 tick，潜行最多推进 30 tick；单方向总物理步不得超过 30,000。四方向一致性验证不得超过 120,000 步。清单同时冻结轴值、顺序、后缀、上限和输入 hash，测试逐项锁定。

跳跃成功必须落到冻结的第二目标支撑面。潜行从开始到结束的每一帧都要保持在冻结的中间平台支撑上，并在 30 tick 内把水平速度降到 `<0.003` 格/tick。243 个格点的三个后缀最多使用 27,702 步；名义前缀、真实 raw 精确重放、方向对照和变异检查分别计数，正式输出不得只报一个无法拆分的总数。

逐个入口验证 L+1 jump、L+2 固定 hold 后 jump、L+1 sneak。第二跳要真实起跳、落在对岸目标支撑、零伤害并能安全收尾；潜行整段留在中间平台并停住。分别保存失败维度、时序、首个失败 tick 和支撑。计算规模有界，墙钟不参与采样和可行性判断。

输出的 `sample_coverage` 可以通过；`sound_envelope` 继续 unverified。不要插值证明格点内部，不输出未经计算的矩形保证。

## 5. S2：旧 raw 只读门禁

```powershell
.\.venv\python.exe scripts\d120_momentum_entry_calibration.py `
  --run-dir artifacts\fabric-deployment\20261018T145935753542Z-be4892d2 `
  --output .tmp\d120-entry-s3-prerequisite-v1
```

验证所有相关朝南 trial 的真实落地态是否位于登记格点的外接边界，并验证 receipt、physics、F-TICK 与预期输入逐项对应。`within_registered_bounds` 只表示边界命中；每个真实 post-state 还要直接运行三个后缀，单独输出 `raw_suffix_replay`。原文件先后哈希一致。不能通过选择另一帧落地或平移 origin 使证据命中。

受影响和相邻检查：

```powershell
.\.venv\python.exe -m unittest `
  tests.motion_nav.test_d120_momentum_entry_calibration `
  tests.motion_nav.test_d118_momentum_calibration `
  tests.motion_nav.test_b09r_physics_step `
  tests.motion_nav.test_b09r_physics_rollout -q
```

离线覆盖或旧 raw 门禁未通过，到此停止，不运行 Fabric。

## 6. S3：朝南三族最小 Fabric

包装器先在组件后端覆盖真实启动分支、试次汇总和停止分支。D118 旧清单的两个冻结哈希检查保持不变；D120 使用独立 schema、输入哈希与结果目录。

```powershell
.\.venv\python.exe scripts\d120_momentum_calibration.py `
  --output .tmp\d120-fabric-south-java21-r1-v1 `
  --offline-prerequisite .tmp\d120-entry-s3-prerequisite-v1 `
  --server-port 25597 --ipc-port 8140
```

底层正式命令固定 seed 21001、`--timeout-seconds 600`，保留原传送等待和第 0 帧着地静止硬检查。必须从静止通过正式输入加速，不传送到高速入口。

三族各一次。核对 L、jump/sneak 实际应用 tick、真实落地态、所有命令身份、离散物理与数值误差、安全尾迹和来源释放。包装器必须核对本次使用的离线结果 hash；离线门禁未签署时不得启动 south。任一试次 INVALID 或 FAILED，都不能继续扩大。

## 7. S4：四方向小矩阵与收尾

只有 S2 与 S3 全部通过，才运行：

```powershell
.\.venv\python.exe scripts\d120_momentum_calibration.py `
  --output .tmp\d120-fabric-full-java21-r1-v1 `
  --offline-prerequisite .tmp\d120-entry-s3-prerequisite-v1 `
  --south-prerequisite .tmp\d120-fabric-south-java21-r1-v1 `
  --server-port 25597 --ipc-port 8140 --full-matrix
```

四方向、三族、每族每方向三次，共 36 场。不能调用旧 D118 的 84 场 full matrix 替代。

full 包装器必须同时核对离线结果和 south 结果 hash。D120 使用独立 schema 和冻结 hash，不得把新 hash 加进 D118 的旧 smoke/full 允许集合。

正式签署前核对源码语义 hash、清单 hash、完整原始结果索引和公开导出。只在发生新修改或失败时复跑受影响检查；不因诊断文案变化反复跑无关大矩阵。完整 Windows 门禁按仓库当前阶段规则执行，结果与测试数如实登记。新文件按 LF 写入；既有文件保留原行尾，不夹带换行治理。

## 8. 完成与停止

完成须同时满足：有限样本覆盖完整；旧 raw 门禁通过；south 3/3；full 36/36；物理和身份门槛通过；零伤害、owner 空档、期限错过与无界等待；相邻检查及公开导出通过。不得将这些结果签成连续 sound envelope 或生产新能力。

当前目标最多两轮限定修正。需要改生产链、输入语义、固定输入或放宽全局容差时停止；记录具体受阻维度与证据，不继续扩批。

## 9. 实际完成情况

2026-10-19，四方向有限覆盖 972 个入口、2916 个后缀全部通过，主物理步 47,628。旧 D118 raw 只读门禁通过。south 3/3、full 36/36，身份、物理、真实健康、L+1／L+2 与完整安全尾迹均通过。正式 full 为 `.tmp/d120-fabric-full-java21-r1-v1`，不能与初次 full 的只读复算混为一份运行。

JDK 预检、south 首条 reset 健康观察误拒、full 北向 ±π 误拒的历史失败都保留。两项限定夹具修正没有改固定输入或生产代码。完整轨迹保留本地，公开包只发布摘要、紧凑分析、原始哈希和实现语义哈希。

Windows 源码树正逆各 197 模块／2534 项、独立公开快照正逆各 196 模块／2510 项全绿，失败、错误、超时、跳过和清理失败均为 0；Java 各 3/3，矩阵快照 verify 2634 文件。校准不授权 D118-1—D118-5。连续 sound envelope 仍 unverified。详见更新后的验收文档与 `evidence/motion_navigation/d120-momentum-entry-v1/`。
