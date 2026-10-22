# D129：区分预试次环境失败与产品失败

日期：2026-10-22。状态：**ACTIVE**。

依据：[D128 决定](0128-bind-d123-formal-launch-to-repository-jdk21.md)、[D123 计划](../stages/d123-proof-dependency-and-transport-loss-plan.md)和[D123 验收](../acceptance/d123-proof-dependency-and-transport-loss.md)。

## 问题

D128 唯一正式 smoke 的 offline preflight 已通过，但 probe 在启动 Minecraft 前构建失败。失败原因是固定 Gradle 缓存缺少 `build.gradle` 已锁定版本的运行时 native 包。这类失败没有运行产品，也没有提交运动试次，不能据此判断运动、transport 或安全逻辑失败。

## 决定

正式运行按失败边界分类：

- 环境、工具或夹具在首个 movement trial 前失败，上位类型统一记为 `INVALID_PRETRIAL_RUN`，并明确记录 `environment`、`tool` 或 `fixture` 子类。保存全部原始证据，不消耗正式 attempt；修复对应环境、工具或夹具后，可以在新的 append-only 目录重试。
- 产品逻辑、正式协议、安全门槛或运动试次失败，才触发阶段 `STOPPED`。原始失败同样永久保留。
- 环境修复不得改动产品语义。若依赖缓存缺失，只能按当前 `build.gradle` 和固定 Gradle／JDK 下载精确依赖；不得升级版本或修改构建声明。
- 修复后先用同一固定环境完成正式 offline build。只有 offline build 通过且 tracked 产品代码未变，才允许运行新的 smoke。smoke 3／3 后才允许一次矩阵。

### Post-submit invalid harness 边界

movement request 一旦正式 dispatch，默认按已进入 movement trial 处理。只有以下七项同时成立，才能改记为 `INVALID_HARNESS_RUN`，保存证据、修复 harness 并在新的 append-only 目录重试：

- fixture 或 tool 明确且因果性地破坏了被测产品成立所需的注册前提；
- 正式 request、receipt、F-TICK 和 physics sidecar 能唯一对应同一个 episode、request、observation、generation 和 window；
- 被测非中性输入在客户端的 application 数为 0；
- safe default 在客户端的 application 数也为 0；
- 身体没有进入被测行为，原始位置和着地证据完整；
- health 没有下降；
- 没有独立的 transport／协议失败，也没有由产品逻辑本身触发的正式安全拒绝。由已证实的 harness 过期直接导致且行为输入为 0 的 `window_missed`，只证明客户端正确拒绝了失效请求，不能反过来冒充产品失败或产品通过。

任一项无法证明，或者出现任一被测输入／default application、身体进入动作、伤害、独立 transport／正式安全失败、原始 identity 不唯一，就按产品、协议或安全失败处理并停止。`INVALID_HARNESS_RUN` 不能靠结果行推断；必须同时绑定正式 request、receipt、F-TICK、physics sidecar 和身体终态。历史原始文件不得改写。

## 对 D128 的应用

`d128/smoke-run-1` 归类为 `INVALID_PRETRIAL_RUN/environment`。D128-ENV-01 的仓库 JDK 修复已由该运行的 offline preflight 证明通过；新的问题登记为 D128-ENV-02／BUILD_CACHE。原始四个 launcher 文件与 build 两文件保持原字节，由 manifest 绑定。

授权使用仓库固定 JDK 21 和 `.gradle` home 在线预取当前 `build.gradle` 已锁定的精确依赖，再执行同一正式 offline build。通过后只能在新的 append-only 目录重试；只有真实 smoke 3／3 后才执行一次 matrix。

## 实施结果

环境缓存已经按锁定版本修复，正式 offline build 与 launch export 均通过。前九次运行的上位类型都是 `INVALID_PRETRIAL_RUN`：run-1 是 `environment/build_cache_native`，失败点为 `:remapJar` 缺少 24 个 LWJGL Windows native；run-2 是 `tool/standalone_export_manifest`，required manifest 与 `.log` 排除规则冲突；run-3 是 `environment/runtime_classpath_jline`，offline client runtime classpath 缺少 JLine reader／terminal jars；run-4—run-7 是 `fixture/assembly`；run-8 和 run-9 是 `fixture-adapter`，分别暴露旧 velocity 属性和收口后残留的未定义 `current`。原始 `stop.json` 中形成于分类统一前的状态值保持原字节，只作为历史记录。

