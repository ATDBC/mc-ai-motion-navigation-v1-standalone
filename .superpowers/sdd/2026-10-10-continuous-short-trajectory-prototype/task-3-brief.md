## Task 3：实现 P0 合同和参考场景

新建：

- `experiments/motion_navigation/trajectory_proto/contracts.py`
- `experiments/motion_navigation/trajectory_proto/scenarios.py`
- `tests/motion_nav/test_trajectory_proto_contracts.py`

先写失败测试，覆盖五类结果、不可变请求、有限入口分支、预算和 UNKNOWN 分类。实现最小合同与场景登记，不写搜索器。
