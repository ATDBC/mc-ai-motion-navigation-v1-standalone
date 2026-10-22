# D124 实施计划：恢复证明依赖分层

日期：2026-10-21。状态：**COMPLETED／SIGNED**。D124-0—D124-3、完整 Windows 源码树／standalone 正逆分片、Java 和最终导出均已完成。本阶段没有运行 Minecraft／Fabric。

依据：[决定](../decisions/0124-separate-broad-physics-reads-from-proof-dependencies.md)、[架构](../architecture/proof-dependency-projection-v1.md)、[验收](../acceptance/d124-proof-dependency-projection.md)。先读 [D123 验收](../acceptance/d123-proof-dependency-and-transport-loss.md)，不从旧工作日志恢复阶段状态。

## 1. 交付范围

让计算器宽查询、proof 必要依赖和 actor 缺失事实使用不同含义。先证明完整方块屏障，再只在源查询收窄依赖，最后核对在线／prepared 恢复证明与正式门禁。

待验证目标是证明每方向 9 个有完整支撑的下层格不再属于必要依赖，保留另外 3 个真实必要格。58 格只是假设结果，不是允许硬编码的数量。即使达到该结果，缺少剩余 3 格的 actor 事实仍须拒绝。

不实现观察站位、通用校准、D123-2、transport、Java、Session、planner 或新动作，不运行 Minecraft／Fabric。原版离线规则检查只用于验证模型，不向 actor 提供隐藏事实。

## 2. 文件边界

| 范围 | 预计文件及职责 |
|---|---|
| 源查询 | `geometry.py`、`physics_adapter.py`：候选范围、完整屏障、查询用途和必要依赖 |
| 物理与材料 | `physics_1_21.py`；必要时 `physics_types.py`、`block_motion_traits.py`：材料触发事实、typed 查询结果；不支持新材料运动 |
| 恢复证明 | `prepared_motion_proof.py`、必要时 `motion_solver.py`：支撑及尾段汇总，禁止把宽读重新加入 |
| 正式门禁 | `scripts/d122_fabric_timing.py` 的新证明清单调用点：消费源查询结果，不新增 gate 过滤 |
| 新检查 | `scripts/d124_proof_dependency_probe.py`、`tests/motion_nav/test_d124_proof_dependency_projection.py`；必要时增加同范围 mutation 检查 |
| 相邻检查 | B09-R、几何、D116、D121、D122 和 D123 对应测试；历史证据检查与现行语义断言分清 |
| 发布与文档 | standalone 清单／必需文件检查、四类文档和入口；只选择本阶段实际产生的紧凑证据 |

`world_model.py` 优先保持事实与版本实现不变，仅在合同确需澄清时最小修改。不得全局收紧 `BlockGeometry` 来抹掉 D123 合成反例。需要新的长期状态、第二份事实源或跨层大改时停止。

## 3. 实施方式

代码实施已由一个职责明确的子 agent 完成；主 agent 负责独立复核、完整发布门禁和整合。

每批先保存真实行为 RED，再做最小实现；先通过低成本直接检查，才扩大回归。不能以“新类尚不存在”的导入错误作为唯一 RED。最多两轮相邻限定修正；仍有未收敛阻塞则停止，不做第三轮补丁。

## 4. D124-0：冻结读取，并关闭真实原版规则边界

**交付：** 按“格子＋用途＋父／后继／尾段／release 分支”分类的表，以及完整方块屏障的物理与材料论证。本批先只读，不改生产行为。

