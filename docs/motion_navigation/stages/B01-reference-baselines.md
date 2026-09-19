# B01：三版参照与统一比较口径

日期：2026-09-18
状态：已完成，按用户要求在此暂停
下一阶段：B02 尚未开始

## 本阶段解决的问题

重构需要保留旧成果，但旧成果来自三个用途不同的版本，记录格式也分散。B01 固定它们的身份和证据入口，让后续工作可以比较具体能力，而不用把某个旧版本整体当成新底座。

## 已交付

1. [版本注册表](../../../config/motion-navigation/reference-versions-v1.json)固定三版的用途、GitHub 提交、公开摘录目录、完整实机源码树指纹和已有封存运行。
2. [场景注册表](../../../config/motion-navigation/reference-scenes-v1.json)固定 S00 空地、S02 转角走廊、S04 悬浮墙和 S05 坑边的地图身份、起终点与用途。
3. [公开快照校验表](../../../config/motion-navigation/reference-SHA256SUMS.txt)固定 165 个公开文件。
4. [参照校验](../../../mc2p/motion_nav/reference_baselines.py)逐文件检查快照，拒绝缺失、修改和未登记文件。
5. [证据归一化](../../../mc2p/motion_nav/reference_evidence.py)只读现有封存记录，导出 `inputs.jsonl`、`body.jsonl`、`timings.jsonl` 和 `run.json`；摘要同时保留原始场景编号，避免把旧成绩错贴到共同场景。
6. [命令入口](../../../scripts/motion_navigation_references.py)提供 `verify` 和 `normalize` 两个操作。

统一计时只在同一时钟内计算。控制进程的请求到接收耗时和客户端采样耗时分别保存，不生成跨 JVM／Python 时钟的伪延迟。

## 明确没有做的事

- 没有修改三个参照版本的导航行为。
- 没有把任何一版切换为重构底座或项目默认版。
- 没有启动 Minecraft，也没有为了补齐四场乘三版的矩阵重跑旧实验。
- 没有开始 B02 的数据、几何或动力学重构。

## 进入 B02 前的状态

B01 的身份和证据入口已经固定。后续若开始 B02，应直接使用这里的场景编号和证据格式。改变参照身份、场景布局或计时口径时，必须新增决策记录，不能静默覆盖。
