# D121 发布修复实施计划

> **执行要求：** 使用 `superpowers:subagent-driven-development` 按任务实施。每个任务先建立 RED，再做最小修复并独立复核。

**目标：** 修复 D121 公开快照的历史字节破坏和两处回归，在不改变 Task 4／5 结论的前提下，把 D121 可靠收口为 `PARTIAL／STOPPED`，为 D122 提供全绿基线。

**做法：** 公开导出继续由现有清单选择文件，但发布时允许指定上一份可信公开快照作为字节基线。若新旧文件除 CRLF／LF 外完全相同，继续使用基线字节；内容真正变化时才使用当前源码。冻结输入本身补上明确的 `-text` 规则。代码修复只恢复公共接口与测试意图，不放宽动作证明、安全门槛或终态。

**技术范围：** Python 3.11、Git 字节快照、Windows 正式测试、现有 standalone 导出器。没有 Java／Fabric 行为变化。

**依据：** [D121 决定](../decisions/0121-prove-fallback-backed-momentum-handoff.md)、[D121 验收](../acceptance/d121-fallback-backed-momentum-handoff.md)、三方审查 `reviews/2026-10-20-d121-transport-review.md`。

## 全局边界

- 正式平台是 Windows；Linux 结果只作交叉复核。
- `3889d10e298fec57ce7b0a82a63bef331c431bb7` 是本轮历史字节基线。
- 不更新历史记录中的哈希来适配新行尾；恢复原字节。
- 不修改 Task 2—5 的原始失败证据，只在新的 repair 目录追加检查结果。
- 不修改 Session、planner、动作 profile、Java、wire、Fabric 或安全阈值。
- 不把 D121 重新标成通过；最终状态仍为 `PARTIAL／STOPPED`。
- D121-R 全部通过之前，不开始 D122。

## 重点复核

- 新 worktree 中 LF 文件不得再次改写公开仓库已有的 CRLF 历史文件。
- 真正修改过内容的文件不能被旧公开字节覆盖。
- 公开快照新增、删除文件以及元数据、`SHA256SUMS.txt` 必须准确。
- prepared registry 的公开访问不能让测试替身或普通非 prepared 路径报错。
- 强化后的 `VerifiedMotionResult` 构造约束保持不变，测试不能伪造已不合法的证明。

---

### 任务 1：冻结失败与历史字节基线

**文件：**
- 修改：`.gitattributes`
- 修改：`tests/sim/manifests/navigation-product-r28-v8.json`
- 修改：`evidence/motion_navigation/F2REC-recovery-v1/r0/tool-provenance.json`
- 修改：`experiments/navigation-floating-wall-mazes-v1/layouts/maze_03_floating.json`
- 新增：`evidence/motion_navigation/d121-momentum-handoff-v1/release-repair/`

**接口：**
- 输入：公开快照 `3889d10e` 的原始字节。
- 输出：三个冻结输入在任意 worktree 中均保持原 SHA-256。

- [x] 保存当前 7 个回归模块的 Windows RED 和三个冻结文件的现有／期望哈希。
- [x] 给三个冻结路径增加最小 `-text` 规则，从 `3889d10e` 恢复原字节。
- [x] 运行四个行尾相关模块，确认由哈希造成的失败关闭。
- [x] 提交独立的历史字节修复。

### 任务 2：让公开导出保留上一版的等价字节

**文件：**
- 修改：`scripts/export_motion_navigation_standalone.py`
- 修改：`tests/motion_nav/test_standalone_export.py`
- 修改：`AGENTS.md`

**接口：**
- 产生：`ByteReconciliationReport`，以及
  `export_tree(root, *, clean, manifest=None, byte_base_root=None)` 的可选基线参数。
- 产生：导出命令的可选 `--byte-base-root <verified-standalone-root>`；命令在 stdout 输出结构化协调统计，不把临时报告混入公开树。
- 规则：共同路径的 UTF-8 文件仅在 CRLF／LF 归一后相等时复用基线字节；二进制、内容变化、新增和删除按当前导出处理。
- 输出：结构化报告列出复用、当前、新增和删除的路径；元数据仍指向当前源码提交，哈希在复用后重建。

- [x] 写 RED：基线 CRLF／候选 LF 保留基线字节；真实内容变化使用候选；新增／删除准确；损坏的基线哈希被拒绝。
- [x] 实现最小的字节协调函数和 CLI 参数，不引入远端访问或隐式 Git 操作。
- [x] 用 `3889d10e` 公开树和当前源码生成候选，断言 1614 个纯行尾变化归零，D121 实际改动仍存在。
- [x] 运行 standalone 导出与 verify 检查。
- [x] 提交导出修复。

### 任务 3：修复 prepared registry 接口和证明测试

**文件：**
- 修改：`mc2p/motion_nav/action_route_executor.py`
- 修改：`mc2p/motion_nav/route_body_controller.py`
- 修改：`tests/motion_nav/test_navigation_session.py`
- 修改：`tests/motion_nav/test_action_continuity_solver_limits.py`

**接口：**
- 产生：`ActionRouteExecutor.activation_registry` 只读公开属性，返回现有 registry 或 `None`。
- 消费：`RouteBodyController.activation_registry` 只使用公开属性，不读取 `_prepared_result_inbox`。

