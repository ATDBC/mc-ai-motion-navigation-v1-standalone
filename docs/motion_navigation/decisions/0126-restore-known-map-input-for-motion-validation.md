# D126：运动验收恢复使用已知地图输入

日期：2026-10-21。状态：**COMPLETED**。

依据：[计划](../stages/d126-known-map-motion-validation-plan.md)、[验收](../acceptance/d126-known-map-motion-validation.md)和 D121—D125 的历史记录。

## 要纠正的问题

当前阶段验证已知地形中的运动能力，不验证机器人怎样发现地图。

D122-4 的 Fabric 夹具却要求当前相机逐格确认 proof 依赖。被遮挡的格子因此保持 `UNKNOWN`，运动试验在第一场开始前停止。D123-1 和 D125 随后继续调查视觉取证，偏离了当前阶段范围。

另有一个直接实现问题：D122 预检反复创建临时 `NavigationObservationAdapter`，正式 `RuntimeNavigationDriver` 使用的却是 Runtime 自己持有的 adapter。预检和执行不是同一个 `WorldKnowledge` owner。

## 决定

运动夹具通过已有的 `seed_test_oracle_memory(TEST_ORACLE, blocks, air)` 把声明过的 fixture 地图装入 Runtime 自己的 `WorldKnowledge`。同一份 fixture rows 同时用于服务器建图和测试地图输入。

这是一项测试权限，不是生产感知能力：

- 只能由明确的 `TEST_ORACLE` capability 调用；
- 只覆盖当前 fixture 声明的有限区域；
- 不进入 `mc2p/runtime`、`mc2p/skills` 的正常启动路径；
- 不创建第二份地图；
- 世界会话、逐格语义版本、依赖变化和旧 proof 失效规则保持不变；
- 未提供的位置继续为 `UNKNOWN`，但已知位置不要求出现在相机画面里。

D122-4 的原始 `0／3`、D123-1 和 D125 的停止证据全部保留。现行解释改为：它们证明纯视觉冷启动无法满足该 fixture，不代表已知地图运动能力失败，也不再阻塞主线。

## 继续顺序

```text
D126：已知地图装入同一 Runtime owner
  ↓
D123-2：传输生命周期
  ↓
D123-3：Fabric smoke／矩阵
  ↓
连续两跳产品阶段
```

D123-3 的地图前置改为：Runtime owner 中当前 fixture 的 58 个必要依赖全部 known、0 mismatch。它不再依赖 D123-1 或 D125 视觉诊断通过。

## 不做什么

本阶段不实现主动感知、视点规划、新地图接口、第二份 WorldModel、Session 状态、Java sensing 或 proof 放宽；也不删除历史失败。

## 实施结果

四方向均由服务器同源 rows 生成测试地图，并在 Runtime owner 中达到 58／58 known、0 mismatch。漏 joint 格和错材质仍分别被现有门禁识别；临时 owner、非法权限、首次 frame 前写入和 block／air 冲突均被检查覆盖。TEST_ORACLE 写入后绑定依赖版本，再注入真实方块变化时返回 `DependencyStatus.CHANGED`。生产 `mc2p` 没有修改，Minecraft／Fabric 没有运行。D126 关闭后进入 D123-2。
