# D126 实施计划：纠正运动夹具的地图输入

日期：2026-10-21。状态：**COMPLETED**。

依据：[决定](../decisions/0126-restore-known-map-input-for-motion-validation.md)和[验收](../acceptance/d126-known-map-motion-validation.md)。

## 1. 范围

本阶段只修 D122 Fabric 运动夹具的地图来源和阶段依赖。生产运动、物理、proof、Session、Runtime、Java 和观察协议保持不变。本批不运行 Fabric；组件与导出门禁通过后再进入 D123-2。

## 2. D126-0：冻结错误门槛

- [x] 复现同一个 fixture 已知地形在当前 actor-only 预检中仍有 3 个 `UNKNOWN`。
- [x] 证明临时 adapter 与正式 Runtime adapter 不是同一个 WorldKnowledge owner。
- [x] 冻结四方向当前 proof 的 58 个必要依赖和 fixture rows 摘要。
- [x] 历史 D122-4／D123-1／D125 证据保持原字节。

## 3. D126-1：使用现有 TEST_ORACLE 入口

- [x] 从一次规范化的 `trial_fixture_rows()` 同时生成服务器建图 rows 和已知地图 blocks／air。
- [x] 传送稳定后，把 facts 写入 `runtime.navigation_observation_adapter`。
- [x] 预检和正式 driver 只读取 Runtime 的同一个 owner；删除夹具里的临时 adapter。
- [x] proof precheck 报告来源、fixture 摘要、required／known／unknown／mismatch。
- [x] 运动 observation request 不再负责补齐地图。

RED／mutation：

1. 四方向均为 58／58 known、0 mismatch；
2. 漏掉 joint 格仍返回 `NEEDS_WORLD`；
3. 一个方块材质错误仍返回 `FACT_MISMATCH`；
4. 临时 adapter 装入、Runtime owner 未装入时必须失败；
5. 后续 `occluded` observation 不删除已有地图事实；
6. 非 `TEST_ORACLE`、首次 frame 前装入、block／air 冲突均拒绝；
7. `mc2p/runtime` 和 `mc2p/skills` 不得调用 test oracle。
8. TEST_ORACLE 写入后绑定的依赖，在正式观察改变语义时必须返回 `DependencyStatus.CHANGED`。

## 4. D126-2：修正文档与发布

- [x] D122-4 追加“错误范围的夹具门槛”，不回写历史结果。
- [x] D123-1／D125 标为非阻塞视觉诊断；D123-2 不再依赖它们。
- [x] D123-3 前置改为已知地图门禁＋D123-2。
- [x] D124 的 67→58 结论保持不变。
- [x] D126 脚本、测试和紧凑证据进入 standalone，并验证哈希。

## 5. 通过与停止

通过：同一 Runtime owner 使用同一 fixture rows 得到四方向 58／58，错误地图仍被现有门禁拒绝；生产源码无改动；历史证据不改写。

停止：必须新增生产地图接口、第二份地图、私有字段注入，或地图变化不能撤销旧 proof。

通过后立即进入 D123-2；本阶段不提前运行 D123-3 Fabric。

实际结果：D126 8／8，D122—D124 与 D126 相邻检查 65／65；standalone 导出模块 37／37，最终字节导出与 verify 共 3213 个文件。D125 视觉测试依赖工作树外未复制的 native DLL，本阶段不重建或重跑；历史证据树摘要已冻结。没有运行 Fabric。