- [x] 保存 cancel 测试的 `AttributeError` RED，以及短恢复证明测试在构造阶段提前失败的 RED。
- [x] 增加只读 registry 接口；普通路径和测试替身返回 `None`，严格 release 逻辑不变。
- [x] 改写短恢复测试：求解仍拒绝 1 tick recovery；重验证使用合法证明和真实的不安全变化触发拒绝，不通过构造非法 `VerifiedMotionResult` 绕过契约。
- [x] 运行三个代码相关模块以及 D102／D117／D121 相邻检查。
- [x] 提交代码与测试修复。

### 任务 4：修正 Task 5 诊断和入口文档

**文件：**
- 修改：`scripts/d121_transport_late_probe.py`
- 修改：`tests/motion_nav/test_d121_transport_late.py`
- 修改：`packaging/motion-navigation-standalone/AGENTS.md`
- 修改：`packaging/motion-navigation-standalone/README.md`
- 修改：`docs/motion_navigation/acceptance/d121-fallback-backed-momentum-handoff.md`
- 修改：D121 计划、决定和本计划的结果段。

- [x] 将 never 场从“协议阻止过期应用”改成有类型的“未送达／不适用”，保留旧证据并追加新结果。
- [x] 公开入口统一写成 `PARTIAL／STOPPED`，不再写 `IN PROGRESS` 或“不新开 D122”。
- [x] S2 复现命令明确固定在 `3889d10e` 检出，不引用当前已删除脚本。
- [x] 运行 Task 5、设计量尺和文档／导出检查。
- [x] 提交诊断与文档修复。

### 任务 5：完整 Windows 与公开快照签署

**文件：**
- 新增：`evidence/motion_navigation/d121-momentum-handoff-v1/release-repair/final/`
- 修改：D121 验收和入口状态的最终数字。

- [x] 源码树运行完整 `tests/motion_nav` 正序、逆序；零失败、错误、超时和跳过。
- [x] 用 `3889d10e` 作为字节基线生成独立公开快照并 verify。
- [x] 在独立公开快照运行完整正序、逆序；零失败、错误、超时和跳过。
- [x] 核对历史证据引用、三个冻结输入哈希、D121 原始失败证据和生产语义哈希。
- [x] 独立审查全部修复；发现新 P0／P1 或清单外退步时停止，不进入 D122。
- [x] 提交最终验收记录。完成后才允许编写 D122 决定。


## Task 4 结果（2026-10-20）

Task 5 诊断 12/12 通过，v2 三场矩阵仍为 `FAILED_STOPPED`。及时与未送达的过期拦截状态均为 `not_applicable`／null，晚到真实应用为 `not_prevented`／false；输入、物理终态和停止原因与旧记录一致。旧证据不改，新增结果见 `release-repair/task-4/`。包装入口和历史复现命令已修正，D122 尚未开始。

相邻、设计量尺和导出检查共 87 项，85 项通过；两项失败来自 Task 3 修复后尚未追加最终生产指纹，由任务 5 集中登记，不改历史指纹。因此本任务不签署完整发布全绿。动作分支 43、未分类 0。

基于 `3889d10e` 字节基线生成的本任务独立快照 verify 2829 文件通过；这不替代任务 5 的完整正逆序测试。


## D121 发布修复最终 Windows 门禁（2026-10-20）

源码树正序、逆序各 202/202 模块、2662 项通过；独立公开快照正序、逆序各 201/201 模块、2638 项通过。四轮均为零失败、错误、超时、跳过和清理失败，单模块上限保持 300 秒。公开快照按既有清单排除依赖非公开 G0/G1 原始证据的 24 项模块；该模块在源码两轮均已运行并通过。

首次完整正序因隔离 worktree 缺少仓库相对 `.venv/python.exe` 而无效。失败报告原样保留，使用忽略的 junction 指向已有正式环境后，独立模块 7/7，再从头重跑完整正序；没有修改测试或历史命令。

公开字节基线由 `3889d10e` 的 Git 对象独立解包，并先校验自身 `SHA256SUMS.txt`。1614 个历史纯行尾改写中，1611 个恢复原字节；另外三个是本轮真实修改的导出器和 Task 3 的两个测试，按当前内容保留。最终纯行尾噪声为 0，四个冻结输入及 48 个旧 Task 5 文件的字节哈希一致。Task 1b、Task 4 和最终证据均实际导出。

当前生产语义记录以 `0e3b37d0` 为基准，覆盖 12 个 D121 生产文件；后续没有生产逻辑或测试变化。最终数字、完整命令、逐模块结果和导出复核见 `evidence/motion_navigation/d121-momentum-handoff-v1/release-repair/final/`。最终只读独立审查已 PASS，未发现新的 P0／P1，允许进入 D122；持久摘要见同目录 `independent-review.json`，它是审查结论记录，不是新增命令原始输出。D121 保持 PARTIAL／STOPPED，Task 5 真实传输失败不改写。没有新增 Fabric 或 Java 运行；D122 已另写决定和计划，代码尚未开始。
