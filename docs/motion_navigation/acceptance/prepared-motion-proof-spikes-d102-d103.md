# D102／D103 提前准备与可信注册证明：隔离试验验收

日期：2026-10-12。关联设计状态：**设计冻结；尚未实施正式链。**

设计见 [D102 决定](../decisions/0102-prepare-and-register-motion-proof-ahead-of-handoff.md)和[架构规格](../architecture/prepared-motion-proof-pipeline-v1.md)。本文件归档已完成的可丢弃试验及其独立复审，不预登记实施任务，也不把隔离成功计作正式链交付。

## 1. 结论与共同边界

D102 支持固定单跳在提前完成证明与父侧注册后，以比较完成动作开始热路径。D103 支持固定朝南 2／3／4 格平台、精确预测入口、Windows 20 Hz 模拟、0／1 项积压下的连续两跳；A／B 各 120／120。两项最终独立复审均为 **APPROVED**，只适用于各自声明范围。

这些结果没有覆盖 Fabric、±0.05 落点偏差、多方向、多并发、复杂材料或动态正式世界。正式 Runtime／Session、客户端队列、撤销／替换和完整 generation 生命周期尚未交付。D103 不能当成 D101 M1 通过，也不能回答 M2 的真实 tick 顺序。

D102／D103 的首条 control sequence 是隔离夹具预先分配的数值，不证明生产 Runtime 有序号预留 API。正式 `player_runtime_v1` 在仲裁发送时使用全局 request sequence，并可能组合战斗／视角命令。规格因此分开准备与激活：bundle 先绑定 ledger／session、generation、expected inputs 和 requested start／effect window；Runtime 在首条命令真正赢得仲裁后创建实际首序号与复合命令绑定，交给执行器／tracker。该正式两阶段接口尚未实现、未验收。

原 D101 M0 仍为**整体未通过**：完整决定 P95／P99／最大值为 0.1683／48.6079／57.5597 ms，P99 和最大值不满足 15／30 ms。M1／M2 未实施、未运行，Fabric 未运行。D098 P3 原失败和所有原始证据不改写。

试验来源修订为 `c181f414cc9d22b5d13165cbe094c031e6231174`。正式平台为 Windows，两个试验都使用项目 Python 3.11.16、现有 1.21 计算器和冻结第一跳输入 `jump_gap_1:A3`，输入 SHA-256 为 `8e1c73bf14b044c746c557b2e569198860ea9b25f00f646a17a370bc55af02ed`。临时脚本的精确字节身份分别记录在 summary 与原始 metadata 中，不仅靠仓库修订代表。

## 2. D102 第三轮：固定单跳开始

证据为[D102 summary](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d102/summary.json)、[独立 verification](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d102/verification.json)、[原报告](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d102/report.md)和[完整审查记录](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d102/review.md)。

按时与整体晚一 tick 各 200 个开始样本，总计 400。每个样本只运行到第一条输入实际应用和立即剪枝；不能写作 400 次完整落地。晚一分支包含已证明等待前缀，两组共推进 600 物理步，热路径完整证明和搜索调用均为 0。

400 份准备证明与重新调用原 scanner 的 fresh scan 完整一致。准备扫描和 fresh scan 各 162,800 物理步。该检查验证包装与同一算法等价，不是第二套独立物理实现，也不是 400 种世界或身体状态。

| 测量项 | 样本 | P95 ms | P99 ms | 最大 ms |
|---|---:|---:|---:|---:|
| 已登记 bundle 的动作开始决定 | 400 | 0.4061 | 0.4257 | 0.4525 |
| worker 结果注册后的开始决定 | 200 | 0.5051 | 0.5277 | 0.6262 |
| 现有 `validate_commitment` | 400 | 17.4525 | 17.7467 | 19.3831 |
| 本地完整准备 | 400 | 76.9043 | 78.1197 | 78.8662 |
| 本地完整接纳与登记 | 400 | 28.8226 | 29.6585 | 29.9259 |
| worker 内部完整准备 | 200 | 77.4650 | 77.8581 | 78.0260 |
| 提交至收到 proof bytes | 200 | 80.4645 | 81.0114 | 81.0583 |
| 父侧摘要、解包、完整校验与注册 | 200 | 25.5703 | 34.6822 | 35.4011 |
| 提交至可信可用 | 200 | 105.9756 | 107.5297 | 108.5532 |

