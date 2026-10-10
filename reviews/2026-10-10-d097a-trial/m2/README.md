# D097-A M2：逐次（lazy）安全证明 —— 结果

日期：2026-10-10。平台：Linux 6.18、Python 3.11.15、4 核；测量时 M1 子 agent 同时在跑（第一次性能运行时系统负载约 1.2–2.0；存档的最终性能运行时约 3–4，四核基本占满）。
预登记：`../../2026-10-10-d097a-trial.md` 与补记 1。本目录只含 M2 的代码、数据和结果，没有改动项目代码或 `entries.py`。

## 1. 结论

| 门槛 | 观测值 | 阈值 | 结论 |
|---|---|---|---|
| G8 等价性 | A 集 23/23 与完整扫描逐项相同；B 集 1,605 个候选（764 个被扫描拒绝，另 109 个得到“信息不足／预算／过期”分类，共 873 个非 VERIFIED）状态 0 不符、原因 0 不符、0 个“逐次放行而扫描不放行”；C 集 120/120 相同。**D 集（M1 验证候选）未运行**：运行时 `../m1/accepted_candidates.jsonl` 尚不存在 | 全部一致，且不得在扫描拒绝时放行 | **A/B/C 通过；D 待 M1 产出后重跑（命令见 §6）。G8 在 D 集完成前不能整体判通过。** |
| G9 变异 | 基线 27 项测试全过；7 种注入缺陷各至少被 2 项测试发现（7/7） | 7/7 | **通过** |
| G10 性能 | 普通 tick 决定 P95 = **3.92 ms**（8,349 个样本）；越过承诺点的决定 P95 = **66.2 ms**（291 个样本；三轮各自 P95 为 67.0／67.5／64.6 ms；第一次运行为 62.0 ms，轻负载下） | tick ≤ 10 ms；承诺 ≤ 50 ms | **tick 通过；承诺决定在两次运行中都未通过（62–66 ms 对 50 ms）。G10 判为不通过。** |

按预登记第 7 节，G10 未过即 D097-A 在该门槛“走不通”。我没有使用“一次限定性能修正”的名额（见 §4 的选项分析，该修正若采用 minimal 模式则不再与扫描器等价，需要项目方决定）。

三条要点：

1. **严格模式（与扫描器逐边界相同）与扫描器等价，但没有省下物理计算**。对每个 VERIFIED 候选，逐次证明用的 physics step 数、node 数和依赖格集合与完整扫描完全相同（如混合反例 427／427 step）。它只是把总成本摊到各 tick 上。等价性在很大程度上是构造性的（同一批私有函数，同一组尾部）；G8 实际检验的是增量版区间记账 `_risk_step`、错误优先级的镜像、在途命令的接法，以及许可对象的结构。
2. **承诺点的成本是真实的，且来自“每个空中边界都要证一遍尾部”**：一次承诺决定要算约 300 个 physics step（P50 308，P95 333），中位 39.4 ms。普通 tick 只要约 18 步（P95 24 步）。
3. **minimal 模式能把承诺决定压到 P95 23.4 ms（最终运行；第一次运行 27.7 ms；183 步），但不等价**：它跳过空中边界的尾部，于是在 B 集里有 13 个扫描器因 `tail_not_settled` 拒绝的候选被它放行（另有 1 个是预算分类的人为差异）。在登记的运行总体（A 集 23 + C 集 120）里，minimal 与扫描器的区间 0 差异，但这只能说明登记总体里没有空中尾部不落定的情形，不能推出一般成立。minimal 只用于成本对照，没有用于 G8。

## 2. 方法

### 2.1 逐次证明 `incremental.py`

`lazy_prove(request, inputs, world, boundary_inputs, *, mode="strict", faults=frozenset(), counter=None)` 离线模拟滚动提交：

