# Task 4 独立审查修复

基线提交为 `a0a4645aa9d51f37a42a5781869605b0c6be1c0f`。独立审查指出一个 P1：supervisor 用 `HandoffEvidence.reason` 的具体字符串决定是否消费已观察到的 fallback 释放见证。改动诊断文本会改变身体责任的结束流程，违反 reason 只用于报告的约定。

生产修复只删除这一个字符串条件。缓存中的 `RouteControl` 对象、committed executor、`QUIESCENT` 和原有 `retired_release_still_current()` 继续决定资格。后者仍要求 `RELEASED`、无 pending、选定的真实 tail、精确 witness tick、已应用回执及下一拍中性输入。owner、ledger、session、依赖和身体状态检查保持原样。没有新增 owner、状态或释放出口。

新增测试先让正式 `RuntimeNavigationDriver → Session → supervisor → route executor → coordinator → CalculatorBackend` 链真实执行到 tick36。在原 `retire_route()` 确认释放后，只改缓存 evidence 的 reason，其余字段和 controller 对象都不变。三个合法文本分别为 `renamed_for_display`、`observed_exact_fallback_release_witness_v2` 和 `consumed_exact_fallback_source_release`。测试禁止通用地面 rollout，要求 tick37 正常清理来源和一次性缓存，且伤害为零。

`reason-mutation-red-valid-identifiers.json` 保存修复前的正式 RED：三个子例都因转入被禁止的通用 rollout 而失败。`focused-green.json` 保存修复后的三项聚焦检查，包含 reason 变异、tiny-support 真实见证和尾段热路径。`reason-mutation-red.json` 是更早的夹具诊断：两个合法文本已复现同一失败，但第三个中文文本不符合现有 identifier 合约，并遇到 GBK/UTF-8 输出混用；它不作为正式变异门槛，原记录保留。

本目录的 `production-semantic-hashes.json` 单独保存修复后的语义哈希，并绑定旧 Task4 清单的原始 SHA-256。旧 Task4、Task3、Task2、D120 和历史 stop 证据没有修改。新的导出检查优先使用本目录清单，同时继续校验旧清单身份。

本轮只关闭这一个审查问题。原有四方向 88 场结果保留为初版事实，没有重跑或覆盖。真实 transport L+2、Fabric、全量发布矩阵和空闲／受控负载性能签署仍未运行。

Windows 受影响与相邻回归412/412通过（480.240秒）；独立导出内Task4与metrics59/59通过（71.762秒），2699文件verify通过。聚焦三项检查通过（15.237秒）。命令、原始输出和最终检查汇总分别见 `regression.json`、`independent-export.json`、`focused-green.json` 和 `checks.json`。

结论：Task4及时输入组件执行PASS。
