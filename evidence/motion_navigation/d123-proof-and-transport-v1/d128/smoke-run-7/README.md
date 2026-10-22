# D128 smoke-run-7

状态：**INVALID_FIXTURE_RUN**。

正式 offline preflight、构建和启动通过；proof replay 与实时 anchor proof 构造也通过。首个 movement trial 尚未提交时，`prepare_fixed_successor` 以 `fixed successor selection differs from the current candidate context` 拒绝。具体不一致字段尚未签署。

该运行完成 0／3，`sessions=[]`，不消耗 smoke attempt，也不产生产品结论。按协调要求，后续正式运行暂停，矩阵未运行。