- **计划阶段（kind=`plan`）**：与扫描器相同的检查，按分支顺序：证据检查、`_prove_entry`、逐分支 rollout（`_calculated`）、正常伤害对 `task_damage_budget`、末状态着地。
- **决定循环**：对边界 k = 0..n，对每个分支算 k 处的尾部（在途命令 `inputs[k:k+1]` 再接停车输入），复用 `commitment._tail`；风险区间按 `_risks` 的规则增量记账（`_risk_step`，是唯一重述的部分）：
  - 第一个 UNSAFE 尾部开区间；之后第一个“SAFE_STOP 且 `tails.states[0].on_ground` 且通过 `_safe_support`”的边界关闭区间；
  - k 处无任何分支处于开区间 → **tick 决定**，许可第 k 条命令；
  - k 处有分支开区间 → **commit 决定**：严格模式对所有分支继续算 k+1…，直到所有开区间都关闭，恢复边界 r；命令 k..r−1 是锁定后缀，命令 r 的尾部已在同一决定内证明，作为普通命令一起许可；
  - 最后一个边界（没有命令了）→ **final 决定**；若在承诺内到达，则承诺决定同时收尾（末尾尾部必须 SAFE_STOP，区间必须已关）。
- **minimal 模式（仅作成本对照）**：承诺内，开着区间的分支只在“着地且通过支撑查询”的边界算尾部。
- **裁决镜像**：扫描器按“分支 0 全部 → 分支 1 全部；分支内：计划错误 → 尾部错误（边界序）→ FINAL_STOP_UNSAFE → `_risks` 错误 → UNRECOVERED_RISK”给出第一个错误。在线阶段在时间上第一个错误处停下（真实系统也会在那里停）；随后 `resolve()` 在不计入任何决定的情况下，把排在前面的分支补算完，使返回的 status/reason 与扫描器一致。`online_halt` 记录在线停下的位置，`resolution_steps` 记录为此额外花的 step。
- **许可** `Permit`（冻结 dataclass）：边界、命令序列、锁定条数、请求／锚点／入口状态／目标与目标修订／输入账本 ID 与首个控制序号／分支前奏／世界会话，以及该决定所依赖的全部格事实（计划阶段依赖 + 本决定尾部依赖）。
- `validate_permit(permit, request, world)`：请求 ID、世界会话、锚点或入口状态、目标或目标修订、输入账本或分支前奏任一变化 → `STALE`（带原因）；依赖格事实变化 → `STALE/world_dependency`；依赖格由已知变为未知 → `NEEDS_INFORMATION/unknown_world`（显式过期优先，同 `validate_commitment`）。
- `check_execution(permit, command, boundary)`：只有许可（或锁定）的那条命令在其边界上放行；不同命令 `COMMAND_MISMATCH`，超出范围 `OUT_OF_RANGE`。
- **注入缺陷**（默认关闭，`faults=`）：`no_landing_proof`（越过承诺点后在第一个着地边界直接关区间，不要求 SAFE_STOP 与支撑）、`unknown_as_free`（试验盒内未知格读作空气，其余未知处 rollout 继续／尾部当 SAFE_STOP）、`one_branch`（只处理分支 0）、`ignore_inflight`（尾部不带在途命令）、`accept_stale`（许可校验直接放行）、`ignore_damage`（计划与尾部都不查伤害额度）、`swap_locked_command`（执行门放行任何命令）。

### 2.2 候选集与等价比较 `run_m2.py equiv`

对每个候选，在同一个请求、输入、`_boundary_evidence` 下比较 `scan_commitment` 与严格模式 `lazy_prove`：状态必须一致；原因另计；双方 VERIFIED 时比较每个分支每个风险区间的 (last_abandon, first_committed, recovered)、区间个数、完整 `RiskInterval`（含 tick）、每个边界的尾部状态与尾部输入、每个状态、依赖格集合。任何“逐次 VERIFIED 而扫描非 VERIFIED”记为严重失败。

