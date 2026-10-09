# F2-REC R2 停止证据

状态：候选保存在 `archive/f2rec-r2-stopped-20261009`，候选代码提交为 `8b9163a3`，尚未签署，也不进入恢复主线。只有选面函数及导入发生生产变化，没有改规划、动作或生命周期。

第一轮的 F2 528、v8 1904 和 v9 216 正式集合保存在紧凑索引中。原始完整输出在本地 `.tmp/f2rec-r2-win-*`；索引没有把第一轮数据重标成第二轮完整运行。第二轮只将距离下界前移到列查询前，1904 个选面结果仍与冻结旧选择器相同。

正式环境为 Windows `.venv/python.exe`，Python 3.11.16。全局 Python 3.14 的初次检查仅是环境误用的补充证据，见 `environment-mismatch.json`。

D058 通过。D061 第一轮只在跟随修订 P95 失败；第二轮的杂乱正式桥修订＋准备仍为 P95 8.8542 ms。因此按 D090 止损，完整正逆未运行。`revision-probe-invalid-boundary.json` 保留第一次把同步测试 planner 混入 production 的错误计时；正确拆分结果在 `selection-probe-round2.json`。

完整轨迹与 17 MB 的 D061 原始性能文件没有复制进 Git；原始 D061 文件哈希、来源、环境、全部门槛和三段统计见 `d061-round1-summary.json`。命令见 `COMMANDS.txt`。当前文件的字节哈希见 `SHA256SUMS.txt`。
