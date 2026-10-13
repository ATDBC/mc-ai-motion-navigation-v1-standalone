# D103 Windows 连续两跳独立审查

2026-10-12。第一批结论：**CHANGES_REQUESTED**。正常矩阵、控制耗时和连续两跳结果有原始证据支持；父侧没有检查返回的输入是否等于提交的输入，阻塞“完整绑定已保留”的签署。

本次先读根 AGENTS、D099／D100／D101、D101 阶段及 M0 验收、D102 最终报告与审查，再核对 D103 源码、清单、原始记录、证明文件及四次预跑。审查不修改 tracked 文件，不提交，不采集正式性能，不覆盖原始证据。当前实际原始文件为未压缩的 `formal/raw.jsonl`。

受审源码 SHA-256：`a6aea471506f741cda763c888ab6ab4c8dd425aa4a1793efb98d6c4724d32f91`；raw：`0a852b8e55cc7b78079d1c289462a37bb03a9cc66414b139eea27e0fd3e0dc04`；报告：`b3ca83c759a50dbebaf6e10f140b1e4a57339f1126df8ade589866a9e95c8fc6`。审查全部通过后，实施者把本批移入 `attempt-before-input-binding/`，这对应第一批；下面发现仍按这些字节身份签署。

## 阻塞发现

**[P2] receiver 没有把返回输入与原作业输入逐项比较。**

[make_job](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/attempt-before-input-binding/formal/spike-source.py:156) 确实把完整 inputs 序列化传给 worker，但父侧保留的 job 只有 request、generation、owner 和 world digest，没有保留 expected inputs。[receiver](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/attempt-before-input-binding/formal/spike-source.py:243) 登记时也没有传入原输入。D102 `TrustedRegistry.admit()` 只检查返回的 `bundle.inputs == proof.inputs`，并重算返回输入自己的摘要。这不能确认它与提交内容相同。

已通过实际 `_receive()` 路径复现：读取第一场第二跳的 30 条输入，在末尾增加一条合法 Neutral，保持 request、generation、owner 和世界不变，再用原 scanner 生成有效的 31 条输入证明。receiver 完整检查后仍发布 handle，`hot_check()` 返回 `OK`。这不依赖旧摘要、任意内存修改或恶意 pickle；两份证明各自都来自可信 scanner。复现只检查接纳，不推进身体，不生成新的性能样本。

因此报告第 21 行“inputs 与预登记的父侧请求一致”尚不成立。现有正常 worker 确实按收到的输入计算，原 240 场没有发生这项错配；本发现不把它们改判为运动失败，但说明接纳边界缺少一项独立比较。

最小修正：父侧 job 保留精确提交输入及其预先计算的摘要，receiver 在登记前比较返回输入；错配返回 typed `INPUTS`。增加“同 request、不同合法输入、重新完整证明”的负例。按主任务裁决保留第一批原始证据，再用修正源码采集第二批；不能把第一批计时改标为修正版本。

## 已核实的结果