run-10 已正式 dispatch request 7，因此不属于 `INVALID_PRETRIAL_RUN`。harness 在 live anchor tick 7 上把 parent command 0 的 exact window 固定为 tick 8，随后同步执行 proof／prepared worker 工作；请求到达客户端时已是 tick 34／35。客户端没有应用被测输入或 default，tick 8—125 的身体位置、health 和着地状态不变。另一个结果组装错误把这个 windowed parent 请求误认成 successor，但 request／receipt／F-TICK／physics 的原始 identity 唯一。只读 D129 validator 先验证原 50 项 manifest，再从 raw trial、trace、typed V4 receipt、F-TICK、physics 和 terminal 重算七项门槛与 disqualifier；它不读取手工 `classification-review.json`。最终 `classification-validation-v2.json` 还证明 physics tick 连续且唯一、相邻 state 连贯，全部 92 个区间 observation 无 health 下降或 damage event。因此 run-10 仍归类为 `INVALID_HARNESS_RUN/fixture-scheduling`，不产生产品结论。有效 smoke 仍是 0／3，矩阵未运行；新 formal run 等待设计审查。

run-10 的 harness 修复只改正式脚本和测试。imports、profile、固定模板和正式 `MotionSolverWorker` 在 fresh anchor 前预热；fresh anchor 后只绑定 live identity／依赖并安装 parent。`bind_and_start()` 先完成第一个正式 `driver.tick()`，再异步提交 successor，不等待 receiver，也不调用同步 `_execute_job` 或手工回填结果。exact window 仍是 anchor 的 L+1。正式 worker 由 harness 显式持有，并在 trial 后有界关闭和记录 close report。

successor expected identity 在 backend step 外从 `PreparedStreamDispatch.proposal` 冻结；parent 只按 `safe_default` 识别。prepared generation、proof token、owner、ledger、world session、intent、handoff、movement 和 window 先独立保存。只有正式结果为 `ACTIVATED`，才补齐 episode、request、observation 和 receipt generation。assembler 分别比较 expected、backend step、typed receipt 和 remote injection，不再按“第一个 windowed 请求”或 `jump` 形状猜测。组件 mutation 覆盖 parent／successor 都有 window、顺序交换、中性请求插入、重复 sequence、错误 generation、缺 successor、receipt 错绑、remote reject 误命中 parent、未 activation 和各字段 stale identity；身份 mutation 保持 movement／window 不变。没有修改 `mc2p`、Session、transport、Java 或窗口规则，也没有运行新的 Fabric。

最终复审门禁位于 `d128/run10-review-continuity-fix/`。四个连续性／健康 mutation 4／4，必要 D122／D123 相邻 3／3；正式 offline 为 D129 raw validator PASS、组件 smoke 3／3、matrix 36／36、Java／overlay PASS，`fabric_started=false`；standalone 最终源码 export／verify 3504 个文件。较早 offline 目录和手工 `classification-review.json` 原样保留，只作为历史证据。

系统修复不再逐字段平移冻结 proof，而把隔离服务器夹具部署到冻结坐标。单一 `assemble_frozen_fixture_pretrial()` 负责 live anchor 身份、replay 轨迹、依赖和 candidate identity。组件测试在四个方向逐一运行正式 pre-trial 组装到 `prepare_fixed_successor`，检查 start tick 大于 1、candidate revision 一致、依赖属于同一个 Runtime owner，并确认 worker 收到 typed prepared job。代表方向另用六类 mutation 锁住关键绑定。run-8／9 后，单一 `RuntimeTerminalPayload` 从同一个正式 `SelfStateV2` 生成 body、health、observation sequence 和 movement tick；position／velocity 严格读取 `Vec3V0.x/y/z`。runner 只通过一次严格合并消费这四个 row 字段。真实 Runtime observation 回归走同一最终 row 合并路径；旧 `current` 的 RED 精确复现 run-9。结构门禁与 mutation 拒绝独立 identity 拼接、旧属性、漏字段和错名字段，没有兼容双轨或默认值。最终受影响与相邻检查 38／38，正式 offline 为组件 3／3、36／36、Java／overlay PASS。新的正式 smoke 暂停，等待独立复核。
