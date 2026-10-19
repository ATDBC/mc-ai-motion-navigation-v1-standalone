# 导航实验回放页

## B09-R 运动计算器

打开 `http://127.0.0.1:8766/physics.html`，可以把封存的 B08／B09 Fabric 实机记录与 B09-R 的逐 tick 计算结果放在同一条时间轴上比较。页面提供俯视轨迹、高度轨迹、放大误差图，以及每一 tick 的输入、姿态、速度和碰撞事件。

计算按连续证据段进行。实机状态不连续时，页面会显示切段原因，并从新的实机状态重新起算。它只读取封存证据，不连接游戏，也不替在线控制选择动作。

选择实验轮次与场次，回放实际位置、朝向、轨迹、阶段目标与地形认知。页面不连接游戏，不向机器人提供地图，也不改原实验记录。

B03 运动形状记录也会自动出现。此类记录用橙色虚线显示评分用理想轨迹，用蓝线显示实际身体轨迹，并显示当前误差、整场指标和误差随时间的变化。理想轨迹只进入验收产物和回放，不进入机器人控制输入。

解析版本 11 保留旧记录兼容，读取阶段结束、探索结果、观察请求与补看用途，并显示精细停步时选择保持视角还是慢转调整。新记录还直接显示等待计算、等待证据、暂时接不上路线、制动和安全接管等原因，以及候选交付和采用的实际时间。缺少这些字段的旧记录继续使用原有说明，不补造原因。

导航调度记录可另带最近一次已执行空闲分片的额度和实际耗时。它与本帧前台搜索分开显示；本帧没有领取额度，不代表此前没有执行后台搜索。实际耗时超过额度时保留原值。

分层增量导航记录会分别显示当前走廊、局部轨迹、地图变化影响、前台／后台耗时和控制截止状态。后台规划尚未完成时，页面会明确说明当前动作是否继续；只有局部控制超过截止时间才显示“释放输入”。旧记录没有这些字段时继续使用旧说明。

观察参考可点击展开，显示待确认格子、参考位置和实际身体位置的检查状态、已知遮挡及搜索进度。默认最多显示四条问题关联线。参考位置变化不会另算正式检查点，候选也不会提前获得检查点编号。旧数据缺少这些字段时显示“旧记录未提供”。

新记录用虚线菱形显示尚未采用的临时候选，用实线圆显示正式检查点。搜索调度面板显示本次分配的额度；实际耗时另行记录，不能把额度当成已消耗时间。

## 启动

也可以直接双击仓库根目录的 `Start-navigation-replay.cmd`。脚本会启动或重启本项目的回放服务，再打开本机网页；后台服务不会随启动窗口关闭而退出。保留已有 Tailscale Serve 配置，启动时自动读取本机域名。若端口被其他程序占用，脚本会报错，不会结束那个程序。启动日志保存在 `artifacts/navigation-replay-cache/launcher`。

在仓库根目录运行：

```powershell
& 'D:/Miniforge3/Scripts/conda.exe' run --prefix 'D:/My_project/mc_ai/.venv' --no-capture-output python -B tools/navigation_replay/server.py --port 8766
```

浏览器打开 <http://127.0.0.1:8766>。端口被占用时可换成 `--port 8767`，或用 `--port 0` 自动分配，以终端打印地址为准。终端中 Ctrl+C 停止这个服务。

无需安装依赖或启动 Minecraft。服务只监听本机；原始证据只读，解析结果单独缓存在 `artifacts/navigation-replay-cache`。网页刷新目录后能发现新封存的场次。

## 通过 Tailscale 在外访问

将下面的域名替换为本机 Tailscale 完整域名，再启动回放服务：

```powershell
& 'D:/Miniforge3/Scripts/conda.exe' run --prefix 'D:/My_project/mc_ai/.venv' --no-capture-output python -B tools/navigation_replay/server.py --port 8766 --allow-host device.tailnet.ts.net
& 'C:/Program Files/Tailscale/tailscale.exe' serve --bg http://127.0.0.1:8766
```

外出设备连接同一 Tailscale 网络后，打开 Serve 打印的 HTTPS 地址。首次启用可能需要按命令提示在管理页面开启 HTTPS。
回放服务仍只监听本机，`--allow-host` 只允许指定域名，不提供登录认证；访问权限由 Tailscale 网络控制。此入口使用私人网络内的 Serve，不使用公开到互联网的 Funnel。
Serve 的后台配置会保留，但回放 Python 服务仍需要保持运行；电脑重启后需重新启动回放服务。

## 如何看

