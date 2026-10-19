# D118-0 带动量连续跨隙校准计划

日期：2026-10-18。对应决定见 [D118](../decisions/0118-calibrate-delay-tolerant-momentum-gap-chain.md)。

状态：**FAILED／STOPPED。** C0—C3 的代码与组件检查已经完成。修正 setup、单会话收尾和观察诊断后，最新 Windows 朝南 smoke 的 Fabric probe 通过，273／273 条命令的 Runtime receipt、physics 和 F-TICK 身份逐项对应。7 个 trial 中 6 个与计算器一致。`fallback-back-south-0` 从 movement tick 246 起出现离散状态差异，查明项目计算器遗漏了原版的 0 高度 step 候选。该结果触发物理一致性停止门槛，D118-0 不签署。

当前正式 Runtime 只能逐 tick 串行提交固定输入。因此 C2 只能观测下一次可用的 `L+1` 应用，不能把它写成着地 tick `L` 的预排程，也不能把清单里的对照 hold 当成真实传输晚到。两项能力都保持未签。

C3 为每个 trial 单独记录并重建地形。fixture 明确记录 origin 和 feet_y；不同跨隙长度即使复用同一个世界坐标，也不会在离线分析时互相覆盖。分析从该 trial 第一条 physics 记录的真实 `pre_state` 开始，同时运行逐 tick 对照和开环重放。fallback 余量使用真实平台覆盖边界，并同时检查前后和横向；origin 不从玩家位置反推。最终 smoke 中，`fallback-sneak-south-0` 在 `L+1` 生效并安全停住，但纵向余量只有 `0.000551` 格；第二跳按时输入在 `L+1` 生效，late1 在 `L+2` 生效。第二跳入口仍没有冻结过可证明安全的状态包络，因此入口余量保持 typed `unverified`。

封存结果分开写 `capture_status` 和 `acceptance_status`。本次原始证据完整捕获，Fabric probe 通过，但物理验收失败，所以顶层结果保持 failed。此前 JDK、锁定资产、独立导出、setup 和 F-TICK 关联问题产生的 `INVALID` 运行全部保留，不能被最新运行覆盖。

## 1. 目标

在不改生产运动能力的前提下，回答四个问题：四格疾跑跳是否与计算器一致；落地即跳是否能按预定 tick 生效；走出边缘的最后一个着地 tick 是否仍能起跳；高速落到一格平台后，潜行是否能在 `L+1` 前安全收尾。

推荐主场景是同高度直线地形：第一段一格间隙，中间一格平台，第二段四格间隙。主场景只用于冻结输入和校准，不接入规划器。

## 2. 文件边界

新增：

- `scripts/d118_momentum_calibration_runtime.py`：建立固定地形，执行固定输入，产出逐试次记录；
- `scripts/d118_momentum_calibration.py`：运行 Windows Fabric、分析并封存证据；
- `tests/motion_nav/test_d118_momentum_calibration.py`：冻结计算器事实、输入清单和证据判定。

最小修改：

- `scripts/probe_fabric_deployment_observation.py`：增加独立的 `--d118-motion-calibration-probe` 分支，放开现有物理／F-TICK sidecar；
- 部署探针已有选项测试、公开导出清单和项目入口文档。

复用而不改：

- `ClientPhysicsTickDiagnostics`；
- `ClientFtickDiagnostics`；
- `compare_motion_prediction.py` 的 declared-command 回放；
- D114 已验证的传送稳定等待；
- Runtime 唯一输入出口和输入回执。

不修改 `mc2p/` 生产运动代码、Java sidecar 协议、动作 profile、规划器和 Session。

## 3. 实施任务

### C0：冻结行尾和探索事实

1. 记录所有预计修改文件的行尾状态；
2. 用项目运动计算器重放以下事实：
   - 冻结模板内，停稳四格跳候选对输入流首条晚一 tick 的容忍数为 0；
   - 带动量入口存在同时覆盖起点 0／1 tick 分支的候选；
   - 当前第一跳出口速度／落点与第二跳入口范围不相交；
   - 高速落到一格平台后，neutral 和 back 尾段离开平台，sneak 尾段安全停住；
3. 测试表述限定为冻结模板，不声称穷尽 Minecraft 全部技巧；
4. 固定输入、地形、方向变换和预期事件写入不可变清单并计算哈希。

### C1：建立组件校准运行器

1. 建立四族试次：
   - 四格疾跑跳；
   - 落地即跳／提前准备下一跳；
   - 走出边缘仍着地的最后起跳 tick；
   - 高速落地后的 neutral、back、sneak 尾段；
