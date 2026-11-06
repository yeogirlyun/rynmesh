# 自动处理 = 完全放行（2026-11-06）

## 问题

用户在 Agent 输入框选“自动处理”后，任务仍然弹“变更待确认”。

## 原因

`execution_policy.decide()` 中 `automatic` 只放行设置里“无需询问即可执行的变更”勾选过的工具，
而这个列表默认是空的，输入框里切换模式时也看不到它。结果：

| 模式（未勾选任何工具） | Shell / 写文件 / 打补丁 | 插件写入（如企业微信工具） | 模型分享等配置 |
| --- | --- | --- | --- |
| 自主执行 `workspace` | 直接执行 | 确认 | 确认 |
| 自动处理 `automatic`（修复前） | **确认** | 确认 | 确认 |
| 修改前确认 `confirm_changes` | 确认 | 确认 | 确认 |

“自动处理”实际和“修改前确认”一样，比默认的“自主执行”还严格。
另外公司插件 `company-workspace-mcp` 的 `read_only_tools` 为空，企业微信聊天记录读取工具被当作“变更”，
在“自主执行”下也会弹确认。

## 改动（用户选择：全部自动，不再确认）

- `automatic` 改为完全放行：所有变更直接执行，包括插件写入和配置变更，不再看勾选列表。
- 仍保留的硬性限制：`requires_explicit_approval` 工具（安装组件、把数据交给其他应用的应用操作）仍需确认；桌面操作仍需“允许电脑和浏览器操作”；
  任务或全局任一处为“只读检查/修改前确认/自主执行”时取更严格的一方；已有任务不会因全局改为自动处理而被提权。
- 任务分叉按 只读 > 修改前确认 > 自主执行 > 自动处理 取更严格者。
- `ryn_set_default_model` 的自动授权复核不再要求勾选列表。
- 设置对话框移除“无需询问即可执行的变更”勾选列表；说明文字改为“自动处理为完全放行…”。
- 模型提示中的权限说明同步更新；`docs/design/ryn-agent-loop-v4.md` 权限表同步更新。
- `allowed_write_tools` 字段保留并继续校验（兼容旧客户端），但不再影响授权。

## 行为变化提醒

- 定时自动化任务本身是 `automatic`，因此现在按全局模式执行：全局为“自主执行”时本机工具不再等待确认；
  全局为“自动处理”时插件写入（如发企业微信消息）也会直接执行。
- 子任务（subagent）继承父任务的自动处理，同样不再询问。

## 验证（VM，基线 develop 90228de）

- 复现：`decide()` 在 `automatic` + 空勾选列表下，`ryn_shell_execute`/`ryn_file_write`/`ryn_apply_patch`/插件写入/模型分享全部返回 `ask/tool_not_granted`；修复后全部 `execute/automatic`。
- 新增 `tests/test_agent_automatic_full_access.py`（全部自动、更严格模式优先、自主执行仍拦外部变更、硬性确认保留、分叉取更严格者）及更新的 `test_agent_computer_automation.py`：33 passed。
- 权限相关后端 25 个测试文件（-n 4）：分支 12 failed / 370 passed；其中 8 个在未改动的 develop 上同样失败
  （compaction 3 个、`test_default_task_create_rejects_a_configuration_race`、`test_attachments_large_results_checkpoint_and_restart`、
  `test_app_agent_requires_approval_even_in_automatic_mode` 2 个、`test_task_http_discovers_writes_real_desk_and_retries_without_duplicate`）；
  3 个进程退出类单独重跑通过（负载下不稳定）；1 个（第三方桌面 MCP 在自动处理下被拦）为本改动引入，已修正后通过。
- 前端 `RynAgent.test.tsx`：分支与基线相同，仅 `can select autonomous workstation execution and retain it in the workspace cache` 失败（基线同样失败）；
  `RynAgentComposer.test.tsx` 全部通过；`tsc -b --noEmit` 通过。
- 未验证：真实模型下的完整任务、宿主机上的已安装 Ryn（本 VM 无法连接宿主 Ryn）。
