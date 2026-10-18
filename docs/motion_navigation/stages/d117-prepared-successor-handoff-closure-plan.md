# D117 prepared 后继交接收口计划

日期：2026-10-18。对应决定见 [D117](../decisions/0117-close-prepared-successor-handoff.md)。

状态：**COMPLETED／SIGNED OFF（组件范围）。**

## 1. 范围和事实基线

本阶段只在组件后端中临时恢复 `prepare_upcoming_action()`，用来验证 prepared 后继链。生产默认路径继续使用普通 `SOLVE`。

冻结以下基线：

- 现行生产两跳成功，提交两次普通 `SOLVE`，prepared 入口调用为 0；
- D116 代表链恢复自动准备后，tick 20 能取得 `READY_MATCH`，tick 21 能应用第二跳首条输入；
- prepared 登记成功后会出现 I19、tracker 身份落后和 1 ULP 状态差；
- 快照借用持续失败时，执行器会无限停在 `prepared_action_boundary`；
- D116 原失败证据、重建 RED 和现行结果不回写。

## 2. 实施顺序

### R0：修正量尺并冻结 RED

修改：

- `tests/motion_nav/test_d116_handoff_timeline.py`
- `scripts/d116_handoff_timeline.py`
- `tests/sim/async_monitor.py`

要求：

1. 时间线测试断言唯一的 `boundary < approval < activation < first application`，不把 receiver 调度写死为绝对 tick；
2. 增加确定性快照端口，可配置 `BUSY` N 次或一直 `BUSY`，并让它经过真实 `_seal_snapshot()`；
3. 监视器只读取 prepared requirement、pending boundary、controller state 和 movement tick 等 typed 字段，增加“prepared 边界等待有上界”和“prepared recovering 有上界”；
4. 先保留五项 RED，确认每个测试都会在对应错误上失败。

R0 只改测试和诊断，不改生产行为。

### R1：统一异步 apply 入口

修改：

- `mc2p/motion_nav/motion_coordination.py`
- `tests/motion_nav/test_d102_action_handoff.py`
- `tests/motion_nav/test_d116_handoff_timeline.py`

实现：

1. 抽出 coordinator 内部的结果应用函数；
2. 普通和 prepared 两条路径都执行 `try_apply -> install -> admission -> retire`；
3. prepared admission 写入同一个 `accepted_monotonic_ns`；
4. `try_apply()` 失败时不安装、不写 `APPLIED`。

RED：prepared 安装后出现 I19。

mutation：跳过 `try_apply()`，I19 必须重现。

### R2：统一完成当前动作的提交

修改：

- `mc2p/motion_nav/action_route_executor.py`
- `mc2p/motion_nav/route_body_controller.py`
- `mc2p/motion_nav/route_admission.py`
- `mc2p/motion_nav/execution_supervisor.py`
- `tests/motion_nav/test_d102_action_handoff.py`
- `tests/motion_nav/test_d116_handoff_timeline.py`

实现：

1. `ActiveRouteTracker` 把现有判断拆成纯 prepare／commit，prepare 返回只对当前 tracker 有效的本地计划；
2. 执行器提供完整的动作完成准备和提交，不读取 tracker 私有状态；
3. `RouteControl` 先取得 tracker 计划，再提交执行器和 tracker；不复制 `record_progress()` 的判断；
4. 普通 `_advance()` 与 prepared 提交共用伤害结算、候选清理和边界清理；
5. tracker 拒绝时，执行器下标、控制器、伤害和候选保持不变。

prepared 当前 strict 动作用 proof 出口／后继入口作为交接证据。以后若把该路径扩展到 Walk，必须提供真实 `RouteProgressEvidence`，不能默认传 `None`。

RED：prepared 激活后下一帧得到 `route_identity_changed`。

mutation：跳过 tracker 提交；另让 tracker 预检失败，断言执行器没有半提交。

### R3：统一 prepared 状态数值比较

修改：

- `mc2p/motion_nav/motion_candidate.py`
- `mc2p/motion_nav/action_route_executor.py`
- `tests/motion_nav/test_d102_action_handoff.py`
- `tests/motion_nav/test_d116_handoff_timeline.py`

实现：

1. 把现有 verified start 数值比较提升为同模块公开函数；
2. prepared 入口和运行中比较都调用该函数；
3. movement tick、物理条件和离散状态继续精确匹配；
4. 不新增新的容差常量。

RED：已冻结的 1 ULP 速度差进入 `RECOVERING`。

mutation：恢复逐位相等，正例失败；注入大于现有范围的真实偏差，负例必须拒绝。

### R4：给快照借用增加有类型的瞬时结果

修改：

- `mc2p/motion_nav/world_model.py`
- `mc2p/motion_nav/prepared_motion.py`
- `mc2p/motion_nav/motion_worker.py`
- `tests/motion_nav/test_d102_world_snapshot.py`
- `tests/motion_nav/test_d102_receiver.py`
- `tests/motion_nav/test_d116_handoff_timeline.py`

实现：

