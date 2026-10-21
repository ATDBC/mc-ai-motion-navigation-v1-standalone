# Task 1：恢复冻结输入的原始字节

三个冻结文件已恢复为公开提交 `3889d10e298fec57ce7b0a82a63bef331c431bb7` 的原始字节。根 `.gitattributes` 只为这三个路径增加 `-text` 和保留 CRLF 的空白检查规则。文件内容及已有校验值未改。

修复前，四个 Windows 模块共 24 项检查出现 1 个失败、13 个错误，均已保存。修复后为 24/24，失败、错误和跳过均为 0。这里只验证行尾问题；其余代码回归由后续任务处理。D121 仍为 `PARTIAL／STOPPED`。

各模块分别用下面的命令运行，`<module>` 依次取四个完整模块名：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest <module> -v
```

- `tests.motion_nav.test_action_entry_handoff_repair`
- `tests.motion_nav.test_f2rec_r0_baseline`
- `tests.motion_nav.test_f2rec_r1_region_geometry`
- `tests.motion_nav.test_reference_baselines`

`checks-red.json` 和 `checks-green.json` 记录各进程的命令、目录、开始时间、耗时、退出码及日志路径。测试由上述 conda 环境启动的 Python 进程逐一调用同一解释器，记录中的实际命令因此使用该解释器的完整路径。

每份日志 JSON 的 `output` 保留进程合并输出的所有字符，包括 CRLF；转回 UTF-8 后可用 `raw_output_sha256` 验证原输出字节，避免 Git 再次改写日志行尾。最初的 conda 多行 `python -c` 调用因不支持带换行的参数，在启动 Python 前失败；随后改用本地脚本执行，没有把这个工具调用错误记成测试 RED。

`frozen-byte-baseline.json` 保存修复前及公开基线的 SHA、大小和 CRLF 数量；三个文件只在行尾上不同。`frozen-byte-restored.json` 保存恢复后的逐文件字节核对。`verification.json` 记录暂存 blob 校验、两种 `core.autocrlf` 设置下六次全新检出核对，以及 `git diff --check`、暂存区检查和忽略行尾后零内容差异的结果。检出使用 `git checkout-index`，没有新建完整 worktree 或运行公开导出。