开始决定满足 P95≤8 ms、P99≤15 ms、最大值<30 ms。后台工作单独计时，不能由开始决定通过推导出 worker 交付通过。现有验证器虽然不重跑物理，仍完整序列化 proof 并计算摘要，这说明接纳也要提前完成。

worker 为一个持久 Windows spawn 子进程，请求／结果队列容量 1，预热 10 次后顺序测量 200 作业。冷启动至 ready 为 344.5348 ms；100 ms 内可信可用率为 87.5%。worker 自建固定 fixture，本批未测真实世界快照构造、序列化、传输，未叠加队列积压、持续 20 Hz 控制和 receiver／控制线程 GIL 竞争。3 个完整 tick 只能覆盖本批 108.5532 ms 观测最大值，不能写成连续动作硬期限。

25 类负例全部拒绝，身体推进为 0。正常完成、构造失败、父侧异常、worker 内部异常四条受控关闭路径均退出码 0、无活子进程。硬退出而没有返回结果、有界等待和队列 `join_thread()` 的完整异常边界未验证，不作为正式生命周期通过。

## 3. D103 第二批：具体模拟快照与连续两跳

证据为[D103 summary](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d103/summary.json)、[独立 verification](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d103/verification.json)、[原报告](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d103/report.md)和[完整审查记录](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/d103/review.md)。

平台长度 2／3／4 格，输入流起点 `ON_TIME`／`LATE_ONE`，六个组合各 20 场。A 无积压，B 在第二跳前插入一项同工作量前序作业，两组各 120 场。B 使用 FIFO，不是优先级调度器。请求／结果队列及未消费作业上限为 2；第三个作业明确拒绝，不覆盖旧 pending。

| 检查 | A 无积压 | B 一项积压 |
|---|---:|---:|
| 完成两跳 | 120／120 | 120／120 |
| 第一跳落地前可信可用 | 120／120 | 120／120 |
| 落地到第二段实际有效输入 | 全部 1 tick | 全部 1 tick |
| 第二次实际起跳前完整停车 | 0 | 0 |
| 控制 deadline miss | 0 | 0 |
| 安全违规／UNKNOWN 放行／伤害超额／无人负责推进 | 均为 0 | 均为 0 |
| 热决定样本数 | 6,180 | 6,180 |
| 热决定 P95／P99／最大 ms | 0.2073／0.2350／0.3227 | 0.2032／0.2305／0.2850 |
| 注册提前量 tick | 9.435366—10.032234 | 8.194408—9.287130 |

第一跳 boundary 17 首次安全着地。第二跳请求从该分支完整预计落地状态生成，两段使用同一模拟世界；未移动身体到方便位置，也未改写观察。后六条原停车输入尚未发送，交接时按事实退场。第二段输入在功能预跑后冻结，正式批未继续搜索。

父侧每场构造 10,150 格不可变事实快照，覆盖固定 7×58×25 窗口，朝南两个一格坑和中间平台。worker 恢复传入数据，不创建 fixture。所谓“真实快照”是该具体模拟世界的实际事实副本，不是真实服务器采集。

只有 BLOCK／AIR 两种共享不可变 `CellFact`、一种 full-cube 几何；pickle 对共享对象的复用降低了字节量。因此它不能代表正式多材料、复杂形状、事实 stamp 差异、变化率或窗口规模。

| 字节／成本 | 实测 |
|---|---:|
| 每份快照 | 165,780 bytes |
| 请求 | 167,679—167,707 bytes |
| 完整结果 | 292,470—393,156 bytes |
| 246 次快照构造 P95／P99／最大 | 39.1137／42.7386／45.0731 ms |
| 246 次快照序列化 P95／P99／最大 | 1.2299／1.2735／1.2947 ms |

快照构造已接近一个 50 ms tick，且超过完整决定最大值<30 ms。因此正式设计要求持续维护或增量导出，并把构造、序列化和摘要移出控制热路径。本批是在场景开始前构造快照，没有证明正式世界已经采用这项办法。

