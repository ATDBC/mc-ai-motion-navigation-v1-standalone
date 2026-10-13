# D102 prepared proof 独立审查

2026-10-11。结论：**CHANGES_REQUESTED**。

固定单跳的性能方向有实测支持：父侧完成准备、完整检查和登记后，动作开始至首条回执的热路径很短；完整证明和父侧完整检查均不能直接放入现行控制线程。连续两跳没有验证。当前还不能签署“完整 ruleset 绑定和接纳语义等价”，因为父侧漏掉了 proof 自身的物理规则比较。报告还应写清 107.1387 ms 所覆盖的作业边界。

本审查读取了根 AGENTS、D099／D100／D101、D101 阶段计划及 M0 验收，再检查代码、原始记录、摘要和报告。未修改已跟踪文件，未提交，未改写原始计时。受审 spike 的 SHA-256 为 `e9796f7191d0d128c80781e645ef90672986351b82085d45b6968656b10a376d`。

**阻塞发现**

1. **[P2] 父侧接纳漏比 `proof.ruleset`，比原验证器少了一项前提。**

   [spike.py:156](D:/My_project/mc_ai/.tmp/d102-prepared-proof/spike.py:156) 比较 `b.ruleset == world.ruleset`，也比较 proof 内的 request、inputs、boundary inputs 和依赖；没有比较 `b.proof.ruleset == b.ruleset`。重新序列化及计算摘要只确认内容与封存 bytes 一致，不能补上这个字段之间的关系。原 [validate_commitment:405](D:/My_project/mc_ai/experiments/motion_navigation/trajectory_proto/commitment.py:405) 明确比较 `world.ruleset != proof.ruleset`。

   已通过普通 `dataclass.replace` 复现：只改变 `proof.ruleset.ruleset_id`，按现有封存算法重算 proof 摘要、binding bytes 和 bundle 摘要，保留合法 bundle 的其他字段。父侧 `admit()` 登记成功，`guarded_hot()` 接受并推进 **1 tick**；原 `validate_commitment()` 对同一对象返回 **STALE／request_identity**。复现没有使用 `object.__setattr__`、任意内存修改或外部 pickle。具体代码见 [review_recheck.py:116](D:/My_project/mc_ai/.tmp/d102-prepared-proof/review_recheck.py:116)，结果见 [review_checks.json](D:/My_project/mc_ai/.tmp/d102-prepared-proof/review_checks.json)。

   原 scanner 正常返回的 400 份证明没有这个错误，因此本发现不推翻其计时。它阻塞的是“父侧完整绑定已保留原校验语义”的结论。最小修正是在登记前比较 proof、bundle 和当前世界的规则，并增加“重新封存、但规则字段互相矛盾”的接纳负例。原计时与源码应保留，后续核验明确标注修正版本，不能把旧计时直接改标为新代码测量。

2. **[P2] “没有省略序列化、传输”和“提前 3 tick”的范围应写得更具体。**

   [worker_main:298](D:/My_project/mc_ai/.tmp/d102-prepared-proof/spike.py:298) 在子进程创建固定 `jump_gap_1` 世界，随后所有作业复用它。[run:347](D:/My_project/mc_ai/.tmp/d102-prepared-proof/spike.py:347) 只提交 serial、request、inputs、generation 和 owner；参数 `world` 只用于父侧接纳。因而本批没有每作业世界快照采集、封存、序列化、传输和子进程解包成本。200 项逐项提交并等待完成，也没有前序作业积压和持续控制负载的竞争。

   `ceil(107.1387 / 50) = 3` 的算术正确，报告已经保留“本批单作业、非硬保证”的限制。不过它只说明：**对已预热、固定世界、没有积压的本批作业，按完整 50 ms 提前量计，3 tick 才覆盖观测最大值。** 它没有建立连续动作的通用提前量，更没有建立两跳可连续完成的结论。请在报告后台性能段直接列明上述未计成本；无需为这个说明另开大矩阵。

**逐项判断**

