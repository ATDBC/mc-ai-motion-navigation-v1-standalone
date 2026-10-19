# R28-0 批次 5：v3 冻结记录

本目录保存 2026-10-02 批次 5 的固定基线。2,000 场中 1,706 场完成；原八层仍为 1,589/1,600，新增玩家站位为 117/400。294 个未完成任务保留。192 项边界有界结束，其中 68 项完成；56 场 Fabric 中 20 场完成、36 场未完成。

结果和限制以 `docs/motion_navigation/acceptance/navigation-coordination-convergence.md` 第 10.7 节为准。固定清单成功不代表所有地形可靠，R28-1 尚未实施。

| 文件 | 内容 |
|---|---|
| `baseline.tar.gz` | 全部 2,000 个任务、元数据、输入和轨迹 |
| `player-before.tar.gz` | 新玩家站位族修改前的 400 场 |
| `comparison.json` | 原成功未丢失、严格身体／输入／风险比较、实际归属与性能 |
| `player-boundary.tar.gz` | 192 项四向、三种入口速度与两种交付条件 |
| `migration-*.json.gz`／`migration-comparison.json` | 1,448 组协调检查、差异与函数入口 |
| `migration-faults.tar.gz` | 四个额外正式路径故障场景，不加入产品成功率 |
| `changed-perturbation-delivery.json` | 16 组历史诊断误抑制外力的实际交付对照，不能当相同输入配对 |
| 四个运行 ID 的 `.tar.gz` | 56 场最终 Fabric 的原始结构化观察、控制、计时与结果 |
| `intermediate-failures.json` | 开发中运行器错误和源码变化导致的失败，未重标通过 |
| `post-run-contract-check.json` | 运行后的入口拒绝与只读接口差异，包含实际运行哈希 |

公开模拟轨迹使用原解压 JSON 字节集中压缩，减少每个任务单独 gzip 的重复开销。`runs.jsonl` 与元数据不改写。档内 `trace-transport.json` 将原压缩文件哈希关联到公开路径及其字节哈希。`scripts/r28_batch5_report.py` 支持两种传输格式；运行记录和原 gzip 文件仍保留在本地。v1、v2 没有改写。

`runtime-source/` 只存已匹配实际运行哈希的源码。`review-reference/` 只供语义对照；部分原换行字节未能恢复，不能把它称作原字节快照。实际运行哈希与当前文件的区别在接口差异记录中明确标出。

复核时在新目录解开模拟包，再执行报告工具。不得覆盖已有证据：

```text
python -m tarfile -e evidence/motion_navigation/r28-baseline-v2/baseline.tar.gz output/inspect-v2
python -m tarfile -e evidence/motion_navigation/r28-baseline-v3/baseline.tar.gz output/inspect-v3
python -m tarfile -e evidence/motion_navigation/r28-baseline-v3/player-before.tar.gz output/inspect-player-before
python scripts/r28_batch5_report.py --old output/inspect-v2 --new output/inspect-v3 --player-before output/inspect-player-before --output output/inspect-comparison.json
```

SHA256SUMS.txt 覆盖本目录所有交付文件。实机包合计约 9.7 MiB，没有画面或控制台日志；模拟、对照和函数覆盖证据另计。