| 测量项 | A P95 ms | A P99 ms | A 最大 ms | B P95 ms | B P99 ms | B 最大 ms |
|---|---:|---:|---:|---:|---:|---:|
| 提交至 worker 取到请求 | 0.4270 | 0.5555 | 0.5581 | 55.0874 | 55.5625 | 55.6093 |
| worker 完整证明与封存 | 44.4488 | 48.0616 | 48.1995 | 44.2159 | 47.5576 | 47.8203 |
| 父侧 expected inputs 精确比较 | 0.0061 | 0.0063 | 0.0063 | 0.0062 | 0.0064 | 0.0080 |
| 父侧收到结果至可信注册 | 15.9187 | 30.3140 | 32.0565 | 15.2658 | 42.2437 | 44.5568 |
| 提交至可信可用 | 71.7751 | 76.6060 | 78.2196 | 119.3228 | 135.4790 | 140.4254 |
| 快照／请求实际成本加提交至可信可用 | 93.8311 | 96.5016 | 98.5269 | 156.7258 | 161.4944 | 162.7335 |
| 开始构造快照至可信可用的真实全程 | 560.4220 | 568.2312 | 635.6296 | 622.0501 | 625.8300 | 627.5772 |

“实际成本加提交至可信可用”是逐场相加实际耗时后再求分位数，不能用表中各项 P99 相加替代。真实全程另含第一跳与 fallback 的准备／登记和起跳前运行，不能解释为 worker 独占耗时。

ready 时刻取在完成注册、创建 handle 处，略早于字典发布，没有单独计入这段短间隔。控制循环只消费已发布后实际 poll 到的对象，接续结论来自真实输入 tick。正式设计要求进一步记录实际发布与首次消费。

receiver 与控制循环在同一父进程，实际争用 GIL，worker 同时运行。总计 12,567 个节拍决定；控制线程 scan、搜索、完整 proof hash 和反序列化均为 0。A／B 唤醒迟到的最大值为 24.0889／36.9721 ms，与决定耗时分开保留，不能因决定很短忽略调度延迟。

六项 800 ms worker 延迟注入分别覆盖六个组合，全部 `OVERDUE`，两跳完成为 0／6，fallback 为 6／6。第二跳未获授权；执行预登记 `Walk×1→Neutral×16` 后在中间平台安全停住，未越过第二坑、零伤害、零无人负责推进。它们不混入 A／B 正例分母，也不算连续性通过。

五项负例是依赖格变化、world revision 错配、重新封存的内部 ruleset 错配、旧 generation，以及同 request 下替换另一条合法输入；全部拒绝且推进为 0。最后一项由真实 worker 用原 scanner 得到 `VERIFIED_CANDIDATE`，再被 receiver 以 typed `INPUTS` 拒绝，没有注册。

五条生命周期路径为正常停止、构造失败、在途父侧异常、worker 内部异常、满队列停止；结束时 worker、receiver 和活子进程均为 0。满队列停止实际退出码为 −15，其余为 0；不写成“所有异常正常退出”。

## 4. 修正与复审记录

| 轮次／批次 | 当时发现与处理 | 当前证据地位 |
|---|---|---|
| D102 第一轮 | 固定单跳性能探索；原 raw、summary、脚本留在 `attempt1/` | 原结果保留，规则绑定不完整，不作修正版验收 |
| D102 第二轮 | 独立审查复现 `proof.ruleset` 错配并重新封存后，旧接纳仍登记并推进 1 tick；原验证器为 STALE | `CHANGES_REQUESTED`；原失败及六份历史哈希保留 |
| D102 第三轮 | 补三向 ruleset 与 typed `RULESET` 负例；随后只修报告的快照、积压、20 Hz／GIL 和“3 tick”边界说明 | 25／25 拒绝、0 推进；最终复审 APPROVED；报告修改没有增加性能样本 |
| D103 调试与预跑 | 前两次因 JSON 编码／不存在的公开属性结束；第三、第四次各六场功能预跑完成 | 保存四版源码和对应 metadata；失败不混入正式矩阵，raw 未保存异常 traceback |
| D103 第一批 | A／B 物理连续性成立；同 request 下 30→31 条合法输入重新证明，旧 receiver 仍发布 handle | `CHANGES_REQUESTED`；原数据迁入 `attempt-before-input-binding/`，不改判运动失败，也不签署完整输入绑定 |
| D103 第二批 | 父侧保留 expected inputs 与提交前 digest，receiver 登记前独立比较；重新采集完整正式矩阵 | A／B 各 120／120，五项反例拒绝；最终定向复审 APPROVED |

三处长期保留的修正是三向 ruleset、D102 成本边界说明、父侧 expected inputs。D102 有三轮性能批次，D103 有两批正式测量，不能把报告修订或功能预跑另算正式成功样本。旧报告、旧计时和失败发现都保留原字节，不把旧源码测量改标为修正版结果。

## 5. 独立核验与来源字节

