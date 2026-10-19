# D120 带动量入口校准验收

日期：2026-10-19。状态：**COMPLETED；有限入口、Fabric 校准与最终发布门禁均通过。**

关联：[决定](../decisions/0120-resume-momentum-entry-calibration.md)、[实施计划](../stages/d120-momentum-entry-calibration-plan.md)、[公开证据](../../../evidence/motion_navigation/d120-momentum-entry-v1/README.md)。

## 1. 结果

| 项目 | 结果与范围 |
|---|---|
| 有限联合入口 | 四方向各 243 点，共 972 个入口、2916 个后缀，全部通过；主物理步 47,628，低于 120,000 上限 |
| 旧 D118 raw 门禁 | 原文件哈希不变，真实 landed post-state、身份和后缀重放通过 |
| 朝南三族 | 3/3；123 条 receipt／physics／F-TICK 身份对应；捕获和验收均通过 |
| 四方向三次矩阵 | 36/36；1476 条身份对应；全部入口、时序与后缀通过 |
| 物理一致性 | full 离散差异 0；每轴最大位置误差约 1.975e-6 格，速度误差约 7.924e-8 格/tick；分别低于 0.001／0.0001 门槛 |
| 真实健康 | south 138 条、full 1623 条健康观察，零健康下降、零伤害事件 |
| 责任与关闭 | 来源显式释放；责任截止前检查健康与全部物理，截止后 south 13 条、full 164 条中性尾迹继续检查身份、物理与安全支撑，全部通过 |
| 相邻与导出 | 直接／相邻 69/69；Windows 源码树正逆各 197 模块／2534 项，独立公开快照正逆各 196 模块／2510 项，全部通过 |

五轴取完整笛卡尔积：横向／纵向位置各 ±0.002 格，水平横向／纵向速度各 ±0.0002 格/tick，yaw ±0.1°，每轴包含零。清单 hash：`89c5ee269da0b8ac969d69be781a589c8f28449faf0b153fe69a6b2c2a433dd6`。

south 清单为 `0ee556cbd682b3692066dc0371b4936c0abe43c125c37f6629368df878e66f18`，full 为 `9c54a00e635ccabd4224c79255645b03cafeab51332e669800fc10dc9f2e75c0`。offline 索引为 `d4bf9646adf76031d8f9c2bb9c3de39437ec0aa7ef5b455ba63a87aa1ba1e3e9`，full 绑定的 south 索引为 `8a15797f2c6472ca319a5187718063d4f77c48a1769cb98f41a66057dd0df77c`。

## 2. 已验证与未验证

有限样本覆盖 `sample_coverage=passed`；连续实数入口范围始终 `sound_envelope=unverified`。真实落地态命中登记边界后，还分别从该真实状态推演三个后缀，不能把边界命中当作连续范围证明。

第二跳和潜行为 L+1 生效；late1 是固定 RUN hold 后 L+2 生效，不是网络迟到注入。三个后缀分别最多推进 42／42／30 tick。潜行全段保持中间平台支撑。full 的真实入口重放中，潜行后缀最小脚印重叠约 0.000551309 格；它表示这次离散停止位置，不能当作入口宽度。

本轮不改 `mc2p/`、Java 或生产控制路径，不开启 D118-1—D118-5、prepared 自动后继或生产四格跨隙能力。D118 继续保持历史 FAILED／STOPPED，D119 物理修复仍为 COMPLETED。

## 3. 实际运行与限定修正

| 本地输出 | 原记录 | 原因与后续 |
|---|---|---|
| `.tmp/d120-fabric-south-v1` | FAILED | JDK 预检失败，未产生 Fabric 场景；改用明确的 Java 21 环境 |
| `.tmp/d120-fabric-south-java21-v1` | FAILED | 初始 reset 观察的 movement tick 为 null，被健康校验误拒；真实健康均为 20 |
| `.tmp/d120-fabric-south-java21-r1-v1` | PASSED | 首条 reset 特例经 RED/GREEN 验证后正式运行，south 3/3 |
| `.tmp/d120-fabric-full-java21-v1` | FAILED | 北向实际 −π、名义 +π 被直接相减成 −360°，三族各三次误拒；物理无差异 |
| `.tmp/d120-fabric-full-java21-r1-v1` | PASSED | 两处分析共用最短有符号角差后正式运行，full 36/36 |

两项限定修正均先写失败测试：只允许第一条 `seq=0, request=None, movement=None` 的 reset 观察；其他缺 movement tick 的健康记录仍拒绝。±π 视为相同朝向，真实超出 0.1° 的角差仍拒绝。健康下降、缺健康、遗漏尾迹、离开平台、错误身份和未释放来源的变异继续拒绝。

历史三个失败目录原字节保留；只读复算没有回写原摘要。初次 full 的原始文件也曾按修正后的分析器只读复算，36/36，通过前后 14 个 raw 文件哈希不变；它与随后正式 r1 是两份不同记录，不能混为同一次运行。

最终 source：south `artifacts/fabric-deployment/20261019T042801511243Z-5eea69a8`；full `artifacts/fabric-deployment/20261019T044001601177Z-7e26be5c`。正式平台为 Windows，Java 21，seed 21001，显式 600 秒期限，physics 与 F-TICK sidecar 同开。确切命令保存在公开包 south/full 的 `command.json`。

## 4. 证据与复现边界

公开包只存小文件、紧凑分析、原文件哈希和实现语义哈希，不复制完整轨迹。`source-index.json` 是原本地输出索引；公开包自己的索引为根 `index.json`。两种索引不能混用。公开文件可核对结果和来源，不能单凭这些摘要独立重放完整 Fabric 轨迹。

离线命令按计划 S2，最终采用输出 `.tmp/d120-entry-s3-prerequisite-v1`。正式包装器分别传入该 offline 前置路径和 south r1 前置路径。所有运行与失败的精确命令、退出码、source 和哈希均随摘要保存。

## 5. 最终发布门禁

| 运行 | 模块数 | 测试项数 | 结果 |
|---|---:|---:|---|
| Windows 源码树正序 | 197 | 2534 | 通过 |
| Windows 源码树逆序 | 197 | 2534 | 通过 |
| 独立公开快照正序 | 196 | 2510 | 通过 |
| 独立公开快照逆序 | 196 | 2510 | 通过 |

四份报告全部完成，failure／error／timeout／skip／cleanup failure 均为 0；源码树与快照 Java 检查各 3/3 通过。完整矩阵所用快照 `verify` 为 2634 文件。报告路径、字节数、SHA-256 和起止时间见公开证据 `windows-regression/summary.json`，不复制完整分片报告。

本节与摘要是在上述完整检查后补写，运行代码和校准数据未变。补写会新增一个公开小文件；收尾后的导出模块检查与 verify 单独登记，不把新文件数倒写进原快照记录。

补签后完整导出模块 22/22 通过（27.699 秒）；独立收尾快照 `.tmp/d120-public-signed-final-v1` 的 verify 为 2635 文件。增加的唯一文件是公开的 Windows 汇总；完整矩阵所用 2634 文件计数保持原记录。