| 集 | 内容 | 个数 |
|---|---|---|
| A | 冻结矩阵：22 个原正例（输入来自 `m0_probe.run_case(FORMAL_OPTIONS)`）+ 混合反例（`expected_inputs`） | 23 |
| B | 构造的拒绝／压力候选：对 A 的 23 个输入做确定性编辑（截断末尾 1–20 tick、跳跃改走、起跳提前／延后一格、第一 tick 起跳、全部直走、全部停车、去掉末尾停车、4 个随机单点替换）；项目 `unknown_landing`／`one_twelfth_support`／`collision_only`／`gap_start_4_width_3_a3`／`tiny_budget` 夹具上重放正例输入及其编辑；伤害落差（深 2–6 格 × 额度 0／1／2）；请求层 14 种（缺前奏、账本 tick／序号错位、缺回执、几何修订变化、分支／节点／step／tick 预算、前奏输入或入口被改、未支持方块、流体方块）；8 个夹具上的 800 个随机输入；两洞地形上的 112 个“落到中间平台后紧接再起跳”候选 | 1,605 |
| C | 随机入口：6 个支持模板 × 10 个分箱（rest-L/R、walk-C-s/L-n/R-p/L-p/R-n、sprint-C-s/L-n/R-p）× 每箱 2 个样本，种子 **77**（不是 20261010／20261011）；输入同样来自 `m0_probe.run_case`，120 个全部找到 | 120 |
| D | `../m1/accepted_candidates.jsonl` 的全部行；独立命令 `run_m2.py setd`；**尚未运行** | — |

B 集被扫描拒绝的 873 个中：`final_exit_not_grounded` 402、`damage_allowance` 268、`tail_not_settled` 75、`final_stop_unsafe` 12、`prelude_outside_same_safe_support` 3、`unknown_world` 84、`missing_input_application` 2、`input_ledger` 2、`world_dependency` 1、预算类 20、`prelude_entry_mismatch` 2、`unsupported_physics` 2。**没有出现 `UNRECOVERED_RISK` 的样例**，因此该分支只靠与扫描器共享的记账规则保证，没有被数据覆盖；这是覆盖缺口。
VERIFIED 的候选里：A 集 17、B 集 189、C 集 80 个带风险区间；1 个 B 候选（两洞地形）每分支有两个区间，且等价；双跳候选里有单个区间覆盖两次起跳的情形（区间 (0,1,25)），这正是 `no_landing_proof` 的见证。

### 2.3 变异 `test_m2.py`、`run_mutation.py`

27 项 unittest（约 7 秒）：冻结矩阵子集的精确等价、与扫描器相同的物理工作量、混合反例的决定结构（tick／commit／final、锁定 12 条）、在途命令（边界 1 的尾部 UNSAFE）、被拒绝候选（空中截断、跳跃改走、走过空洞、未知落点、未知路径格、1/12 支撑、只有迟到分支失败的候选、伤害额度零、额度内伤害仍放行、14 种请求层、双跳单区间）、许可（未变、锚点 ID、入口状态、目标／目标修订、账本、请求 ID／世界会话、依赖格变化、依赖格变未知、无关格变化不过期）、执行门（锁定后缀每条被换均拒绝、tick 许可范围）。环境变量 `D097A_FAULT=<name>` 让所有 lazy／许可／执行门调用带上该缺陷。`run_mutation.py` 先跑无缺陷基线（必须全过），再对 7 种缺陷各跑一遍（必须有失败）。

结果（`mutation_results.json`、`logs/mutation_*.txt`）：

| 缺陷 | 检出 | 发现它的测试（部分） |
|---|---|---|
| no_landing_proof | 是 | 冻结矩阵子集、双跳单区间、同等物理工作量 |
| unknown_as_free | 是 | 路径上的未知格、未知落点、依赖格变未知 |
| one_branch | 是 | 只有迟到分支失败的候选、冻结矩阵、混合反例结构 等 6 项 |
| ignore_inflight | 是 | 在途命令、冻结矩阵、混合反例结构 等 6 项 |
| accept_stale | 是 | 7 项许可测试 |
| ignore_damage | 是 | 伤害额度零、截断／走过空洞（原因不符） |
| swap_locked_command | 是 | 锁定后缀、tick 许可范围 |

此外 `fault_coverage.py` 在 624 个 A/B/C 候选上直接统计每种缺陷被“数据”发现的次数（`fault_coverage.json`），不依赖单元测试：见 §5。

