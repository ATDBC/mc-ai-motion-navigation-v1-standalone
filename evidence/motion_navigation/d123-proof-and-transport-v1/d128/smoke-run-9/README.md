# D128 smoke-run-9

状态：INVALID_PRETRIAL_RUN/fixture-adapter。

正式进程已启动 Minecraft／Fabric，但 sessions=[]，在首个 movement trial 前因 payload 继续引用已经收口掉的局部变量 current 而触发 NameError。本次不消耗 attempt，也不产生产品结论；matrix 未运行，新的 formal run 继续暂停。

manifest.json 逐字节绑定四个 launcher 文件和 source-run/ 中 45 个原始 source-run 文件。封存包含 worker、client、时序、trace、physics、fixture、teleport、cleanup 和 server 日志；只排除生成的 server 安装、依赖库和 world 目录。