| 审查项 | 判断与依据 |
|---|---|
| request／inputs／proof／world／dependencies／ruleset／ledger／generation 绑定 | request、inputs、边界事实、依赖、ledger、generation／owner 的比较可在代码中追溯。父侧确实重算完整 proof bytes 和摘要，未只相信自报 hash；旧摘要的 proof 状态、payload、ruleset 修改也被审查补测拒绝。ruleset 的交叉比较缺口见阻塞项 1。SHA-256 仍然只是完整性检查，可信 scanner 是语义来源；没有要求它认证外部恶意证明。 |
| hot 计时是否完整 | 对报告声明的“已登记对象开始动作至首条回执”范围成立。计时从 counter 和运行参数处理前开始，覆盖登记对象检查、封存 bytes SHA、当前依赖、generation／tracker／账本、1 或 2 步物理推进、首条回执、剪枝及结果构造。准备、接纳、patch 装设和 fixture 在外，报告已区分。它不是完整落地或下一动作交接的计时。 |
| scan／search 阻断 | `guarded_hot` 阻断 spike 与 commitment 的 scan 入口，以及冻结输入搜索入口。结合 `start_hot` 调用图和 physics step 计数，当前热路径确实没有搜索或完整证明。拦截器没有覆盖所有未来可能新增的搜索别名，不能代替以后代码变更的检查。 |
| fresh scan 400／400 | 原始记录一致，使用独立 counter 重新执行同一 scanner，再比较完整 dataclass 及主要部分，足以支持此次包装没有改变这一个固定输入的正常证明。400 项改变的是 request／ledger／sequence 身份，不是 400 种身体、世界或输入。它是同一算法重新计算，不是独立物理实现，也未覆盖有矛盾的接纳对象。审查另行重建 4 份世界、请求及边界事实后，结果与原记录一致。 |
| 24 类负例 | 原记录确有 24 个不同名称，均拒绝且 0 身体推进。列出的请求、入口、目标、账本、世界、依赖、输入、generation、owner 等修改与代码相符。proof states 和 payload 的例子经 `dataclass.replace` 形成新对象，走 `UNREGISTERED_BUNDLE`；它们验证热路径的精确对象规则，不能代表父侧接纳所有篡改类别。规则交叉矛盾未覆盖。 |
| worker 计时拆分 | `received_ns` 从父进程提交前至收到结果；含实际请求传递、worker 准备、结果 pickle 与传递。父侧摘要、解包、完整检查、登记另有分项，`ready_ns` 含它们。没有把两进程绝对时钟相减。冷启动 333.3806 ms 独立记录；前 10 项预热不进入后 200 项统计。分项包含关系已从每条 raw 记录核对。世界快照及积压边界见阻塞项 2。 |
| close 与活进程 | 对实际注入的四条路径可信：正常结束；ready 前明确回传 error；父侧在 worker 空闲时异常；worker 捕获准备异常并回传 error。finally 调用 close，记录真实 `is_alive`、exitcode 和 `active_children`；四条均为退出码 0、无存活子进程。不能扩写成任意崩溃或在途退出均已验证。 |
| 总结论 | 可以保留“固定单跳、已完成登记的热路径在性能上可行；后台准备及父侧完整检查应离开控制 tick；持续 20 Hz 下如何交付尚未证明；连续两跳尚未验证”。阻塞项关闭前，不签署所有接纳前提均完整保留。原 D101 M0 未通过的结论不变。 |

**非阻塞发现与后续边界**

- [run:351](D:/My_project/mc_ai/.tmp/d102-prepared-proof/spike.py:351) 的结果等待没有作业期限或子进程退出检查。worker 若硬退出而未回传 error，调用者可能一直等待，外层 finally 也不会到达。[close:404](D:/My_project/mc_ai/.tmp/d102-prepared-proof/spike.py:404) 的 queue `join_thread()` 也没有期限。当前四条受控路径没有触发这些条件；本试验无需因此扩成完整 worker 生命周期阶段，但不能把它作为正式有界交付能力签署。
- “父侧接纳在主进程提前做完”不等于已经证明它不影响控制线程。父侧接纳 P99／最大 34.0061／34.4804 ms 超过 15／30 ms；worker 准备也达到约 79 ms。报告承认没有独立接纳线程和 GIL 竞争试验，这个限制合理。后续应根据实际接口决定在哪个执行单元完成检查，不应把“移到一个线程”直接当作已验证解法。
- 24 类负例的旧摘要篡改和精确对象拒绝是有效证据；报告应保持两类检查的区分。新对象在接纳前合法，登记后必须按精确对象消费，这两道门的测试不能互相替代。

**本次核验记录**

- `spike.py --recompute` 退出码 0，完整 summary 逐字段一致，没有新增性能样本。
- 审查自己的 [review_recheck.py](D:/My_project/mc_ai/.tmp/d102-prepared-proof/review_recheck.py) 退出码 0：原始 2,832 行、全部主分位数和 worker 分项重算一致；24 项拒绝及四条退出记录一致；测量源码和原 D101 五份基础证据哈希一致。
- 独立重建 serial 1、2、199、400 的请求、fixture 和边界事实，重新扫描 1,628 physics steps；proof 各部分和原热路径首条应用后的状态一致。
- 额外接纳核验拒绝三种沿用旧摘要的修改，并复现上面的 ruleset 交叉绑定缺口。补测不计入原 24 项，不计入原 400 个样本，没有保存或替换任何性能数据。
- 本审查只新增 `review.md`、`review_recheck.py` 和 `review_checks.json`。`raw.jsonl.gz`、`summary.json`、`spike.py`、`verification.json` 在核验前后字节未变。报告文案由主任务继续维护；原审查脚本执行期间也确认当时的 `report.md` 字节未变。

---