- [x] 冻结四方向 67／12 身份、parent、successor、全部 fallback 和 release witness，以及 D120 原始输入与 D122 生产语义身份。记录 D123 历史哈希。
- [x] 保存当前 RED：actor 55 known／12 unknown 仍被正式门禁拒绝；宽读会进入 proof；parent-only 或 successor-only 检查会漏掉必要依赖。
- [x] 列明普通碰撞、step、潜行支撑、近地支撑、净空、面积和材料读取调用点，包括没有最终选中的候选读取。尝试读取与可执行尾段分别记录；不能仅因某候选未选中，就默认它的读取可以删除。
- [x] 绑定现有 Minecraft 1.21 JAR、映射和来源哈希。审查碰撞、step、身体内方块作用、脚下倍率、落地／踩踏和流体触发位置；不要凭材料注册名推断作用范围。
- [x] 明确 `vanilla-block-registry-1_21.json` 只列 ID、`block-motion-traits-v1.json` 只列移动效果；它们不能穷尽 `BlockState`、邻居和碰撞上下文的形状。所需上下文分别由通用上界覆盖、合法已知事实确认或列为不适用，不能假装已经枚举。
- [x] 比较真实原版高形状、特殊材料和普通材料在完整屏障下的行为。真实形状与材料必须成对；合成两格高蜂蜜块另列为宽合同负例，旧证据不改。
- [x] 给未知格全部合法状态提供保守上界或通用调用点论证。在现有 `PhysicsWorldView.__init__()`／`from_snapshot()` 入口冻结可执行的适用范围检查表，逐项指出既有正式观察／会话来源、固定规则、已知屏障或身体事实及其校验调用点。当前类型／规则校验不足以证明该范围；缺少依据时停止，不凭版本标签、有限样本或再次读取已省略格补足。
- [x] 核对 `NavigationObservationAdapter.ingest()`／WorldModel 会话 → 借用版本／`seal_snapshot_version()` → `PreparedMotionRequest.snapshot_identity`／`PreparedMotionJob` → `prepare_fixed_motion()`／bundle／proof 的身份链。逐项记录世界会话、ruleset、世界版本、snapshot digest、请求与快照身份及 proof 请求归属的检查位置；适用前提无法沿现有链确认时停止，不新增未授权管线。
- [x] 为链中世界会话、ruleset、版本、digest、request snapshot 和 proof 请求归属各做独立 mismatch mutation。构造前拒绝或 worker 明确拒绝均可，但不能进入可授权证明，不能由后续 gate 补救。
- [x] 从 D124 代码修改前基线冻结不依赖新收集器的宽查询 reference 及源码哈希，对照每种源查询的候选范围、屏障判定前后行为和必要依赖见证。expected 不得调用同一个新收集器生成；少收一个真实候选的 mutation 必须被独立 reference 检出。
- [x] 以去掉完整支撑、换半砖／楼梯、身体或探测盒进入下层等反例推翻错误屏蔽。半砖和楼梯在本轮仅作负例，不扩大放行范围。
- [x] 冻结源查询的最小接口：必要依赖、缺失事实、未支持结果，以及诊断所需的候选／屏障记录。复用已有值和拥有者，不预建新框架。

**竞争解释：** A，真实原版机制确实会跨完整支撑改变结论；B，现有阻塞来自宽数据合同和材料触发集合混用。实验必须区分两者。

**通过：** 每种将被省略的读取都有适用范围明确的论证；完整屏障事实仍必要；未关闭的真实机制不被默认为普通材料。独立复核通过后才进入 D124-1。

**停止：** 原版反例仍不能解释，适用范围无法在现有入口守住，或必须依赖隐藏事实、额外 sensing、全局合同重写。

## 5. D124-1：源查询按用途形成依赖

**交付：** 完整方块屏障下的最小必要依赖；缺失与未支持结果保持分开。

- [x] RED 验证候选、必要依赖与缺失集合的关系；证明屏障前提成立时宽读不能直接成为 actor 请求。
- [x] 在普通碰撞、step、潜行支撑和支撑面积各自的源查询实施 D124-0 规则；没有对应论证的用途保持保守。
- [x] 材料读取采用已证明的触发范围，真正需要的特殊材料仍拒绝；不直接删除 `_contacts_unmodeled_material()`。
- [x] 屏障事实进入依赖；下层 `UNKNOWN` 保持原样。屏障去掉、变形、未知或身体范围越界后，恢复下层必要依赖和缺信息结果。
- [x] 检查 live／detached／sealed snapshot 的事实与缓存语义。缓存不得让屏障变化后继续复用旧查询；宽快照范围不得重新扩大 proof 依赖。
- [x] 单格、相邻双格和全部 9 格一起变异；组合改变屏障与扫掠边界。覆盖四方向、负坐标和平移。