| 审查项 | 结论与证据 |
|---|---|
| 父侧快照与传输 | `build_snapshot()` 在父侧创建 detached 世界并冻结 facts 映射；序列化 Snapshot 的 10,150 个格子、session、revision 和 ruleset。请求同时包含完整 request、inputs、world bytes、world digest 和第一跳依赖。worker 只用传入 Snapshot 恢复世界，没有调用 fixture。165,780 bytes 是实际封存快照大小，构造、序列化、摘要、传输和解包成本均有记录。 |
| 第二跳的预测入口 | 第二跳 request 的完整 PhysicsState 等于第一跳原证明选中分支的 recovered boundary；两跳使用同一 Snapshot。每 tick 在推进前后分别比较原证明保存的状态，没有只把真实状态与一步即时预测自比。 |
| 交接与输入授权 | 真实第一跳已着地后才选择第二段；再次比较真实状态等于第二段入口，并核对 handle、依赖、规则与账本。没有在落地前授权第二段。第二段首条输入恰在落地后 1 movement tick 应用；这支持 **1 tick 交接**，不支持“已预排程”。原第一段六条未发送停车输入退场，未把已应用输入删除。 |
| 控制线程 | 完整 scan、search、hash、序列化／反序列化均移到控制循环之外；guard 在控制线程调用时会失败。receiver 完整检查并注册，控制侧只 poll 和比较。计时涵盖物理一步、账本、首条回执、关键节点、伤害、交接及提交。证据行构造、JSON 与写盘不在计时内；它不是整套生产观察到客户端发送的耗时。 |
| A／B 矩阵 | 各 120 场，2／3／4 格平台 × ON_TIME／LATE_ONE 的六格各 20 场。每场的绝对调度时间严格递增 50,000,000 ns。B 每场 low 与 second 的 request bytes 差小于 0.1%，扫描物理步相同；low 先提交且完成结果封装后 worker 才取 second。这里是 FIFO 中一个同工作量前序作业，没有真正的优先级调度器。 |
| 连续性和时限 | 独立从 raw 重算 A／B 各 120 次两次落地；第二段有效输入距第一次落地均为 1 tick。另从第一次起跳直到第二次实际起跳检查真实速度，完整停车均为 0。ready-before-landing、提前 tick、deadline 均与记录时间关系一致；两组 deadline miss 都为 0。热决定 P95／P99／最大 A 为 0.2317／0.2664／0.5457 ms，B 为 0.2053／0.2338／0.4996 ms。 |
| OVERDUE | 六场各覆盖一个平台／起点组合。第二跳没有被执行；第一跳落地后只执行已事先扫描、登记的 Walk×1＋Neutral×16。独立检查无 jump、只有一次落地、最终在中间平台支撑内且速度为零，没有越过第二坑。它是提前证明的短期安全后段，不是“看到未及时交付就随意松键”。 |
| 安全和负例 | 每次回放 CalculationStatus 都为 OK，并匹配原 proof；246 场伤害都是 0，owner、ledger、generation 都可对应当前段。四项原负例确实走各自比较／接纳门禁且记录 0 推进；不能把这些负例称为已覆盖返回 inputs 错配。 |
| 生命周期与队列 | 正常结束、构造失败、在途父异常、worker 异常和满队列停止均有真实 close 记录；worker、receiver 和活子进程为空。第三作业明确拒绝，原 pending 未被替换。固定矩阵只在结果已发布后 forget，未见正常运行中的队列竞态。 |
| 序号 | 同一场实际第一段与实际第二段／fallback 的 sequence 没有重用。second 与互斥 fallback 预备候选使用同一区间，但实际只执行其中一份，ledger／generation 不同。跨独立场景并非全局单调分配；不能据此声明正式多任务序号分配器已经实现。 |
| 三向规则与身份 | proof／bundle／world ruleset 三向比较保留；request、session、revision、逐格依赖、账本、first sequence、generation 和 owner 均有检查。该结论不包括上述 missing expected-input 比较。 |
| 回放可信度 | 738 份 fresh scan 使用原 scanner 重算，不是另一个物理实现；它验证保存的完整证明与原算法一致。12,567 tick 则从保存快照按记录输入重新推进，逐个状态和实际 effect tick 一致；492 条首条回执及 972 个关键节点一致。没有新增性能样本。 |
| 预跑历史 | 四版保存源码哈希均对应各自 raw metadata。前两次留下未完成 raw 和正常 close；第三／四次各六场完成。历史源码能解释先前 JSON 编码和不存在属性的问题；raw 本身未保存异常 traceback，不能把报告中的根因文字当作独立异常日志。 |

## 非阻塞边界

- **10,150 格不等于正式世界窗口的代表性负载。** 这是 7×58×25 的固定朝南小地形，主要是空气，只复用 BLOCK／AIR 两种 CellFact 对象和一种 full-cube 几何。pickle 因共享对象获得很高复用。它确实传了具体模拟世界的数据，但没有测实际观察世界的复杂方块形状、事实时间戳差异、变化率或正式窗口体积。报告已声明不是服务器采集；后续引用字节数与成本时应一并保留这层限制。
- **ready 时间取在 handle 创建处。** `handle.ready_ns` 在 `_receive()` 把 handle 发布到 `handles` 字典前记录。因此“提交至可信可用”严格说是提交至完成检查并创建 handle，未单独计入其后的指标组装和发布开销。下一 tick 的真实消费由记录证明；本批超过 8 tick 的领先不能当作任意场景的交付保证。
- **只验证固定预测状态，没有入口范围。** 三种平台、朝南、精确预计位置与速度；没有 ±0.05 偏差或新地形。UNKNOWN 的零推进来自已知世界和证明门禁，不代表未知地形探索已验收。
- **并发边界仍是固定作业。** 接纳与控制确实同进程争用 GIL，worker 实际并行运行。但没有取消或复用 job id，也没有在 receiver 正校验时撤销 pending；`forget()` 本身没有跨阶段 token 防止这种未来竞态。当前固定调用顺序不触发，不作为本批阻塞，也不能扩写成正式撤销／替换协议。
- **精确对象的信任依赖发布路径。** HotHandle 由 receiver 构造，控制只取 pipeline 发布对象。临时 `TrustedRegistry` 没有保留供控制查询的长期登记表；这依赖不可变对象契约和当前单一路径，不能扩写为接纳任意外部 handle 的安全边界。
- Fabric、正式 Runtime／Session、真实客户端队列及正式控制窗口均未运行。本试验不改变 D101 M0 的 P99 48.6079 ms、最大 57.5597 ms 和整体失败，也不改变 D098 P3。

## 可复跑核验

