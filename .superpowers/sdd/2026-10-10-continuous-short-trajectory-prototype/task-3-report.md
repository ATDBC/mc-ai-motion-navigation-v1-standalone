# Task 3 实现报告

Task 3 已完成。来源基线为 `2fd228c9`，聚焦提交为 `7d997913e4a6e1a406be5309b5b4b01c0681f88c`。没有修改 `mc2p` 生产源码，没有创建搜索器、worker 或其他空壳模块。报告保存在本地代理报告目录，不进入源码提交。

已先读取 task-3-brief、D095、连续短轨迹架构、TP 阶段和验收文档，再按严格 TDD 写测试和实现。

## RED 与 GREEN

第一轮先创建 `tests/motion_nav/test_trajectory_proto_contracts.py`，当时原型两个模块尚不存在。实际命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts -v
```

原始输出关键部分：

```text
ImportError: Failed to import test module: test_trajectory_proto_contracts
from experiments.motion_navigation.trajectory_proto.contracts import (
ModuleNotFoundError: No module named 'experiments.motion_navigation'
Ran 1 test in 0.000s
FAILED (errors=1)
```

这次失败由 API 不存在导致。随后实现合同和场景登记，相同命令首次输出：

```text
Ran 15 tests in 16.119s
OK
```

检查公开导出时发现，原有 `tests/motion_nav/test_*.py` 会把新的原型测试选入公开包，但不会导出它依赖的原型代码。经父任务明确授权，在本任务增加精确排除。先补失败测试，实际命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts.TrajectoryProtoContractsTests.test_standalone_export_does_not_include_prototype_tests -v
```

原始输出关键部分：

```text
AssertionError: True is not false
Ran 1 test in 15.292s
FAILED (failures=1)
```

最终新模块检查：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts -q
```

```text
Ran 16 tests in 32.667s
OK
```

受影响合同和 common 检查：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_b09r_physics_contracts tests.motion_nav.test_b06_movement_contracts tests.test_runtime_contracts -v
```

```text
Ran 55 tests in 35.096s
OK
```

上述 55 项检查之后只补强了公开排除测试的断言，没有再改实现；新模块随后按上文 16 项重新运行。

现有公开导出检查：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

```text
Ran 12 tests in 27.177s
OK
```

`git diff --cached --check` 返回 0。所有命令使用项目 Windows `.venv\python.exe`，没有用 Linux 结果替代正式平台。没有运行完整运动导航、参考搜索、承诺扫描或 Fabric，不把这些组件检查写成 P0 验收通过。

## 文件与接口

- `experiments/motion_navigation/trajectory_proto/contracts.py`：不可变 `SearchBudget`、`TrajectorySearchRequest`、`TrajectorySearchResult`，以及 `SearchStatus`、`SearchReason`、`TimingBranch`。
- `experiments/motion_navigation/trajectory_proto/scenarios.py`：不可变 `P0Scenario` 和 18 项 `P0_SCENARIOS`。登记平地 Walk／Sprint、90° 转弯、直线／转弯 JumpUp、一至三格 JumpGap、半砖、楼梯、短落地、UNKNOWN、已知阻塞、预算反例，以及 JumpUp／JumpGap 落点移除扫描。
- `tests/motion_nav/test_trajectory_proto_contracts.py`：冻结身份、不可变数据、所有五类结果、四项预算、时序分支、输入前缀、场景登记和静态边界检查。
- `config/motion-navigation/standalone-export-v1.json` 与 `scripts/export_motion_navigation_standalone.py`：支持 `exclude_globs`，仅配置原型源码目录与 `test_trajectory_proto_*.py` 两条精确排除。静态检查同时确认正常生产和既有物理合同测试仍在导出中。

`SearchBudget` 显式包含 `max_nodes`、`max_physics_steps`、`max_trajectory_ticks`、`max_timing_branches`，只接受正整数，拒绝 bool；轨迹最长 40 tick，最多两支。

`TrajectorySearchRequest` 绑定 request_id、PhysicsState 入口集合、world_session／geometry_revision、GoalState／goal_revision、TaskDamageBudget、anchor_id、input_ledger_id、预算、时序分支、允许生效 tick、共享输入前缀、支持的输入集合和路线引导。集合均为不可变 tuple。两支必须保持共同世界、物理规则和锚点 tick，且输入前缀只保存一份。生效 tick 固定为锚点的下一 tick，以及可选的再晚一 tick。

一个请求可声明两支，而搜索预算只允许一支。这种请求仍保留两支安全要求，后续搜索必须返回 `NO_TRAJECTORY_IN_BUDGET/TIMING_BRANCH_BUDGET`，不能删除晚到支或改报 STALE。

结果严格保持 `FOUND`、`NEEDS_INFORMATION`、`BLOCKED`、`NO_TRAJECTORY_IN_BUDGET`、`STALE` 五类。`SearchReason` 与状态交叉校验：UNKNOWN／缺输入应用证据只可归入 NEEDS_INFORMATION；BLOCKED 只接受已知必要条件原因；各项搜索预算和固定搜索耗尽只可归入 NO_TRAJECTORY_IN_BUDGET；STALE 原因仅为请求身份、世界依赖、目标、锚点和输入账本过期。单个候选碰撞没有请求 BLOCKED 的原因枚举。

## 证据边界与未决事项

`TrajectorySearchResult` 当前只保存分类，不生成轨迹或证明。后续 verifier／参考搜索负责实际验证完整轨迹和请求级必要条件，不能把填入 `VERIFIED_TRAJECTORY` 原因当作验证完成。

`P0_SCENARIOS` 只登记冻结场景元数据；可执行世界、具体入口／目标、测量命令及覆盖证明由后续 P0 实现补齐。扫描登记也不代表已经测出承诺边界。

默认 P0 量尺上限暂为 4096 nodes／65536 physics steps／40 ticks／2 branches。它是待测量的有界配置，不能声称覆盖已满足，也不能把它写成 P1 已冻结的正式预算。

原型目录被仓库既有 `experiments/` 规则忽略；两个原型源码已用精确路径 `git add -f` 纳入提交，没有放开整个实验目录。工作区提交后干净。
