# D055 启动交付结果与迁移参照 v6

日期：2026-10-03。生产来源为 `9ec5e4f` 加 D055，实际字节由 `metadata.json` 的 `production` 指纹固定。产品、协调、四项补充故障和最终完整测试均在同一最终实现上运行。旧 v1—v5、原下降失败和中间失败不改写。

产品 **1,712/2,000**，v5 为 1,699；正常下降 200/200，晚到下降 185→198/200。原六场全部完成，原成功全部保留，其余 1,600 场输入、轨迹及外部指标逐项不变。异常、安全违规和证据不足为零。固定集合非退步通过，不作统计发布结论。

协调 1,448 项及四项补充故障符合各自预设结果。408 项签名变化，其中一项业务失败变为成功；没有以有界结果冒充逐项等价。41 个迁移函数有入口证据，不能替代分支覆盖。运动导航 960/960，最后直接相关检查 86/86。

| 文件 | 保存的内容 |
|---|---|
| `baseline.tar.gz` | 最终二千场产品，原观察、输入、轨迹、指标、清单及生产指纹 |
| `migration.tar.gz`、`migration-faults.tar.gz` | 最终 1,448 项协调及四项正式故障 |
| `comparison.json`、`migration-comparison.json` | v5 与 v6 的逐项差异 |
| `coordination-outcomes.json` | 唯一改变的业务终态，仍单列检查 |
| `initial-failed-drop.tar.gz` | 初次修正的四百场：正常 200、晚到 189，种子 1、130 退步；不是最终结果 |
| `fabric/failed-original-flat-view/`、`fabric/failed-diagnostic-flat-view/` | 平视下降未启动的原失败，保留清理与实际输入 |
| `fabric/scoped-downward-view/` | 向下观察落点的两场低成本验证 |
| `fabric/passed-with-natural-regeneration/` | 中间四十场通过；不能用净扣血作为最终伤害证据 |
| `fabric/release-no-natural-regeneration/` | 最终四十场的结构化帧、观察流、worker 时序、源码身份和清理记录 |
| `fabric-independent-check.json`、`fabric-application-timing.json` | 从原始帧和实际采样重新统计首条生效、伤害、连续性及本机时钟成本 |
| `worker-performance.json`、`preparation-performance.json` | 真实进程冷／热往返与局部安全查询；不混作整帧成本 |
| `archive-verification.json`、`SHA256SUMS.txt` | 归档原字节与文件哈希核对 |

最终 Fabric **40/40**：五类场景、四方向，正常和首条晚一 tick 各一次。二十个首条晚到都由实际应用账本核实。无自然回血时，二格下降伤害均为零，五格均为两点。控制准备 P95 1.792 ms，客户端采样到应用 P99 99.561 ms。

下降限定持续向下观察已知落点。平视后信息获取与准备相互打断仍未修复，登记为 R28-C-06；原失败不是被替换成通过。没有放宽支撑新鲜度、伤害额度、输入账本或身体释放条件。R28-1、室内接近、正式跟随和长期期限仍打开。

tar 内的 gzip 轨迹还原为原 JSON 字节再统一压缩，`trace-transport.json` 保存原压缩哈希及归档字节哈希；不改变字段。Fabric 只保存结构化记录，不保存画面、世界、游戏 JAR 或启动凭据。比较入口为 `scripts/r28_baseline_alignment.py`，完整命令和范围见运动导航验收第 14 节。

发布整理只补充导出清单和公开入口文档。导出清单属于 metadata 扫描的配置文件，因此其发布后哈希与运行时不同；`publication-verification.json` 单列这一个差异并核对其余生产文件和实机源码未改动。没有回填原 metadata 或重写归档。此目录按原字节提交，换行不由 Git 自动转换。
