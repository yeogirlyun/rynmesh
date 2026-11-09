# 插件工具报错不再锁住对话（2026-11-09）

## 问题

一次插件聊天记录查询在审批等待后返回了工具错误（插件给的临时会话引用已过期）。
这个查询工具没有被确认为只读，按“变更”处理；`adapter.py` 把变更工具返回的任何 `isError`
都记成 `agent_write_outcome_unknown`，自动核对也只能得到 `unknown`，对话从此无法继续。

## 改动（`rynmesh/agent_runtime/extensions/adapter.py`、`runtime.py`）

- 变更工具正常返回了错误（MCP `isError=true`，即工具自己给了回复）：
  - 没有声明结果查询工具（`x-ryn-result`）的：这次回复就是结果。操作记录写成 `confirmed`（保留 `is_error`），
    错误原样交给模型，并附 `outcome_note`：Ryn 不会重发；报错不能证明没有改动，重试改动前先用只读工具核对；
    引用过期就先重新查找。任务继续，不再锁住。
  - 声明了结果查询工具的（发送、重试、上传这类可能已经动作过的工具）：先用结果查询工具查一次；
    查到结果按结果记录；查不到仍按“结果不明”处理，不自动重发（保持原有保护）。
  - 查不到工具定义时按后一种处理。
- 同一 `operation_id` 再次执行时返回已记录的回复（包括错误回复），绝不重新发出调用。
- 重启后的核对：`confirmed` 记录（包括错误回复）返回 `executed`，回复作为工具结果放回对话，并标记 `is_error`。
- 不变：传输中断、超时、回复丢失等“发出后没有收到回复”的情况仍是 `agent_write_outcome_unknown`；
  只读工具的错误一直是普通错误结果。

## 验证（VM，基线 develop e2156ce）

- `tests/test_agent_extensions_runtime.py` 中原测试改写为
  `test_mcp_write_result_is_error_returns_reply_without_lock_or_replay`，假 MCP 服务器提供四个工具：
  - 变更工具返回 `isError`：调用计数 1，返回错误和 `outcome_note`，记录为 `confirmed`，核对为 `executed`，
    同一 `operation_id` 再执行计数仍为 1；
  - 声明结果查询工具的变更工具返回 `isError`、结果查询也查不到：仍为 `agent_write_outcome_unknown`，
    记录保持 `dispatched`，核对为 `unknown`，再执行不重发；
  - 完整运行时：任务没有进入 `interrupted`，模型看到错误后正常给出回答，调用只发生一次；
  - 同一数据目录重建节点：不重发、不重新锁住。
- 相关 10 个测试文件（扩展运行时、插件视图、托管 MCP、工具调度、插件发现、本机服务、
  应用不确定回复、安全修复、操作权限、业务插件交接）：152 通过、2 失败；
  `test_agent_plugin_discovery[12000]` 在 develop 上同样失败，
  `test_cancelling_code_cell_stops_waiting_children_and_discards_worker` 单独运行通过（并行负载下不稳定）。
- 未在真机或真实模型上验证；未安装。配套的插件改动（稳定会话引用）在插件仓库单独集成。
