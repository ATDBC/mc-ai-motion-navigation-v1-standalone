# 客户端首效窗口与安全默认输入 v2

状态：D122-0—D122-3 已实现并通过组件、完整 Windows、独立 standalone 和 Java 门禁；D122-4 Fabric 未运行。依据：[D122 决定](../decisions/0122-enforce-first-effect-window-and-prearm-safe-default.md)。

## 1. 数据只增加两项

继续使用现有 episode、request sequence 和 movement tick 身份。对协商后的 v2 action，增加两个可空字段：

| 字段 | 内容 | 约束 |
|---|---|---|
| `first_effect_window` | `earliest_tick`、`latest_tick` | 非负闭区间；只约束第一次真实采样 |
| `safe_default` | `activation_tick`、`movement`、`valid_for_ticks`、`wall_budget_ns` | 绝对激活 tick 和一个完整 movement 快照；tick 数沿用现有 1—20 上限；墙钟额度至少为 `valid_for_ticks × 50 ms`、不超过既有 30 秒上限；不含 look、operation 或另一个默认 |

Python action snapshot 的 v2 表示必须保留上述字段，arbiter → Runtime → backend → wire 不能丢失。实现可以复用现有 Python snapshot 类并按协商版本编码，不另建一条控制管线。Python 内部 proof／registry 绑定默认对应的 owner、出口状态、输入、duration、tick 和依赖；完整证明和摘要不进入 Java。

本阶段的窗口化请求只支持 movement 和 look，`operation` 必须为空。一次性 operation 的执行时点还没有接入 player-tick 采样边界；客户端收到这种组合时，以 `windowed_operation_unsupported` 拒绝整条请求，不能先执行 operation，也不能静默丢掉它。普通非窗口请求的 operation 语义保持不变。

`safe_default.activation_tick` 绑定证明的精确出口后第一拍，本场为 `L+1`，不能因父输入提前耗尽就提前执行。安装时必须已经有对应的出口证明，且父请求 wall deadline 足以覆盖到激活点的名义时间和默认 wall budget；额度不足则 arm 失败。默认 wall budget 是相对时长，在默认首次激活时由客户端本地时钟起算。不能把 Python 单调时钟的绝对值传给 JVM。

激活点必须满足同一条精确关系：普通请求的 `activation_tick = 下一次真实采样 tick + valid_for_ticks`；窗口请求只有在首效窗口为单一 tick 时才能携带默认，并要求 `activation_tick = 实际首效 tick + valid_for_ticks`。客户端在排队时检查一次，在真实首效拍再检查一次。关系不成立时拒绝后继并保留旧默认。极大 tick 或额度运算使用有界结果，不能让整数溢出变成会话异常。

`valid_for_ticks` 是必须真实采样的证明时长，墙钟额度不能缩短它。已成功 arm 后，即使线程暂停使 wall budget 或父响应期限先到，也必须完成声明的默认采样数。missed／未成功激活的 future 响应须持有到默认真实采样完成，不能先返回普通响应，再由下一条普通请求清掉默认。没有 movement tick 时物理也不推进；外层若硬终止，必须走既有 reset／许可撤销边界，不能作为普通控制继续。这样不会把“至少潜行 8 拍”悄悄变成“机器慢时只潜行 2 拍”。

默认在客户端采样 `L` 之前随第一跳最后一条 incumbent 请求安装，`L` 的响应以 `fallback_armed` 确认。该请求正常 lease 覆盖至 `L`，默认从 `L+1` 激活，此后没有普通输入续租。普通请求、cancel、reset 和 episode 切换清掉整份旧安排，旧默认不能跨这些请求存活或以后复活。本阶段不增加跨普通请求保持默认的语义。

默认持续期间，每个采样仍带父请求的 episode／sequence。默认不重复父请求的一次性 look 或 operation。默认耗尽后转为现有中性输入并报告 `default_exhausted`；上层只有已观察到证明终态时才能把身体责任记为解决。失去物理许可或硬重置属于既有强制释放边界，不承诺继续持键。

## 2. 协商发生在会话开始

