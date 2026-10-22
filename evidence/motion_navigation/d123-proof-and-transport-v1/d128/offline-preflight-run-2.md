# D128 当前离线证据

`offline-preflight-run-2.json` 在最终修正后的源码上生成。运行进程的 `JAVA_HOME` 为空，`PATH` 只含 Windows 系统目录：

```text
D:\My_project\mc_ai\.venv\python.exe scripts\d123_fabric_transport.py --offline-preflight evidence\motion_navigation\d123-proof-and-transport-v1\d128\offline-preflight-run-2.json
```

结果为 PASS，`fabric_started=false`。组件 smoke 为 3／3，组件矩阵为 36／36，Java／overlay 通过。该 JSON 的 probe hash 与当前源码一致，当前 `validate_offline_preflight(..., require_java=True)` 返回 true。

同目录旧 `offline-preflight.json` 保持原字节。它绑定的是更早的 probe 字节，当前 validator 返回 false，只保留为历史离线运行，不作为新正式 smoke 的门禁依据。
