# M0 RED／GREEN

## RED 1：量尺模块不存在

先增加 `test_m0_probe_module_exists_in_the_isolated_experiment`。首次运行得到预期失败：

```text
Ran 1 test in 0.001s
FAILED (failures=1)
AssertionError: False is not true
```

随后只建立隔离实验模块，再确认该检查转绿。

## RED 2：M0 合同尚未实现

在实现前写入冻结集合、正式选项、混合动作元反例、完整扫描／lazy 分栏、nearest-rank 和哈希确定性检查。首次导入得到预期错误：

```text
ImportError: cannot import name 'FORMAL_OPTIONS' from
'experiments.motion_navigation.trajectory_proto.m0_probe'
```

最小实现完成后，六项合同检查转绿；加入 CLI 后为七项。

## RED 3：测量入口不存在

先写 CLI 检查，要求输出正式口径、完整扫描 step 和 lazy step 估算。首次运行：

```text
AssertionError: False is not true
```

加入测量入口后转绿。既有包边界门禁随后证明 `scripts/` 不能静态依赖实验代码，因此入口移入 `m0_probe.py`，以 `python -m experiments.motion_navigation.trajectory_proto.m0_probe` 运行；原门禁恢复绿色。

## RED 4：缺少每轮原始耗时

第一轮正式采集暴露出 JSON 只有分位数，无法复核样本对象。先增加 `samples_ms` 断言，得到预期 `KeyError: 'samples_ms'`，再补齐每轮原始值，并从全部动作样本直接计算总体 P95。

最终直接检查：7／7 通过。
