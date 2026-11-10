# 工具调用调度（2026-11-01）

本次对齐 Codex 的核心机制：模型提交多少个调用，与宿主同时执行多少个调用分开控制。没有把旧的 8 个改成另一个小整数。

## 已实现

- 移除直接工具调用的 8 个数量上限，包括最终响应校验、流式事件索引、工具规范化、Codex/Antigravity 响应 schema 和内置提示词。
- 移除 `ryn_code_exec` 的 16 个子调用上限。每个代码单元最多并发处理 8 个子调用，其余排队。
- 直接调用、流式提前执行和代码模式子调用共用任务级调度门。宿主标记可并行的只读工具共享执行；其他工具独占执行；等待中的独占调用不会被后来读调用越过。
- 代码模式子调用接入运行时的权限、参数与 schema 校验、任务预算、取消检查和持久化回执。每个实际获准执行的子调用占用一次工具预算，代码单元自身仍占一次。父调用负责代码单元耗时，避免并行子调用重复计时。
- 子调用回执带 `source=code_mode` 和 `parent_operation_id`。重启只结束未完成记录，不重放代码单元，也不伪造顶层对话工具消息。
- 对旧会话中完全匹配上一版内置基础提示词的内容移除过时的 8 个限制；自定义基础提示词及显式模型/会话覆盖保留。

## 仍然有效的边界

- 同时执行上限为 8，这是 Ryn 的资源保护参数，并非声称 Codex 使用相同数值。
- 直接调用保留 256 KiB 批量字节上限与 96 KiB 单次参数上限。代码单元子调用采用相应的累计请求帧字节保护及单次参数保护；仍有单元超时、输出限制和用户显式任务预算。
- `ryn_code_exec` 仍采用原有只读工具契约；需要写入或明确批准的工具继续走直接调用的预览和审批流程。本次没有将写工具开放到 JS。
- 不支持并行的扩展工具默认独占。扩展适配器根据本地所有者配置生成只读并行能力，桌面操作不获得此能力；不采信模型声称的能力。
- 这不是 Codex 完整执行运行时的移植，也未包含网络重试、shell 输出、沙箱等其他差异。

## 验证

专项测试 168 项通过，覆盖直接调用 40 个、JS 实际执行 40 个子调用、三类流式协议、写操作审批边界、用户总预算、取消、并发与独占屏障、重启回执和扩展数据策略。模型响应由测试桩提供，JS 使用实际 Node worker；尚未做真实模型对测或安装包发布。

扩大回归首轮 192 项通过、2 项失败：扩展只读工具并行回归已修复并纳入上述通过结果；另一项 `test_automatic_compaction_retains_policy_and_rejects_disallowed_summary_destination[True]` 在 HEAD 就断言旧的 `compact factual checkpoint` 文案，而 HEAD 已使用 `CONTEXT CHECKPOINT COMPACTION`，不属于本次修改。

本次独立修改文件的 Ruff 检查通过，`git diff --check` 通过。`loop.py` / `runtime.py` 的 HEAD 已有导入排序、`Candidate_msgs` 命名和缺少 `math` 导入的检查问题，本次未顺带修改。

## 对照来源

对照官方 `rust-v0.166.0`（提交 `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`）中的共同工具执行路径与共享/独占调度；在这些路径中没有发现 8/16 个的批量数量限制。

- [工具并行调度](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/parallel.rs)
- [代码模式桥接](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/code_mode/mod.rs)
- [JS 工具回调](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/code-mode-runtime/src/runtime/callbacks.rs)