**通过：** 所有被省略读取的几何、材料和版本前提均有检查；候选诊断与必要依赖不会被混用。不能只让最终 JSON 变小。

## 6. D124-2：在线与 prepared 恢复证明、正式门禁

**交付：** parent、successor、fallback 和 release 使用同一依赖语义；门禁原样检查新证明的全部必要事实。

- [x] RED 复现 `prepared_motion_proof._support_fraction()` 把宽候选重新并回，以及只核父或只核后继遗漏必要格的问题。
- [x] 修改对应汇总，使在线和 prepared 都只合并源查询返回的必要事实；保留全部可执行尾段、风险边界与 release witness。
- [x] 每方向检查 9 个屏障候选及 3 个必要格；数量变化逐格解释，不在 gate 中硬编码 58。
- [x] 具名保留 `0e6b0141` 在 `tests/motion_nav/test_d121_successor_binding.py` 的 `test_parent_only_dependency_change_is_rejected_before_prepare`、`test_tail_only_changed_dependency_revokes_full_candidate`、`test_tail_only_unknown_dependency_revokes_full_candidate`、`test_tail_only_stamp_refresh_keeps_full_candidate_eligible` 和 `test_stamp_only_refresh_keeps_semantic_dependencies_eligible`。另补 parent-only 的 `UNKNOWN` 撤销和纯 stamp 保持资格用例；parent-only／完整 safety tail-only 的见证来自冻结 reference，不能仅由新收集器的集合差生成，也不能因新集合为空而跳过。
- [x] 对旧完整已知世界比较主轨迹、出口、风险、伤害、支撑、fallback、release 和输入。依赖及新摘要允许按本合同变化，不能强求旧摘要相同。
- [x] 对 9 格保持 `UNKNOWN` 的世界重新证明，再调用正式门禁。必要事实缺失／不匹配照旧拒绝；屏障变化、owner 缺失和历史不完整使旧证明失效。
- [x] 只读回放 D122 历史 actor 事实，报告新证明仍缺哪些格。不得回填 3 格，不得把离线回放称为 actor 校准通过。
- [x] 保留 D123 历史证据及其哈希。原测试中要求当前行为恒为 67 格的断言改为区分历史检查与新合同检查，不增加生产兼容开关。
- [x] mutation 删除必要格、漏掉屏障、只核单分支、序列化过滤、gate 过滤、`UNKNOWN→AIR`、异常材料普通化、缓存越过版本变化，均必须失败。

**通过：** 9 格省略均由源查询证明，3 格继续阻止缺信息执行；未新增 actor 事实、控制权限或生命周期。

## 7. D124-3：Windows 回归、成本、导出与签署

- [x] D120 972 个冻结入口／2916 个后缀零旧成功退步，行为逐项相同；依赖与摘要的预期变化另表解释，不回写历史 coverage。
- [x] D122-3 正式 20 场、v1 兼容 120 项，以及 D116 语义版本、D121 后继／fallback、B09-R 物理和受影响几何集合通过。
- [x] 单列 `0e6b0141` parent-only／successor 完整 safety tail-only 的内容变化、`UNKNOWN` 撤销与纯 stamp 刷新结果，以及源查询对冻结宽查询 reference 的独立对照和身份 mismatch mutation；不能只以总通过数代替这些行为。
- [x] 分开执行调用计数和无 profile 计时，冻结同机样本、重复次数和计时区间后再运行。控制准备 P95 ≤ 8 ms、P99 ≤ 15 ms、最大 < 30 ms；同步提交计算 < 50 ms。合法等待与端到端延迟单列，不扣除 GC 或失败样本换取通过。
- [x] 报告每类正式 worker 请求／结果的 protocol-5 字节数，均 ≤ 2 MiB；不重复完整证明，不增加第二管线。Java／wire 不改，既有 16 KiB action 约束保持。
- [x] Windows 源码树正逆完整分片、独立公开快照正逆完整分片、Java 相邻门禁、导出 verify 全绿；失败、错误、超时、跳过、预期失败、意外成功和清理失败为 0。
- [x] 导出显式指定上一份已验证快照作为 byte base；共同文件纯行尾差异保留原字节。新的四类文档与紧凑证据纳入 include／required，检查缺文件和缺哈希反例。
- [x] 独立 reviewer 核对源查询、全部证明分支、门禁、证据边界和历史字节。把实际命令、环境、原始失败及最终结果追加到验收。

