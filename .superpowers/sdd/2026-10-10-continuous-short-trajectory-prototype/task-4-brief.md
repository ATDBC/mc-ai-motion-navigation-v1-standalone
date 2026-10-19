# Task 4：实现 P0 承诺边界扫描

## 目标

在 `experiments/motion_navigation/trajectory_proto/` 内，用现有 Java 1.21 运动计算器逐 tick 重放一条显式候选输入，并测出每个风险区间的：

- 最后一个仍可安全放弃的边界；
- 首个已承诺边界；
- 重新落地并恢复为可停止状态的边界；
- 从每个边界开始的停车尾迹与依赖。

本任务不实现搜索器、worker、Runtime 接入或生产动作迁移。

## 必读

- `docs/motion_navigation/decisions/0095-isolate-continuous-short-trajectory-prototype.md`
- `docs/motion_navigation/architecture/continuous-short-trajectory-prototype-v1.md`
- `docs/motion_navigation/stages/TP-continuous-short-trajectory-prototype-plan.md`
- `docs/motion_navigation/acceptance/TP-continuous-short-trajectory-prototype.md`
- `docs/superpowers/plans/2026-10-10-continuous-short-trajectory-prototype.md`
- Task 3 的 contracts、scenarios、report、review
- `mc2p/motion_nav/physics_1_21.py`
- `mc2p/motion_nav/physics_rollout.py`
- `mc2p/motion_nav/motion_solver.py` 中 `_release_recovery_evidence`

## 文件范围

新增：

- `experiments/motion_navigation/trajectory_proto/physics.py`
- `experiments/motion_navigation/trajectory_proto/commitment.py`
- `tests/motion_nav/test_trajectory_proto_commitment.py`

允许最小修改：

- Task 3 contracts，只在扫描结果必须绑定 typed 证明时修改；
- TP stage/acceptance，登记 P0 已开始、首笔代码提交时间盒起点；
- standalone export 的精确原型测试排除如果现有通配已经覆盖则不要再改。

禁止修改 `mc2p` 生产代码。

## 必须先写的 RED

1. **承诺可早于起跳**：边界上已有不可撤回／在途的跳跃输入时，停车尾迹必须先消费它；因此首个承诺边界可以位于身体真正离地之前。
2. **风险区间不是全局单点**：同一轨迹可以 `可停止 → 已承诺 → 落地后重新可停止`。结果必须保留此前风险区间，不能因末尾重新可停而抹掉它。
3. **停车尾迹先消费无法撤回输入**：同一个边界在“不含在途输入”和“含在途跳跃输入”时可以得到不同结论，证明扫描器确实消费在途输入。
4. **边界前世界变化阻止提交**：候选和停车尾迹读取的依赖必须进入证明；承诺前相关依赖变化后，旧证明不能继续授予提交资格。
5. **UNKNOWN 分类**：候选或停车尾迹遇到 `NEEDS_WORLD` 时返回 `NEEDS_INFORMATION/UNKNOWN_WORLD`，不能当碰撞、BLOCKED、STALE 或预算耗尽。
6. **输入应用证据缺失**：无法确定在途输入时返回 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`，不能假设中性输入。
7. **确定性和有界性**：相同输入、状态、世界与预算得到相同轨迹哈希、区间和计数；节点／物理 step／尾迹 tick 上限耗尽必须有 typed 结果，不读取墙钟。

RED 必须确实由 API/行为缺失触发，并在报告中保留命令与关键输出。

## 实现约束

- 直接调用现有 `step()`／`PhysicsWorldView`／`JAVA_1_21_RULESET`；不要复制运动公式。
- 候选是一条显式、不可变的 `TickInput` 序列。本任务不生成候选。
- 每个边界单独声明已经无法撤回的输入。扫描器先重放这些输入，再追加中性输入直到安全停止或达到有界上限。
- “安全停止”至少要求：真实已知支撑、着地、水平速度进入现有稳定阈值并完成剩余中性滑行、没有超过候选正常结局的额外伤害风险。不要只看 `on_ground`。
- 如果安全判定需要阈值，优先复用正式 motion solver 的现有常量或公开规则；若私有常量不宜导入，在原型中写明来源并由测试锁定，不反向依赖生产私有函数。
- 风险区间按边界序列形成。一个区间结束后，后续再次不可停止可形成新风险区间。
- 证明必须绑定请求／世界身份、轨迹输入、依赖、物理规则、预算和轨迹哈希；不得只返回布尔值。
- 承诺前依赖变化检查只验证已绑定事实。不要因未绑定事实变化而重选另一条证明。
- `BLOCKED` 不用于单条候选失败。本任务若候选本身不安全，应返回候选拒绝或扫描失败；请求级 BLOCKED 留给参考搜索证明所有必要条件都不成立。
- `STALE` 只用于身份、目标、锚点或已绑定依赖过期。
- 决策逻辑不能读 `perf_counter` 或其他墙钟。
- 两个时序分支必须消费同一条候选输入前缀。本任务至少让扫描 API 能分别接收 Task 3 的两支入口状态和同一候选；不要让每支携带不同 inputs。

## 组件场景

优先使用小型确定性世界：

- 平地刹停；
- 一格 JumpUp；
- 一格 JumpGap；
- 落地后继续滑行再停止；
- 候选依赖格变为 UNKNOWN／被移除；
- 在途输入证据缺失。

测试夹具可以调用已有测试 helper 的公共生产构造，但原型源码不能 import `tests`。

## 验证

至少运行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment -v
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_motion_solver_recovery -q
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

若最后一个模块名不存在，先查找实际覆盖 `_release_recovery_evidence` 的测试并替换，不能跳过相关验证。

## 文档与报告

- 将阶段状态改为 P0 已开始；登记首笔原型代码提交 `7d997913e4a6e1a406be5309b5b4b01c0681f88c`，时间 `2026-10-10T11:52:53+08:00`，两周时间盒从该时刻开始。
- 在 `.superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/task-4-report.md` 记录 RED、GREEN、API、依赖／预算边界、没有执行的项目和完整命令。
- 做一个聚焦提交，不把 Task 5 搜索器混入本任务。