在仓库根目录运行，命令只重算与审查，不采集计时：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d103-continuous-prepared-proof/spike.py --recompute --output .tmp/d103-continuous-prepared-proof/formal
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d103-continuous-prepared-proof/review_recheck.py
```

两条最终退出码均为 0。第二条拦截原 verifier 的唯一写盘，使 `formal/verification.json` 不被覆盖；重新检查清单中的 283 个原文件、完整回放、原 D101 manifest、预跑源码和输入错配复现。封存结果在 [review_checks.json](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/attempt-before-input-binding/review_checks.json)，代码在 [review_recheck.py](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/attempt-before-input-binding/review_recheck.py)。原文件检查前后字节一致。上列命令是在第一批尚未归档时执行；第二批建立后应使用其定向复审入口，不能用新源码重算旧批并忽略源码身份差异。

审查脚本最初误把 D102 同名 `spike.py` 当成预跑主源码，在全部 proof／tick 回放通过后触发自身路径断言；改为精确父目录后重跑通过。这是审查工具的修正，没有改变受审源码或正式证据。

---

## 第二批定向复审：APPROVED

2026-10-12。**当前结论：APPROVED。第一批唯一阻塞项已关闭。** 本结论只适用于第二批的固定地形、精确预测入口、Windows 模拟连续两跳试验。上文第一批的失败发现和原性能数字保留，不改写成通过。

第二批源码 SHA-256 为 `07d3ede3314b4277a00096245d7b6ea2cf6ff6aa0d627bfd5cdceef9be2e7a8d`，与 `formal/spike-source.py` 和 raw metadata 一致；raw 为 `52e60b61914dd643e91163b1b17583d5d1bfb1fe784614101148933975345731`；报告为 `2fbc835a91d25cd71895c1de160361504fda12bb8c2ce6400bb5932c1b5659b9`。

- **绑定修正成立。** [make_job](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/formal/spike-source.py:156) 在父侧保留精确的 `expected_inputs`，并在提交前计算 `expected_input_digest`。[receiver](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/formal/spike-source.py:247) 在调用 `register()` 前比较完整输入元组及摘要；返回 proof 自己内部一致不再足以接纳。
- **原复现现在正确拒绝。** 审查重新构造同 request 的 31 条合法输入证明，再送入实际 `_receive()`。结果是 typed `ReuseReason.INPUTS`、无 handle、无 metrics 发布。额外监视 `register()`，调用次数为 **0**；身体推进为 **0**。正式新增负例也由真实 worker 完成原 scanner 的 `VERIFIED_CANDIDATE` 后拒绝，未用无效证明代替原缺口。
- **新增成本没有漏计。** 父侧输入摘要生成在 `request_digest_ns` 内；receiver 精确比较的 `input_binding_ns` 位于整体 `parent_receive_validate_register_ns` 和提交至注册耗时内。对 366 条作业记录逐条核对：整体 receiver 时间不小于消息摘要、反序列化、输入比较及完整校验之和。新增输入比较 A 的 P95／P99／最大为 **0.0061／0.0063／0.0063 ms**，B 为 **0.0062／0.0064／0.0080 ms**。该分项在 receiver 执行，不进入控制线程。
- **第二批统计成立。** A、B 各 120／120 完成两跳；六种平台／起点组合各 20 场。每场调度仍递增 50 ms，B 仍是一项同工作量前序作业。实际第二段输入均在第一跳落地后 1 tick 应用，直到第二次实际起跳前完整停车为 0。热决定 P95／P99／最大分别为 A **0.2073／0.2350／0.3227 ms**、B **0.2032／0.2305／0.2850 ms**，deadline miss 均为 0。
- **超时、安全与退出成立。** 六场 OVERDUE 都只走既有安全 Walk×1＋Neutral×16，停在中间平台，没有第二跳。五项负例均拒绝且 0 推进；五条生命周期路径均无 worker、receiver 或活子进程泄漏。原有状态、输入、伤害、依赖和 owner 回放检查保持通过。
- **证据重新核对。** 13,685 行 raw 与 summary 逐字段一致；738 份证明、12,567 tick、492 条首条回执、972 个关键节点完整核对，fresh scan 为 178,022 物理步。独立合法替换输入再扫描 212 步。检查开始时清单的 556 个文件均匹配，旧批次归档清单的 259 个文件逐项保持原字节，原 D101 manifest 一致。此次复审新增性能样本为 0，原 verification 写盘已被拦截。
- **报告范围已说明。** 当前报告明确快照只有固定 7×58×25 窗口和共享的两种 CellFact；注册完成时间略早于字典发布。它保留 1 tick 交接、三种固定平台、朝南、精确入口和未运行 Fabric／正式链的边界。上文其他非阻塞限制继续有效，D101 M0 的整体失败不变。

第二批可复跑命令：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d103-continuous-prepared-proof/spike.py --recompute --output .tmp/d103-continuous-prepared-proof/formal
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python .tmp/d103-continuous-prepared-proof/review_attempt2.py
```

两条退出码均为 0。第二条复用封存的独立审查逻辑，只把原缺口的预期改成 typed 拒绝，并增加登记调用监视、输入比较计时覆盖及归档检查。输出见 [review_attempt2_checks.json](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/review_attempt2_checks.json)，脚本见 [review_attempt2.py](D:/My_project/mc_ai/.tmp/d103-continuous-prepared-proof/review_attempt2.py)。Git tracked 工作区仍干净，无提交、push 或新 Fabric。
