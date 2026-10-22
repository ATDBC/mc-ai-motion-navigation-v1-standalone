# D125 实施计划：验证三个必要事实的提交前观察

日期：2026-10-21。状态：**PARTIAL／STOPPED**。D125-0 已完成；D125-1／D125-2 未开始。

依据：[决定](../decisions/0125-test-bounded-observation-before-adding-acquisition.md)、[D124 验收](../acceptance/d124-proof-dependency-projection.md)和[验收页](../acceptance/d125-three-required-fact-observation.md)。

## 1. 目标和边界

目标是回答一个问题：D124 每方向保留的 3 个必要格，能否在第一跳承诺前由真实 actor 从安全站位观察到。

先做 D125-0。只有它通过，才允许进入 D125-1／D125-2。

本阶段不增加通用观察规划，不修改 Session、transport、Java、planner、动作控制或证明规则，不运行 Minecraft／Fabric。现有 D123、D124 和 D122 历史证据不得改写。

## 2. D125-0：冻结几何可行性和前置循环

### 要做的事

- [x] 从 D124 的正式证明生成四方向 3 个必要格，逐格记录 parent／successor 角色，不能硬编码南向坐标作为结论。
- [x] 使用冻结 fixture 和现有 native 可见性核心，扫描起点平台上仍有安全支撑的站位和有界相机朝向。
- [x] 保存能同时观察多个格子的见证站位，也保存始终不可见的格子。
- [x] 在中间平台做对照，区分“永远不可见”和“只能在第一跳后看见”。
- [x] 对只缺阻塞格的正式 proof gate 重算，确认结果仍是 `NEEDS_WORLD`，没有 `UNKNOWN→AIR`。
- [x] 检查现有 `LandingEdgeProbe` 的站位语义，证明它不能从起点侧取得该格，也不能越过 gap 后倒灌事前授权。
- [x] 输出 append-only `stop.json`，记录源码、fixture、manifest 和证据哈希。

### RED 和 mutation

新检查至少覆盖：

1. 四方向的安全起点站位可见集合；
2. 所有起点侧合法站位对阻塞格的可见数；
3. 中间平台可见对照；
4. 只知道另外两格时，parent proof 仍缺阻塞格；
5. 删除 parent 必要依赖、把未知当空气或把平台后观察当事前事实时，检查必须失败；
6. `LandingEdgeProbe` 不能把跨隙后的站位当成起点侧观察成功。

### 通过条件

每个方向的 3 个必要格都能在第一跳承诺前，从预登记的安全站位真实看见。站位有完整支撑和净空；结论不依赖 fixture AIR 真值或隐藏信息。

### 停止条件

任一 parent 必要格只能在第一跳后看见，或必须新增通用视点搜索／sensing／协议能力。命中后立即保存结论，D125-1／D125-2 不开始。

## 3. D125-1：窄观察动作（仅在 D125-0 通过后）

- 复用 `InformationAcquisitionState` 的多格事实和总等待窗口。
- 复用 `ExecutionSupervisor` 的 owner、`request_stop` 和安全释放。
- 使用动作几何直接给出的固定站位，不做通用搜索。
- 身份绑定 task、goal revision、route id/revision、action index、world session、geometry revision 和精确 fact tuple。
- 目标变化、取消、路线替换、超时、外力和支撑变化统一走停止请求。
- 事实齐全后从真实状态重新求解；旧 proof 和旧 acquisition 不授权新路线。

门槛：单一 movement winner、owner 无空档、部分事实不重置 40 movement ticks／2 秒总期限、所有失败有界。

## 4. D125-2：四方向 calibration-only（仅在 D125-1 通过后）

顺序为：建立 fixture → 到安全站位 → 连续稳定 → 正式 observation request → actor observation 写入 WorldModel → 回到动作起点 → 重新核对身体和 58 格门禁。

通过条件：四方向 58／58 known、0 mismatch，事实可追溯到正式 actor observation；运动 trial 为 0；D120 972 个入口／2916 个后缀和 D122 组件矩阵不退步。

## 5. 后续顺序

```text
D124 已签署
  ├─ D125：必要事实观察
  └─ D123-2：transport 生命周期
           └─ 两项都通过 → D123-3 Fabric → 连续两跳产品阶段
```

D125 不夹带 D123-2。D125 停止时，D123-2 仍可在组件层继续，但不得启动 D123-3 产品 smoke。

## 6. 实施结果

D125-0 命中停止条件。四方向各自的 3 格都由当前 D124 proof 动态分类：1 格只属于 parent，1 格同时属于 parent 和 successor，1 格只属于 successor。

每方向在起点侧扫描 4,550 个近边姿态，联合必要格的可见次数均为 0；固定几何区间论证覆盖连续安全站位域。移除中间平台后，同一 actor core 能看见该格，说明探针没有固定返回遮挡。中间平台站位能同帧看见三格，但这已晚于第一跳承诺。

只把联合必要格设为 `UNKNOWN` 时，parent 和 successor 都返回 `needs_world`，正式 gate 为 `NEEDS_WORLD`。现有 `LandingEdgeProbe` 为该格计算的 corner vantage 位于中间平台，不能作为第一跳提交前取证。

因此 D125-1／D125-2 没有启动。要继续，必须另立决定，选择重新证明第一跳安全合同、提供侧向安全观察路线、使用此前真实观察过的地图事实，或新增 sensing。

## D126 纠偏

上面的“要继续”只适用于纯视觉取证研究。当前运动主线使用测试已知地图，因此 D125 不再是 D123-2 或 D123-3 的前置，也不继续实现主动感知。原停止结果保留。
