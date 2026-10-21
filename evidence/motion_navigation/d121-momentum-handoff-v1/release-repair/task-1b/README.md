# Task 1b：恢复 D101 清单的历史字节

这是 Task 3 定向检查发现的额外历史字节前置问题。D102 的 G0 证据绑定 D101 清单的原 SHA；隔离 worktree 将清单检出成 LF 后，历史链校验失败。没有更改任何历史哈希、断言或证据内容。

`byte-baseline.json` 记录修复前后字节。可信来源是公开提交 `3889d10e298fec57ce7b0a82a63bef331c431bb7`：原文件为 8417 字节、201 个 CRLF，SHA-256 为 `f3189c60dff31c701c53bf199250e8bcf3d6931bd04e375d688fb5abd4640395`。修复前 LF 版本为 8216 字节，除换行外完全一致。

仅恢复 `evidence/motion_navigation/continuous-motion-d101-v1/manifest.json` 的原字节，并为该路径增加一条精确的 `-text whitespace=cr-at-eol` 规则。`fresh-checkout.json` 记录暂存区在 `core.autocrlf=false`、`true` 两种新 index checkout 下均保留原 SHA。

`red.json` 和 `red.txt` 保存修复前的 Windows 模块结果：24 项测试，56 次失败（包含子测试）、5 次错误。字节恢复后，56 次失败全部关闭；第一次复跑仍有 5 次错误，来自两个测试要复制的 G1 忽略夹具尚不存在。该次结果独立保存在 `restored-missing-g1.json` 和对应日志，没有覆盖。

测试继续使用 Task 3 只读复制的 24 文件 G0 夹具；`fixture-integrity.json` 逐文件确认复制物没有变化。另经主 agent 确认，只读复制本机已有的 73 文件 G1 夹具。`g1-fixture-preparation.json` 记录复制前后源目录不变、复制物 SHA 完全相同。两个夹具均位于忽略目录，不提交。

`green.json` 和 `green.txt` 保存夹具齐备后的同一命令最终结果：24/24 通过，零失败、错误和跳过，测试耗时 68.086 秒。新日志统一为 LF 并去掉行末空格，保留全部诊断文字。D121 保持 PARTIAL／STOPPED；未开始 Task 4 或 D122，未推送。