部署握手增加 action schema 的显式选择和客户端确认。v2 会话使用 `mc2p.client_action.v2`，v1 继续严格按旧键集合解码。Runtime/backend 保存本次协商结果，不能仅凭 Python 常量声称客户端支持 v2。

双方都支持时才启用 v2；客户端只支持 v1 时，可以运行普通 v1 行为，但需要窗口或默认的请求必须在发送前以有类型的“不支持能力”拒绝。声明不一致、确认丢失和中途降级也拒绝。不能先发新字段，再靠客户端异常猜兼容性。

具体握手载体沿用 Fabric 部署会话和现有 envelope；新增字段要同步修改严格解码键集合、Python handshake 测试和 Java 部署测试。CraftGround 正式 backend 若尚未完成协商，继续声明 v1，拒绝新能力，不能替它伪造确认。

## 3. 客户端只保留三份有界状态

- 当前请求和它原有的 lease；
- 一个待生效请求；
- 最后一条 incumbent 请求携带的一个安全默认及其剩余额度。

这些状态由现有客户端线程独占，不加跨线程锁、租约层或第二本输入账本。选择发生在 `prepareForPlayerTick` 内、原版读取 sneak 之前；`Input.tick` 只消费本拍已经选好的输入，并且只扣一次 tick。下一次真实 movement 采样按下表推进：

| 条件 | 采样前动作 | 实际输入／回执 |
|---|---|---|
| 请求早于 `earliest_tick` | 放入唯一 pending 槽；不执行 look／operation | 当前 lease 或已激活默认继续；后继 `window_pending` |
| pending 到达窗口内，身份、许可和 wall budget 有效 | 原子激活后继，清掉旧默认及 pending | 后继输入；`window_applied`，首效 tick 明确 |
| 请求／pending 已超过 `latest_tick` | 拒绝后继并清 pending；不修改当前／默认 | `window_missed`；继续当前或默认 |
| 到达默认绝对激活 tick，父动作 lease 已结束且后继未激活 | 从本拍开始计默认 tick／wall budget | 默认输入；`default_active` |
| 默认已完整采样声明的 tick 数 | 清默认、报告一次耗尽；单独 wall 到期不能提前清默认 | 中性；`default_exhausted` |
| 合法普通请求通过既有检查 | 沿用原替换语义，清旧当前、pending 和默认，再安装新请求 | 新请求的合法输入；既有回执；旧默认不得复活 |

同一拍同时发生“后继可激活”和“默认到达激活点”时，先检查后继；成功就直接采样后继，否则采样默认。不能先清默认再尝试接纳。pending 首效之前不产生父请求取消、视角增量或一次性操作。早于激活点的当前 lease 耗尽不能冒充默认到期；父动作仍由现有证明和 input-loss 责任处理，不能计作 D122 已覆盖的后继传输场景。

每拍选择时必须同步覆盖 sneak 投影，不能只在 sneak=true 时置位。否则上一拍潜行、下一拍起跳时，原版在 `Input.tick` 之前仍可能读到旧 sneak。选择、原版读取、输入消费和 lease 扣减的相对顺序由 D122-0 的只读时序 probe 先核实。

更高 sequence 可以替换 pending 槽，但不能仅因到达就替换当前／默认。更低或重复 sequence 按旧规则拒绝；窗口拒绝也记录该 sequence 已处理。窗口只约束首效：一旦在窗口内开始，后续 lease 按原 tick 数继续，不因下一 tick 已离开首效窗口而中断。

pending 接纳时只消费一次 sequence，并核对一次 observation 身份。激活不是重新接收请求，不得再次调用会把已接纳请求判为 duplicate／stale 的完整 `validate`。激活时只复核：它仍是当前登记的 pending，episode 未切换且未 reset，控制许可有效，wall deadline 未过，首效窗口有效。排队后观察生成或响应推进，不撤销已经完成的接纳；若显式 reset、替换或取消已清掉 pending，则不能激活。

客户端必须先消费 episode／sequence／observation 接纳，再检查首效窗口、默认时序和窗口 operation 等语义。语义不合法时不安装 lease 或 pending，但该 sequence 已处理；用同一 sequence 改好 payload 后重试仍返回 `duplicate_request`。