完整命令与结果表见[验收](../acceptance/d124-proof-dependency-projection.md)。本阶段不产生新 Fabric 能力结论，也不自动恢复 D123-2。

## 8. 当前实施结果

D124-0 同时记录原始 Minecraft 1.21 client JAR（SHA-256 `4B56BC9B44DE7E88F9FBB52E716C18240DA34469FCBAFA5ED2E75B04D60617E8`）和用于自动 `javap` 审计的 Yarn named transformed JAR（SHA-256 `F470E595E849846ECC74E4F029877D1E649E75BA9B5B40A5C9F26BA72E6B1FED`）。原始 JAR只用于来源追溯；named JAR 用于核对 `Entity`、`BlockCollisionSpliterator`、`PistonBlock`、`PistonExtensionBlock`、`PistonBlockEntity`、`FlowableFluid`、半砖和楼梯字节码。自动审计确认身体内材料与流体扫描使用收缩后的身体包围盒，碰撞枚举保留一格边界，支撑格与速度影响格另行选取，活塞移动形状和推实体范围受屏障边界约束，双半砖使用完整方块，单半砖和楼梯保持部分形状。

D124-1／D124-2 已在源查询处分开宽候选与必要依赖。只有已知、非流体的完整方块可屏蔽下层宽候选；半砖和楼梯不继承该规则。屏障本身进入依赖，身体进入下层、屏障变化或查询用途不满足规则时恢复下层依赖。仅因宽查询纳入的下层 owner 完全不读取，因此 UNKNOWN、ordinary、fluid 和 unsupported 不会形成未登记的缓存依赖；直接占用时仍按原规则检查。跨 16 格 section 的变化已验证旧缓存与 fresh view 一致。保守 `shapes()` 与 sweep 查询使用不同缓存键，避免旧宽依赖污染。`PhysicsWorldView` 现在正式持有可选 `WorldQueryCache`，cell、碰撞盒、完整方块屏障和支撑查询共用同一入口。`support_surfaces.py` 中直接并入宽依赖的路径只服务规划与终点站立判断，不进入 prepared proof 或 motion solver，因此本轮明确排除。

四个方向的联合依赖均为 58。每方向 9 个被完整方块屏蔽的下层格不再进入门禁，3 个 parent／successor／fallback 必要格继续在未知时返回 `NEEDS_WORLD`。parent-only 内容变化与 UNKNOWN 会拒绝，纯 stamp 刷新保持资格；完整 safety tail-only 的既有五项具名检查继续通过。

低成本 D124-3 门槛已经通过：D120 为 972 个入口、2916 个后缀逐项一致且旧成功退步为 0；D122 正式矩阵 20／20，v1 兼容 120／120；生产控制 P95／P99／最大为 0.4712／4.4607／4.7067 ms，同步提交最大 14.4417 ms；正式 worker job、bundle canonical 与 wire payload 分别为 92,875／442,163／493,588 字节，均低于 1 MiB 半限和 2 MiB 上限。首次定向回归 73／73、D116／D121／D122 相邻 101／101；独立审查修复后 D124／B09／ground／D102／D121／D122 低成本回归 206／206，设计度量与导出检查 36／36 通过。

完整 Windows 源码树正逆分片、独立 standalone 正逆分片、Java 相邻门禁和最终 export verify 均已通过。D124 已按登记范围签署完成；D123-2、连续跑酷和新 Fabric 仍未授权。

## D126 纠偏

D124 已证明的 58 个必要依赖保持原样。测试已知地图可以直接向同一 Runtime owner 提供这些事实；可见性不是本阶段的新前置。缺失和语义变化仍走 D124 的 `NEEDS_WORLD`／依赖失效规则。
