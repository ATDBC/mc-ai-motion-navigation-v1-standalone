# D123-0 只读依赖实验

结论：**PARTIAL／STOPPED，等待独立审查**。没有生产改动，没有运行 Fabric，也没有批准删除依赖。

`run-1/` 保存原始有限实验：四方向各 67 个依赖、12 个下层格，144 个单格对照、96 个支撑变化、80 个相邻组合、72 个材料合同边界，以及另存的 144 个支撑／扫掠组合。四份 D120 原始 coverage 已按历史 SHA-256 核验；972 个入口和 2916 个后缀重算完全相同。

以 `classifications-final.json` 为最终分类。`run-1/classifications.json` 只看父动作，保留为原始探索输出；它不能识别“父证明不变但后继失败”的格。最终分类同时检查父、后继、上方几何和独立材料读取。每方向 3 格必须保留并观察；9 格虽有三态行为等价，但没有完整删除证明。36 个受支撑格均有允许最大形状高度处的材料边界反例。两格高 honey 向量只说明数据合同的边界，不表示原版存在这种蜂蜜块。

`required-facts.json` 补充同一构造器另一次实例的 67 个语义事实和该实例的 snapshot 身份；每次 WorldModel 的 owner 版本命名空间不同，该文件不能冒充 `run-1` 的原 snapshot。原始 proof 的完整依赖版本保存在 `run-1`。`read-paths.json` 在原始目录中按格、操作、调用点保存完整读取路径。主轨迹、出口、风险边界、伤害、fallback、后继 binding 和离线依赖门禁分别保存，不能互相代替。风险边界使用现有 release-safe command indices 的补集；没有新建风险定义。

34／34 定向及相邻检查通过。第一次测试断言失败和 bytes 摘要错误保留在 `verification/focused-initial-failed.txt`。所有生产事实、D120／D122 原始证据均未修改。`final-manifest.json` 绑定本批文件和新脚本；`self-check.json` 是最终自检。

复现命令（必须使用尚不存在的新输出目录）：

```powershell
& 'D:/My_project/mc_ai/.venv/python.exe' scripts/d123_dependency_probe.py --output <new-directory> --d120-source D:/My_project/mc_ai/.tmp/d120-entry-s3-prerequisite-v1
& 'D:/My_project/mc_ai/.venv/python.exe' scripts/d123_dependency_analysis.py --raw <new-directory> --output <new-analysis-file>
& 'D:/My_project/mc_ai/.venv/python.exe' -m unittest tests.motion_nav.test_d123_dependency_probe tests.motion_nav.test_d120_momentum_entry_calibration -v
```

D122 v8 没有历史序列化 proof 摘要。本批冻结相同确定性构造器重建的 proof，核对了 67／12 依赖及平移后的 v8 坐标。不能把重建 proof 摘要冒充缺失的历史摘要。D120 的旧原始结果则有真实历史哈希和逐项重算对照。