拒绝清理按实际成员身份执行，不能根据请求“带有 safe_default 字段”推断它已经安装了默认。若请求在 `stageWindowed` 前因额度、屏幕、世界或玩家状态被拒绝，它没有拥有任何时序成员，父 fallback 保持不变。若请求已经安装 pending，只删除该 pending；只有确实由该 sequence 安装的 fallback 才能清除。

同一 sequence 也不能充当清理凭据。重复、stale 或 episode 不匹配的请求，可能碰巧与已存在成员使用相同数字；它们本次没有安装任何时序成员，因此不能删除旧 pending 或 fallback。Executor 只维护一个单次 `execute` 的事务标记：调用开始为 false，`stageWindowed` 或 `armDefault` 成功后才置为 true，并在 `finally` 和 reset 中清零。这个标记只授权本次同步拒绝做清理，不拥有 pending、fallback 或任何跨 tick 生命周期。

窗口后继在 stage 前被拒绝时，如果父 fallback 尚未完成，拒绝响应继续保持未就绪，直到父默认完整采样并报告 `default_exhausted`。否则 Python 可能在收到拒绝后提交普通请求，提前清掉父默认。screen conflict、no world、player dead 和 permission revoke 走 typed `HARD_RELEASE`，依赖父身份的 `disallowed` 或外层硬终止，不强行等待 fallback。生命周期判断使用 `RejectionDisposition`，不能读取 reason 文案。

## 4. 撤销和结束

| 事件 | pending | 当前和默认 |
|---|---|---|
| 客户端收到合法 cancel | 清空旧 pending | 清整份旧安排；Runtime 不得在承诺中途发送这种 destructive cancel |
| Runtime 收到普通任务取消或换目标 | 沿用 D117，先停未激活工作 | 已承诺 owner 先收尾；安全结束后再向客户端撤销，不能提前清掉默认 |
| 更高 sequence 在窗口内成功接管 | 删除旧 pending | 原子替换当前和默认；新默认来自新请求 |
| 更高 sequence 被拒绝、错过窗口或仍早到 | 只更新待处理身份 | 当前／默认不变 |
| 更高 sequence 的合法普通更新 | 清旧 pending | 清整份旧安排并安装新请求，不保留旧默认 |
| 后继 transport 失联 | 到期删除 pending | 已预排默认照常激活并有界结束 |
| reset／episode 切换 | 清空 | 硬重置全部状态，旧 episode 永不再采样 |
| Runtime close／撤销身体许可 | 清空待执行工作 | 普通 close 必须先按现有 owner 收尾；强制许可撤销直接清空并明确报告，不冒充安全落地 |

客户端撤销物理许可时，如果还有 pending，先为它记录 `window_missed`；顶层回执使用 `rejected／permission_revoked`。已经激活的 current／default 仍在同一拍留下父身份的 `disallowed` 真实采样。Python 账本用这两项事实分别结束 pending 和父输入责任。reset 和断连仍是硬终止，不伪造这些普通运行事实。

这里不保证父请求自身迟到或丢失。它仍属于现有动作的 input-loss 责任；D122 只保证最后父请求已到位后的后继传输行为，不把它登记成承诺前可安全放弃的负例。父请求 wall 额度不足不能成功 arm，提前过期也不能触发位置不匹配的出口默认。

## 5. 回执与监视

新增 typed 状态为 `fallback_armed`、`window_pending`、`window_applied`、`window_missed`、`default_active` 和 `default_exhausted`。沿用现有 receipt／input application 通道承载，版本显式协商；不新增旁路。状态需包含实际 movement tick 和请求身份。armed 只是预排确认，不产生按键应用；拒绝回执可在本拍无后继输入时出现，默认采样则带父请求身份，三者不能混为一个应用事实。

Python 账本根据 `window_applied` 确认应用，根据 `window_missed` 确认未应用。它继续对真实输入事实做逐 tick 便宜比较；不因默认已预排就停止观察。过期后继的跳键在物理 trace 中必须为零。默认中任意 look／operation 重复均为违规。

