# D116 准备动作交接归因验收

日期：2026-10-17。状态：**D116-1 PARTIAL／STOPPED。语义依赖修复通过，连续两跳未完成。**

## 1. 运行范围

- 平台：Windows；
- 运行类型：组件正式链；
- Fabric：未运行；
- 生产行为：D116-1 只修改 prepared handoff 的语义依赖判断；
- 场景：3 格宽已知地形，两处单格间隙，终点位于第二次落点；
- 路线：`WalkSegment -> JumpGapSegment -> JumpGapSegment`。

诊断使用进程内补丁恢复 D115 已撤回的后继准备调用。它只提供待检查的 prepared proof，不改 Session、监督者或执行器顺序。

## 2. 检查

命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_d116_handoff_timeline -v
```

最初的 D116-0 未提交时间线曾在 D116-1 复跑时被同名文件覆盖，不能再当作保留的原始 RED。证据卫生修正后，检查同时运行两条明确分开的路径：

- `legacy_full_cell_fact`：只在进程内恢复旧完整 `CellFact` 门槛，重建 RED；
- `semantic`：运行现行 D116-1 生产语义依赖判断。

当前时间线检查 6／6 通过。最初 D116-0 的 4／4 只保留为工作记录，不作为可校验的原字节证据。

覆盖内容：

1. 代表场景经过正式 Runtime、驱动、Session、监督者、执行器、协调器和计算器；
2. tick 20 的边界发布早于 Session 组装提案和监督者批准；
3. prepared query 只因完整 `CellFact` 的观察时间戳变化而拒绝；
4. tick 21 的入口偏差和 tick 22 的过期属于第一次误拒后的结果。

## 3. 重建的 D116-0 RED：tick 20 事实

下表来自显式 `legacy_full_cell_fact` 诊断 mutation。它重放旧门槛，但不是最初未提交文件的原字节。

| 项目 | 结果 |
|---|---|
| 旧控制器状态 | `complete` |
| `_advance()` 是否调用 | 是 |
| 边界变化 | `None -> 2` |
| Session 路线决定 | `prepared_action_boundary` |
| 真实身体命中 proof entry | 是 |
| 输入账本追到当前 tick | 是 |
| 已观察到着地 | 是 |
| 请求窗口允许 tick 21 | 是 |
| 完整依赖事实相等 | 否 |
| 去掉观察时间戳后的依赖内容相等 | 是 |
| 正式依赖版本相等 | 是 |
| prepared query | `dependency_changed` |
| 监督者结果 | `rejected / entry` |
| Runtime 最终输入 | neutral，tick 21 生效 |

## 4. 结论

D115 的顺序判断不成立。执行器在同一控制调用中先发布边界，Session 随后才申请批准。

当前第一个阻塞点是依赖身份：`stage_prepared_action()` 把带新观察时间戳的同一格方块当成依赖变化。D116-1 应先修这条共享规则。现在调整 Session 调用顺序会改错层。

重建 RED：`evidence/motion_navigation/d116-handoff-timeline-v1/d116-0-red-timeline.json`。

## 5. 证据边界

- 没有运行 Fabric；
- 没有证明连续两跳已经可用；
- 诊断补丁不属于生产能力；
- “去掉观察时间戳后的内容相等”只用于归因，不能直接作为生产安全规则；
- D116-0 最初的同名未提交时间线已经被覆盖；当前 RED 是显式旧门槛重放，不能冒充原字节；
- D115 原始失败证据不回写，本验收只更正其根因解释。

## 6. D116-1 定向结果

命令：

```powershell
.\.venv\python.exe -m unittest `
  tests.motion_nav.test_d116_semantic_dependency_handoff `
  tests.motion_nav.test_d116_handoff_timeline -v
```

结果：12／12 通过，其中 6 项语义依赖边界，6 项分别检查重建 RED 和现行正式链结果。

| 检查 | 结果 |
|---|---|
| 纯 stamp 刷新 | `READY_MATCH` |
| source owner 缺失 | 保守拒绝 |
| `HISTORY_INCOMPLETE` | 保守拒绝 |
| 七类 knowledge／block／fluid／collision 变化 | 全部拒绝 |
| visual-air 元数据刷新 | 不作为物理变化 |
| ControlledDrop 缺失、过期、过远、下部不可见证据 | 仍由前置条件拒绝 |
| tick 20 query | `READY_MATCH` |
| tick 20 监督者 | `APPROVED` |
| tick 20 Runtime movement winner | 1 条 |
| tick 21 第二跳首条输入 | 实际 `APPLIED` |
| owner 空档 | 0 tick |
| 伤害 | 0 |

相邻的正式动作连续性和动作前置条件检查 39／39 通过。

## 7. 新阻塞与止损

第二跳输入虽然已经应用，但任务随后没有完成。

### 7.1 路线追踪身份落后一段

tick 21 的事实：

- prepared controller 已经对应 action 2；
- `ActiveRouteTracker` 的 incumbent 仍对应 action 1；
- 路线复核返回 `STOP / route_identity_changed`；
- 执行器以 `dependency_changed` 取消新控制器；
- 控制器进入 `recovering`，Session 最终保持 `BODY_RESPONSIBILITY_UNRESOLVED`。

这说明动作索引的提交和路线追踪身份的提交没有形成同一个交接。继续调入口或依赖规则不能解决它。

### 7.2 prepared admission 缺少生命周期 apply 事件

第二跳求解结果有 `finish`，接纳记录也是 `APPLIED`，但异步生命周期没有对应 `apply` 事件。监视器在 tick 20 之前报告一次 I19：

`result applied without current identity, facts or time`

显式旧门槛重放也报告同一个 I19。因此它是此前已存在、这次纳入门禁的缺口，不是 D116-1 语义修复造成的回归。

具体 movement tick 会受进程内 worker 调度影响，所以门槛检查事件的相对顺序和唯一性，不把某个绝对 tick 写死。

两项均已加入缺陷台账。按 D116-1 止损规则，本轮不修生产生命周期，不运行扩大矩阵和 Fabric。

计划中的五类独立 mutation 没有运行；当前 11 项定向检查直接覆盖相同边界，但不能替代 mutation 结论。

## 8. 证据与性能边界

证据现在通过 `evidence/motion_navigation/d116-handoff-timeline-v1/index.json` 索引：

- 重建 RED：`d116-0-red-timeline.json`，SHA-256 `D2B8E05D2293BA8E1DBD5BF293925F614A9A69EA1A3608114A95D095E34FDB69`；
- 现行结果：`d116-1-semantic-result-timeline.json`，SHA-256 `295A62DE067B0D4B26F0447C0D9999DFA71219BE1C4FE343C27D9A7BC6290093`。

两份文件都连续生成两次并得到相同哈希。含混的 `timeline.json` 已删除。

本轮没有运行完整 Windows 正逆、公开快照或 Fabric。只读时间线含方法级插桩，不适合作性能量尺。因此 D116 没有签署依赖检查耗时或 8／15／30 ms 门槛，也没有宣称连续两跳能力完成。

计划见 [D116-1 prepared proof 语义依赖修复计划](../stages/d116-semantic-dependency-repair-plan.md)。