### 2.4 性能 `run_m2.py perf`

- 总体：A 集 23 + C 集 120 的 143 个 VERIFIED 候选，3 轮；每轮每个候选各做冷（新建 `PhysicsWorldView`，形状缓存为空）与暖（同一视图第二次）两次，各跑严格逐次、minimal 与完整扫描。
- 决定的墙钟用 `time.perf_counter_ns`，从决定开始到许可对象建成为止（含依赖格事实读取，不含候选级的证据构造）。tick 指无开区间、只许可一条命令的决定；commit 指跨过承诺点的决定（k..r 的全部尾部）；plan 与 final 单列。
- 分位数取 nearest-rank。门槛按更严的读法判定：冷缓存、严格模式、`max(合并 P95, 各轮最差 P95)`。
- 做了两次完整运行：第一次（轻负载，早于最后一处代码改动）结果见 `logs/perf_first_run_summary.json`；存档的 `performance.json` 是用最终代码重跑的，此时 M1 把四核基本占满，数字略差。两次的结论相同。

存档运行（严格模式，冷缓存）：

| 项目 | 样本 | P50 | P95 | P99 | max | step P50 / P95 |
|---|---|---|---|---|---|---|
| tick 决定 | 8,349 | 2.17 ms | **3.92 ms** | 4.90 ms | 24.05 ms | 18 / 24 |
| commit 决定 | 291 | 39.39 ms | **66.20 ms** | 72.51 ms | 80.03 ms | 308 / 333 |
| plan（一次性） | 429 | 7.73 ms | 13.32 ms | 14.66 ms | 17.12 ms | 63 / 77 |
| final | 414 | 0.40 ms | 1.93 ms | 3.22 ms | 3.88 ms | 2 / 14 |

第一次运行：tick P95 4.02 ms；commit P50 40.5 ms、P95 62.0 ms（三轮 60.9／60.7／68.9 ms）、max 73.7 ms。暖缓存并不更快（commit P95 66.0 ms），形状缓存对这里没有明显帮助。

对照（有承诺点的 97 个候选 × 3 轮 = 291 次，冷缓存，存档运行）：

| | P50 | P95 | step P50 / P95 |
|---|---|---|---|
| 严格 commit 决定 | 39.4 ms | 66.2 ms | 308 / 333 |
| **minimal commit 决定（不等价）** | 6.4 ms | 23.4 ms（第一次运行 27.7 ms） | 45 / 183 |
| 严格逐次整条候选合计（含 plan、tick、final） | 89.7 ms | 142.8 ms | — |
| `scan_commitment` 整条候选 | 141.3 ms | 233.5 ms | 687 / 736 |

注：完整扫描的墙钟比逐次合计大，是因为扫描器在返回前还要建 `CommitmentProof` 并算 `trajectory_digest`（JSON 化后做 SHA-256），逐次版不做；两者的 physics step 数相同。所以“整条合计省了约 40%”不是等价证明带来的收益，而是没算证明哈希。

补充总体：B 集中全部 732 个 VERIFIED 候选（含变异、双跳、随机），1 轮冷缓存：tick P95 4.63 ms（15,377 个样本），commit P95 66.35 ms（190 个样本）。结论不变。

## 3. 等价性明细

- 严格模式 vs `scan_commitment`：A+B+C 共 1,748 个候选，**状态不符 0、原因不符 0、严重失败 0**；875 个 VERIFIED 候选（A 23、B 732、C 120）的区间、区间个数、尾部状态、状态序列、依赖集合全部一致。
- 严格模式在被拒绝候选上给出的原因与扫描器相同，这依赖 §2.1 的“裁决镜像”；如果直接取在线第一个错误，在分支 0 尾部错误与分支 1 前奏错误同时存在的候选上会得到不同的原因甚至不同的状态类别（NEEDS_INFORMATION 对 CANDIDATE_REJECTED），所以必须镜像。镜像发生的额外 step 单独记在 `resolution_steps`，不计入任何决定。
- minimal 模式（仅供参考，`equivalence_results.json` 每行的 `minimal` 字段）：区间在全部 VERIFIED 候选上与扫描器相同；状态不符 14 个：1 个是 300 step 预算分类（minimal 用得少），13 个是扫描器因空中边界尾部 `tail_not_settled` 拒绝而 minimal 放行（12 个在两洞地形的“跳跃后再跳”族，1 个在 `one_twelfth_support` 随机候选）。