**第三轮定向复审（2026-10-11）**

结论：**CHANGES_REQUESTED，仅剩报告边界说明。规则绑定缺陷已关闭。** 本段对应 spike SHA-256 `43189806cc23e1583d93090d166da07b334594134ad294cf7fe756d8a7e67c5a`，报告 SHA-256 `03e9f01ca71f1b220815a958c3e22324a2ac086396b75d86094eb3187c6b2b03`。上文保留第二轮当时的发现，不把旧失败改写成通过。

- **阻塞项 1 关闭。** [spike.py:161](D:/My_project/mc_ai/.tmp/d102-prepared-proof/spike.py:161) 在登记前要求 `proof.ruleset == bundle.ruleset == world.ruleset`，错配抛出携带 `ReuseReason.RULESET` 的 `AdmissionViolation`。热检查也保留 proof 与 bundle 的关系。修正没有移动原有扫描、准备或接纳的计时边界。
- **新增负例有效。** `proof_ruleset_resealed_admission` 用普通冻结对象副本修改 proof 内部规则，并重新计算全部摘要。第三轮在接纳阶段 typed 拒绝，登记对象为空、身体推进 0；原验证器仍为 `STALE`。原始负例增加到 25／25 拒绝，总身体推进 0。审查者重新执行对应检查，结果相同。
- **产物和统计一致。** `attempt3-manifest.json` 所列 current 与 `attempt3/` 的十份文件逐一匹配；清单副本一致；所列 `attempt2/` 的六份历史文件哈希一致。第三轮 raw 共 2,833 行，summary、verification、源码哈希与原始记录对应。`spike.py --recompute` 退出码 0，完整 summary 一致。审查者又独立复扫 serial 1、2、199、400，共 1,628 physics steps，证明和首条应用状态一致。没有新增性能样本，也没有改写这些产物。
- **阻塞项 2 仍打开。** 受审报告第 37 行仍称“测量没有省略序列化、传输或父侧接纳”，第 54 行仍从 107.5297／108.5532 ms 写出“连续动作需要约 2–3 tick”。该版本没有直接说明每作业的世界快照采集、序列化和传输未测，也没有说明无队列积压。虽然它另行保留了 GIL／20 Hz 和单作业限制，仍未完成本次要求的成本边界说明。

关闭最后一项只需要修改报告：明确这些耗时仅覆盖已预热 worker、子进程预建固定 fixture、顺序单作业；世界快照创建／序列化／传输、队列积压和持续控制负载竞争均未计入。`ceil(108.5532 / 50) = 3` 只表示三个完整 tick 提前量覆盖本批观测最大值，不是连续动作的通用提前量。报告修订后同步 `attempt3/` 和清单即可，不需要新增计时。

本轮只读核验入口为 [review_followup_check.py](D:/My_project/mc_ai/.tmp/d102-prepared-proof/review_followup_check.py)，退出码 0。它拦截已有独立检查脚本的写盘，重新核对结果而不覆盖已冻结证据。先前列出的 worker 硬退出、有界等待和持续控制负载边界保持非阻塞；连续两跳仍未验证。

---

**第三轮最终复审（2026-10-11）：APPROVED。** 两项阻塞发现均已关闭。此结论适用于当前固定单跳、提前完成准备与父侧登记的可丢弃试验，不代表 D101 M0、连续两跳或正式控制链通过。

报告当前 SHA-256 为 `948d7ce6f82f8d9d2b521a63567a9e505843af185a970b134023d2db300d9799`，与 `verification.json` 的 `report_sha256`、`attempt3-manifest.json` 和 `attempt3/report.md` 一致。报告第 37／39／56 行已明确：

- worker 在子进程预建固定 fixture，未测真实世界快照的构建、序列化和传输；
- 作业顺序提交，没有队列积压，未叠加持续 20 Hz 控制负载或控制／接纳之间的 GIL 竞争；
- 3 tick 只覆盖本批已预热、固定 fixture、无竞争条件下的 108.5532 ms 观测最大值，不能用作连续动作的硬期限。

这关闭了原阻塞项 2。原阻塞项 1 的三向 ruleset 绑定及重新封存错配拒绝仍保持通过。再次运行只读 `review_followup_check.py`，退出码 0：current／attempt3 清单一致，attempt2 历史哈希不变；2,833 行、25 项负例、typed `RULESET` 拒绝和 0 身体推进均一致。检查同时重新核对四份证明与首条应用状态，没有保存或替换性能数据，新增计时样本为 0。

第三轮计时源码、raw、summary 和独立核验结果的哈希与上次定向复审相同。允许保留的结论是：已登记 bundle 的动作开始热路径在该固定单跳下满足性能门槛；完整准备和父侧完整检查应离开控制 tick；真实负载下的交付期限、完整 worker 异常生命周期及连续两跳仍未验证。先前非阻塞边界不变，原 D101 M0 未通过的记录不变。