- 灰色墙体和白色网格是初始评测地图，始终与机器人认知分开。
- 半透明蓝色表示当前导入的合法观察，包含脚下接触；半透明红色表示仅在记忆中保留的方块。蓝色优先。
- 通行体积实验中，蓝色表示本帧获得的通行几何，允许穿过遮挡，不代表识别了具体方块。悬停会分别显示通行信息与视觉身份；两者各自保留真实时间。未识别身份的位置不会根据评测底图补上名称。
- 采用五秒历史分批的新记录会显示“当前覆盖（latest）”；离开后显示历史年龄区间。视觉身份仍使用自己的精确时间。旧记录保持原有时间表达，不按新规则重新解释。
- 默认合并地面与上方三层。同一列任一所选高度当前观察到，该列即为蓝色；下拉框可切到单个 Y 高度，鼠标悬浮可查看各层。
- 蓝线是实际轨迹，橙色编号是截至当前时刻选择过的阶段目标，橙色虚线是记录中的计划路线或当前控制参考段。
- 同一位置反复成为阶段目标时，地图只画最近的编号，右侧列表保留完整选择记录与时间，可点击定位。
- 原始样本逐帧显示，不做平滑。时间轴支持来回拖动，播放支持 0.25–8 倍；空格暂停／播放，左右方向键逐帧。
- 耗时包括观察、规划、行走和成功确认；是否有开局扫描取决于该场版本。面板上的移动输入是请求，位置与速度来自实际观察，不能把二者混为一谈。
- 探索版本 2 的原因栏还显示记录数、已压缩摘要数、依赖引用，以及当时的观察尝试编号和状态。尝试可以对应多次正式动作；不会把旧版记录补标成新版。

## 读取与边界

自动发现 `artifacts/joint-j5` 及 `artifacts/*/artifacts/joint-j5`。矩阵与独立单场分别列出，成功、超时、受阻和无效记录均保留。没有封存结束标记的记录暂不可回放。

迷宫按记录中的地图边界显示，支持本轮 16×16、22×22 布局及完整 90 秒超时记录；中文场景名、边缘坐标、往返拖动与原版本记忆还原均已通过真实记录检查。详见[迷宫记录](../../docs/worklogs/2026-09-13-navigation-maze-trials.md)。

记忆在独立 Python 进程中使用该场封存的源码还原。先核对封存 Python 文件哈希，再按实际导入顺序和时间执行记忆更新，并核对地形记录与空间索引数量。没有重跑策略，也没有根据评测地图补造认知。当前验证覆盖历史 J5 与本次连续控制实验；更早或不完整的记录可能只有轨迹／观察可用，页面会说明原因。

分段原始记录读取时检查哈希、顺序和封口计数。缓存绑定解析脚本、输入清单内容、来源文件及分段的大小和修改时间；不是抵御恶意篡改文件时间的认证系统。

底图支持初始坑洞和记录中的挖地／新墙事件。斜线表示坑洞，变化提交后用琥珀色虚线标记，首次合法确认后更新地形。两个时刻都有跳转按钮。记录没有精确服务器生效时间，网页不把提交时刻当作实际生效时刻。

紫色方形表示当前合法观察中的村民或其他实体，大小取实际包围盒。位置按原始观察更新，离开视野后隐藏，不外推轨迹。该颜色不属于蓝红地形记忆。

俯视合并不表示每个高度都已看见，也不把 500ms 动作新鲜度当成视觉颜色分界。坑洞图案在合并视图和地面高度显示；其他单层继续通过蓝红记录及悬浮信息检查。

## 验证

```powershell
& 'D:/Miniforge3/Scripts/conda.exe' run --prefix 'D:/My_project/mc_ai/.venv' --no-capture-output python -B -m unittest discover -s tools/navigation_replay -p test_*.py -v
node tools/navigation_replay/test_timeline.mjs
node tools/navigation_replay/browser_check.cjs
node tools/navigation_replay/browser_dynamic_check.cjs
node tools/navigation_replay/browser_maze_check.cjs
node tools/navigation_replay/browser_shape_check.cjs
node tools/navigation_replay/browser_physics_check.cjs
```

浏览器检查使用当前机器已有的 Playwright 和无头 Edge，不安装依赖。浏览器命令要求本地服务已启动，地址可作为参数传入。常规截图位于 `output/playwright/navigation-replay`，迷宫截图与核验结果位于 `output/navigation-mazes-v1/browser`。

[设计](../../docs/superpowers/specs/2026-09-12-navigation-replay-web-design.md) · [实施计划](../../docs/superpowers/plans/2026-09-12-navigation-replay-web.md) · [验证记录](../../docs/worklogs/2026-09-12-navigation-replay-web.md)