## 4. 对 G10 失败的分析与选项（不是已采用的修正）

- 每个 physics step 约 0.12 ms（tick 决定）到 0.2 ms（承诺决定，尾部更长、`_damage` 对前缀每 tick 重算）。一个承诺决定要证约 12 个边界 × 2 个分支 × ~12 步尾部；按本机速度，50 ms 只够约 250–400 步，而中位承诺决定就要 308 步。
- 选项一：采用 minimal 判法。承诺 P95 降到 23–28 ms 并通过，但等价性破坏：必须由项目决定“空中边界的停车尾部是否必须落定”这条要求是否保留；若保留，minimal 不可用；若放弃，需要把完整扫描器同步改成同一口径，否则 G8 的“逐次不得在扫描拒绝时放行”会失败（B 集已有 13 个）。
- 选项二：保持严格判法，但把未来边界的尾部在前面的 tick 空闲时预算好（候选已知，尾部只依赖候选和世界）。这能把承诺决定的延迟压到 tick 级，但总计算量不变，需要后台线程或分片，已超出 M2 的范围，没有试。
- 选项三：缩短最长承诺（例如限制空中时长），因为成本近似与承诺边界数成正比，但这会改变候选空间。

## 5. 缺陷被“数据”发现的次数（`fault_coverage.json`）

在 624 个 A/B/C 候选上（A 23、C 120、B 的确定性子集 481：每 3 个输入变异取 1 个，加全部夹具、请求层、落差、双跳候选，不含随机），每种缺陷单独打开后与完整扫描比较：

| 缺陷 | 候选数 | 区间／尾部与扫描器不同（双方都 VERIFIED） | 状态不同 | 其中“逐次 VERIFIED 而扫描不放行” | 仅原因不同 |
|---|---|---|---|---|---|
| no_landing_proof | 624 | 134 | 0 | 0 | 0 |
| unknown_as_free | 624 | 0 | 38 | 2 | 0 |
| one_branch | 624 | 334 | 3 | 3 | 0 |
| ignore_inflight | 624 | 320 | 0 | 0 | 0 |
| ignore_damage | 624 | 0 | 6 | 6 | 115 |
| accept_stale、swap_locked_command | — | 只作用于许可／执行门，只由单元测试覆盖 | | | |

说明：`no_landing_proof` 在冻结矩阵里就有影响。例如 `jump_gap_1:A3`（W×5、J、N×17）：边界 3、4 的尾部 UNSAFE（再停车就会滑出边缘），扫描器的区间 (2,3,17) 要到落地后的边界 17 才关；缺陷版在边界 4 看到“`on_ground` 为真”就关闭，得到 (2,3,4)，那时身体仍在边缘上。边界 5 还出现了扫描器注释里说的 `on_ground` 滞后：状态标志仍为真，但整个身体已在空洞上方、支撑查询为假。`ignore_inflight` 把所有区间的开口推迟一个边界（(2,3,17)→(3,4,17)），在不需要承诺的候选（平地、一格上升）上区间不变（空集），只是每个边界的尾部输入少了在途命令。

## 6. 复现

在项目 checkout（be38685）根目录，`PYTHONPATH=<checkout>:<reviews>/2026-10-10-d097a-trial:<reviews>/2026-10-10-d097a-trial/m2`：