`default_exhausted` 必须发生在声明时长之后，且账本已收到整个默认区间的真实采样，才可结束父默认责任。合法新请求的真实输入采样可以结束旧默认；仅收到普通请求的 `executed` 不够。取消则由已提交的 cancel action 和同身份 v4 执行回执确认：它清掉整份旧安排，内部账本使用 `CANCELLED`，不能伪装成 `REJECTED`。账本按现有事件和真实采样维护 current／pending／default 的明确成员，取消只结束这些成员和取消请求自身；不属于当前安排的旧 `AMBIGUOUS` 历史保持原状。这不是新增 wire timing status，也不证明身体已经安全。

更高 sequence 被确认放入 pending 时，旧 pending 已被客户端取消，账本当拍用内部 `CANCELLED` 结束旧记录，不能等一条永远不会出现的输入采样。新 pending 随后 missed 不会恢复旧 pending。默认耗尽同拍若仍报告父身份的中性采样，它只是结束后的中性状态，不能算另一条父主输入，也不能重新登记默认。

## 6. 本阶段的固定时间线

`L` 是第一次 authoritative 落地 post-state 对应的 movement tick，不是 Python 循环编号。第一跳最后一条 incumbent 请求在采样 `L` 之前已安装默认，正常 lease 覆盖到 `L`；`L` 响应确认 `fallback_armed`，默认绝对激活点为 `L+1`。冻结轨迹的 sequence 25 在 tick 26=`L` 应用，不能据此声称最后可放弃点之前已经 armed。其后只允许一个窗口化后继等待或激活；不靠普通续租维持默认。

- 后继在 `L+1` 采样前可用：校验通过，采样第二跳，默认不被采样。
- 后继在 `L+2` 才到：`L+1` 已潜行；`L+2` 物理前报告 `window_missed`，仍潜行。
- 后继永不到：默认从 `L+1` 连续执行证明要求的至少 8 tick，停在中间平台。

CalculatorBackend 必须先过与 Java 同一张转换表，再调用 physics step。Task 5 必须把“排队”、“接纳”和“实际采样”分开注入，不能把 worker 返回晚当作 transport 晚。

### 6.1 一问一答时如何继续推进

沿用现有一次请求对应一次响应的正式循环，不为测试增加同步观察通道：

- future 后继到达后，`receiptReady` 保持 false，客户端自主推进至窗口并完成输入采样后，再返回该请求的响应。pending 中间状态写入现有客户端 trace，不冒充已经交付给 Python 的独立响应。
- late／missed 后继不立即释放响应。客户端继续已经预排的默认，直到默认有界结束，再返回带最终观察的响应；期间不允许普通下一帧抢先清掉默认。
- never 场没有后继请求可响应。用客户端 trace 记录默认逐拍执行；需要取得最终观察时，只能沿现有循环延后释放该次观察，明确其实际 tick，不能伪造 Python 每 tick 收到了观察。

未送达与“请求到了但 missed”的注入要分开。客户端在等待后继传输期间能否自主推进，是 D122-0 的首项前提；若现正式循环在这里冻结 movement tick，就停止，不能仅修改 CalculatorBackend 造出通过结果。

## 7. 性能和扩展边界

客户端每拍只检查一个 pending、当前 lease 和一个默认，工作量为常数。现有 16 KiB action payload 和 2 MiB worker 消息上限不变。证明摘要只在后台或准备阶段计算，`L` 帧不重算摘要、不搜候选。

等待窗口或保持 missed 响应到默认结束，是合同允许的等待，不是同步计算。分别报告客户端持有响应的 tick／墙钟时长、Python 同步提交计算、控制准备计算和端到端响应时长。默认持续至少 8 tick 所产生的合法等待，不计入 Python 提交 <50 ms 或控制准备 8／15／30 ms；这些门槛只测各自同步计算区间。默认未完成时，wall 或父期限到期不能交还普通响应；若外层硬终止，只能走 reset／许可撤销边界，不再作为普通控制继续。默认按真实 tick 数有界；无 tick 时世界不推进。端到端报告必须保留这笔等待。

本规格不定义通用输入队列、多默认、wire generation、连续状态包络或产品路线优化。新动作仍复用动作规格与 D117 owner 交接，不能为两跳另造一个 Session 状态机。
