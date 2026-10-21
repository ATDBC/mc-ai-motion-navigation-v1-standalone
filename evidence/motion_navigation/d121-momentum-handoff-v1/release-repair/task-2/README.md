# Task 2：公开快照字节协调

状态：实现与 Windows 直接检查完成，等待独立复核。

导出器新增显式 `--byte-base-root`。先核对基线自己的 `SHA256SUMS.txt`，再按当前清单导出。共同的 UTF-8 文本只有 CRLF／LF 差异时保留基线字节；非 UTF-8、含 NUL 的二进制、BOM 变化、其他空白变化和真实内容变化使用当前字节。元数据和根哈希清单始终重新生成。报告通过同次调用的可选 sink 或 CLI stdout 输出，不写入公开树。

`export_tree` 继续返回 `Path`；不传基线时保持原导出行为。没有新增 Git 或网络读取；来源提交仍由原有元数据逻辑读取。

## 检查

- `checks-red.json`：三个新行为检查因缺少 `byte_base_root` 参数失败。
- `checks-green.json`：当前 Windows 导出模块 28/28 通过。
- `checks-encoding-error.json`：一次中间工具编辑使用了 Windows 默认编码，导致旧测试中的中文断言乱码；两项失败已保留。随后按 HEAD 原始 UTF-8 字节重建旧测试，只追加本任务新测试，28/28 通过。
- `integration.json`：从公开 `3889d10e` 按原字节展开独立基线，先验证哈希，再比较普通导出和协调导出。
- `byte-reconciliation.json`：同次协调的完整路径分类。

历史发布 `dfaa0b4f` 相对 `3889d10e` 有 1614 个纯行尾变化。当前候选恢复其中 1613 个原字节；剩下一处是本批实际修改的导出器，保留真实内容。当前候选全部纯行尾差异归零，真实内容改动、新增和删除保留。

复现集成检查（只读 Git 中已有的两个提交；不 fetch、不 push）：

```text
python evidence/motion_navigation/d121-momentum-handoff-v1/release-repair/task-2/integration_probe.py <source-root>
python -m unittest tests.motion_nav.test_standalone_export -v
```

本任务不代表完整发布门禁通过。D121 仍为 PARTIAL／STOPPED，Task 3—5 和 D122 未在本任务实施。