2. 所有输入经过正式 Runtime 出口；
3. 每个输入同时记录 Runtime receipt、物理 sidecar 和 F-TICK 的 request sequence 与 tick；
4. 明确区分后台交付、输入流起点分支和流中段偏离；
5. 每场开始使用 D114 的传送稳定规则，并硬检查第 0 帧着地、静止、位置、yaw 和世界事实；
6. 组件结果只证明夹具、固定输入和计算器判定，不替代 Fabric。

### C2：接入 Windows Fabric 探针

1. 为部署入口增加互斥的 `--d118-motion-calibration-probe`；
2. 复用现有 server、client、IPC、physics sidecar 和 F-TICK sidecar 生命周期；
3. 先运行四族各一场朝南 smoke；
4. smoke 全部有效且符合预期后，运行四方向各 3 次；
5. trial 若不能唯一对应 observation、decision、receipt、input sampled、movement applied 和 post-state，则标为 `INVALID_HARNESS_RUN`，不能计入通过。

### C3：只读比较和签署

1. 按每个试次实际观察到的初态逐 tick 重放计算器；
2. 报告三轴位置／速度误差、yaw、着地、碰撞、疾跑、潜行和姿态；
3. 报告离地、落地 `L`、第二跳输入和潜行 fallback 的实际应用 tick；
4. 检查潜行 fallback 在 `L+1` 前生效，并让身体留在中间平台；
5. 报告第二跳入口和 fallback 在实机误差后的剩余余量；
6. 产出 manifest、输入哈希、源码语义哈希、精简 summary 和原始文件哈希；
7. 只在全部门槛通过后签署 D118-0。否则保留失败并停止。

## 4. RED 和 mutation

组件检查至少能够发现：

- 把输入流首条晚一 tick 错当成流中段晚到；
- receipt、physics 和 F-TICK 的 request sequence 对不上；
- 潜行改成 neutral 或 back；
- 潜行延迟到 `L+2`；
- 用传送直接伪造高速入口；
- 删除一个离散状态比较；
- 位置或速度误差越过 B09-R 上限；
- 运行中改写固定输入；
- 只写行尾变化的文件。

## 5. 验收顺序

1. 直接单元测试；
2. 探针 CLI、组装和证据分析测试；
3. 相关 Runtime／sidecar／传送等待回归；
4. Windows Fabric 朝南 smoke；
5. 四方向小矩阵；
6. 受影响与相邻 Windows 检查；
7. 公开快照校验。

任何前置步骤失败，都先查明它是否否定当前阶段假设。不能跳过失败继续跑更大的矩阵。

## 6. 正式命令

目标命令：

```powershell
.\.venv\python.exe scripts\d118_momentum_calibration.py `
  --output evidence\motion_navigation\d118-momentum-calibration-v1 `
  --server-port 25597 `
  --ipc-port 8140
```

包装器内部使用：

```powershell
.\.venv\python.exe scripts\probe_fabric_deployment_observation.py `
  --seed 21001 `
  --server-port 25597 `
  --ipc-port 8140 `
  --d118-motion-calibration-probe `
  --time-diagnostics `
  --physics-tick-diagnostics `
  --ftick-diagnostics `
  --timeout-seconds 600
```

正式平台是 Windows。其他平台结果只能作为参考，不能签署 D118-0。

## 7. 原定后续条件路线图（未授权）

D118-0 没有通过，所以下列路线没有获得实施授权。将来只有在新决定修复并重新校准物理后，才可以重新评估 fallback、链式求解和组合边。原设计约束保留如下：

- 复用现有 `PreparedSafetyTail` 和 D117 原子交接；
- 先求第二跳 `SegmentEntryWindow`，再约束第一跳；
- 一条短平台组合边接纳后仍展开为两个现有 `JumpGapSegment`；
- 不增加 Session 状态，不给通用规划节点增加速度维度；
- 控制 tick 只比较已经绑定的证明和依赖，不现场做无界搜索。

## 8. 阶段收尾

- C0、C1 和 C2 的朝南 smoke 已执行；C3 在物理一致性门槛失败；
- 四方向 full matrix 未运行；
- 生产 profile、求解器、规划器、Session 和 prepared 默认路径均未接入本能力；
- D118-1—D118-5 未授权；
- 最终失败与全部历史 `INVALID` 记录保留在 `evidence/motion_navigation/d118-momentum-calibration-v1/`；
- 计算器遗漏 0 高度 step 候选登记为 `D118-PHYSICS-01`。修复该问题需要新决定，不能在已经停止的 D118-0 中继续追加修正。
