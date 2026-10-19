# B02 验收记录：导航层实时观察原型

日期：2026-09-18
结论：原型通过；不代表 B02 完成

## 实机范围

- Minecraft 1.21 可见 Fabric 客户端；创造模式；普通第一人称键鼠输入。
- 新建隔离本地平坦服务器，种子 21001。
- 导航采样 120°×120°、16 格、遮挡无关、仅限已加载区块。
- 网页地址：`http://127.0.0.1:8770/`。
- 运行：`20260918T114721770180Z-0b3d4d2c`。

## 结果

| 检查 | 结果 |
|---|---|
| Minecraft 可见窗口 | 通过；窗口标题为 `Minecraft* 1.21 - 多人游戏（第三方服务器）` |
| 网页显示方式 | WebGL2 第一人称三维视图；与机器人位置和视角同步 |
| 人工控制边界 | 客户端模组没有输入写入器；实际键鼠操作留给用户检查 |
| 连续推送 | 2.000 秒取得 40 个不同帧，约 19.5 Hz |
| 当前普通平视查询 | 4,456 格，未加载未知为 0 |
| 客户端采样耗时 | 中位 0.991 ms，P95 1.027 ms，最大 1.038 ms |
| 匿名语义 | 当前调色表只有 `air` 和 `occupied`；快照无 `block_id` 或 `minecraft:` 身份 |
| 慢消费者处理 | Java 与 Python 均只保留最新帧，契约检查通过 |
| 网页传输 | 持久 TCP 到 SSE；实机网页检查时最新帧龄为 27 ms |
| 网页渲染 | WebGL2；实机识别为 `ANGLE (NVIDIA…)`，没有退回软件渲染 |

采样耗时来自同一 JVM 的每帧起止时间。27 ms 是网页检查瞬间距离最近一次收到快照的时间，不是跨 JVM 与浏览器拼接出来的精确端到端延迟。

## 检查命令

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python -m unittest tests.motion_nav.test_navigation_layer_live -v

node --check tools/navigation_layer_live/web/app.js

$env:JAVA_HOME='D:\My_project\mc_ai\.venv\Library'
$env:GRADLE_USER_HOME='D:\My_project\mc_ai\.gradle'
& 'D:\My_project\mc_ai\.gradle\wrapper\dists\gradle-8.8-bin\cx57xx7zsiden606ef8ncmv16\gradle-8.8\bin\gradle.bat' `
  --offline --no-daemon --console=plain classes
```

## 证据边界

平坦世界只直接出现空气和完整地面，没有验证玻璃、流体、半砖、悬浮墙或未加载区块。遮挡无关语义由采样器实现和数据契约保证，仍需在 B01 的 S04 悬浮墙场景做正式行为验收。
