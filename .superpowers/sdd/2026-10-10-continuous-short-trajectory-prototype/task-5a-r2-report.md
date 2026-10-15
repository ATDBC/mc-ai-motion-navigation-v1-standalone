# Task 5A-R R2：候选与终结

基线：`104318a00e1af8302e986e1b6b465a99e6a7d71f`。R2 候选：`cfd36acd`。停止记录提交：`f439e9d`。正式范围：D096 第4节。最终状态：R2未通过，TP结束。

## 实现

- 新增必填累计 `InputTier`。后一层严格保留前一层有序前缀，停车输入存在于每层，最后一层等于 `supported_inputs`。
- 冻结 A3／A5／A15 分别包含3／5／15个真实输入；A15 新增五组朝向 Walk／Jump，每个步态都有同朝向起跳配对。
- 请求新增最小终速，默认0，且不能超过目标最大终速。
- 候选按层枚举 `G(0..20) → 可选J1 → A(0..16) → B(k)`。旧层完整优先；旧层 FOUND 后不进入新层。
- 动作元身份显式绑定输入层、步态索引、移动 yaw、是否起跳和 G／A／B 保持 tick。
- 同一输入前缀保存全部时序分支状态，不按单一准时状态合并。全部层、目标检查和 scanner 共用同一 `CountedPhysics`。
- 正常路线中的 B 只枚举停车输入前缀，不要求完全停止。scanner 的安全 `StopTail` 独立保存，不混入返回 inputs。
- 结果新增 `winning_tier`、`completed_candidates`、`commitment_scans`。

## RED／GREEN

合同、结果字段、完整矩阵和疾跑层真实覆盖均先取得 RED。完整记录在 `evidence/motion_navigation/trajectory-proto-5ar-v1/red-green.md`。

`flat_sprint` 先测得 A3／A5 冻结20-tick模板的最远安全停车点分别为 z=6.962757644806954 和 z=9.970127011683733，再把目标带固定为7.5—8.3。A3 为 SEARCH_EXHAUSTED，A5 由含 sprint 的输入 FOUND；未调整 P0 预算。

## 当前结论

候选的22项正例全部 FOUND，六类负例保持精确分类，TP 94／94、相关回归62／62、standalone 12／12。但独立审查发现 G／J／A 被绑定，旧检查没有覆盖合法混合动作元，候选不能签署。

唯一限定修正使审查反例转绿，但三个冻结A15正例均耗尽65,536 physics step。按D096，R2未通过，R3未开始，TP结束。未提交修正diff与计数保存在 `evidence/motion_navigation/trajectory-proto-5ar-v1/`，代码恢复到 `cfd36acd`。

R2 只记录计数，没有执行 R3 预热、20次测量或墙钟门槛。没有修改 `mc2p`、正式文档、worker、Runtime 或闭环。