```
python3 m2/gen_inputs.py a        # 重新生成 Set A 输入（约 15 秒）
python3 m2/gen_inputs.py c        # 重新生成 Set C 输入（约 1 分钟）
python3 m2/run_m2.py equiv        # G8（A、B、C），约 4 分钟
python3 m2/run_m2.py setd         # G8 的 D 集；读取 ../m1/accepted_candidates.jsonl，文件不存在时只记录“不可用”
python3 m2/run_m2.py perf --passes 3            # G10 主总体，约 4 分钟
python3 m2/run_m2.py perf --population b --passes 1 --strict-only   # 补充总体
python3 m2/run_mutation.py        # G9（基线 + 7 种缺陷），约 1.5 分钟
python3 m2/fault_coverage.py      # 补充：数据层面的缺陷检出
python3 m2/run_m2.py gates        # 汇总 gates.json
python3 -m unittest test_m2 -v    # 单跑测试（D097A_FAULT=<name> 注入缺陷）
```

## 7. 偏离、疑点与限制

1. **G8 未整体完成**：D 集在本次运行时不存在。`setd` 命令已用一份由 C 集行构造的 5 行合成文件验证过读取和比较流程（合成文件在 scratchpad，不在本目录，结果已删除，不计入任何结论）。
2. **只覆盖了 `CANDIDATE_REJECTED` 的大部分原因，没有 `UNRECOVERED_RISK` 与 `INVALID_PHYSICS` 样例**。我用了约 1,000 个随机／变异候选和两洞地形仍未构造出来。
3. **“决定”的定义是我做的**：承诺决定同时包含恢复边界 r 处的尾部，并一并许可命令 r（其尾部已证）；plan 与 final 不计入 tick 样本。若把 r 处的命令另算一个 tick，tick 样本多约 97 个，不影响分位数结论。
4. **许可没有防篡改哈希**（用冻结 dataclass 代替）。每个许可算一次 `trajectory_digest` 会花数毫秒到数十毫秒，会直接破坏 tick 门槛；这是一个没有测的取舍。许可的依赖事实用的是 `world.cell(...) != fact` 的严格比较，重新观察同一格而时间戳不同也会被判过期。
5. **`unknown_as_free` 的实现**：试验盒（x −9..9、y −50..7、z −14..16）内的未知格读作已知空气，盒外未知处 rollout 保持原状态、尾部当 SAFE_STOP。它在“路径上的一个未知格”夹具上会把 NEEDS_INFORMATION 变成 VERIFIED（见 `test_unknown_cell_on_the_route_is_not_free_space`）；在“未知落点”夹具上它变成拒绝（地板读作空气），仍然被检出但不是放行。
6. **性能数字的环境**：M1 同时占用约 3 个核，机器 4 核；Python 3.11 纯解释执行；没有做 CPU 绑定。冷／暖缓存差异不大。Windows 是正式平台，这些数字只是补充证据。
7. **Set C 的 ID 不唯一**（同一箱内两个样本可能 tick 数相同），因此 `performance.json` 里的“有承诺点的候选数 86”是去重后的 ID 数；每轮实际是 97 个候选（291／3）。
8. **证据假设**：`_boundary_evidence` 假定“下一条命令已在途”，应用事实标为 PREDICTED；本试做没有接入真实的回执链，决定延迟不含任何通信或排队开销。
9. 所有输入都来自项目 `m0_probe` 的转向枚举，不是 M1 的控制器；因此 A/C 集的等价性和性能不能代表 M1 的候选分布，D 集才是登记的对象。
10. `witness_search.py` 找到双跳见证后被我手动终止，日志里没有结尾的统计行。

## 8. 文件

- 代码：`incremental.py`（逐次证明、许可、执行门、缺陷）、`equiv.py`（比较）、`candidates.py`（A–D 集构造）、`mutants.py`、`common.py`、`gen_inputs.py`、`run_m2.py`、`run_mutation.py`、`fault_coverage.py`、`witness_search.py`、`test_m2.py`
- 输入数据：`set_a_inputs.json`、`set_c_inputs.json`（`logs/set_c_inputs_first_pass_60.json` 是第一次只取每箱 1 个样本的 60 行，是现行文件的子集）
- 结果：`equivalence_results.json`、`performance.json`、`performance_b.json`、`performance_samples.json`、`mutation_results.json`、`mutation_log.txt`、`fault_coverage.json`、`gates.json`、`logs/`
