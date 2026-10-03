# D052／D053 后的迁移基线 v5

日期：2026-10-03。状态：迁移前参照已整理，产品非退步门槛未通过。源码为 `1a67050` 加 D054 的两处修正，以 `metadata.json` 中逐文件生产指纹为准。输入清单仍使用 v4 的原标签，不据此判断执行源码版本。旧 v1—v4 和失败不改写。

产品二千项中 **1,699 完成**，v4 为 1,703。晚到下降从 189/200 降到 185/200，其余一千六百项非下降任务逐项不变，正常下降 200/200。六项原成功转为安全失败，两项原失败转为成功。异常、安全违规和证据不足均为零，但有界失败仍计为失败，不能写成非退步通过。

协调 **1,448 项**及四个补充故障符合各自预设判定。408 项公共签名变化，其中四项业务终态改变；原成功变失败的一项也完整保留。41 个迁移函数均有入口记录，只证明进入过，不能替代分支验证。完整运动导航 951/951；当前没有新的 Fabric 结果，R28-1 未开始。

| 文件 | 内容 |
|---|---|
| `baseline.tar.gz` | 当前二千任务的原记录、实际轨迹、严格轨迹、量尺及源码身份 |
| `migration.tar.gz` | 当前 192 项中断、1,000 项事件序列和 256 项异步组合，共 1,448 项 |
| `migration-faults.tar.gz` | 四项补充正式路径故障，单独统计 |
| `comparison.json`、`migration-comparison.json` | v4 与当前逐项差异，包含未完成与终态变化 |
| `migration-readiness.json`、`migration-inventory.json` | 函数入口记录、当前行数与按拥有者划分的迁移清单 |
| `coordination-outcome-review.json` | 四项业务终态变化的原输入及当前独立复跑 |
| `initial-failed-baseline.tar.gz` | 修复前 1,695/2,000 批次，含坑边无界等待，不能替换为当前结果 |
| `coordination-smoke-failure.tar.gz` | 原夹具遗漏后续服务的十九项记录，其中一项失败 |
| `coordination-smoke-passed.tar.gz` | 夹具修正后的 24 项代表检查 |
| `smoke.tar.gz` | 先运行的八十项预检；它没有发现后来的完整集合失败 |
| `overbroad-retry-failed-tests.txt` | 中间修正给所有入口失败重试，导致 947 项中两项失败 |
| `interrupted-runs.json` | 中断批次的已保存清单和原因，不能作为完整矩阵结果；原记录继续保留在本地 |
| `terminal-recovery-cost.json` | 单个坑边退回组件成本，非正式在线期限验收 |
| `archive-verification.json`、`SHA256SUMS.txt` | 实际归档字节核对与文件哈希 |

归档把 gzip 内的原 JSON 字节直接放入 tar，再统一压缩；不修改字段或轨迹。`trace-transport.json` 保存原压缩哈希、归档路径和解压后哈希。比较工具支持原 gzip 文件和该传输形式，两种都检查记录及字节身份。

解包 v4 与 v5 后，可以用 `scripts/r28_baseline_alignment.py` 逐项核对。产品比较当前应返回非零，因为六项成功转失败；协调比较没有违反各自判定，但也不代表行为等价。命令、具体数字和新实机证据边界见 `docs/motion_navigation/acceptance/navigation-coordination-convergence.md` 第 13 节。
