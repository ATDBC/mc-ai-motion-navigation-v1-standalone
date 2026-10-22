# D126 验收：运动夹具使用已知地图

日期：2026-10-21。状态：**PASS／SIGNED**。

依据：[决定](../decisions/0126-restore-known-map-input-for-motion-validation.md)和[计划](../stages/d126-known-map-motion-validation-plan.md)。正式平台为 Windows。

## 1. 纠偏门槛

| 项目 | 要求 | 结果 |
|---|---|---|
| 地图 owner | 预检与正式 driver 使用 Runtime 同一 owner | PASS |
| fixture 来源 | 服务器建图和 known map 来自同一规范 rows | PASS；按服务器顺序归并，support 覆盖 clear |
| 四方向依赖 | 每方向 58／58 known、0 mismatch | PASS |
| 缺失格 | 少一格仍为 `NEEDS_WORLD` | PASS |
| 错误材质 | 仍为 `FACT_MISMATCH` | PASS |
| 地图变化 | 旧 proof 按现有语义失效 | PASS；TEST_ORACLE 后绑定版本，正式观察改变方块后返回 `CHANGED` |
| 可见性 | 不再是运动试验前置 | PASS；请求为空 |
| 生产改动 | 0 | PASS |
| 历史证据 | D122—D125 原字节保留 | PASS；树摘要已记录 |

## 2. 证据边界

`TEST_ORACLE` 只代表当前运动验收已经知道 fixture 地图。它不能证明生产机器人拥有隐藏世界感知，也不能进入普通启动路径。未声明区域仍然未知。

本阶段不运行 Minecraft／Fabric。通过后只授权进入 D123-2；D123-3 仍须等待 transport 生命周期关闭。

## 3. 实际结果

`scripts/d126_known_map_motion.py` 生成的紧凑证据为 PASS。四方向的 required／known／unknown／mismatch 都是 58／58／0／0，并且预检 frame 的 owner 与 Runtime adapter 的 snapshot port 相同。漏掉联合必要格时，该格仍为 unknown；替换成 stone 时产生一个 mismatch。对已知 AIR 绑定依赖版本后，通过正式 observation 把该格改成 stone，结果为 `DependencyStatus.CHANGED`，且 changed positions 只含该格。

测试结果：

- D126 定向检查：8／8；
- D122—D124 与 D126 相邻检查：65／65；
- standalone 导出模块：37／37；
- 最终字节 standalone export／verify：3213 个文件；
- D125 视觉模块未重跑。该模块需要工作树外的 `surface_cache.dll`，且已退出当前主线；现有历史证据及树摘要保留。

没有修改 `mc2p`、Session、transport 或 Java，也没有运行 Minecraft／Fabric。证据位于 `evidence/motion_navigation/d126-known-map-motion-v1/run-1/`。下一步是 D123-2。
