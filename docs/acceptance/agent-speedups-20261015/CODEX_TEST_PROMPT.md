# 给 Codex 的测试提示词

在装有并已登录 Codex CLI 的 Windows 主机上，于仓库 `D:\code\rynmesh` 中执行。

---

你在 `D:\code\rynmesh`（Ryn 项目，Python 后端 + `webapp/` 前端）里验收一批尚未提交的 Agent 提速改动。请只测试、测量和报告，不要修改业务代码，不要 commit 或 push，也不要覆盖安装用户正在使用的 Ryn（`%LOCALAPPDATA%\Ryn`）。如果发现缺陷，请在报告里写清复现步骤和定位，不要自行修复。

## 改动范围（都在工作区，未提交）

1. Codex 模型适配器复用线程（`rynmesh/llm_package/codex_session.py`、`codex_threads.py`、`codex_tools.py`；说明见 `docs/acceptance/codex-session-reuse-20261015/README.md`）。一次工具循环内复用同一个 `codex app-server` 进程和线程，只发送新增消息，逐段流式输出。同一窗口在最终回答后追问时继续用原线程，线程按"提问节点 + 窗口"分组；设置 `RYN_CODEX_SESSION_REUSE=0` 可退回旧的无状态模式。
2. Agent 运行时提速（说明见 `docs/acceptance/agent-speedups-20261015/README.md`）：
   - 核心工具常驻；`ryn_discover_tools` 改为累加加载，不再替换已加载的工具。
   - 新增 Codex 风格的多文件补丁工具 `ryn_apply_patch`（`rynmesh/agent_runtime/local_patch.py`）。
   - 上下文上限按模型声明的窗口计算（上限 60 万字符；模型未声明窗口时仍为 12 万）。
   - worker 改用 `run_continuously` 连续执行步骤，去掉每步之间固定 0.5 秒的等待。
   - 同一次回答里的连续本地只读工具并行执行。
   - 命令输出过长时保留开头和结尾。
   - 状态改为按任务分文件加密存储（`sessions.json` 是根文件，每个任务在 `tasks/` 下一个文件）；首次写入时自动从旧的单文件格式转换。
   - 前端轮询带 `since_revision`，任务没变化时只返回少量实时字段。

## 1. 自动化测试

使用仓库的虚拟环境 `.venv\Scripts\python.exe`。

```powershell
.venv\Scripts\python.exe -m pytest -q -rfE tests/test_agent_apply_patch.py tests/test_agent_speedups.py tests/test_agent_store_layout.py tests/test_agent_tool_menu.py tests/test_agent_codex_loop.py tests/test_agent_local_tools.py tests/test_codex_thread_reuse.py tests/test_codex_tool_proposals.py tests/test_inference_privacy.py tests/test_cli_agent.py tests/test_agent_peer_cli.py
.venv\Scripts\python.exe -m pytest -q -rfE -n auto tests -k "agent or codex or cli_ or inference"
cd webapp; npm run lint; npx vitest run src/screens src/domain; cd ..
```

记录每组的通过、失败、跳过数量。对每个失败用例：

- 判断失败是否由这批改动引起。改动前的状态无法从 git 还原，因为 v4 整体都没有提交，所以请直接分析失败原因并定位到文件和行号。Codex 适配器的问题可以用 `RYN_CODEX_SESSION_REUSE=0` 对照运行。
- 已知 `tests/test_agent_run_timing.py` 有 5 项在改动前就失败：测试只替换了 `runtime.time`，而循环代码已移到 `loop.py`。请确认是否仍只有这 5 项。
- 已知浏览器和 Windows 无障碍相关测试依赖本机环境（Chromium 调试端口、可以获得焦点的桌面窗口）。如果失败，注明环境原因。

## 2. Codex 线程复用实测

```powershell
.venv\Scripts\python.exe docs/acceptance/codex-session-reuse-20261015/measure_steps.py --live --rounds 3 --stream
```

报告首步和后续步骤的中位数与 p90，对比 `RYN_CODEX_SESSION_REUSE=0` 和 `=1`，以及首个流式文字出现的时间。改动前的基线是 Codex 模型每步中位约 17 秒、最长 97 秒。

## 3. 端到端 Agent 实测（隔离环境）

参照 `docs/acceptance/agent-loop-v4-20261015/frozen-smoke.json` 的做法：在新的临时目录中，用独立的 home 和数据目录启动后端，只监听本机回环地址。启用 Agent，默认模型选本机的 Codex CLI 模型，权限选"自主执行（workspace）"，工作区用一个新的临时目录。

依次通过本地 API（`/api/local/agent/...`）或界面创建下列任务。每个任务结束后，用 `GET /api/local/agent/tasks/{id}` 导出 JSON，再运行 `docs/acceptance/agent-speedups-20261015/step_gaps.py <task.json>`：

| 场景 | 要验证的点 | 改动前参考 |
| --- | --- | --- |
| A. 发送"你好" | 总耗时、首字时间 | 20.6 秒（局域网 Gemini）；Codex 的数值请一并记录 |
| B. "只读检查这台电脑的操作系统" | 不弹审批；总耗时 | 64.7 秒 |
| C. "在工作区新建 a.py 和 b.py，然后把 a.py 里的函数名改掉" | 使用 `ryn_apply_patch`；文件内容正确；没有额外的工具发现步骤 | — |
| D. "读取 a.py、b.py，并列出工作区文件" | 同一步里多个读取并行执行（事件中多个 `tool_started` 时间接近） | — |
| E. "运行一个会失败并产生大量输出的 python 脚本，告诉我最后的错误" | 输出同时包含开头和结尾，模型能直接说出末尾的错误 | — |
| F. 在同一个窗口里对 C 追问一句"再加一个单元测试" | Codex 线程续用（不重新播种完整历史），第二轮首步明显更快 | — |
| G. 再开一个新窗口，第一句同样发"你好" | 不会挤掉 F 那个窗口的线程，两个窗口的数据互不可见 | — |

每个场景记录：总耗时、模型步数、`model_started -> assistant` 中位数、`tool_result -> model_started` 中位数（改动前为 1.2–6.4 秒）、`ryn_discover_tools` 调用次数、上下文压缩次数。

## 4. 存储迁移（只在副本上做）

把用户现有的 Agent 数据目录（安装版 home 下的 `agent` 目录，至少包含 `sessions.json`）**复制**到临时目录，原目录不要碰。用改动后的代码打开这份副本，检查：

- 所有任务都能读出，数量和标题与迁移前一致；
- 第一次写入后，`sessions.json` 的版本变为 `ryn.agent.v2`，`tasks/` 下每个任务一个文件；
- 只追加一个事件时，只有那个任务的文件发生变化；
- 删除任务后，对应的任务文件被删除；
- 用 `docs/acceptance/agent-speedups-20261015/profile_store.py` 测量单次事务耗时，并与副本迁移前的大小一起记录。

报告中不要写入任何任务内容、账户信息或金额。

## 5. 报告

把结果写到 `docs/acceptance/agent-speedups-20261015/live-results.md`，包括：

- 运行环境：Codex CLI 版本、模型、CPU；
- 第 1–4 项的结果表；
- 改动前后对比；
- 发现的回归问题和复现步骤；
- 没能验证的部分及原因。

不要提交。
