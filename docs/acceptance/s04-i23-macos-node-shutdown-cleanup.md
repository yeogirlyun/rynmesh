# S04-I23 mac/Linux 节点退出清理 Agent 后台命令验收说明

## 1. 根因核实

1. POSIX 下 Agent 子命令通过 `subprocess.Popen(..., start_new_session=True)`（及 `PosixPtyProcess` 的 `start_new_session=True`）各自建立独立进程会话/进程组，桌面壳向节点进程组发信号不会自动到达这些独立会话。
2. 改动前 `rynmesh/peer_http.py` 的 `lifespan` 关闭段（`yield` 之后）未调用 `Processes.close()`，仅依赖 Python 进程退出阶段的 `atexit`；同时 `webapp/src-tauri/src/node.rs:stop_child` 在 Unix 下仅向单个 `pid` 发 `SIGTERM` 且只等 3 秒即 `SIGKILL`。一旦 `lifespan` 关闭其他组件超过 3 秒，`SIGKILL` 直接跳过 `atexit`，后台命令（如 `sleep 600`）即成为孤儿进程。

## 2. 已落地的修复与本机（Windows 开发机）自动化验证

- `rynmesh/peer_http.py:lifespan`：`yield` 之后的 `finally` 段**最先**调用 `app.state.agent_runtime.tools.local.processes.close()`（随后调用 `local.close()`），在收到 `SIGTERM` 进入 ASGI 关闭流程的第一时间通过 `os.killpg(pid, SIGKILL)` 清理所有在跑进程组，不再依赖 `atexit`。
- `rynmesh/agent_runtime/local_processes.py:Processes.close`：支持幂等重复调用（`lifespan` 显式调用后再触发 `atexit` 不重复等待或误杀），单条异常隔离不阻断其余进程组清理。
- `webapp/src-tauri/src/node.rs`：`#[cfg(unix)]` 启动前设置 `cmd.process_group(0)`，停止时发送 `libc::kill(-pid, libc::SIGTERM)`，优雅退出等待窗口从 3 秒（`30 × 100ms`）放宽到 8 秒（`80 × 100ms`），超时后再发送 `libc::kill(-pid, libc::SIGKILL)`，与 `plugin_services.rs` 对齐。
- 自动化回归测试：`tests/test_agent_local_processes.py::test_s04_i23_t1_lifespan_shutdown_hook_kills_all_running_process_groups_first` 与 `test_s04_i23_t2_tauri_node_rs_unix_process_group_and_8s_grace_contract`。

## 3. macOS 实机验收步骤（待在 macOS 主机执行后补充输出）

> 注：当前并行队列工人运行于 Windows 虚拟机环境（`win32`），无 macOS 桌面实机硬件；在 macOS 打包机验证时按以下步骤执行：

1. 启动桌面版 Ryn，进入 Agent 新建任务并以后台模式执行：
   ```sh
   sleep 600
   ```
2. 在终端确认后台进程存在：
   ```sh
   pgrep -fl "sleep 600"
   ```
3. 从菜单栏 / Dock 正常退出 Ryn 桌面应用，等待退出完成（≤ 8 秒）。
4. 再次检查进程残留：
   ```sh
   pgrep -fl "sleep 600"
   ```
   预期输出为空（退出码 `1`，无残留 `sleep 600` 孤儿进程）。
