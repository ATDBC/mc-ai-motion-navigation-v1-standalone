# D093：入口刹车和输入失联分类

正式环境为 Windows、项目 Python 3.11.16。`source_commit` 是取证时的 HEAD `d85ecba5`；取证包含未提交改动，因此以各组的文件指纹区分实际来源，不能只用 HEAD 判断版本。

## 可以复核什么

- `formal-red100/`：原代码四族各 100 个固定种子。`formal-red-focused.txt` 是生产修改前的三项预期失败。
- `round2-fixed/`：最终代码六族各八个种子。入口修复四族各 8／8；两个最终落点族各 3／8。
- `round2-random100/`：最终代码六族各 100 个种子。输入种子和具体晚到 tick 在 `inputs.json` 与逐项记录中。
- `formal-key-departure/`：入口前八 tick、起跳和实际离地的单次晚到，41／41。它在第二轮报告修正之前采集；四族全部成功，没有走第二轮修改的最终失联分类路径，H-01 生产文件指纹与最终版本相同。原 36 项 `formal-key-ticks/` 保留。
- `h1-only-landing-baseline/` 与 `h2-classification-only-comparison.json`：仅 H-01 对照和最终代码的 16 项身体位置、速度、输入与窗口逐项相同。H-02 改报告，不增加恢复输入。
- `paired-random100-comparison.json`：原代码与最终代码四族 400 项配对，完成 242→399，旧成功退步为 0。
- `regressions/`：v7、F2 528、v8、v9 对冻结历史的逐项比较。v9 的 24 项变化来自已完成的 D092，不归为本批能力提升。
- `performance/`：D058、D061 原始 JSON 压缩归档和压力族原始结果。D061 的原始 `passed=false` 保留；只未通过已有修订帧 P95≤8 ms 债务门槛。
- `full-forward.json`、`full-reverse.json`：最终测试树的 Windows 完整检查。
- `red-tool-compatibility.txt`、`tool-compatibility-focused-and-export.txt`：最终审查发现复制当前脚本回旧版本时缺少 `failure_cause` 字段，先用实际 `run_case` 报告读取路径复现，再兼容字段缺失。真正旧代码的种子 1 复跑在 `legacy-tool-replay/`，行为与原 RED 相同。三个生产文件没有再次修改。
- `fabric/`：四方向真实任务都完成，南／西真正注入一次普通 Walk 晚到，东／北未触发夹具条件。四批原始门槛都失败，新增 Fabric 签署未关闭；`audit.py` 只核对原始记录，不改变通过标记。

`formal-fixed/`、`formal-random100/`、`focused-green-formal.txt` 是第一轮候选。第一轮 H-02 将执行状态改成 INPUT_LOST，触发额外恢复，已否决。`h1-only-landing-control/` 也是第一轮诊断采集，名称不代表仅 H-01 对照；真正的对照目录是 `h1-only-landing-baseline/`。`round2-red.txt` 保留发现该问题的失败断言。它们不能作为最终分类已通过的证据。

工具兼容修正后，最终 Windows 正序、逆序各 1681／1681，零失败、错误和跳过。原 1680 项通过记录另保存在 `pre-tool-fix-full-forward.json`／`pre-tool-fix-full-reverse.json`，不改写旧记录。只增加一项工具兼容回归，生产代码、Fabric和性能证据保持原范围。

## 复跑入口

在项目根目录使用 `.venv/python.exe`：

```powershell
& .venv/python.exe -m unittest tests.motion_nav.test_action_entry_late_hardening tests.motion_nav.test_action_entry_handoff_repair -v
& .venv/python.exe -m scripts.action_entry_late_hardening --output .tmp/d093-fixed-replay --seeds 8 --workers 4
& .venv/python.exe -m scripts.action_entry_late_hardening --output .tmp/d093-random-replay --seeds 100 --workers 4
& .venv/python.exe -m scripts.action_entry_late_hardening --output .tmp/d093-key-replay --key-ticks --families column_outer_turn column_outer_aligned turn_jumpup turn_drop --workers 4
& .venv/python.exe -m scripts.benchmark_d058_route_revalidation --output .tmp/d093-d058-replay
& .venv/python.exe -m scripts.benchmark_d061_long_session --output .tmp/d093-d061-replay
& .venv/python.exe -m scripts.run_motion_navigation_checks --output .tmp/d093-forward-replay.json
& .venv/python.exe -m scripts.run_motion_navigation_checks --reverse --output .tmp/d093-reverse-replay.json
& .venv/python.exe -m scripts.f2_ground_route_runtime --handoff-entry-late --output-root .tmp/d093-fabric-replay --timeout-seconds 600
```

原代码 RED 需要从 `d85ecba5` 检出，只带测试及取证脚本，不带三个生产修改；正式 RED 指纹已经记录。性能使用实际经过时间，不参与机器人决策。归档轨迹为 `runs.jsonl.gz`，每行一个完整任务；`index.jsonl` 去掉轨迹，便于配对。`SHA256SUMS` 关闭本目录的文件集合；生产语义指纹另见 `production-semantic-hashes.json`。
