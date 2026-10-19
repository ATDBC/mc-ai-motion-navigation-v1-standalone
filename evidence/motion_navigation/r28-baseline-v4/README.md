# D051：撤回筛选后的 v4 记录

本目录保存 2026-10-02 的进入修正。移除终点推演筛选和按原因改名，保留静态站位、末段直连、路线长度、依赖及严格动作。没有修改墙面接触控制、身体释放或输入权限。

2,000 个任务中完成 **1,703**；原八层为 **1,589/1,600**，玩家站位为 **114/400**。三项原成功任务变为失败，因此同输入非退步比较未通过。800 个严格任务逐项不变。全部未完成记录保留，不能把 v4 称为发布门槛已经关闭。

| 文件 | 内容 |
|---|---|
| `baseline.tar.gz` | 与 v3 相同的 2,000 个输入、全部任务和轨迹、实际源码身份 |
| `comparison.json`／`direct-v3-comparison.json` | 配对差异及直接核对 v3 全部任务的成功数，含三项丢失成功；不是通过报告 |
| `details.json` | 按分组统计的停顿，以及四个迁移故障场景的函数入口 |
| `repeatability.json` | 同机 12 对重复任务，第二次规划轮询增加 5 ms 等待；不代表完整跨平台验收 |
| `support-change.json.gz` | 正式世界变化入口的 12 个变体：三场完成、三场取消、六场有界失败 |
| `migration-faults.tar.gz` | 四个额外正式故障场景及函数覆盖，不计入产品成功率 |
| `migration.tar.gz`／`migration-comparison.json` | 1,448 个同输入协调记录及公共签名比较，全部符合检查、签名无差异 |
| `c01-invalid-reference.json.gz` | 未修改源码上原错误注入的原字节压缩及来源指纹；只改观察、身体未下降，不能作为真实下落证据 |
| `20261002T084447721722Z-aa08dd58.tar.gz` | Fabric 绕墙四方向的原始结构化流、计时与结果，4/4 完成 |
| `fabric-summary.json` | 实机试次原结果，不把有界失败改为完成 |
| `archive-verification.json`／`delivery-audit.json` | 归档独立复核和运行后源码差异；只有公开导出清单追加证据 |

模拟包集中压缩原解压 JSON 字节。档内 `trace-transport.json` 保留原 gzip 哈希与公开字节哈希；原任务及元数据不改写。原 v1—v3 没有覆盖或删除。运行记录保留当时 HEAD 与实际工作树哈希，不用交付后的提交号回填运行身份。

542 个步行停顿 tick 中，原八层仍为 8，玩家站位为 534；严格入口恢复的 187 tick 继续单列。实际移动输入没有空归属。本次撤回恢复了固定终点规则，也失去了筛选挑选替代位置的三场偶然成功；墙面与边缘接近仍待专项修复。

复核时将模拟包解到新目录，再用已有报告工具比较；输出文件也必须新建：

```text
python -m tarfile -e evidence/motion_navigation/r28-baseline-v3/baseline.tar.gz output/inspect-d051-v3
python -m tarfile -e evidence/motion_navigation/r28-baseline-v4/baseline.tar.gz output/inspect-d051-v4
python -m tarfile -e evidence/motion_navigation/r28-baseline-v3/player-before.tar.gz output/inspect-d051-player-before
python scripts/r28_batch5_report.py --old output/inspect-d051-v3 --new output/inspect-d051-v4 --player-before output/inspect-d051-player-before --output output/inspect-d051-comparison.json
```

比较因三项成功丢失返回 1，是当前事实。不得改用另一个对照来消除它。共享工作退场 R28-C-02、协调迁移、终点控制、持续预算和正式跟随没有在本轮完成。完整结果、命令及证据边界见验收第 11 节。
