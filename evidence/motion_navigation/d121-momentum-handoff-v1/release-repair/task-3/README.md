# Task 3：prepared registry 接口与恢复复核测试

状态：接口修复和直接检查已完成；18 个模块、601 项通过，无跳过。D102 证据模块被独立历史字节问题阻塞。D121 仍为 PARTIAL／STOPPED。

## 修了什么

`ActionRouteExecutor.activation_registry` 现在公开只读的既有 activation registry。普通执行器或没有初始化 prepared 服务的测试替身返回 `None`。`RouteControl` 只读取这个公开属性，不再跨模块读 `_prepared_result_inbox`。没有新 registry、字段或释放路径。

短恢复测试不再把 `recovery_horizon_ticks=1` 附到已有的长尾迹上。它仍断言 1 tick 求解被拒绝，并断言非法的证明缩短会被构造器拒绝。复核部分使用原本合法的完整证明，在已知世界里缩窄落点支撑并移除其后一格。测试逐 tick 确认主轨迹完全不变，然后确认停车尾段不安全时复核拒绝。生产求解器和构造约束没有改动。

## RED 和变异

- `cancel-red.json`：取消收尾访问测试替身缺少的私有字段，抛 `AttributeError`。
- `proof-red.json`：旧测试在 `VerifiedMotionResult` 构造阶段就抛错，根本没进入复核。
- `revalidation-mutation.json`：让停车尾段复核错误地使用旧世界，改写后的测试会因错误返回 `SOLVED` 而失败。
- `line-endings.json`：四个修改文件保留原 LF，没有整文件换行转换。

## 测试基础设施前置

D102 证据模块需要现有的 G0 历史原始夹具。隔离 worktree 起初没有该忽略目录；经主 agent 确认，从主工作区只读复制了 24 个文件，复制前后 SHA 完全相同，主目录未变。目录只在 `.tmp` 中，不提交。见 `historical-fixture-preparation.json`。

复制后该模块仍被另一个历史字节问题阻塞：`continuous-motion-d101-v1/manifest.json` 在隔离 worktree 中是 LF，而历史证据绑定 CRLF。公开 3889d10 和主工作区原字节一致。本任务不修改该文件或历史哈希；主 agent 将用独立 Task 1b 恢复。两次失败记录都保留，分别为 `test_d102_evidence_contract-missing-fixture.json` 和 `test_d102_evidence_contract-fixture-ready.json`。见 `historical-reference-byte-blocker.json`。

完整 Windows／公开快照门禁、Task 4 和 D122 不属于本任务。

## Windows 检查结果

三个代码模块，以及 D102／D117／D121 相邻检查共 19 个模块。18 个模块、601 项通过；唯一阻塞是上文的 D102 历史字节前置。`checks-green.json` 只保存命令、结果、数量和原始日志引用，完整输出各保留一份。未宣称完整源码树或公开快照全绿。