1. WorldModel 提供兼容旧调用方的 typed try-borrow 结果：`READY／BUSY／CAPACITY`；封存异常由 receiver 记录为 `INTERNAL_ERROR`；
2. 锁忙只标记本周期未完成，receiver 下一周期继续；
3. `BUSY` 不消耗本地失败额度，最终期限使用现有 `AsyncWorkWindow` 和 movement tick；
4. 容量不支持立即返回永久拒绝；
5. 封存异常返回内部错误，不再 `except Exception: pass`；
6. receiver 线程里不 `sleep`，测试也不靠增加 barrier 或等待次数修绿。

RED：一次锁忙被写成永久 `WORLD_SNAPSHOT`；封存异常也被写成缺少信息。

mutation：去掉跨周期重试；锁忙一次后代表链必须失败。

### R5：prepared 撤销、回退和恢复上限

修改：

- `mc2p/motion_nav/action_route_executor.py`
- `mc2p/motion_nav/motion_coordination.py`
- `tests/sim/async_monitor.py`
- `tests/motion_nav/test_d116_handoff_timeline.py`

实现：

1. coordinator 在永久拒绝、过期或重试用尽时撤销对应 prepared 要求；
2. coordinator 同时清理 prepared stream、submission、signature、selection 和 registry candidate，防止复用旧结果；
3. executor 只撤销尚未激活的 `current + 1` 要求，并清除对应 pending boundary；
4. 尚未激活时，执行器离开 `prepared_action_boundary`，由现有普通 `SOLVE` 继续；
5. 已激活且在空中时继续保留身体责任，本阶段不声称已经具备任意外力下的安全尾迹；
6. prepared 控制器在代表链稳定着地后转成现有 `INPUT_LOST`，进入 grounded recovery／replan；
7. 恢复预算用尽后返回类型化终态；
8. 监视器按 movement tick 检查边界等待和 recovering 上限。

RED：持续快照失败时 tick 20—80 一直等待；真实偏离后安全落地仍不结束。

mutation：去掉撤销通知或着地交接，活性门禁必须失败。

## 3. 端到端组件门槛

R1—R5 全部完成后运行：

1. D116 代表链，空闲条件 30／30；
2. D116 代表链，受控同核负载 30／30；
3. 快照 `BUSY` 1、3、5 次，各自完成；
4. 快照持续 `BUSY`，回退普通 `SOLVE` 并完成；
5. 容量不支持和内部错误，得到不同的类型化结果；
6. prepared 控制器进入 `RECOVERING` 且代表链已经安全着地后，进入普通恢复或有界终态；
7. 超出数值等价范围的真实状态偏离继续被拒绝；
8. I18—I22 零违规；
9. 同一帧只有一个移动 winner，owner 无空档，伤害不超额度。

完成 tick 只作为诊断。核心门槛使用相对顺序和上界，不依赖线程调度得到某个固定 tick。

## 4. 回归顺序

每个切片先跑直接模块，再扩大：

1. D116 RED 和正例；
2. D102 prepared proof、handoff、receiver、registry、runtime activation；
3. D113 交付抖动；
4. D114 正式驱动夹具；
5. `test_action_continuity_formal`；
6. package boundary 和 standalone export；
7. Windows 完整分片正序、逆序；
8. 独立公开快照。

完整组件通过前不运行 Fabric。

## 5. 提交与审查

R0—R5 各自形成一个可回退提交。每个生产切片都遵循：

1. 先提交 RED；
2. 实现最小修复；
3. 跑直接和相邻回归；
4. 独立审查该切片的职责、mutation 和旧成功退步；
5. 再进入下一项。

不把四个三方对照 diff 直接应用到生产树。它们只用于确认根因。

## 6. 止损条件

出现任一情况立即停止：

- 需要新增 Session 状态或第二套身体责任协议；
- 需要放宽动作入口、落点、UNKNOWN 或伤害规则；
- 快照忙闲处理需要在 receiver 中读取墙钟或睡眠；
- tracker 和执行器无法在预检后无半提交地完成；
- 五项修完后又出现第六个新的 prepared 阻塞；
- 两轮修正后端到端门槛仍未通过；
- 出现旧成功退步、安全违规、无界等待或无关行尾变化。

止损后保持自动 prepared 准备撤回。现行普通 `SOLVE` 路径不变。

## 7. 实施结果

R0—R5 已全部完成：

- R1 统一普通与 prepared 结果的异步 apply 顺序；
- R2 让 tracker、执行器、伤害账本和候选清理共同完成一次动作交接；
- R3 复用既有 verified-start 数值等价规则，只吸收浮点表示误差；
- R4 把快照锁竞争、容量不足和内容无效分成有类型的结果；
- R5 为未激活 prepared 后继增加完整撤销与普通 `SOLVE` 回退，并让已激活控制器在安全着地后进入既有 `INPUT_LOST` 恢复。

D116 代表链在空闲和受控同核负载下各 30／30 完成，完成 tick 都为 36。R5 直接与相邻检查 271／271 通过。Windows 源码树正序、逆序各 195／195 个模块、2473 项通过；独立公开快照正序、逆序各 194／194 个模块、2449 项通过。四组结果均为零失败、错误、超时和跳过。

生产默认路径仍使用普通 `SOLVE`。本阶段没有运行 Fabric，也没有把一格两跳的 1 tick 改善当作产品能力发布。
