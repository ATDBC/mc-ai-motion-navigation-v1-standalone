# D125 验收：三个必要事实的提交前观察

日期：2026-10-21。状态：**PARTIAL／STOPPED**。D125-0 已完成并命中停止条件；D125-1／D125-2 未开始。

依据：[决定](../decisions/0125-test-bounded-observation-before-adding-acquisition.md)和[计划](../stages/d125-three-required-fact-observation-plan.md)。正式平台为 Windows；本阶段先只运行 D125-0，不运行 Minecraft／Fabric。

## 1. 前置事实

- D124 已签署。每方向 proof 联合依赖为 58 格。
- 原 12 个下层未知格中，9 格已证明可由完整方块屏障排除。
- 另外 3 格仍参与 parent、successor 或 fallback；未知时正式结果必须为 `NEEDS_WORLD`。
- D123 固定眼睛只转头无法看见原 12 格。本阶段允许检查安全站位变化，但不新增 sensing。

## 2. D125-0 门槛

| 项目 | 要求 | 结果 |
|---|---|---|
| 必要格身份 | 四方向各 3 格，角色来自正式 proof | 通过；每方向 parent-only／joint／successor-only 各 1 格 |
| 起点侧站位 | 有安全支撑、净空和明确采样范围 | 通过；有限扫描和连续区间论证分开记录 |
| 提交前可见性 | 每个 parent 必要格至少有一个安全见证 | **未通过**；joint 格四方向均无安全提交前视点 |
| 平台后对照 | 只作几何归因，不得倒灌事前事实 | 通过；中间平台同帧看见三格 |
| 正式门禁 | 只缺阻塞格时仍为 `NEEDS_WORLD` | 通过；parent、successor 同为 `needs_world` |
| 隐藏事实 | fixture 真值写入 WorldModel 为 0 | 通过 |
| 生产修改 | D125-0 为 0 | 通过 |
| 运行范围 | Minecraft、Fabric、movement trial 均为 0 | 通过 |

## 3. 必须检出的错误

- 删除一个真实 parent 依赖；
- 把 `UNKNOWN` 当成 `AIR`；
- 把第一跳后的观察当成第一跳提交前事实；
- 用 `LandingEdgeProbe` 越过 gap 后报告起点侧 acquisition 成功；
- 用有限单点样本冒充整个安全站位域的结论。

## 4. 停止后的解释

若 D125-0 证明某个 parent 必要格只能在第一跳后看见，D125 记为 **PARTIAL／STOPPED**。这表示当前固定地形和 actor-only 权限形成观察前置循环。它不授权放宽 proof，也不说明运动计算器、transport 或一般观察系统失败。

可另行评估的方向只有：重新设计第一跳 fallback／proof、选择带安全侧向观察位的路线、使用此前真实观察过的地图事实，或新增 sensing。任何方向都必须另立决定。

## 5. 实际结果

D125-0 的最终 typed 结论为 `PRECOMMIT_FACT_NOT_OBSERVABLE_FROM_SAFE_REACHABLE_VIEWPOINT`。

- 四方向各扫描 4,550 个近边姿态，joint 格可见次数为 0。
- 连续几何证书分别约束起点平台、三格宽中间平台、眼高和目标单元。五项区间不等式均通过。
- 起点 staging stance 同帧看见另外两格；中间平台 stance 同帧看见三格。
- 移除中间平台后，起点 staging stance 能看见原 joint 格，mutation 通过。
- 只缺 joint 格时，正式 replay 的 parent、successor 和 offline gate 分别为 `needs_world`、`needs_world`、`NEEDS_WORLD`。
- `LandingEdgeProbe` 为该格计算的 vantage 落在中间平台，不能提供事前授权。

定向与相邻检查为 36／36 通过，standalone 导出模块 36／36。没有运行完整 Windows 分片、完整 standalone 正逆分片、Java 或 Fabric；本阶段没有生产代码变化，因此不据此签署产品能力。

正式命令：

```powershell
D:\Miniforge3\Scripts\conda.exe run `
  --prefix D:\My_project\mc_ai\.venv `
  --no-capture-output python -m unittest `
  tests.motion_nav.test_d123_actor_calibration `
  tests.motion_nav.test_d124_proof_dependency_projection `
  tests.motion_nav.test_d125_three_fact_observation -v

D:\Miniforge3\Scripts\conda.exe run `
  --prefix D:\My_project\mc_ai\.venv `
  --no-capture-output python `
  scripts\d125_three_fact_observation.py `
  --output evidence\motion_navigation\d125-three-required-fact-observation-v1\d125-0\run-2

D:\Miniforge3\Scripts\conda.exe run `
  --prefix D:\My_project\mc_ai\.venv `
  --no-capture-output python -m unittest `
  tests.motion_nav.test_standalone_export -v
```

第二条命令以非零状态返回，因为 `PARTIAL／STOPPED` 是预期的阶段结果，不是探针崩溃。最终证据为 `evidence/motion_navigation/d125-three-required-fact-observation-v1/d125-0/run-2/stop.json`；`run-1` 是增加连续区间证书前的同结论初次运行，原样保留。

## D126 纠偏记录

D125 现在登记为非阻塞视觉诊断。它不再决定已知地图运动能否开始，也不授权在当前阶段增加 sensing。原始 `PARTIAL／STOPPED`、脚本输出和哈希全部保留。
