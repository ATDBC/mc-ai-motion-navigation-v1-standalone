# 动作入口交接证据

日期：2026-10-09。正式平台：Windows。

- `h0-red/`：`dfb74f9d` 原生产代码在冻结 80 项矩阵上的修复前结果，0／80。
- `h3-formal/`：同一输入在修复后正式调用链上的结果，80／80。
- `regression-summary.json`：v7、F2 528、v8 固定与杂乱对 R4 的逐项比较。v7 使用项目既有的 `navigation_structure_baseline.product_index` 生成规范化签名；成功状态读取 `metrics.success`，不能把 R4 签名索引当成运行记录。
- `v7-current-index.jsonl`：本轮 v7 的规范化签名和实际成功状态，供公开快照直接复核。
- `performance-summary.json`：D058／D061 正式 Windows 性能摘要，以及未收入 Git 的原始大文件哈希。
- `fabric/`：最终 8 项的计划、原始控制帧、试次和部署结果。`history/` 保留两次 fail-fast 夹具问题，不能把它们改写成正式通过。
- `production-semantic-hashes.json`：本阶段修改的生产文件按 LF 归一后的哈希。
- `lifecycle-checks.txt`：连续 stale、入口转头期间改目标／取消，以及 Walk 预转职责的专项测试输出。
- `mutations/`：三个隔离错误副本的正常对照与行为断言失败输出。
- `full-forward.json`、`full-reverse.json`：最终 Windows 完整检查，正序、逆序各 1670／1670；没有失败、错误或跳过项。

Fabric 第一次失败来自把 JumpUp 正常离地和入口等待套进纯地面 `drop`／十 tick 停滞判据。第二次失败来自把“首条 late1”注入到前段普通 Walk，而本阶段要验证的是首条严格 JumpUp 输入。两次任务本身均到达目标；修正只改测试分类和注入位置，没有放宽生产入口、证明、伤害、期限或来源门槛。

逐项回归可用下面的命令重新生成。四个输入目录是本轮原始 Windows 运行目录；脚本会重算 v7 轨迹签名，同时核对冻结 R4 的 2,000 项分母。

```powershell
python scripts/action_entry_handoff_regression.py `
  --v7-root .tmp/action-handoff-v7-four-dfb74f9 `
  --f2-runs .tmp/action-handoff-f2-dfb74f9/runs.jsonl `
  --v8-fixed-runs .tmp/action-handoff-v8-formal-dfb74f9/runs.jsonl `
  --v8-clutter-runs .tmp/action-handoff-v8-clutter-dfb74f9/runs.jsonl `
  --output-root evidence/motion_navigation/action-entry-handoff-v1 `
  --source-commit dfb74f9d8b4c4b767647f86e1133273356a9be42
```