D102 原 raw 共 2,833 行，独立重算 summary 逐字段一致，400 份 fresh scan 等价、25 项负例和四条退出记录一致。复审独立重建四份证明与首条应用状态，新增性能样本为 0。

D103 原 raw 共 13,685 行，独立核验重新扫描 738 份 proof、回放 12,567 tick、核对 492 条首条回执与 972 个只比较节点；扫描为 178,022 物理步。合法替换输入的独立反例再扫描 212 步，不进入性能分母。summary 逐字段一致、旧批次 259 文件原字节保持，新增性能样本为 0。

| 精确来源 | SHA-256 |
|---|---|
| D102 测量脚本 | `43189806cc23e1583d93090d166da07b334594134ad294cf7fe756d8a7e67c5a` |
| D102 原 raw | `d9863c03a127f6ccc610397daca1395b72c4d7426708fcf43e35f5e637c0490c` |
| D102 最终报告 | `948d7ce6f82f8d9d2b521a63567a9e505843af185a970b134023d2db300d9799` |
| D103 第二批测量脚本 | `07d3ede3314b4277a00096245d7b6ea2cf6ff6aa0d627bfd5cdceef9be2e7a8d` |
| D103 第二批原 raw | `52e60b61914dd643e91163b1b17583d5d1bfb1fe784614101148933975345731` |
| D103 第二批最终报告 | `2fbc835a91d25cd71895c1de160361504fda12bb8c2ce6400bb5932c1b5659b9` |

## 6. Git 中的紧凑证据与复核命令

Git 保存两个试验各四份文件：summary、verification、report、review。它们从 `.tmp` 逐字节复制，[manifest](../../../evidence/motion_navigation/prepared-motion-proof-spikes-v1/manifest.json)逐项记录原路径、SHA-256、大小、来源修订和平台。局部 `.gitattributes` 关闭这些 JSON／Markdown 的换行转换，保护源字节；它也列入本包导出边界，不修改仓库级规则。

正式规格复审另指出计算退场与执行引用的边界、计算／身体／输入流三类身份需要分开。本次只补文档：同一结果服务在接纳后可退场计算 token，registry 继续唯一记录 candidate／executing／safe-tail 引用；当前动作引用不被下一计算替代，执行引用到安全交接、真实命令终态齐全且监督者释放最后责任后才注销。输入流 generation 由现有 `MotionRouteCoordinator` 唯一创建／换代，receiver 只校验父侧各 owner 提供的字段。

已发布 handle 持续持有不可变快照；lease 释放与依赖变化分开，最后使用引用释放后才能回收对应快照／bytes。计算 scope 失效与服务关闭都不能代替已执行安全收尾。这些是设计契约补充，D102／D103 隔离试验尚未验证正式执行引用转移、关闭期间保留与取消竞态；原性能、summary、verification 和最终试验 APPROVED 的范围没有变化。

序号契约补充只改文档：receiver 不生成或猜首序号，执行开始需要同时具有已登记 handle 与匹配的 Runtime `StreamActivationBinding`。第一条实际回执仍立即选择唯一 0／1 tick 分支。首条未赢仲裁、激活绑定不符或 generation 过期时，该 bundle 不推进身体；原实验没有覆盖这些正式仲裁边界，不能用预分配夹具序号替代新接口证据。

当前约 50 MB 的原始轨迹／证明，以及临时测量／核验脚本、历史批次和预跑继续留在 gitignored `.tmp`，未复制到 Git。manifest 保存当前 raw 摘要、proof 集合数量／总字节／索引摘要及原清单身份。紧凑包只能核对结论、审查历史和来源身份；没有本地原文件就不能独立重放证明或重算性能。review 中的 `.tmp` 绝对链接是原文的一部分，保留原字节，不保证在别的检出中可用。

以下为原试验最终使用的重算与定向复审命令，要求对应 `.tmp` 原脚本和完整原证据存在，不采集新的性能样本：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d102-prepared-proof/spike.py --recompute
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d102-prepared-proof/verify_evidence.py
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d102-prepared-proof/review_followup_check.py
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d103-continuous-prepared-proof/spike.py --recompute --output .tmp/d103-continuous-prepared-proof/formal
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d103-continuous-prepared-proof/review_attempt2.py
```

原最终重算／复审退出码均为 0。本次文档归档只检查复制哈希、数字、文档互链、证据边界与 `git diff --check`，不重新测量性能，不用新报告覆盖原文件。正式链验收、Fabric tick 顺序和更广动作矩阵均尚未实施。
